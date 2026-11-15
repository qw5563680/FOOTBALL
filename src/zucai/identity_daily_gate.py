"""Read-only, per-competition T68 opening register. Never changes registry policy."""

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from zucai.cloud_jc_catalogue import complete_month_window
from zucai.identity_gate_evidence import (
    document as _document,
)
from zucai.identity_gate_evidence import (
    existing_links,
    held_inventory,
    outcome,
    recent_importer,
    replay_importer,
    transitions,
)
from zucai.identity_gate_evidence import (
    export as _export,
)
from zucai.identity_gate_evidence import (
    proof as _proof,
)
from zucai.identity_gate_evidence import (
    snapshot as _snapshot,
)
from zucai.sources.apif_aliases import binding_scope_allows, validate_approvals

BEIJING = ZoneInfo("Asia/Shanghai")
RISKS = {"unbound", "same_team_same_time", "reversed", "rejected", "partial_pages", "cross_day"}


def _clock(value):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("cutoff/evidence time must include timezone")
    return instant.astimezone(UTC)


def _bound(facts, league, provider_id, when):
    entries = facts["formal"].get(league, {})
    entry = entries.get(str(provider_id), {})
    if entry.get("status") not in {"approved", "auto_verified"}:
        return None
    if not binding_scope_allows(entry, league, kickoff_utc=when):
        return None
    canonical = entry.get("canonical")
    owners = [
        pid
        for pid, other in entries.items()
        if other.get("canonical") == canonical
        and other.get("status") in {"approved", "auto_verified"}
        and binding_scope_allows(other, league, kickoff_utc=when)
    ]
    return canonical if len(owners) == 1 else None


def _project(facts, key):
    return facts["teams"].get(key, {}).get("project_team_id")


def _provider_project(facts, league, pid, when):
    name = _bound(facts, league, pid, when)
    ids = {
        team["project_team_id"]
        for team in facts["teams"].values()
        if name is not None
        and team.get("project_team_id")
        and (team.get("project_name") or team["name"]) == name
    }
    return next(iter(ids)) if len(ids) == 1 else None


def _relation_graph(facts, matches):
    """One ownership graph for legacy pointers and currently eligible explicit links."""
    eligible = [
        link
        for link in facts.get("result_links", [])
        if (
            not facts.get("gate_cutoff")
            or _clock(link["verified_utc"]) <= _clock(facts["gate_cutoff"])
        )
        and all(
            mid in matches
            and all(
                matches[mid][key] == link[key] for key in ("league", "home_team_id", "away_team_id")
            )
            and _clock(matches[mid]["kickoff_utc"]) == _clock(link[clock])
            for mid, clock in (
                (link["provider_match_id"], "provider_kickoff_utc"),
                (link["counterpart_id"], "counterpart_kickoff_utc"),
            )
        )
    ]
    edges = [
        dict(
            left=r["match_id"],
            right=r["counterpart_id"],
            kind="legacy_pointer",
            pointer=f"state/matches.jsonl#/line/{r.get('row_number')}",
        )
        for r in matches.values()
        if r.get("counterpart_id")
    ]
    edges.extend(
        dict(
            left=r["provider_match_id"],
            right=r["counterpart_id"],
            kind="result_source_link",
            provenance=r["provenance"],
        )
        for r in eligible
    )
    parents = {}

    def find(mid):
        parents.setdefault(mid, mid)
        if parents[mid] != mid:
            parents[mid] = find(parents[mid])
        return parents[mid]

    for edge in edges:
        parents[find(edge["left"])] = find(edge["right"])
    groups = defaultdict(set)
    for mid in parents:
        groups[find(mid)].add(mid)
    bad = []
    for members in groups.values():
        if any(count > 1 for count in Counter(mid.split(":", 1)[0] for mid in members).values()):
            bad.append(
                dict(members=sorted(members), edges=[e for e in edges if e["left"] in members])
            )
    return eligible, bad


def _current_participants(facts, code, selected):
    """Current JC identities need a proven project and approval at their current date."""
    league = facts["configs"][code]["league"]
    appearances = defaultdict(list)
    for mid, row in selected.items():
        for side in ("home", "away"):
            appearances[row[f"{side}_source_id"]].append((mid, row))
    rows = []
    for tid, seen in sorted(appearances.items()):
        team = facts["teams"].get("jc-team:" + tid)
        if team is None:
            team = next(
                (
                    t
                    for key, t in facts["teams"].items()
                    if any(
                        r.get(side) == key and r[f"{side}_source_id"] == tid
                        for r in selected.values()
                        for side in ("home", "away")
                    )
                ),
                {},
            )
        project = team.get("project_team_id")
        clocks = []
        for _, row in seen:
            if row.get("kickoff_utc"):
                clocks.append(row["kickoff_utc"])
            else:
                low = datetime.combine(
                    date.fromisoformat(row["match_date"]), datetime.min.time(), BEIJING
                )
                clocks.extend(
                    [
                        low.isoformat(),
                        (low + timedelta(days=1) - timedelta(microseconds=1)).isoformat(),
                    ]
                )
        pids = [
            pid
            for pid in facts["formal"].get(league, {})
            if project
            and all(_provider_project(facts, league, pid, clock) == project for clock in clocks)
        ]
        rows.append(
            dict(
                official_team_id=tid,
                project_team_id=project,
                provider_team_ids=pids,
                match_ids=sorted(mid for mid, _ in seen),
                reason=None
                if len(pids) == 1 and not team.get("ambiguous")
                else "current_official_identity_or_binding_unverified",
            )
        )
    return dict(total=len(rows), participants=rows, unbound=[r for r in rows if r["reason"]])


def _facts_coverage(facts, code):
    league = facts["configs"][code]["league"]
    official = [
        r
        for r in facts["official"]
        if r["competition_id"] == code and r["match_id"].startswith("jc:")
    ]
    by_id = defaultdict(list)
    for row in official:
        for side in ("home", "away"):
            by_id[row[f"{side}_source_id"]].append((row, side))
    bound, unbound, bindings, identity_conflicts = [], [], [], []
    blockers = []
    for team_id, appearances in sorted(by_id.items()):
        projects = {_project(facts, row[side]) for row, side in appearances}
        project = next(iter(projects)) if len(projects) == 1 else None
        pids = {
            pid
            for pid in facts["formal"].get(league, {})
            if project
            and all(
                _provider_project(facts, league, pid, row.get("kickoff_utc")) == project
                for row, _ in appearances
            )
        }
        team = facts["teams"].get(appearances[0][0][appearances[0][1]], {})
        item = dict(
            official_team_id=team_id,
            project_team_id=project,
            canonical=team.get("project_name"),
            project_identity_evidence=team.get("project_identity_evidence", []),
        )
        hard_issues = [
            issue
            for issue in facts.get("issues", [])
            if issue.get("match_id") in {r["match_id"] for r, _ in appearances}
            and set(issue.get("reasons", []))
            & {"current_snapshot_identity_conflict", "conflicting_jc_kickoff_evidence"}
        ]
        if team.get("identity_status") == "conflict" or team.get("ambiguous") or hard_issues:
            blockers.append("official_identity_conflict")
            identity_conflicts.append(
                dict(
                    item,
                    identity_status=team.get("identity_status"),
                    ambiguous=team.get("ambiguous", False),
                    conflicting_project_ids=team.get("conflicting_project_ids", []),
                    issues=hard_issues,
                    provenance=team.get("provenance", []),
                )
            )
        if len(pids) == 1 and not (
            team.get("identity_status") == "conflict" or team.get("ambiguous") or hard_issues
        ):
            bound.append(team_id)
            bindings.append(dict(item, provider_team_id=next(iter(pids))))
        else:
            unbound.append(dict(item, reason="no_unique_approved_scoped_binding"))
    for row in official:
        for entry in facts["formal"].get(league, {}).values():
            if entry.get("status") in {"approved", "auto_verified"}:
                owners = [
                    pid
                    for pid, candidate in facts["formal"][league].items()
                    if candidate.get("canonical") == entry.get("canonical")
                    and candidate.get("status") in {"approved", "auto_verified"}
                    and binding_scope_allows(candidate, league, kickoff_utc=row.get("kickoff_utc"))
                ]
                if len(owners) > 1:
                    blockers.append("non_unique_approved_binding")
    matches = {r["match_id"]: r for r in facts["matches"]}
    originals = defaultdict(list)
    for row in facts["apif"] + facts.get(
        "fd_originals", [r for r in facts["official"] if r["match_id"].startswith("fd:")]
    ):
        originals[row["match_id"]].append(row)
    eligible_links, graph_conflicts = _relation_graph(facts, matches)
    conflict_nodes = {mid for conflict in graph_conflicts for mid in conflict["members"]}
    graph_conflicts = [
        c for c in graph_conflicts if set(c["members"]) & {r["match_id"] for r in official}
    ]
    from zucai.identity_gate_inventory import current_scope, prior_pending

    scope = current_scope(facts, code)
    catalogue_count = len(official)
    never_stored = [r["match_id"] for r in official if r["match_id"] not in matches]
    official = [r for r in official if r["match_id"] in scope["match_ids"]]
    if not scope["verified"]:
        blockers.append("current_season_scope_unverified")
    if scope["newer_current"]:
        blockers.append("newer_season_originals_require_review")
    linked, unresolved = [], []
    for mid in sorted(set(scope["match_ids"]) - {r["match_id"] for r in official}):
        unresolved.append(
            dict(jc_id=mid, provider_match_id=None, reason="stored_current_jc_original_missing")
        )
    for row in official:
        stored = matches.get(row["match_id"], {})
        counterpart = stored.get("counterpart_id")
        link_rows = [
            link
            for link in facts.get("result_links", [])
            if link["counterpart_id"] in {row["match_id"], counterpart}
        ]
        verified_links = [link for link in link_rows if link in eligible_links]
        referenced = {link["provider_match_id"] for link in verified_links}
        if counterpart:
            referenced.add(counterpart)
        conflict = any(n > 1 for n in Counter(mid.split(":", 1)[0] for mid in referenced).values())
        if not counterpart and len(referenced) == 1:
            counterpart = next(iter(referenced))
        evidence = dict(
            manifest=facts["manifest"],
            matches_pointer=f"state/matches.jsonl#/line/{stored.get('row_number')}",
            official_provenance=row.get("provenance", []),
        )
        if verified_links:
            evidence["result_source_link"] = verified_links[0]["provenance"]
        reason = "no_stored_relation" if stored else "official_fixture_not_in_current_table"
        if link_rows and not verified_links:
            reason = "explicit_relation_snapshot_or_cutoff_unverified"
            evidence["unverified_result_links"] = link_rows
            blockers.append(reason)
        for counterpart in sorted(referenced) or [None]:
            if conflict or row["match_id"] in conflict_nodes:
                reason = "non_unique_relation"
                blockers.append(reason)
            elif counterpart:
                candidates = originals.get(counterpart, [])
                source = counterpart.split(":", 1)[0]
                valid = candidates and all(
                    candidate["competition_id"] == code
                    and candidate.get("complete")
                    and (
                        not facts.get("gate_cutoff")
                        or all(
                            _clock(p["fetched_at_utc"]) <= _clock(facts["gate_cutoff"])
                            for p in candidate["provenance"]
                        )
                    )
                    and row.get("kickoff_utc")
                    and candidate.get("kickoff_utc")
                    and abs(_clock(candidate["kickoff_utc"]) - _clock(row["kickoff_utc"]))
                    <= timedelta(hours=3)
                    and row.get("score") is not None
                    and candidate.get("score") == row["score"]
                    and candidate.get("regular_time_basis")
                    == row.get("regular_time_basis")
                    == "regular"
                    and all(
                        _project(facts, row[side]) is not None
                        and (
                            _provider_project(
                                facts, league, candidate[side], candidate["kickoff_utc"]
                            )
                            if source == "apif"
                            else _project(facts, candidate[side])
                        )
                        == _project(facts, row[side])
                        for side in ("home", "away")
                    )
                    for candidate in candidates
                )
                if valid:
                    linked.append(
                        dict(
                            jc_id=row["match_id"],
                            source=source,
                            provider_match_id=counterpart,
                            **(
                                {"apif_id": counterpart}
                                if source == "apif"
                                else {"fd_id": counterpart}
                            ),
                            project_team_ids={
                                side: _project(facts, row[side]) for side in ("home", "away")
                            },
                            provenance=[p for c in candidates for p in c["provenance"]],
                            provenance_scope="auxiliary_fd_originals"
                            if source == "fd"
                            else "frozen_archive",
                            time_difference_seconds=sorted(
                                {
                                    abs(
                                        (
                                            _clock(c["kickoff_utc"]) - _clock(row["kickoff_utc"])
                                        ).total_seconds()
                                    )
                                    for c in candidates
                                }
                            ),
                            provider_team_ids=[
                                {
                                    side: c.get(f"{side}_source_id", c[side])
                                    for side in ("home", "away")
                                }
                                for c in candidates
                            ],
                            proposal_entity_issues=sorted(
                                {
                                    reason
                                    for issue in facts.get("issues", [])
                                    if row["match_id"] in issue.get("entity_members", [])
                                    for reason in issue["reasons"]
                                }
                            ),
                            stored_evidence=evidence,
                        )
                    )
                    continue
                reason = (
                    "stored_relation_original_unverified"
                    if not candidates
                    else "relation_identity_time_or_score_conflict"
                )
                if candidates:
                    blockers.append(reason)
            unresolved.append(
                dict(
                    jc_id=row["match_id"],
                    provider_match_id=counterpart,
                    reason=reason,
                    evidence=evidence,
                )
            )
    linked_ids = {r["jc_id"] for r in linked}
    unverified_sources = [r for r in unresolved if r["jc_id"] in linked_ids]
    unresolved = [r for r in unresolved if r["jc_id"] not in linked_ids]
    pending = prior_pending(facts, code)
    represented = {r["provider_match_id"] for r in linked}
    pending_extra = [r for r in pending if r["fixture_id"] not in represented]
    stored_by_id = {r["match_id"]: r for r in facts["matches"]}
    resolved_extra = []
    for row in pending_extra:
        mid = row["fixture_id"]
        source_rows = [
            r for r in facts["apif"] if r["match_id"] == mid and r["competition_id"] == code
        ]
        match = stored_by_id.get(mid)
        result = next((r for r in facts.get("results", []) if r["match_id"] == mid), None)
        if (
            match
            and mid not in conflict_nodes
            and source_rows
            and result
            and all(
                r.get("complete")
                and r.get("score") == [result["home_goals"], result["away_goals"]]
                and r.get("kickoff_utc")
                and all(
                    _provider_project(facts, league, r[side], r["kickoff_utc"])
                    == str(match[f"{side}_team_id"])
                    for side in ("home", "away")
                )
                and abs(_clock(r["kickoff_utc"]) - _clock(match["kickoff_utc"]))
                <= timedelta(hours=3)
                for r in source_rows
            )
        ):
            resolved_extra.append(mid)
    total = len(scope["match_ids"]) + len(pending_extra)
    complete = len(linked_ids) + len(resolved_extra)
    if unresolved or len(resolved_extra) != len(pending_extra):
        blockers.append("current_season_fixtures_unresolved")
    counts = Counter(r["source"] for r in linked)
    return (
        dict(
            total=len(by_id),
            bound=len(bound),
            binding_rate=len(bound) / len(by_id) if by_id else None,
            bound_ids=bound,
            bindings=bindings,
            unbound=unbound,
            identity_conflicts=identity_conflicts,
        ),
        dict(
            total=total,
            linked=complete,
            link_rate=complete / total if total else None,
            denominator="stored_current_season_jc_plus_prior_pending_apif_without_double_counting",
            season_scope=scope,
            stored_current_jc=len(scope["match_ids"]),
            prior_pending=pending,
            pending_extra=pending_extra,
            pending_materialized_ids=resolved_extra,
            catalogue_12_month_total=catalogue_count,
            catalogue_never_stored_ids=never_stored,
            linked_by_source={source: counts[source] for source in ("apif", "fd")},
            apif_linked=counts["apif"],
            apif_link_rate=counts["apif"] / total if total else None,
            relations=linked,
            unresolved=unresolved,
            unverified_source_relations=unverified_sources,
            relation_conflicts=graph_conflicts,
        ),
        blockers,
    )


def _materialized_original(facts, code, state, original):
    stored = outcome(state, original["match_id"])
    if stored is None:
        raise ValueError("original fixture has no materialized state")
    match, result = stored
    league = facts["configs"][code]["league"]
    if (
        match["league"] != league
        or not original.get("kickoff_utc")
        or not match.get("kickoff_utc")
        or abs(_clock(match["kickoff_utc"]) - _clock(original["kickoff_utc"])) > timedelta(hours=3)
        or any(
            _provider_project(facts, league, original[side], original["kickoff_utc"])
            != str(match[f"{side}_team_id"])
            for side in ("home", "away")
        )
        or (
            original.get("score") is not None
            and all(v is not None for v in original["score"])
            and (not result or [result["home_goals"], result["away_goals"]] != original["score"])
        )
    ):
        raise ValueError("materialized identity/time/90-minute score differs from original")


def _current_replay(facts, code, section, root):
    """Recheck a real collector report and the prior held audit against current state."""
    season = facts["configs"][code].get("current_season")
    current = [r for r in facts["apif"] if r["competition_id"] == code and r["season"] == season]
    if not current or not all(r["complete"] for r in current):
        raise ValueError("complete current-season originals missing")
    from zucai.sources.apif_ingest import COUNT_KEYS

    report = _document(root, section["report"])
    scopes = [r for r in report["scopes"] if str(r["code"]) == code and r["season"] == season]
    known_sha = {p["sha256"] for r in current for p in r["provenance"]}
    if len(scopes) != 1 or any(
        type(scopes[0].get(k)) is not int or scopes[0][k] < 0 for k in COUNT_KEYS
    ):
        raise ValueError("current season report counts missing or invalid; not completely replayed")
    if (
        len(scopes) != 1
        or scopes[0]["status"] != "complete"
        or scopes[0]["raw"]["sha256"] not in known_sha
        or report.get("processed_files", 0) < 1
        or any(
            scopes[0].get(key, 0)
            for key in ("held_unbound", "suspected_duplicates", "rejected_rows")
        )
        or scopes[0].get("parsed_matches") != len({r["match_id"] for r in current})
    ):
        raise ValueError("current season was not completely replayed")
    held_details = _held_reconciliation(facts, code, section, root)
    before, after = [_export(root, section[key]) for key in ("before_manifest", "after_manifest")]
    if before["folder"].resolve() == after["folder"].resolve():
        raise ValueError("current replay state exports must be independent")
    verified_importer = replay_importer(
        facts, code, section, root, report, scopes[0], before, after
    )
    for row in current:
        _materialized_original(facts, code, after, row)
    return dict(
        season=season,
        distinct_fixtures=len({r["match_id"] for r in current}),
        importer=verified_importer,
        **held_details,
        raw_sha256=sorted(known_sha),
    )


def _held_reconciliation(facts, code, section, root):
    """Reconcile archived held IDs; explanations retain pending risk, never resolve it."""
    season = facts["configs"][code].get("current_season")
    current = [r for r in facts["apif"] if r["competition_id"] == code and r["season"] == season]
    audit = held_inventory(root, section)
    current_ids = {r["match_id"] for r in current}
    league = facts["configs"][code]["league"]
    held, excluded = set(), set()
    for row in audit:
        audit_code, audit_league = row.get("competition_code"), row.get("league")
        if not audit_code and not audit_league:
            raise ValueError("prior held inventory missing league scope")
        if audit_code and audit_league and ((audit_code == code) != (audit_league == league)):
            raise ValueError("prior held league scope contradicts competition ID")
        if (audit_code and audit_code != code) or (not audit_code and audit_league != league):
            continue
        raw_id = row.get("provider_match_id", "")
        if not raw_id.isdecimal() or int(raw_id) <= 0:
            raise ValueError("prior held inventory has missing fixture identity")
        mid = "apif:" + raw_id
        held.add(mid)
        # A CSV's date/season hint is not independent scope evidence. Only the
        # verified original with the same fixture ID and raw SHA may exclude it.
        observed_seasons = {
            r["season"]
            for r in facts["apif"]
            if r["match_id"] == mid
            and r["competition_id"] == code
            and any(p["sha256"] == row.get("raw_sha256") for p in r["provenance"])
        }
        if len(observed_seasons) == 1 and season not in observed_seasons:
            if mid in current_ids:
                raise ValueError("prior held scope contradicts current-season original")
            if row.get("season") and row["season"] != str(next(iter(observed_seasons))):
                raise ValueError("prior held scope contradicts original")
            excluded.add(mid)
    stored = {r["match_id"] for r in facts["matches"] if r["league"] == league} | {
        r.get("counterpart_id") for r in facts["matches"] if r["league"] == league
    }
    relevant = held - excluded
    unresolved = relevant - stored
    explained = []
    if section.get("held_explanations"):
        decisions = _document(root, section["held_explanations"])
        ids = [row["fixture_id"] for row in decisions]
        if len(ids) != len(set(ids)) or not set(ids) <= unresolved:
            raise ValueError("held explanations duplicate or refer to unrelated fixtures")
        for decision in decisions:
            if not decision.get("reason") or decision.get("disposition") != "still_held":
                raise ValueError("held explanation missing explicit pending disposition")
            _proof(root, decision["evidence"])
            explained.append(decision)
    if unresolved - {r["fixture_id"] for r in explained}:
        raise ValueError(
            "previously held IDs remain unresolved or unexplained: " + ",".join(sorted(unresolved))
        )
    return dict(
        held_before=len(held),
        held_before_ids=sorted(held),
        resolved_ids=sorted(relevant & stored),
        excluded_other_season_ids=sorted(excluded),
        explained_unresolved=explained,
    )


def _recent_official(facts, code, section, root, start, end):
    """Keep the fixed twelve-month census; verify additional daily JC originals separately."""
    from zucai.cloud_jc_catalogue import _archived_page, parse_day_pages, parse_official_leagues
    from zucai.sources.sporttery import official_value

    base_start = date.fromisoformat(facts["catalogue_window"]["start_beijing"])
    base_end = date.fromisoformat(facts["catalogue_window"]["end_beijing"])
    league = facts["configs"][code]["league"]
    original_rows = {
        r["match_id"]: r
        for r in facts["official"]
        if r["competition_id"] == code and r["match_id"].startswith("jc:")
    }
    pages, dictionary, hashes = defaultdict(dict), {}, []
    for ref in section.get("official_originals", []):
        raw = _proof(root, ref)
        sidecar = (root / ref["path"]).with_suffix(Path(ref["path"]).suffix + ".meta.json")
        if sidecar.is_symlink():
            raise ValueError("JC supplement sidecar cannot be a symlink")
        meta_raw = sidecar.read_bytes()
        meta = json.loads(meta_raw)
        query = parse_qs(urlparse(meta["url"]).query, keep_blank_values=True)
        day = date.fromisoformat(query["matchBeginDate"][0])
        number = int(query["pageNo"][0])
        if not start <= day <= end or number < 1 or number in pages[day]:
            raise ValueError("unexpected or duplicate JC supplement day/page")
        _archived_page(raw, meta_raw, day, number)
        covered_end = datetime.combine(
            day + timedelta(days=1), datetime.min.time(), BEIJING
        ).astimezone(UTC)
        if _clock(meta["fetched_at_utc"]) < covered_end or (
            facts.get("gate_cutoff")
            and _clock(meta["fetched_at_utc"]) > _clock(facts["gate_cutoff"])
        ):
            raise ValueError("JC supplement capture precedes covered day end or exceeds cutoff")
        pages[day][number] = raw
        hashes.append(
            dict(
                path=ref["path"], sha256=ref["sha256"], metadata_sha256=sha256(meta_raw).hexdigest()
            )
        )
        for entry in parse_official_leagues(raw):
            prior = dictionary.setdefault(entry["league_id"], entry)
            if prior != entry:
                raise ValueError("JC supplement dictionary conflict")
    verified_leagues = facts.get("official_leagues", {}).get(code)
    if verified_leagues and any(
        entry["league_abbr"] == league and entry not in verified_leagues
        for entry in dictionary.values()
    ):
        raise ValueError("JC supplement differs from fixed official league identity")
    covered, selected, seen_all = [], {}, set()
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        base = {mid: r for mid, r in original_rows.items() if r["match_date"] == day.isoformat()}
        if day in pages:
            numbered = pages[day]
            if set(numbered) != set(range(1, len(numbered) + 1)):
                raise ValueError("JC supplement missing page")
            raws = [numbered[number] for number in sorted(numbered)]
            parsed = parse_day_pages(day, raws)
            if seen_all & {r["match_id"] for r in parsed}:
                raise ValueError("JC supplement duplicate match across dates")
            seen_all.update(r["match_id"] for r in parsed)
            target = {}
            for raw in raws:
                for row in official_value(raw)["matchResult"]:
                    if any(
                        type(row.get(key)) is not int or row[key] <= 0
                        for key in ("homeTeamId", "awayTeamId")
                    ):
                        raise ValueError("JC supplement missing numeric team identity")
                    if row["leagueNameAbbr"] == league:
                        target[f"jc:{row['matchId']}"] = dict(
                            home_source_id=str(row["homeTeamId"]),
                            away_source_id=str(row["awayTeamId"]),
                            match_date=day.isoformat(),
                            kickoff_utc=base.get(f"jc:{row['matchId']}", {}).get("kickoff_utc"),
                        )
            if base_start <= day <= base_end and (
                set(target) != set(base)
                or any(
                    any(
                        target[mid][side] != base[mid][side]
                        for side in ("home_source_id", "away_source_id")
                    )
                    for mid in target
                )
            ):
                raise ValueError("JC supplement contradicts fixed twelve-month originals")
            known = {
                r["match_id"]
                for r in facts["matches"]
                if r["source"] == "jc"
                and r["league"] == league
                and r.get("kickoff_utc")
                and _clock(r["kickoff_utc"]).astimezone(BEIJING).date() == day
            }
            if not known <= set(target):
                raise ValueError("JC supplement omitted known archived fixtures")
            selected.update(target)
        elif base_start <= day <= base_end:
            selected.update(base)
        else:
            raise ValueError(
                "official 30-day denominator incomplete: missing JC day " + day.isoformat()
            )
        covered.append(day.isoformat())
    return dict(
        start_beijing=start.isoformat(),
        end_beijing=end.isoformat(),
        official_match_ids=sorted(selected),
        matches=selected,
        official_team_ids=sorted(
            {r[key] for r in selected.values() for key in ("home_source_id", "away_source_id")}
        ),
        covered_beijing_dates=covered,
        supplement_originals=hashes,
    )


def _recent_replay(facts, code, section, root, start, end):
    from zucai.identity_binding_archive import _proof as raw_proof
    from zucai.sources.api_football import API, ApiFootball

    official = _recent_official(facts, code, section, root, start, end)
    first_utc = datetime.combine(start, datetime.min.time(), BEIJING).astimezone(UTC)
    end_utc = datetime.combine(end + timedelta(days=1), datetime.min.time(), BEIJING).astimezone(
        UTC
    )
    expected_days = {
        (first_utc + timedelta(days=i)).date().isoformat()
        for i in range((end_utc.date() - first_utc.date()).days + 1)
    }
    days, fixtures, hashes, rejected_ids = set(), {}, set(), set()
    mode = section.get("mode", "dates")
    if mode not in {"dates", "equivalent"}:
        raise ValueError("unknown replay mode")
    expected_ids = None
    if mode == "equivalent":
        expected = [
            r
            for r in facts["apif"]
            if r["competition_id"] == code
            and r.get("kickoff_utc")
            and first_utc <= _clock(r["kickoff_utc"]) < end_utc
        ]
        if not expected or any(
            not r.get("complete")
            or not any(_clock(p.get("fetched_at_utc", "")) >= end_utc for p in r["provenance"])
            for r in expected
        ):
            raise ValueError("equivalent replay has no fresh complete real denominator")
        expected_ids = {r["match_id"] for r in expected}
    parser = ApiFootball(leagues={code: {"league": facts["configs"][code]["league"]}})
    try:
        for ref in section["originals"]:
            _proof(root, ref)
            raw, meta, _ = raw_proof(root / ref["path"], root)
            url = urlparse(meta["url"])
            params = parse_qs(url.query)
            if (
                url.scheme + "://" + url.netloc + url.path != API + "/fixtures"
                or (
                    set(params) != {"date"}
                    and not (
                        mode == "equivalent"
                        and set(params) == {"league", "season"}
                        and params["league"] == [code]
                    )
                )
                or any(len(values) != 1 for values in params.values())
            ):
                raise ValueError("daily original must be an unfiltered UTC date capture")
            day = params.get("date", [None])[0]
            covered_end = end_utc
            if day is not None:
                if day not in expected_days or day in days:
                    raise ValueError("unexpected or duplicate UTC date original")
                covered_end = min(end_utc, _clock(day + "T00:00:00Z") + timedelta(days=1))
            if _clock(meta["fetched_at_utc"]) < covered_end or (
                facts.get("gate_cutoff")
                and _clock(meta["fetched_at_utc"]) > _clock(facts["gate_cutoff"])
            ):
                raise ValueError("daily capture precedes covered interval end or exceeds cutoff")
            doc = parser._document(raw, {key: values[0] for key, values in params.items()})
            parser.parse(raw)
            for rejected in parser.rejected:
                if rejected.get("provider_match_id") is None:
                    raise ValueError("unattributable rejected competitor")
                rejected_ids.add(f"apif:{rejected['provider_match_id']}")
            _complete_pages(doc)
            if day is not None:
                days.add(day)
            hashes.add(ref["sha256"])
            for row in doc["response"]:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("league"), dict)
                    or type(row["league"].get("id")) is not int
                ):
                    raise ValueError("unattributable daily competitor")
                if str(row["league"]["id"]) != code:
                    continue
                fixture = row["fixture"]
                when = _clock(fixture["date"])
                if (
                    type(fixture["id"]) is not int
                    or type(fixture["timestamp"]) is not int
                    or int(when.timestamp()) != fixture["timestamp"]
                    or (day is not None and when.date().isoformat() != day)
                ):
                    raise ValueError("daily competitor identity/time mismatch")
                if first_utc <= when < end_utc:
                    prior = fixtures.setdefault(fixture["id"], row)
                    if prior != row:
                        raise ValueError("conflicting daily fixture observations")
        if mode == "dates" and not expected_days <= days:
            raise ValueError("missing UTC days across Beijing window")
    finally:
        parser.session.close()
    known = {
        r["match_id"]
        for r in facts["apif"]
        if r["competition_id"] == code
        and r.get("kickoff_utc")
        and first_utc <= _clock(r["kickoff_utc"]) < end_utc
    }
    known.update(
        r["match_id"]
        for r in facts["matches"]
        if r["source"] == "apif"
        and r["league"] == facts["configs"][code]["league"]
        and r.get("kickoff_utc")
        and first_utc <= _clock(r["kickoff_utc"]) < end_utc
    )
    if not known <= {f"apif:{mid}" for mid in fixtures}:
        raise ValueError("daily originals omitted known archived fixtures")
    # Equivalent samples require the same real denominator, plus explicit boundary cases.
    if mode == "equivalent":
        if {f"apif:{mid}" for mid in fixtures} != expected_ids:
            raise ValueError("equivalent sample differs from real fixture denominator")
        _boundary_cases(facts, code, _document(root, section["boundary_cases"]), root)
    replay = _document(root, section["results"])
    if set(replay.get("official_match_ids", [])) != set(official["official_match_ids"]):
        raise ValueError("replay official denominator differs from verified JC originals")
    if (
        set(replay["fixture_ids"]) != {f"apif:{i}" for i in fixtures}
        or set(replay["raw_sha256"]) != hashes
    ):
        raise ValueError("replay omitted expected fixtures/originals")
    if type(replay.get("inserted")) is not int or replay["inserted"] < 0:
        raise ValueError("missing replay result counts")
    if not fixtures:
        raise ValueError("zero fixture denominator; recent active window requires review")
    outcomes = replay["outcomes"]
    if len(outcomes) != len(fixtures) or {r["fixture_id"] for r in outcomes} != set(
        replay["fixture_ids"]
    ):
        raise ValueError("replay outcomes omit or duplicate fixtures")
    allowed = {
        "inserted",
        "updated",
        "unchanged",
        "linked",
        "cancelled",
        "held_unbound",
        "suspected",
        "rejected",
    }
    if any(r["outcome"] not in allowed for r in outcomes):
        raise ValueError("unknown replay outcome")
    risks = {r["fixture_id"] for r in outcomes if r["outcome"] in {"suspected", "rejected"}}
    if risks != set(replay["risk_ids"]) or not (rejected_ids & set(replay["fixture_ids"])) <= risks:
        raise ValueError("risk inventory differs from actual replay outcomes")
    checked_transitions = transitions(root, section, outcomes, inserted=replay["inserted"])
    after = _export(root, section["after_manifest"])
    for result in outcomes:
        if result["outcome"] in {"suspected", "rejected"}:
            continue
        raw = fixtures[int(result["fixture_id"].split(":")[1])]
        _materialized_original(
            facts,
            code,
            after,
            dict(
                match_id=result["fixture_id"],
                home=str(raw["teams"]["home"]["id"]),
                away=str(raw["teams"]["away"]["id"]),
                kickoff_utc=raw["fixture"]["date"],
                score=[raw["score"]["fulltime"][side] for side in ("home", "away")],
                status=raw["fixture"]["status"]["short"],
            ),
        )
    return dict(
        transitions=checked_transitions,
        mode=mode,
        distinct_fixtures=len(fixtures),
        expected_distinct_fixtures=len(expected_ids) if expected_ids is not None else len(fixtures),
        utc_dates=sorted(days),
        raw_sha256=sorted(hashes),
        risk_ids=sorted(set(replay["risk_ids"])),
        official_match_ids=official["official_match_ids"],
        official_window=official,
    )


def _verified_recent(facts, code, section, root, start, end):
    result = _recent_replay(facts, code, section, root, start, end)
    result["importer"] = recent_importer(
        facts,
        code,
        section,
        root,
        section["originals"],
        _document(root, section["results"])["outcomes"],
    )
    return result


def _complete_pages(doc):
    if doc["paging"] != {"current": 1, "total": 1}:
        raise ValueError("partial daily pages")


def _boundary_handling(facts, code, case, root, doc):
    """Re-execute the named sample, never accept a prose handling declaration."""
    section = case.get("replay")
    if not isinstance(section, dict):
        raise ValueError("boundary lacks materialized replay evidence")
    report = _document(root, section["results"])
    outcomes = report["outcomes"]
    if (
        len(outcomes) != len(case["fixture_ids"])
        or {r["fixture_id"] for r in outcomes} != set(case["fixture_ids"])
        or type(report.get("inserted")) is not int
        or report["inserted"] < 0
    ):
        raise ValueError("boundary result fixture inventory differs")
    before, after = [_export(root, section[k]) for k in ("before_manifest", "after_manifest")]
    if before["folder"].resolve() == after["folder"].resolve():
        raise ValueError("boundary needs independent saved exports")
    if case["category"] == "partial_pages":
        try:
            _complete_pages(doc)
        except ValueError:
            pass
        else:
            raise ValueError("boundary did not exercise partial-page rejection")
        if (
            report["inserted"] != 0
            or any(r["outcome"] != "partial_pages_rejected" for r in outcomes)
            or before["manifest"]["files"] != after["manifest"]["files"]
            or before["tables"] != after["tables"]
        ):
            raise ValueError("boundary partial-page guard/state differs")
        return dict(partial_pages_rejected=True, inserted=0)
    _complete_pages(doc)
    allowed = {
        "inserted",
        "updated",
        "unchanged",
        "linked",
        "cancelled",
        "held_unbound",
        "suspected",
        "rejected",
    }
    if any(r["outcome"] not in allowed for r in outcomes):
        raise ValueError("boundary handling result unknown")
    # Held is a valid negative boundary result, never permission to open daily.
    from zucai.identity_gate_evidence import outcome

    for row in outcomes:
        if row["outcome"] == "held_unbound" and outcome(before, row["fixture_id"]) != outcome(
            after, row["fixture_id"]
        ):
            raise ValueError("boundary held fixture changed stored state")
    transitions(
        root,
        section,
        [r for r in outcomes if r["outcome"] != "held_unbound"],
        inserted=report["inserted"],
    )
    return recent_importer(facts, code, section, root, [case["original"]], outcomes)


def _boundary_cases(facts, code, cases, root):
    """Require actual risky rows, not six labels attached to one easy fixture."""
    from zucai.identity_binding_archive import _proof as raw_proof
    from zucai.sources.api_football import API, ApiFootball

    if len(cases) != len(RISKS) or {case["category"] for case in cases} != RISKS:
        raise ValueError("equivalent replay lacks risk distribution")
    pairs = {(r["home"], r["away"]) for r in facts["apif"] if r["competition_id"] == code}
    league = facts["configs"][code]["league"]
    for case in cases:
        _proof(root, case["original"])
        raw, meta, _ = raw_proof(root / case["original"]["path"], root)
        url = urlparse(meta["url"])
        params = parse_qs(url.query)
        scope = {k: values[0] for k, values in params.items() if k != "page"}
        if (
            url.scheme + "://" + url.netloc + url.path != API + "/fixtures"
            or url.fragment
            or any(len(values) != 1 for values in params.values())
            or set(params) - {"league", "season", "date", "page"}
            or not (
                set(scope) == {"date"}
                or set(scope) == {"league", "season"}
                and scope["league"] == code
            )
            or facts.get("gate_cutoff")
            and _clock(meta["fetched_at_utc"]) > _clock(facts["gate_cutoff"])
        ):
            raise ValueError("boundary source/scope/capture is invalid")
        parser = ApiFootball(leagues={code: {"league": league}})
        try:
            doc = parser._document(raw, scope)
        finally:
            parser.session.close()
        if "page" in params and params["page"] != [str(doc["paging"]["current"])]:
            raise ValueError("boundary page differs from source query")
        rows = [r for r in doc["response"] if str(r["league"]["id"]) == code]
        for r in rows:
            fixture = r["fixture"]
            when = _clock(fixture["date"])
            if (
                type(fixture["id"]) is not int
                or fixture["id"] <= 0
                or type(fixture["timestamp"]) is not int
                or int(when.timestamp()) != fixture["timestamp"]
                or "date" in scope
                and when.date().isoformat() != scope["date"]
                or "season" in scope
                and str(r["league"]["season"]) != scope["season"]
            ):
                raise ValueError("boundary fixture identity/time/scope mismatch")
        if (
            case["original"]["sha256"] != case["source_sha256"]
            or case.get("outcome") != "verified"
            or not case["fixture_ids"]
            or len(case["fixture_ids"]) != len(set(case["fixture_ids"]))
            or set(case["fixture_ids"]) != {f"apif:{r['fixture']['id']}" for r in rows}
        ):
            raise ValueError("boundary provenance or result missing")
        tuples = [
            (
                str(r["teams"]["home"]["id"]),
                str(r["teams"]["away"]["id"]),
                _clock(r["fixture"]["date"]),
            )
            for r in rows
        ]
        category = case["category"]
        valid = False
        if category == "partial_pages":
            valid = doc["paging"]["total"] > 1
        elif category == "unbound":
            valid = any(
                not _bound(facts, league, team, when.isoformat())
                for h, a, when in tuples
                for team in (h, a)
            )
        elif category == "reversed":
            valid = any((a, h) in pairs for h, a, _ in tuples)
        elif category in {"same_team_same_time", "cross_day"}:
            valid = any(
                (
                    ({h, a} & {h2, a2} and abs(t - t2) <= timedelta(hours=3))
                    if category == "same_team_same_time"
                    else (
                        (h, a) == (h2, a2)
                        and timedelta(hours=3) < abs(t - t2) <= timedelta(hours=36)
                    )
                )
                for i, (h, a, t) in enumerate(tuples)
                for h2, a2, t2 in tuples[i + 1 :]
            )
        elif category == "rejected":
            parser = ApiFootball(leagues={code: {"league": league}})
            try:
                parser.parse(raw)
                valid = bool(parser.rejected)
            finally:
                parser.session.close()
        if not valid:
            raise ValueError(f"boundary sample does not exercise {category}")
        _boundary_handling(facts, code, case, root, doc)


def _risk_review(section, root, risk_ids):
    rows = _document(root, section)
    if not isinstance(rows, list) or {r["fixture_id"] for r in rows} != set(risk_ids):
        raise ValueError("risk inventory differs from replay")
    for row in rows:
        if not row.get("reason") or row.get("disposition") not in {
            "quarantined",
            "different_match",
            "same_match",
        }:
            raise ValueError("unexplained risk")
        _proof(root, row["review_evidence"])
    return dict(reviewed=len(rows))


def _persistence(
    section,
    root,
    code,
    expected_manifest_sha=None,
    *,
    cutoff=None,
    league=None,
    replay_context=None,
):
    if (
        expected_manifest_sha is not None
        and section["before_manifest"]["sha256"] != expected_manifest_sha
    ):
        raise ValueError("persistence proof belongs to a different frozen archive")
    keys = ("before_manifest", "after_manifest", "restored_manifest")
    paths = [(root / section[key]["path"]).resolve() for key in keys]
    if (
        len({p.parent for p in paths}) != 3
        or len({(p.stat().st_dev, p.stat().st_ino) for p in paths}) != 3
    ):
        raise ValueError("persistence requires three independent materialized exports")
    repeat = _document(root, section["repeat_report"])
    if (
        type(repeat.get("inserted")) is not int
        or repeat["inserted"] != 0
        or repeat.get("status") != "complete"
        or not repeat.get("processed_files")
        or not any(str(r["code"]) == code for r in repeat.get("scopes", []))
    ):
        raise ValueError("second run did not actually replay with zero insertions")
    if replay_context is not None:
        facts, current_section = replay_context
        first = _document(root, current_section["report"])
        if (
            repeat.get("run_id") == first.get("run_id")
            or section["repeat_report"]["sha256"] == current_section["report"]["sha256"]
            or section["before_manifest"]["sha256"] != current_section["after_manifest"]["sha256"]
        ):
            raise ValueError("repeat must follow the first replay with a distinct invocation")
        _current_replay(
            facts,
            code,
            dict(
                current_section,
                report=section["repeat_report"],
                replay_options=section.get("replay_options"),
                before_manifest=section["before_manifest"],
                after_manifest=section["after_manifest"],
            ),
            root,
        )
    states = [_export(root, section[key]) for key in keys]
    inventories = [
        {
            p: digest
            for p, digest in state["manifest"]["files"].items()
            if p.startswith("state/")
            and (p.endswith((".jsonl", ".csv")) or "quarantine" in p or "cross_day" in p)
        }
        for state in states
    ]
    if any(inventory != inventories[0] for inventory in inventories[1:]):
        raise ValueError("export/restored state inventories differ")
    snapshots = [
        _document(root, section[key]) for key in ("before_repeat", "after_repeat", "restored")
    ]
    for state, snapshot in zip(states, snapshots, strict=True):
        actual = _snapshot(state, cutoff, league)
        if set(snapshot) != {*actual, "immutable_sha256"} or any(
            snapshot[key] != value for key, value in actual.items()
        ):
            raise ValueError("snapshot differs from actual exported inventory")
        if not snapshot["immutable_sha256"]:
            raise ValueError("immutable artifact inventory missing")
        from zucai.forecast_archive import verify_forecasts
        from zucai.parlay import paper as parlay_paper
        from zucai.scoreboard.command import read_state
        from zucai.scoreboard.sfc import read_settled

        _, _, summary, _ = read_state(state["folder"])
        read_settled(state["folder"], summary)
        parlay_paper.read_ledger(state["folder"], summary)
        forecasts = verify_forecasts(state["folder"])
        expected_immutable = dict(forecasts["files"]) if forecasts else {}
        for candidate in [
            state["folder"] / "ledger.jsonl",
            *(state["folder"] / "data/runs").glob("**/*"),
            *(state["folder"] / "scoreboard").glob("**/*"),
            *(state["folder"] / "sfc").glob("**/*"),
        ]:
            if candidate.is_file():
                if candidate.is_symlink() or any(p.is_symlink() for p in candidate.parents):
                    raise ValueError("immutable inventory contains symlink")
                expected_immutable[candidate.relative_to(state["folder"]).as_posix()] = sha256(
                    candidate.read_bytes()
                ).hexdigest()
        if snapshot["immutable_sha256"] != expected_immutable:
            raise ValueError("immutable snapshot omits or substitutes actual frozen artifacts")
        for path, digest in snapshot["immutable_sha256"].items():
            if not (
                path == "ledger.jsonl"
                or path.startswith(("data/runs/", "forecasts/", "models/", "scoreboard/", "sfc/"))
            ):
                raise ValueError("immutable inventory contains mutable or unknown artifact")
            _proof(state["folder"], dict(path=path, sha256=digest))
    if snapshots[0] != snapshots[1] or snapshots[0] != snapshots[2]:
        raise ValueError("coverage/relations/quarantine/training/immutable restoration differs")
    for state in states[1:]:
        if (
            state["tables"] != states[0]["tables"]
            or state["manifest"]["sources"] != states[0]["manifest"]["sources"]
        ):
            raise ValueError("export/restored core state differs")
    return dict(
        second_inserted=0,
        restoration_equal=True,
        history_selection=dict(
            cutoff_utc=cutoff,
            league=league,
            sources=sorted(
                {
                    r["source"]
                    for r in states[0]["matches"].values()
                    if league is None or r["league"] == league
                }
            ),
            selected_ids=snapshots[0]["training_selection"],
            method="read_history/select_training_history; no model fit",
        ),
    )


def _unbound_explanations(teams, section, root, facts):
    rows = _document(root, section)
    expected = {r["official_team_id"] for r in teams["unbound"]}
    ids = [r["official_team_id"] for r in rows]
    if len(ids) != len(set(ids)) or not set(ids) <= expected:
        raise ValueError("unbound explanations contain duplicate or unknown official IDs")
    for row in rows:
        if (
            not isinstance(row.get("reason"), str)
            or len(row["reason"].strip()) < 8
            or row.get("reason_code") != "no_unique_approved_scoped_binding"
            or not _proof(root, row["evidence"]).strip()
        ):
            raise ValueError("unbound explanation requires nonempty reason and evidence")
        known = {
            p["sha256"]
            for fixture in facts["official"]
            for side in ("home", "away")
            if fixture[f"{side}_source_id"] == row["official_team_id"]
            for p in fixture.get("provenance", [])
            if p.get("sha256")
        }
        if row["evidence"]["sha256"] not in known:
            raise ValueError(
                "unbound evidence is not an authenticated original of this official team"
            )
    return dict(
        explained_ids=sorted(ids), pending_ids=sorted(expected - set(ids)), explanations=rows
    )


def assess_league(facts, code, cutoff, *, evidence=None, evidence_root=None):
    cutoff = _clock(cutoff)
    facts = dict(facts, gate_cutoff=cutoff.isoformat())
    code = str(code)
    league = facts["configs"][code]["league"]
    season = facts["configs"][code].get("current_season")
    if type(season) is not int or season < 1:
        return dict(
            provider_code=code,
            league=league,
            status="blocked",
            cutoff_utc=cutoff.isoformat(),
            official_window=facts["catalogue_window"],
            current_season=None,
            teams=dict(total=None, bound=None, binding_rate=None),
            matches=dict(
                total=None,
                linked=None,
                link_rate=None,
                denominator="not_evaluated_without_current_season",
            ),
            blockers=["current_season_not_configured"],
            missing_evidence=["reviewed_current_season_configuration"],
            evidence=dict(
                current_season=dict(
                    status="blocked",
                    reason="登记未配置有效 current_season；须先审定赛季，不按年份猜测。",
                )
            ),
            daily_changed=False,
            online_permission="reviewer_verification_required",
        )
    teams, matches, blockers = _facts_coverage(facts, code)
    end = cutoff.astimezone(BEIJING).date() - timedelta(days=1)
    start = end - timedelta(days=29)
    missing, details = [], {}
    current_official = _current_participants(
        facts,
        code,
        {
            r["match_id"]: r
            for r in facts["official"]
            if r["competition_id"] == code
            and r["match_id"].startswith("jc:")
            and start.isoformat() <= r["match_date"] <= end.isoformat()
        },
    )
    if current_official["unbound"]:
        blockers.append("current_official_participants_unbound")
    if not teams["bound"] or not matches["linked"]:
        blockers.append("zero_verified_coverage")
    from zucai.identity_gate_inventory import near_duplicates

    duplicate_pairs = near_duplicates(facts, code)
    if duplicate_pairs:
        blockers.append("unreviewed_cross_source_near_duplicate")
    if not teams["total"] or not matches["total"]:
        missing.append("official_window_empty")
    window = complete_month_window(cutoff)
    if (
        tuple(
            date.fromisoformat(facts["catalogue_window"][key])
            for key in ("start_beijing", "end_beijing")
        )
        != window
    ):
        missing.append("official_12_month_window_mismatch")
    season = facts["configs"][code].get("current_season")
    current = [r for r in facts["apif"] if r["competition_id"] == code and r["season"] == season]
    if any(r["competition_id"] == code and r["season"] > season for r in facts["apif"]):
        blockers.append("newer_season_originals_require_review")
    if not current:
        missing.append("current_season_originals")
    elif any(
        not _bound(facts, league, r[side], r.get("kickoff_utc"))
        for r in current
        for side in ("home", "away")
    ):
        blockers.append("current_roster_unbound")
    evidence = evidence or {}
    root = Path(evidence_root) if evidence_root is not None else None
    validators = {
        **(
            {
                "unbound_explanations": lambda section: _unbound_explanations(
                    teams, section, root, facts
                )
            }
            if teams["unbound"]
            else {}
        ),
        "current_season_replay": lambda section: _current_replay(facts, code, section, root),
        "recent_replay": lambda section: _verified_recent(facts, code, section, root, start, end),
        "risk_review": lambda section: _risk_review(
            section, root, details["recent_replay"]["risk_ids"]
        ),
        "persistence": lambda section: _persistence(
            section,
            root,
            code,
            facts["manifest"]["sha256"],
            cutoff=cutoff.isoformat(),
            league=league,
            replay_context=(facts, evidence["current_season_replay"]),
        ),
    }
    for key, validate in validators.items():
        try:
            if (
                root is None
                or evidence.get("provider_code") != code
                or evidence.get("archive_sha256") != facts["manifest"]["sha256"]
                or evidence.get("cutoff_utc") is None
                or _clock(evidence["cutoff_utc"]) != cutoff
            ):
                raise ValueError("missing evidence root or frozen archive/cutoff binding")
            details[key] = validate(evidence[key])
            if key == "recent_replay" and details[key]["risk_ids"]:
                blockers.append("recent_replay_unresolved_risks")
            if key == "recent_replay":
                current_official = _current_participants(
                    facts, code, details[key]["official_window"]["matches"]
                )
                if current_official["unbound"]:
                    blockers.append("current_official_participants_unbound")
            if key == "unbound_explanations" and details[key]["pending_ids"]:
                blockers.append("official_teams_unbound")
            if key == "current_season_replay" and details[key]["explained_unresolved"]:
                blockers.append("current_season_held_unresolved")
        except (KeyError, ValueError, TypeError, OSError, AttributeError) as exc:
            missing.append(key)
            details[key] = dict(status="insufficient_evidence", reason=str(exc))
    if teams["unbound"] and "unbound_explanations" in missing:
        blockers.append("official_teams_unbound")
    return dict(
        provider_code=code,
        league=league,
        status="insufficient_evidence"
        if missing
        else "blocked"
        if blockers
        else "ready_for_review",
        cutoff_utc=cutoff.isoformat(),
        official_window=facts["catalogue_window"],
        recent_window=dict(start_beijing=start.isoformat(), end_beijing=end.isoformat()),
        teams=teams,
        matches=matches,
        unreviewed_near_duplicates=duplicate_pairs,
        current_season=season,
        current_official_participants=current_official,
        missing_evidence=missing,
        blockers=sorted(set(blockers)),
        evidence=details,
        daily_changed=False,
        online_permission="reviewer_verification_required",
    )


def build_register(
    archive, cutoff, *, codes=None, evidence=None, evidence_root=None, fd_raw_dir=None
):
    from zucai.identity_binding_archive import _tables, load_archive, load_fd
    from zucai.jc_catalogue_kickoffs import _catalogue
    from zucai.league_registry import load_registry

    archive = Path(archive)
    registry = load_registry()
    configs = {
        code: entry["config"]
        for code, entry in registry["providers"]["apif"].items()
        if entry["status"] == "verified" and entry.get("mode") == "import"
    }
    selected = sorted(configs if codes is None else codes)
    if not selected or not set(selected) <= configs.keys():
        raise ValueError("gate requires verified import provider IDs")
    loaded = load_archive(archive, leagues=selected, fd_raw_dir=fd_raw_dir)
    tables, manifest = _tables(archive)
    _, official_leagues, catalogue = _catalogue(archive)
    validate_approvals(loaded["formal"], require_metadata=True)
    from zucai.identity_gate_inventory import season_windows

    facts = dict(
        loaded,
        season_windows=season_windows(archive, cutoff),
        configs=configs,
        archive_root=str(archive.resolve()),
        matches=tables["matches"],
        results=tables["results"],
        result_links=existing_links(archive, tables),
        fd_originals=load_fd(
            fd_raw_dir,
            loaded["teams"],
            {code: configs[code] for code in selected},
            league_codes={configs[code]["league"]: code for code in selected},
        )[0]
        if fd_raw_dir
        else [],
        catalogue_window=catalogue["window"],
        official_leagues={
            code: [r for r in official_leagues if r["league_abbr"] == config["league"]]
            for code, config in configs.items()
        },
        manifest=manifest,
    )
    return dict(
        version=1,
        archive=manifest,
        auxiliary_inputs={
            "fd_originals": dict(
                root=str(Path(fd_raw_dir).resolve()), archive_manifest_member=False
            )
        }
        if fd_raw_dir
        else {},
        daily_changed=False,
        leagues=[
            assess_league(
                facts,
                code,
                cutoff,
                evidence=(evidence or {}).get(code),
                evidence_root=evidence_root,
            )
            for code in selected
        ],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--cutoff", required=True, help="带时区验收截止；30天止于此前完整北京日")
    parser.add_argument("--codes", help="逗号分隔的核准APIF import ID；默认全部")
    parser.add_argument(
        "--fd-raw-dir", type=Path, help="独立保存且有sidecar的FD原件目录；不归属冻结清单"
    )
    parser.add_argument("--evidence", type=Path, help="按赛事ID组织的有SHA原件/重放/恢复证据JSON")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists() or args.out.resolve().is_relative_to(args.archive.resolve()):
        parser.error("output must be new and outside the read-only archive")
    report = build_register(
        args.archive,
        args.cutoff,
        codes=args.codes.split(",") if args.codes else None,
        evidence=json.loads(args.evidence.read_bytes()) if args.evidence else None,
        evidence_root=args.evidence.parent if args.evidence else None,
        fd_raw_dir=args.fd_raw_dir,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
