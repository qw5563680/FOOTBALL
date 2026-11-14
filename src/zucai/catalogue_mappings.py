"""Cross-check explicit manual proposals against the official dictionary, never fuzzy-match."""

from collections import defaultdict

from zucai.league_registry import REGISTRY_PATH, load_registry


def crosscheck_official(observed, dictionary, *, observed_ranges=None, registry_path=REGISTRY_PATH):
    registry = load_registry(registry_path)
    official = {entry["league_id"]: entry for entry in dictionary}
    if len(official) != len(dictionary):
        raise ValueError("Duplicate official catalogue league id")
    ranges = {entry["league_id"]: entry for entry in observed_ranges or []}
    counts = defaultdict(int)
    for (league_id, full, abbr), count in observed.items():
        identity = official.get(league_id)
        if identity is None or (identity["league_name"], identity["league_abbr"]) != (full, abbr):
            raise ValueError("Observed league contradicts official catalogue dictionary")
        counts[league_id] += count
    rows, decisions, referenced = [], [], set()
    for row in registry["competitions"]:
        proposal = row.get("official_catalogue")
        observations, discrepancies = [], []
        for entry in proposal["entries"] if proposal else []:
            code = entry["league_id"]
            referenced.add(code)
            actual = official.get(code)
            if actual is None:
                discrepancies.append({"league_id": code, "reason": "official_entry_not_present"})
                continue
            if (actual["league_name"], actual["league_abbr"]) != (
                entry["league_name"],
                entry["league_abbr"],
            ):
                discrepancies.append(
                    {
                        "league_id": code,
                        "reason": "official_identity_changed",
                        "proposed_name": entry["league_name"],
                        "proposed_abbr": entry["league_abbr"],
                        "actual_name": actual["league_name"],
                        "actual_abbr": actual["league_abbr"],
                    }
                )
                continue
            date_range = ranges.get(code, {})
            observations.append(
                {
                    "league_id": code,
                    "league_name": actual["league_name"],
                    "league_abbr": actual["league_abbr"],
                    "match_count": counts[code],
                    "first_match_day": date_range.get("first_match_day"),
                    "last_match_day": date_range.get("last_match_day"),
                }
            )
        if not proposal:
            status = "proposal_missing"
        elif discrepancies:
            status = "catalogue_changed_review_required"
        elif proposal["owner_decision_required"]:
            status = "owner_decision"
        elif not proposal["entries"]:
            status = "not_found_in_official_dictionary"
        elif any(entry["match_count"] for entry in observations):
            status = "official_identity_proposed_observed"
        else:
            status = "official_identity_proposed_not_observed"
        item = {
            "master_row": int(row["master_row_id"].split("-")[1]),
            "master_row_id": row["master_row_id"],
            "master_name": row["master_name"],
            "project_league": row["project_name"],
            "source_names": row["source_names"],
            "status": status,
            "review_status": proposal["review_status"] if proposal else "missing",
            "proposed_status": proposal["proposed_status"] if proposal else None,
            "reason": proposal["reason"] if proposal else "No explicit manual proposal",
            "master_context": proposal["master_context"] if proposal else None,
            "observations": observations,
            "discrepancies": discrepancies,
            "grants_import_permission": False,
        }
        rows.append(item)
        if status in {"owner_decision", "catalogue_changed_review_required", "proposal_missing"}:
            decisions.append(
                {
                    "master_row_id": row["master_row_id"],
                    "master_name": row["master_name"],
                    "reason": item["reason"],
                    "status": status,
                    "league_ids": [entry["league_id"] for entry in proposal["entries"]]
                    if proposal
                    else [],
                    "discrepancies": discrepancies,
                }
            )
    for code, count in sorted(counts.items()):
        if code not in referenced:
            decisions.append(
                {
                    "status": "observed_official_id_unmapped",
                    "reason": "Observed official competition has no explicit master-table proposal",
                    "league_id": code,
                    "league_name": official[code]["league_name"],
                    "league_abbr": official[code]["league_abbr"],
                    "match_count": count,
                }
            )
    return rows, decisions
