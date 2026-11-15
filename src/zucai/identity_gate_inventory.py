"""Independently derived gate inventories; no user-declared counts authorize daily."""

from datetime import UTC, datetime, timedelta


def near_duplicates(facts, code):
    league = facts["configs"][code]["league"]
    rows = [r for r in facts["matches"] if r["league"] == league and r.get("kickoff_utc")]

    def clock(row):
        return datetime.fromisoformat(row["kickoff_utc"].replace("Z", "+00:00")).replace(tzinfo=UTC)

    rows.sort(key=clock)
    pairs = []
    for i, row in enumerate(rows):
        for other in rows[i + 1 :]:
            difference = clock(other) - clock(row)
            if difference > timedelta(hours=36):
                break
            if (
                row["source"] != other["source"]
                and {row["home_team_id"], row["away_team_id"]}
                == {other["home_team_id"], other["away_team_id"]}
                and row.get("counterpart_id") != other["match_id"]
                and other.get("counterpart_id") != row["match_id"]
                and not any(
                    {link["provider_match_id"], link["counterpart_id"]}
                    == {row["match_id"], other["match_id"]}
                    for link in facts.get("result_links", [])
                )
            ):
                pairs.append(
                    dict(
                        match_ids=[row["match_id"], other["match_id"]],
                        difference_seconds=difference.total_seconds(),
                        reason="unreviewed_cross_source_near_duplicate",
                    )
                )
    return pairs


def season_windows(archive, cutoff):
    """Season dates come from authenticated /leagues originals, never calendar guesses."""
    import json
    from collections import defaultdict

    from zucai.identity_gate_evidence import document, proof

    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    windows = defaultdict(list)
    for path, digest in manifest["files"].items():
        if not path.startswith("state/catalogue/") or not path.endswith("/apif-raw.json"):
            continue
        doc = json.loads(proof(archive, dict(path=path, sha256=digest)))
        meta_path = path.replace("/apif-raw.json", "/apif-meta.json")
        meta = document(archive, dict(path=meta_path, sha256=manifest["files"][meta_path]))
        if (
            meta["url"] != "https://v3.football.api-sports.io/leagues"
            or meta["status_code"] != 200
            or meta["sha256"] != digest
            or doc["get"] != "leagues"
            or doc["parameters"] not in ({}, [])
            or doc["errors"]
            or doc["paging"] != {"current": 1, "total": 1}
            or doc["results"] != len(doc["response"])
            or datetime.fromisoformat(meta["fetched_at_utc"].replace("Z", "+00:00"))
            > datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        ):
            raise ValueError("season catalogue original/source/cutoff invalid")
        for index, row in enumerate(doc["response"]):
            code = str(row["league"]["id"])
            for item in row["seasons"]:
                windows[code].append(
                    dict(
                        year=item["year"],
                        start=item["start"],
                        end=item["end"],
                        current=item["current"],
                        provenance=dict(
                            path=path, sha256=digest, pointer=f"/response/{index}/seasons"
                        ),
                    )
                )
    return dict(windows)


def current_scope(facts, code):
    from zoneinfo import ZoneInfo

    season = facts["configs"][code].get("current_season")
    windows = facts.get("season_windows", {}).get(code, [])
    bounds = {(w["start"], w["end"]) for w in windows if w["year"] == season}
    verified = len(bounds) == 1
    low, high = next(iter(bounds)) if verified else ("", "9999")
    stored = [
        r
        for r in facts["matches"]
        if r["source"] == "jc" and r["league"] == facts["configs"][code]["league"]
    ]
    selected = []
    for row in stored:
        day = (
            datetime.fromisoformat(row["kickoff_utc"].replace("Z", "+00:00"))
            .replace(tzinfo=UTC)
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .date()
            .isoformat()
        )
        if low <= day <= high:
            selected.append(row)
    return dict(
        verified=verified,
        season=season,
        start_beijing=low if verified else None,
        end_beijing=high if verified else None,
        match_ids=sorted(r["match_id"] for r in selected),
        evidence=[w for w in windows if w["year"] == season],
        newer_current=any(w["current"] and w["year"] != season for w in windows),
    )


def prior_pending(facts, code):
    """Every APIF held/suspected row in the frozen archive stays in the denominator."""
    import csv
    import io
    import json
    from pathlib import Path

    from zucai.identity_gate_evidence import proof

    if not facts.get("archive_root"):
        return []
    root = Path(facts["archive_root"])
    manifest = json.loads((root / "feed_manifest.json").read_bytes())
    rows = {}
    league = facts["configs"][code]["league"]
    season = facts["configs"][code]["current_season"]
    for path in ["state/audit/apif_held_unbound.csv", "state/audit/apif_suspected_duplicates.csv"]:
        if path not in manifest["files"]:
            continue
        reader = csv.DictReader(
            io.StringIO(proof(root, dict(path=path, sha256=manifest["files"][path])).decode())
        )
        if not {"provider_match_id", "league", "raw_sha256"} <= set(reader.fieldnames or []):
            raise ValueError("pending APIF audit schema invalid")
        for index, row in enumerate(reader, 2):
            if row["league"] != league:
                continue
            if (
                row.get("competition_code") not in (None, "", code)
                or not row["provider_match_id"].isdecimal()
            ):
                raise ValueError("pending APIF audit identity/scope invalid")
            mid = "apif:" + row["provider_match_id"]
            seasons = {
                r["season"]
                for r in facts["apif"]
                if r["match_id"] == mid
                and r["competition_id"] == code
                and any(p["sha256"] == row["raw_sha256"] for p in r["provenance"])
            }
            if len(seasons) == 1 and season not in seasons:
                continue
            rows.setdefault(mid, dict(fixture_id=mid, provenance=[]))["provenance"].append(
                dict(path=path, sha256=manifest["files"][path], pointer=f"/line/{index}")
            )
    return [rows[mid] for mid in sorted(rows)]
