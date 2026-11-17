"""Official numeric identity graph and proposal register, independent of labels."""

from collections import defaultdict
from copy import deepcopy
from zoneinfo import ZoneInfo

from zucai.identity_binding import _clock, _utc


def associate_official(rows, tables, categories, manifest, observed_utc, redirects=None):
    """Associate exact JC match IDs with the same verified current table snapshot.

    Names are display values only. Archived clock snapshots cannot authorize a
    project team. Every contradictory current observation blocks both identities.
    ``redirects`` maps a stored team id that an owner-approved same-club receipt
    absorbed to the id that kept its matches; an archived snapshot naming the
    absorbed id then agrees with the current row naming the kept one.
    """
    redirects = redirects or {}
    matches = {r["match_id"]: r for r in tables["matches"]}
    results = {r["match_id"]: r for r in tables["results"]}
    projects = {str(r["id"]): r for r in tables["teams"]}
    teams, issues = {}, []
    observations, links, blocked = defaultdict(list), defaultdict(set), set()
    proof_links = defaultdict(list)
    for row in rows:
        for side in ("home", "away"):
            key = row[side]
            observations[key].append((row, side))
        match = matches.get(row["match_id"])
        if not match:
            continue
        result = results.get(row["match_id"])
        try:
            when = _utc(match["kickoff_utc"])
            checked = _utc(match["result_checked_utc"])
            valid = (
                match["source"] == match["result_provider"] == "jc"
                and match["league"] == row["league"]
                and match["home_team_id"] != match["away_team_id"]
                and when.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
                == row["match_date"]
                and checked <= _utc(observed_utc)
                and (_clock(row) is None or _clock(row) == when)
                and not row.get("clock_conflict")
                and all(
                    all(
                        proof["match_snapshot"].get(field) == match.get(field)
                        for field in ("source", "league")
                    )
                    and all(
                        redirects.get(old, old) == match.get(field)
                        for field in ("home_team_id", "away_team_id")
                        for old in [proof["match_snapshot"].get(field)]
                    )
                    for proof in row["provenance"]
                    if proof.get("match_snapshot")
                )
                and all(str(match[f"{side}_team_id"]) in projects for side in ("home", "away"))
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            valid = False
        if (
            valid
            and match["status"] == "cancelled"
            and (row["status"] == "cancelled" or row.get("identity_only"))
            and result is None
            and row["score"] is None
        ):
            issues.append(
                dict(
                    match_id=row["match_id"],
                    reasons=["cancelled_fixture_not_identity_witness"],
                    match_snapshot=match,
                    result_snapshot=None,
                )
            )
            continue
        valid = valid and (
            (row.get("identity_only") and _clock(row) == when)
            or (
                match["status"] == row["status"] == "finished"
                and result is not None
                and row["score"] == [result["home_goals"], result["away_goals"]]
            )
        )
        if not valid:
            blocked.update(row[s] for s in ("home", "away"))
            issues.append(
                dict(
                    match_id=row["match_id"],
                    reasons=["current_snapshot_identity_conflict"],
                    match_snapshot=match,
                    result_snapshot=result,
                )
            )
            row["complete"] = False
            continue
        proof = dict(
            kind="same_official_match_id_oriented_current_snapshot",
            manifest=manifest,
            match_id=row["match_id"],
            matches_pointer=f"state/matches.jsonl#/line/{match['row_number']}",
            results_pointer=f"state/results.jsonl#/line/{result['row_number']}"
            if result and not row.get("identity_only")
            else None,
            identity_only=bool(row.get("identity_only")),
            official_provenance=deepcopy(row["provenance"]),
            official_ids={s: row[f"{s}_source_id"] for s in ("home", "away")},
            project_ids={s: str(match[f"{s}_team_id"]) for s in ("home", "away")},
            project_team_pointers={
                side: "state/teams.jsonl#/line/"
                + str(projects[str(match[f"{side}_team_id"])]["row_number"])
                for side in ("home", "away")
            },
        )
        if _clock(row) is None:
            row.update(kickoff_utc=when.isoformat(), time_precision="second")
            row["provenance"].append(
                dict(
                    proof,
                    basis="current_numeric_jc_match_clock",
                    kickoff_utc=when.isoformat(),
                    result_checked_utc=checked.isoformat(),
                )
            )
        for side in ("home", "away"):
            links[row[side]].add(str(match[f"{side}_team_id"]))
            proof_links[row[side]].append(proof)
    reverse = defaultdict(set)
    for key, ids in links.items():
        for tid in ids:
            reverse[tid].add(key)
    for key, entries in sorted(observations.items()):
        first, side = entries[0]
        category_entries = {
            r["competition_id"]: categories.get(r["competition_id"])
            for r, _ in entries
            if r.get("competition_id") is not None
        }
        category_values = {e["category"] for e in category_entries.values() if e}
        targets = links[key]
        conflict = key in blocked or len(targets) > 1 or any(len(reverse[t]) > 1 for t in targets)
        tid = next(iter(targets)) if len(targets) == 1 and not conflict else None
        teams[key] = dict(
            name=first[f"{side}_name"],
            official_names=sorted({r[f"{s}_name"] for r, s in entries}),
            official_name_evidence=[
                dict(
                    label=r[f"{s}_name"], match_id=r["match_id"], side=s, provenance=r["provenance"]
                )
                for r, s in entries
            ],
            official_team_id=first[f"{side}_source_id"],
            project_team_id=tid,
            project_name=projects[tid]["name"] if tid else None,
            identity_status="conflict"
            if conflict
            else "existing_numbered"
            if tid
            else "creation_proposed",
            ambiguous=conflict,
            project_identity_evidence=proof_links[key],
            conflicting_project_ids=sorted(targets) if conflict else [],
            category=next(iter(category_values)) if len(category_values) == 1 else None,
            category_status="approved"
            if category_entries
            and all(e and e["status"] == "approved" for e in category_entries.values())
            else "proposed",
            category_evidence=list(category_entries.values()),
            category_candidates=sorted(category_values),
            provenance=[p for r, _ in entries for p in r["provenance"]],
        )
    return teams, issues


def official_register(inputs, result):
    """One row per competition/official ID; candidates never authorize production."""
    groups = defaultdict(list)
    for fixture in inputs["official"]:
        if not fixture["match_id"].startswith("jc:"):
            continue
        for side in ("home", "away"):
            groups[fixture["competition_id"], fixture[f"{side}_source_id"]].append((fixture, side))
    candidates = [entry for entries in result["apif"].values() for entry in entries.values()]
    candidates += result.get("existing_candidate_reviews", [])
    rows = []
    for (code, official_id), observations in sorted(groups.items()):
        fixture, side = observations[0]
        team = inputs["teams"][fixture[side]]
        matches = {r["match_id"] for r, _ in observations}
        found = [
            e
            for e in candidates
            if str(e.get("provider_competition_id")) == code
            and e.get("official_team_id") == official_id
        ]
        successful = [
            e
            for e in found
            if e.get("evidence_status") in {"strict_verified", "conditional_verified"}
        ]
        reasons = {
            reason
            for issue in result["unverified"]
            if issue.get("counterpart_id", issue.get("match_id")) in matches
            for reason in issue["reasons"]
        }
        reasons.update(e["evidence_status"] for e in found if e not in successful)
        if team["identity_status"] == "conflict":
            reasons.add("official_project_identity_conflict")
        if not successful and not reasons:
            reasons.add("no_verified_numbered_witness")
        gaps = sorted({gap for e in successful for gap in e.get("approval_gaps", [])})
        if team["category_status"] != "approved":
            gaps = sorted(set(gaps) | {"category_approval_pending"})
        if team["identity_status"] == "creation_proposed":
            gaps = sorted(set(gaps) | {"canonical_identity_creation_pending"})
        rows.append(
            dict(
                competition_id=code,
                league=fixture["league"],
                official_team_id=official_id,
                identity_key=fixture[side],
                official_names=team["official_names"],
                project_team_id=team["project_team_id"],
                project_identity_evidence=team["project_identity_evidence"],
                identity_status=team["identity_status"],
                team_category_exceptions=team.get("team_category_exceptions", []),
                identity_proposal=dict(
                    status="proposed",
                    action="create_canonical_identity",
                    official_team_id=official_id,
                    display_name=team["name"],
                )
                if team["identity_status"] == "creation_proposed"
                else None,
                category=team["category"],
                category_status=team["category_status"],
                category_evidence=team["category_evidence"],
                approval_gaps=gaps,
                candidate_ids=sorted({e["candidate_id"] for e in successful}),
                candidate_outcomes=[
                    dict(candidate_id=e["candidate_id"], evidence_status=e["evidence_status"])
                    for e in found
                ],
                reasons=[] if successful else sorted(reasons),
                official_match_ids=sorted(matches),
            )
        )
    from zucai.identity_review_report import enrich_register

    enrich_register(rows, inputs, result)
    scope = set(inputs["summary"]["new_route_leagues"])
    a_rows = [r for r in rows if r["league"] in scope]
    return dict(
        rows=rows,
        official_ids=len({r["official_team_id"] for r in rows}),
        competition_items=len(rows),
        a_official_ids=len({r["official_team_id"] for r in a_rows}),
        a_competition_items=len(a_rows),
        a_items_with_candidates=sum(bool(r["candidate_ids"]) for r in a_rows),
    )
