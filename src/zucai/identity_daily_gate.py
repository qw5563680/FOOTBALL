"""Read-only, per-competition T68 opening register. Never changes registry policy."""

import argparse
import csv
import io
import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from zucai.cloud_jc_catalogue import complete_month_window
from zucai.sources.apif_aliases import binding_scope_allows, validate_approvals

BEIJING = ZoneInfo("Asia/Shanghai")
RISKS = {"unbound", "same_team_same_time", "reversed", "rejected", "partial_pages", "cross_day"}


def _clock(value):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("cutoff/evidence time must include timezone")
    return instant.astimezone(UTC)


def _proof(root, reference):
    if not isinstance(reference, dict):
        raise ValueError("missing artifact reference")
    path = root / reference["path"]
    if (
        path.is_symlink()
        or not path.resolve().is_relative_to(root.resolve())
        or any(p.is_symlink() for p in path.parents)
    ):
        raise ValueError("artifact path escapes evidence root")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("artifact SHA mismatch")
    return raw


def _document(root, reference):
    return json.loads(_proof(root, reference))


def _bound(facts, league, provider_id, when):
    entry = facts["formal"].get(league, {}).get(str(provider_id), {})
    if entry.get("status") not in {"approved", "auto_verified"}:
        return None
    if not binding_scope_allows(entry, league, kickoff_utc=when):
        return None
    return entry.get("canonical")


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
    bound, unbound = [], []
    for team_id, appearances in sorted(by_id.items()):
        names = {facts["teams"].get(row[side], {}).get("name") for row, side in appearances}
        name = next(iter(names)) if len(names) == 1 else None
        approved = name and all(
            any(
                _bound(facts, league, pid, row.get("kickoff_utc")) == name
                for pid in facts["formal"].get(league, {})
            )
            for row, _ in appearances
        )
        if approved:
            bound.append(team_id)
        else:
            unbound.append(
                dict(
                    official_team_id=team_id,
                    canonical=name,
                    reason="no_unique_approved_scoped_binding",
                )
            )
    matches = {r["match_id"]: r for r in facts["matches"]}
    api = defaultdict(list)
    for row in facts["apif"]:
        api[row["match_id"]].append(row)
    owners = Counter(r.get("counterpart_id") for r in facts["matches"] if r.get("counterpart_id"))
    linked, unresolved, blockers = [], [], []
    for row in official:
        stored = matches.get(row["match_id"], {})
        counterpart = stored.get("counterpart_id")
        reason = "not_linked"
        if counterpart and owners[counterpart] > 1:
            reason = "non_unique_relation"
            blockers.append(reason)
        elif counterpart:
            candidates = api.get(counterpart, [])
            if candidates and all(
                candidate["competition_id"] == code
                and candidate.get("complete")
                and row.get("kickoff_utc")
                and candidate.get("kickoff_utc")
                and abs(_clock(candidate["kickoff_utc"]) - _clock(row["kickoff_utc"]))
                <= timedelta(hours=3)
                and all(
                    _bound(facts, league, candidate[side], candidate["kickoff_utc"])
                    == facts["teams"].get(row[side], {}).get("name")
                    and facts["teams"].get(row[side], {}).get("name") is not None
                    for side in ("home", "away")
                )
                for candidate in candidates
            ):
                linked.append(dict(jc_id=row["match_id"], apif_id=counterpart))
                continue
            reason = "relation_identity_or_original_unverified"
        unresolved.append(dict(jc_id=row["match_id"], reason=reason))
    return (
        dict(
            total=len(by_id),
            bound=len(bound),
            binding_rate=len(bound) / len(by_id) if by_id else None,
            bound_ids=bound,
            unbound=unbound,
        ),
        dict(
            total=len(official),
            linked=len(linked),
            link_rate=len(linked) / len(official) if official else None,
            relations=linked,
            unresolved=unresolved,
        ),
        blockers,
    )


def _current_replay(facts, code, section, root):
    """Recheck a real collector report and the prior held audit against current state."""
    season = facts["configs"][code].get("current_season")
    current = [r for r in facts["apif"] if r["competition_id"] == code and r["season"] == season]
    if not current or not all(r["complete"] for r in current):
        raise ValueError("complete current-season originals missing")
    report = _document(root, section["report"])
    scopes = [r for r in report["scopes"] if str(r["code"]) == code and r["season"] == season]
    known_sha = {p["sha256"] for r in current for p in r["provenance"]}
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
    audit = list(csv.DictReader(io.StringIO(_proof(root, section["held_before"]).decode())))
    if not audit and not _proof(root, section["held_before"]).decode().startswith("raw_sha256,"):
        raise ValueError("prior held inventory missing")
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
        scopes = {
            r["season"]
            for r in facts["apif"]
            if r["match_id"] == mid
            and r["competition_id"] == code
            and any(p["sha256"] == row.get("raw_sha256") for p in r["provenance"])
        }
        if len(scopes) == 1 and season not in scopes:
            if mid in current_ids:
                raise ValueError("prior held scope contradicts current-season original")
            if row.get("season") and row["season"] != str(next(iter(scopes))):
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
        season=season,
        distinct_fixtures=len(current_ids),
        held_before=len(held),
        held_before_ids=sorted(held),
        resolved_ids=sorted(relevant & stored),
        excluded_other_season_ids=sorted(excluded),
        explained_unresolved=explained,
        raw_sha256=sorted(known_sha),
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
            doc = parser._document(raw, {key: values[0] for key, values in params.items()})
            parser.parse(raw)
            for rejected in parser.rejected:
                if rejected.get("provider_match_id") is None:
                    raise ValueError("unattributable rejected competitor")
                rejected_ids.add(f"apif:{rejected['provider_match_id']}")
            if doc["paging"] != {"current": 1, "total": 1}:
                raise ValueError("partial daily pages")
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
    return dict(
        mode=mode,
        distinct_fixtures=len(fixtures),
        expected_distinct_fixtures=len(expected_ids) if expected_ids is not None else len(fixtures),
        utc_dates=sorted(days),
        raw_sha256=sorted(hashes),
        risk_ids=sorted(set(replay["risk_ids"])),
        official_match_ids=official["official_match_ids"],
        official_window=official,
    )


def _boundary_cases(facts, code, cases, root):
    """Require actual risky rows, not six labels attached to one easy fixture."""
    from zucai.identity_binding_archive import _proof as raw_proof
    from zucai.sources.api_football import ApiFootball

    if {case["category"] for case in cases} != RISKS:
        raise ValueError("equivalent replay lacks risk distribution")
    pairs = {(r["home"], r["away"]) for r in facts["apif"] if r["competition_id"] == code}
    league = facts["configs"][code]["league"]
    for case in cases:
        _proof(root, case["original"])
        raw, _, _ = raw_proof(root / case["original"]["path"], root)
        doc = json.loads(raw)
        rows = doc["response"]
        if (
            case["original"]["sha256"] != case["source_sha256"]
            or not case.get("outcome")
            or not set(case["fixture_ids"]) <= {f"apif:{r['fixture']['id']}" for r in rows}
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


def _persistence(section, root, code, expected_manifest_sha=None):
    if (
        expected_manifest_sha is not None
        and section["before_manifest"]["sha256"] != expected_manifest_sha
    ):
        raise ValueError("persistence proof belongs to a different frozen archive")
    repeat = _document(root, section["repeat_report"])
    if (
        type(repeat.get("inserted")) is not int
        or repeat["inserted"] != 0
        or repeat.get("status") != "complete"
        or not repeat.get("processed_files")
        or not any(str(r["code"]) == code for r in repeat.get("scopes", []))
    ):
        raise ValueError("second run did not actually replay with zero insertions")
    snapshots = [
        _document(root, section[key]) for key in ("before_repeat", "after_repeat", "restored")
    ]
    required = {"coverage", "relations", "quarantine", "training_selection", "immutable_sha256"}
    if (
        any(
            not isinstance(s, dict) or set(s) != required or any(s[k] is None for k in required)
            for s in snapshots
        )
        or snapshots[0] != snapshots[1]
        or snapshots[0] != snapshots[2]
    ):
        raise ValueError("coverage/relations/quarantine/training/immutable restoration differs")
    # Inspect the three actual exports, not only the separately supplied snapshots.
    manifests = [
        _document(root, section[key])
        for key in ("before_manifest", "after_manifest", "restored_manifest")
    ]
    for key, manifest in zip(
        ("before_manifest", "after_manifest", "restored_manifest"), manifests, strict=True
    ):
        folder = (root / section[key]["path"]).parent
        for path, digest in manifest["files"].items():
            _proof(folder, dict(path=path, sha256=digest))
        if not snapshots[0]["immutable_sha256"]:
            raise ValueError("immutable artifact inventory missing")
        for path, digest in snapshots[0]["immutable_sha256"].items():
            _proof(folder, dict(path=path, sha256=digest))
    for key in ("counts", "columns", "sources"):
        if key not in manifests[0]:
            raise ValueError("export manifest lacks core inventory")
        if any(m.get(key) != manifests[0].get(key) for m in manifests[1:]):
            raise ValueError("export/restored core state differs")
    state_files = {
        p
        for m in manifests
        for p in m["files"]
        if p.startswith("state/")
        and (p.endswith(".jsonl") or "quarantine" in p or "cross_day" in p)
    }
    if any(
        any(m["files"].get(p) != manifests[0]["files"].get(p) for m in manifests[1:])
        for p in state_files
    ):
        raise ValueError("export/restored relation evidence differs")
    return dict(second_inserted=0, restoration_equal=True)


def assess_league(facts, code, cutoff, *, evidence=None, evidence_root=None):
    cutoff = _clock(cutoff)
    code = str(code)
    league = facts["configs"][code]["league"]
    teams, matches, blockers = _facts_coverage(facts, code)
    end = cutoff.astimezone(BEIJING).date() - timedelta(days=1)
    start = end - timedelta(days=29)
    missing, details = [], {}
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
    if not current:
        missing.append("current_season_originals")
    elif any(
        not _bound(facts, league, r[side], r.get("kickoff_utc"))
        for r in current
        for side in ("home", "away")
    ):
        blockers.append("current_roster_unbound")
    if teams["unbound"]:
        blockers.append("official_teams_unbound")
    if matches["unresolved"]:
        blockers.append("unresolved_match_evidence")
    evidence = evidence or {}
    root = Path(evidence_root) if evidence_root is not None else None
    validators = {
        "current_season_replay": lambda section: _current_replay(facts, code, section, root),
        "recent_replay": lambda section: _recent_replay(facts, code, section, root, start, end),
        "risk_review": lambda section: _risk_review(
            section, root, details["recent_replay"]["risk_ids"]
        ),
        "persistence": lambda section: _persistence(
            section, root, code, facts["manifest"]["sha256"]
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
            if key == "current_season_replay" and details[key]["explained_unresolved"]:
                blockers.append("current_season_held_unresolved")
        except (KeyError, ValueError, TypeError, OSError, AttributeError) as exc:
            missing.append(key)
            details[key] = dict(status="insufficient_evidence", reason=str(exc))
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
        current_season=season,
        missing_evidence=missing,
        blockers=sorted(set(blockers)),
        evidence=details,
        daily_changed=False,
        online_permission="reviewer_verification_required",
    )


def build_register(archive, cutoff, *, codes=None, evidence=None, evidence_root=None):
    from zucai.identity_binding_archive import _tables, load_archive
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
    loaded = load_archive(archive, leagues=selected)
    tables, manifest = _tables(archive)
    _, official_leagues, catalogue = _catalogue(archive)
    validate_approvals(loaded["formal"], require_metadata=True)
    facts = dict(
        loaded,
        configs=configs,
        matches=tables["matches"],
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
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
