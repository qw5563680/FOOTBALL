"""Offline T68 identity proposals. No candidate authorizes an importer or DB write."""

import argparse
import hashlib
import json
import logging
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from zucai.sources.apif_aliases import binding_scope_allows, valid_identity_category

LOG = logging.getLogger(__name__)
WINDOW = timedelta(hours=3)
ACTIVE = {"approved", "auto_verified"}


def _utc(value):
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.utcoffset() is None:
        raise ValueError("Time evidence requires an explicit timezone")
    return when.astimezone(UTC)


def _clock(row):
    if row.get("time_precision") not in {"minute", "second", "microsecond"}:
        return None
    try:
        return _utc(row["kickoff_utc"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(row, key, target):
    return f"t68:apif:{row['competition_id']}:{key}:{target}"


def _deduplicate(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["match_id"]].append(deepcopy(row))
    result, conflicts = [], set()
    for mid, copies in grouped.items():
        semantic = [{k: v for k, v in row.items() if k != "provenance"} for row in copies]
        if len({_json(row) for row in semantic}) != 1:
            conflicts.add(mid)
        first = copies[0]
        first["provenance"] = list(
            {_json(proof): proof for row in copies for proof in row.get("provenance", [])}.values()
        )
        result.append(first)
    return result, conflicts


def _complete(row, instant):
    if row.get("complete") is not True or not row.get("provenance"):
        return False
    try:
        return (
            _utc(row["coverage"]["start_utc"]) <= instant - WINDOW
            and _utc(row["coverage"]["end_utc"]) >= instant + WINDOW
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _score(row):
    score = row.get("score")
    return (
        isinstance(score, (list, tuple))
        and len(score) == 2
        and all(type(n) is int and n >= 0 for n in score)
        and row.get("regular_time_basis") in {"regular", "derived"}
    )


def _contradicts(old, current):
    """Positive contradictory evidence, not an absent row in a narrower replay."""
    for field in ("league", "competition_id", "home", "away", "status"):
        if old.get(field) is not None and current.get(field) is not None:
            if str(old[field]) != str(current[field]):
                return True
    if _clock(old) and _clock(current) and _clock(old) != _clock(current):
        return True
    if _score(old) and _score(current) and list(old["score"]) != list(current["score"]):
        return True
    return any(
        valid_identity_category(old.get(field))
        and valid_identity_category(current.get(field))
        and old[field] != current[field]
        for field in ("home_category", "away_category")
    )


def _category_veto(label, category):
    """Negative tokens may veto; their absence never supplies a positive type."""
    if not category:
        return False
    youth = re.search(r"\bU[- ]?\d{2}\b", label, re.I)
    return bool(
        (re.search(r"\b(?:Women|W)\b|女", label, re.I) and "/women/" not in category)
        or (
            re.search(r"\b(?:II|Reserves?)\b|预备", label, re.I)
            and not category.endswith("/reserve")
        )
        or (youth and re.sub(r"[- ]", "", youth[0]).upper() not in category)
    )


def _pair_reasons(source, official, teams, *, diagnostic=False):
    reasons = []
    a, b = _clock(source), _clock(official)
    if a is None or b is None:
        reasons.append("missing_kickoff_evidence")
    elif a != b:
        reasons.append("kickoff_not_exact")
    if a is not None and (not _complete(source, a) or not _complete(official, a)):
        reasons.append("incomplete_competitor_window")
    if source.get("competition_id") != official.get("competition_id"):
        reasons.append("competition_mapping_unverified")
    if any(row.get("status") != "finished" for row in (source, official)):
        reasons.append("not_finished_regular_time")
    if not _score(source) or not _score(official):
        reasons.append("missing_regular_time_score")
    elif list(source["score"]) != list(official["score"]):
        reasons.append("regular_time_score_mismatch")
    for side in ("home", "away"):
        target = teams.get(str(official.get(side)))
        category = source.get(f"{side}_category")
        if not target or not target.get("name") or target.get("ambiguous"):
            reasons.append("canonical_target_not_unique")
        elif diagnostic:
            continue
        elif not valid_identity_category(category) or not valid_identity_category(
            target.get("category")
        ):
            reasons.append("category_evidence_missing")
        elif category != target["category"]:
            reasons.append("category_conflict")
        if _category_veto(source.get(f"{side}_name", ""), category):
            reasons.append("category_conflict")
    return sorted(set(reasons))


def _formal_targets(formal, teams):
    names = defaultdict(set)
    for key, team in teams.items():
        names[team["name"]].add(str(key))
    targets, league_targets = defaultdict(set), defaultdict(set)
    for league, entries in formal.items():
        for key, entry in entries.items():
            if entry.get("status") not in ACTIVE:
                continue
            found = names.get(entry.get("canonical"), set())
            if len(found) == 1:
                targets[str(key)].update(found)
                for target in found:
                    league_targets[league, target].add(str(key))
            else:
                targets[str(key)].add("unresolved:" + str(entry.get("canonical")))
    return targets, league_targets


def _compatible(source, official, known):
    reverse = {target: key for key, target in known.items()}
    return all(
        (not str(official[side]).isdigit())
        or (
            (str(source[side]) not in known or known[str(source[side])] == str(official[side]))
            and (
                str(official[side]) not in reverse
                or reverse[str(official[side])] == str(source[side])
            )
        )
        for side in ("home", "away")
    )


def propose_bindings(
    apif,
    official,
    teams,
    *,
    formal=None,
    previous=None,
    reject=(),
    observed_utc=None,
    occupied=None,
    diagnostic=False,
    use_formal_anchors=True,
    formal_sha256=None,
):
    """Validate already source-checked observations; names/scores never choose edges.

    Each row includes the complete raw window, independently proven clock and
    source pointers. Unknown clocks block their competition's proof rather than
    silently removing possible competitors. Callers must not pass imported-only
    fixture subsets. The archive adapter enforces these input contracts.
    """
    observed_utc = observed_utc or datetime.now(UTC).isoformat()
    _utc(observed_utc)
    teams = {str(key): deepcopy(value) for key, value in teams.items()}
    names = Counter(team["name"] for team in teams.values())
    for team in teams.values():
        if names[team["name"]] != 1:
            team["ambiguous"] = True
    formal, previous = formal or {}, deepcopy(previous or {})
    sources, source_conflicts = _deduplicate(apif)
    counterparts, official_conflicts = _deduplicate(official)
    authorized, formal_reverse = _formal_targets(formal, teams)
    globals_ = deepcopy(authorized)
    # Prior queue entries can veto a conflicting identity; they never authorize it.
    for entries in previous.values():
        for key, entry in entries.items():
            canonical = entry.get("canonical")
            if canonical is None:
                continue
            found = {key for key, team in teams.items() if team["name"] == canonical}
            globals_[str(key)].update(found or {"unresolved:" + str(canonical)})
    reject = set(reject) | {
        entry["candidate_id"]
        for entries in previous.values()
        for entry in entries.values()
        if entry.get("evidence_status") in {"rejected", "withdrawn_conflict"}
        and entry.get("candidate_id")
    }
    for key, targets in (occupied or {}).items():
        globals_[str(key)].update(str(target) for target in targets)
    bad_rows = [
        row
        for row in [*sources, *counterparts]
        if _clock(row) is None or row["match_id"] in source_conflicts | official_conflicts
    ]
    by_league = defaultdict(list)
    for j, row in enumerate(counterparts):
        if (clock := _clock(row)) is not None:
            by_league[row["league"]].append((clock, j))
    for rows in by_league.values():
        rows.sort()
    temporal = []
    for i, row in enumerate(sources):
        when = _clock(row)
        if when is None:
            continue
        others = by_league[row["league"]]
        lower = bisect_left(others, (when - WINDOW, -1))
        upper = bisect_right(others, (when + WINDOW, len(counterparts)))
        temporal.extend((i, j) for _, j in others[lower:upper])

    def incomplete(source):
        when = _clock(source)
        for bad in bad_rows:
            if bad["league"] != source["league"]:
                continue
            try:
                if _clock(bad) is not None:
                    low = high = _clock(bad)
                elif bad.get("possible_start_utc") and bad.get("possible_end_utc"):
                    low, high = _utc(bad["possible_start_utc"]), _utc(bad["possible_end_utc"])
                elif bad.get("match_date"):
                    low = (
                        datetime.fromisoformat(bad["match_date"])
                        .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                        .astimezone(UTC)
                    )
                    high = low + timedelta(days=1)
                else:
                    low = _utc(bad["coverage"]["start_utc"])
                    high = _utc(bad["coverage"]["end_utc"])
            except (KeyError, ValueError, TypeError):
                return True
            if when is None or low <= when + WINDOW and high >= when - WINDOW:
                return True
        return False

    same_provider = (
        {}
        if diagnostic
        else same_provider_proposals(
            sources,
            teams,
            formal,
            formal_sha256 or hashlib.sha256(_json(formal).encode()).hexdigest(),
            observed_utc,
            conflicting_fixture_ids=source_conflicts,
        )
    )
    candidates = {
        (league, key, entry["canonical_team_id"]): entry
        for league, entries in same_provider.items()
        for key, entry in entries.items()
    }
    issues, witnesses = {}, []

    def issue(i, j, reasons, **extra):
        key = (i, j)
        row = issues.setdefault(
            key,
            dict(
                apif_id=sources[i]["match_id"] if i is not None else None,
                counterpart_id=counterparts[j]["match_id"] if j is not None else None,
                league=(sources[i] if i is not None else counterparts[j])["league"],
                provider_team_ids=[str(sources[i][side]) for side in ("home", "away")]
                if i is not None
                else [],
                reasons=[],
                **extra,
            ),
        )
        row["reasons"] = sorted(set(row["reasons"]) | set(reasons))

    for i, row in enumerate(sources):
        if _clock(row) is None:
            issue(i, None, ["missing_kickoff_evidence"])
        if row["match_id"] in source_conflicts:
            issue(i, None, ["conflicting_fixture_originals"])
    for j, row in enumerate(counterparts):
        if _clock(row) is None:
            issue(None, j, ["missing_kickoff_evidence"])
        if row["match_id"] in official_conflicts:
            issue(None, j, ["conflicting_fixture_originals"])
    # Independent unique witnesses are checked before anchors can hide a conflict.
    left, right = Counter(i for i, _ in temporal), Counter(j for _, j in temporal)
    conflicted_keys, conflicting_observations = set(), defaultdict(list)
    for i, j in temporal:
        if left[i] == right[j] == 1 and not _pair_reasons(
            sources[i], counterparts[j], teams, diagnostic=diagnostic
        ):
            for side in ("home", "away"):
                key, target = str(sources[i][side]), str(counterparts[j][side])
                if globals_[key] and globals_[key] != {target}:
                    conflicted_keys.add(key)
                    conflicting_observations[key].append(
                        dict(
                            apif_id=sources[i]["match_id"],
                            counterpart_id=counterparts[j]["match_id"],
                            canonical_team_id=target,
                            provenance=sources[i]["provenance"],
                        )
                    )
                    issue(i, j, ["global_provider_id_conflict"])
    known_by_league, dependency_ids = defaultdict(dict), defaultdict(set)
    formal_by_key = defaultdict(list)
    for entries in formal.values():
        for key, entry in entries.items():
            if entry.get("status") in ACTIVE:
                formal_by_key[key].append(entry)
    anchor_cache = {}

    def anchors(i):
        if i not in anchor_cache:
            row, known = sources[i], {}
            for key, targets in authorized.items():
                if len(targets) != 1 or key in conflicted_keys or globals_[key] != targets:
                    continue
                target = next(iter(targets))
                category = teams.get(target, {}).get("category")
                typed = valid_identity_category(category) and category in {
                    row.get("home_category"),
                    row.get("away_category"),
                }
                if (
                    use_formal_anchors
                    and (typed or diagnostic)
                    and any(
                        binding_scope_allows(
                            entry, row["league"], kickoff_utc=row.get("kickoff_utc")
                        )
                        for entry in formal_by_key[key]
                    )
                ):
                    known[key] = target
            anchor_cache[i] = known
        return anchor_cache[i] | known_by_league[sources[i]["league"]]

    processed = set()
    for layer in range(1, len(sources) * 2 + 2):
        edges = [
            (i, j) for i, j in temporal if _compatible(sources[i], counterparts[j], anchors(i))
        ]
        left, right = Counter(i for i, _ in edges), Counter(j for _, j in edges)
        pending = []
        for i, j in edges:
            if (i, j) in processed:
                continue
            source, other = sources[i], counterparts[j]
            if left[i] != 1 or right[j] != 1:
                issue(i, j, ["not_bidirectionally_unique"])
                continue
            processed.add((i, j))
            reasons = _pair_reasons(source, other, teams, diagnostic=diagnostic)
            if incomplete(source):
                reasons.append("incomplete_competitor_window")
            if any(str(source[side]) in conflicted_keys for side in ("home", "away")):
                reasons.append("global_provider_id_conflict")
            for side in ("home", "away"):
                key, target = str(source[side]), str(other[side])
                if globals_[key] and globals_[key] != {target}:
                    reasons.append("global_provider_id_conflict")
                if formal_reverse[source["league"], target] - {key}:
                    reasons.append("canonical_has_other_provider_id")
            if reasons:
                issue(i, j, reasons)
                continue
            evidence = dict(
                kind="strict_oriented_fixture",
                apif=source,
                counterpart=other,
                apif_candidates=[sources[x]["match_id"] for x, y in edges if y == j],
                official_candidates=[counterparts[y]["match_id"] for x, y in edges if x == i],
                complete_raw_competitors=True,
                layer=layer,
            )
            dependencies = sorted(dependency_ids[source["league"]])
            pending.append((i, j, evidence, dependencies))
        if not pending:
            break
        added = False
        for i, j, evidence, dependencies in pending:
            source, other = sources[i], counterparts[j]
            witness_ids = []
            for side in ("home", "away"):
                key, target = str(source[side]), str(other[side])
                business = (source["league"], key, target)
                cid = _id(source, key, target)
                witness_ids.append(cid)
                if business not in candidates:
                    candidates[business] = dict(
                        candidate_id=cid,
                        provider="apif",
                        provider_competition_id=source["competition_id"],
                        provider_team_id=key,
                        name=source.get(f"{side}_name", ""),
                        canonical=teams[target]["name"],
                        canonical_team_id=target,
                        category=teams[target]["category"],
                        status="proposed",
                        evidence_status="strict_verified",
                        layer=layer,
                        depends_on=dependencies,
                        protocol="T68/strict-v1",
                        proposed_utc=observed_utc,
                        verified_utc=observed_utc,
                        proposed_beijing=_utc(observed_utc)
                        .astimezone(ZoneInfo("Asia/Shanghai"))
                        .isoformat(),
                        binding_scope=dict(
                            leagues=[source["league"]], category=teams[target]["category"]
                        ),
                        evidence=[],
                    )
                    added = True
                if not candidates[business]["evidence"]:
                    candidates[business]["evidence"].append(evidence)
            witnesses.append(witness_ids)
            issues.pop((i, j), None)
        # This update is deliberately after every witness in the layer is evaluated.
        for (league, key, target), entry in candidates.items():
            known_by_league[league][key] = target
            dependency_ids[league].add(entry["candidate_id"])
        if not added:
            break
    # Check all layers together; no first-writer-wins for global IDs or reverse IDs.
    values, reverse = defaultdict(set), defaultdict(set)
    for league, key, target in candidates:
        values[key].add(target)
        reverse[league, target].add(key)
    invalid = set(reject)
    observations = defaultdict(list)
    for row in [*apif, *official]:
        observations[row["match_id"]].append(row)
    for entries in previous.values():
        for key, entry in entries.items():
            if not entry.get("candidate_id"):
                continue
            target = entry.get("canonical_team_id")
            if target is not None and globals_[str(key)] - {str(target)}:
                invalid.add(entry["candidate_id"])
                entry["evidence_status"] = "withdrawn_conflict"
                entry["conflicting_owners"] = dict(
                    canonical_team_ids=sorted(globals_[str(key)]),
                    formal_entries=[
                        dict(league=league, entry=deepcopy(records[key]))
                        for league, records in formal.items()
                        if key in records and records[key].get("status") in ACTIVE
                    ],
                    database_target_ids=sorted(str(t) for t in (occupied or {}).get(key, [])),
                )
                entry["conflict_observed_utc"] = observed_utc
            conflicts = []
            for proof in entry.get("evidence", []):
                for field in ("apif", "counterpart", "target"):
                    old = proof.get(field)
                    if not isinstance(old, dict) or not old.get("match_id"):
                        continue
                    current = observations[old["match_id"]]
                    if old["match_id"] in source_conflicts | official_conflicts or any(
                        _contradicts(old, row) for row in current
                    ):
                        conflicts.extend(deepcopy(current))
            if conflicts:
                invalid.add(entry["candidate_id"])
                entry["evidence_status"] = "withdrawn_conflict"
                entry["conflicting_candidates"] = conflicts
                entry["conflict_observed_utc"] = observed_utc
            if key in conflicted_keys and entry.get("candidate_id"):
                invalid.add(entry["candidate_id"])
                entry["evidence_status"] = "withdrawn_conflict"
                entry["conflicting_candidates"] = conflicting_observations[key]
    for (league, key, target), entry in candidates.items():
        prior = previous.get(league, {}).get(key)
        if (
            len(values[key]) > 1
            or len(reverse[league, target]) > 1
            or key in conflicted_keys
            or globals_[key]
            and globals_[key] != {target}
            or formal_reverse[league, target] - {key}
        ):
            entry["evidence_status"] = "withdrawn_conflict"
            invalid.add(entry["candidate_id"])
        prior_target = prior.get("canonical_team_id") if prior else None
        if prior and prior_target is None and prior.get("canonical") == teams[target]["name"]:
            prior_target = target
        if prior and prior.get("canonical") is not None and prior_target != target:
            entry["evidence_status"] = "withdrawn_conflict"
            entry["conflicting_candidates"] = [deepcopy(prior)]
            invalid.add(entry["candidate_id"])
        if entry["candidate_id"] in reject:
            entry["evidence_status"] = "rejected"
    previous_witnesses = defaultdict(list)
    for entries in previous.values():
        for entry in entries.values():
            for proof in entry.get("evidence", []):
                if entry.get("candidate_id") and proof.get("kind") == "strict_oriented_fixture":
                    pair = (proof["apif"]["match_id"], proof["counterpart"]["match_id"])
                    previous_witnesses[pair].append(entry["candidate_id"])
    witnesses.extend(previous_witnesses.values())
    # Reject a complete two-sided witness when one of its identities conflicts.
    changed = True
    while changed:
        before = set(invalid)
        for witness in witnesses:
            if set(witness) & invalid:
                invalid.update(witness)
        old_entries = [
            e for entries in previous.values() for e in entries.values() if e.get("candidate_id")
        ]
        for entry in [*candidates.values(), *old_entries]:
            if set(entry.get("depends_on", [])) & invalid:
                invalid.add(entry["candidate_id"])
        changed = invalid != before
    output, new, existing_reviews = deepcopy(previous), 0, []
    for (league, key, _), entry in sorted(candidates.items()):
        if entry["candidate_id"] in invalid and entry["evidence_status"] == "strict_verified":
            entry["evidence_status"] = "withdrawn_dependency"
        old = output.get(league, {}).get(key)
        if old and not old.get("candidate_id") and old.get("canonical") == entry["canonical"]:
            existing_reviews.append(dict(entry, league=league, legacy_queue_entry=deepcopy(old)))
            continue
        if (
            old
            and old.get("candidate_id") == entry["candidate_id"]
            and old.get("canonical_team_id") == entry["canonical_team_id"]
        ):
            if old.get("evidence_status") == "withdrawn_conflict":
                for field in (
                    "evidence_status",
                    "conflicting_candidates",
                    "conflicting_owners",
                    "conflict_observed_utc",
                ):
                    if field in old:
                        entry[field] = deepcopy(old[field])
            for field in ("proposed_utc", "verified_utc", "proposed_beijing"):
                entry[field] = old.get(field, entry[field])
            entry["evidence"] = list(
                {_json(e): e for e in [*old.get("evidence", []), *entry["evidence"]]}.values()
            )
        elif old is None:
            new += 1
        output.setdefault(league, {})[key] = entry
    for entries in output.values():
        for entry in entries.values():
            if (
                entry.get("candidate_id") in invalid
                and entry.get("evidence_status") == "strict_verified"
            ):
                entry["evidence_status"] = (
                    "rejected" if entry["candidate_id"] in reject else "withdrawn_dependency"
                )
    source_indices = {i for i, _ in temporal}
    official_indices = {j for _, j in temporal}
    for i in range(len(sources)):
        if i not in source_indices:
            issue(i, None, ["no_counterpart_in_window"])
    for j in range(len(counterparts)):
        if j not in official_indices:
            issue(None, j, ["no_provider_in_window"])
    if diagnostic:
        for rows in output.values():
            for entry in rows.values():
                if entry.get("evidence_status") == "strict_verified":
                    entry["evidence_status"] = "diagnostic_only"
                    entry["not_for_approval"] = "Identity category checks were not applied."
    entries = [e for rows in output.values() for e in rows.values()]
    return dict(
        apif=output,
        existing_candidate_reviews=existing_reviews,
        unverified=list(issues.values()),
        summary=dict(
            new_candidates=new,
            legacy_preserved=sum(not e.get("candidate_id") for e in entries),
            strict_verified=sum(e.get("evidence_status") == "strict_verified" for e in entries),
            withdrawn=sum(
                e.get("evidence_status")
                in {"rejected", "withdrawn_conflict", "withdrawn_dependency"}
                for e in entries
            ),
            diagnostic_verified=sum(e.get("evidence_status") == "diagnostic_only" for e in entries),
            apif_fixtures=len(sources),
            counterpart_fixtures=len(counterparts),
            unverified_reasons=dict(
                Counter(reason for row in issues.values() for reason in row["reasons"])
            ),
            requests=0,
            production_writes=0,
            observed_utc=observed_utc,
            diagnostic_only=diagnostic,
        ),
    )


def _previous_queue(archive, explicit):
    root = Path(archive)
    relative = "state/alias_candidates/apif.json"
    path = Path(explicit) if explicit else root / relative
    manifest_path = root / "feed_manifest.json"
    declared = None
    if not explicit and manifest_path.is_file():
        declared = json.loads(manifest_path.read_bytes()).get("files", {}).get(relative)
    if not path.is_file():
        if declared or explicit:
            raise ValueError("previous candidate queue is missing")
        return {}, dict(path=str(path), status="not_declared")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if declared and declared != sha:
        raise ValueError("previous candidate queue SHA differs from frozen manifest")
    value = json.loads(raw)
    if not isinstance(value, dict) or any(
        not isinstance(entries, dict) for entries in value.values()
    ):
        raise ValueError("previous candidate queue schema is invalid")
    return value, dict(
        path=str(path), sha256=sha, source="explicit_replay" if explicit else "frozen_manifest"
    )


def replay(archive, out, *, leagues=None, previous_candidates=None, reject=(), fd_raw_dir=None):
    from zucai.identity_binding_archive import load_archive

    out = Path(out)
    if out.exists():
        raise FileExistsError(out)
    previous, previous_provenance = _previous_queue(archive, previous_candidates)
    inputs = load_archive(Path(archive), leagues=leagues, fd_raw_dir=fd_raw_dir)
    inputs["summary"]["previous_candidates"] = previous_provenance
    result = propose_bindings(
        inputs["apif"],
        inputs["official"],
        inputs["teams"],
        formal=inputs["formal"],
        previous=previous,
        reject=reject,
        occupied=inputs["occupied"],
        formal_sha256=inputs["summary"].get("formal_aliases_sha256"),
    )
    diagnostic = propose_bindings(
        inputs["apif"],
        inputs["official"],
        inputs["teams"],
        formal=inputs["formal"],
        occupied=inputs["occupied"],
        diagnostic=True,
        use_formal_anchors=False,
    )
    with_anchors = propose_bindings(
        inputs["apif"],
        inputs["official"],
        inputs["teams"],
        formal=inputs["formal"],
        occupied=inputs["occupied"],
        diagnostic=True,
    )
    result["new_competition_diagnostic"] = dict(
        warning=(
            "Category checks omitted; formal approved anchors allowed; no production eligibility."
        ),
        apif={
            league: entries
            for league, entries in with_anchors["apif"].items()
            if league in inputs["summary"].get("new_route_leagues", [])
        },
        summary=with_anchors["summary"],
    )
    result["summary"]["inputs"] = inputs["summary"]
    result["diagnostic_matches"] = dict(
        warning=(
            "Diagnostics omit category checks and all formal anchors; never eligible for approval."
        ),
        apif=diagnostic["apif"],
        summary=diagnostic["summary"],
    )
    result["diagnostic_known_answers"] = known_answers(diagnostic, inputs["formal"])
    result["summary"]["diagnostic_counts"] = {
        league: sum(e.get("evidence_status") == "diagnostic_only" for e in entries.values())
        for league, entries in diagnostic["apif"].items()
    }
    result["summary"]["new_competition_diagnostic_count"] = sum(
        entry["evidence_status"] == "diagnostic_only"
        for entries in result["new_competition_diagnostic"]["apif"].values()
        for entry in entries.values()
    )
    result["unverified"].extend(inputs["issues"])
    result["known_answers"] = known_answers(result, inputs["formal"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out.parent) as temporary:
        staged = Path(temporary) / "proposals"
        staged.mkdir()
        for name, value in result.items():
            (staged / f"{name}.json").write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            )
        staged.rename(out)
    return result


def known_answers(result, formal):
    expected = {"日职": 19, "瑞超": 14, "挪超": 14, "美职": 17, "韩职": 12, "芬超": 6, "澳超": 11}
    rows, opposite = [], []
    for league, target_count in expected.items():
        for key, old in formal.get(league, {}).items():
            if old.get("status") not in ACTIVE:
                continue
            candidate = result["apif"].get(league, {}).get(key)
            status = "not_reproduced"
            if candidate and candidate.get("evidence_status") in {
                "strict_verified",
                "diagnostic_only",
            }:
                status = "reproduced" if candidate["canonical"] == old["canonical"] else "opposite"
            item = dict(
                league=league,
                provider_team_id=key,
                expected_canonical=old["canonical"],
                outcome=status,
                actual={k: candidate[k] for k in ("candidate_id", "canonical", "evidence_status")}
                if candidate
                else None,
                reasons=sorted(
                    {
                        reason
                        for issue in result["unverified"]
                        if issue.get("league") == league
                        and key in issue.get("provider_team_ids", [])
                        for reason in issue["reasons"]
                    }
                )
                if status != "reproduced"
                else [],
                reference_count=target_count,
            )
            rows.append(item)
            if status == "opposite":
                opposite.append(item)
    return dict(
        expected_counts=expected,
        expected_total=93,
        compared_formal_items=len(rows),
        reproduced=sum(row["outcome"] == "reproduced" for row in rows),
        opposite=opposite,
        rows=rows,
        new_competition_reference=dict(
            possible=107,
            finished_db_matches=150,
            note=(
                "Review aggregate only; no per-item artifact supplied. "
                "Fixed JC catalogue ends 2026-10-31."
            ),
        ),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--leagues", nargs="+")
    parser.add_argument("--previous-candidates", type=Path)
    parser.add_argument("--fd-raw-dir", type=Path)
    parser.add_argument("--reject", action="append", default=[])
    args = parser.parse_args(argv)
    result = replay(**vars(args))
    LOG.warning(
        "T68 strict proposals=%s; new=%s; requests=0; production writes=0",
        result["summary"]["strict_verified"],
        result["summary"]["new_candidates"],
    )


def same_provider_proposals(
    apif, teams, formal, formal_sha256, observed_utc, *, conflicting_fixture_ids=()
):
    """Reuse an explicit formal category only with matching target raw evidence."""
    targets, reverse = _formal_targets(formal, teams)
    rows, original_conflicts = _deduplicate(apif)
    blocked = original_conflicts | set(conflicting_fixture_ids)
    output = {}
    for row in rows:
        if row["match_id"] in blocked:
            continue
        for side in ("home", "away"):
            key, category = str(row[side]), row.get(f"{side}_category")
            if (
                not valid_identity_category(category)
                or len(targets[key]) != 1
                or _category_veto(row.get(f"{side}_name", ""), category)
            ):
                continue
            target = next(iter(targets[key]))
            team = teams.get(target)
            if (
                not team
                or team.get("category") != category
                or reverse[row["league"], target] - {key}
            ):
                continue
            origins = []
            for league, entries in formal.items():
                entry = entries.get(key, {})
                source_category = entry.get("binding_scope", {}).get(
                    "category", entry.get("category")
                )
                country = row.get(f"{side}_country")
                country_ok = category.startswith("national/") or (
                    country not in (None, "", "World", "Europe", "Asia", "Africa")
                    and entry.get("identity_country") == country
                )
                if (
                    entry.get("status") in ACTIVE
                    and source_category == category
                    and binding_scope_allows(entry, league, kickoff_utc=row.get("kickoff_utc"))
                    and country_ok
                    and entry.get("evidence")
                ):
                    origins.append(
                        dict(
                            league=league,
                            pointer=f"/{league}/{key}",
                            sha256=formal_sha256,
                            entry=entry,
                        )
                    )
            if not origins or not row.get("provenance"):
                continue
            output.setdefault(row["league"], {})[key] = dict(
                candidate_id=_id(row, key, target),
                status="proposed",
                evidence_status="strict_verified",
                canonical=team["name"],
                canonical_team_id=target,
                name=row.get(f"{side}_name", ""),
                category=category,
                provider_competition_id=row["competition_id"],
                provider_team_id=key,
                layer=0,
                depends_on=[],
                proposed_utc=observed_utc,
                verified_utc=observed_utc,
                binding_scope=dict(leagues=[row["league"]], category=category),
                evidence=[dict(kind="same_provider_id", formal_origins=origins, target=row)],
            )
    return output


if __name__ == "__main__":
    main()
