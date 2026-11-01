"""Offline, evidence-based FD identity and time-basis verification.

Names and scores never select fixture edges. The graph uses the full inclusive
three-hour window, stable oriented aliases, and uniqueness in both directions.
"""

import argparse
import csv
import hashlib
import io
import json
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from zucai.sources.football_data import local_kickoff, season_start

WINDOW_SECONDS = 3 * 3600


def _utc(value):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("Official kickoff must have a UTC offset")
    return instant.astimezone(UTC)


def load_rows(item, year):
    """Validate original bytes, then select combined-file season start year."""
    raw = Path(item["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise ValueError(f"SHA256 mismatch: {item['path']}")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    rows = []
    for number, row in enumerate(reader, start=2):
        if "Season" in row and season_start(row["Season"]) != year:
            continue
        try:
            home_goals = int(row.get("FTHG", row.get("HG", "")))
            away_goals = int(row.get("FTAG", row.get("AG", "")))
        except (ValueError, TypeError):
            continue  # Unplayed rows cannot corroborate historical identity.
        if min(home_goals, away_goals) < 0:
            continue
        rows.append(
            dict(
                row=number,
                date=row.get("Date", ""),
                time=row.get("Time", ""),
                home=row.get("HomeTeam", row.get("Home", "")),
                away=row.get("AwayTeam", row.get("Away", "")),
                hg=home_goals,
                ag=away_goals,
                season=row.get(
                    "Season", item.get("season", f"{year % 100:02}{(year + 1) % 100:02}")
                ),
            )
        )
    return rows


def _compatible(row, official, aliases):
    reverse = {canonical: raw for raw, canonical in aliases.items()}
    return all(
        (raw not in aliases or aliases[raw] == canonical)
        and (canonical not in reverse or reverse[canonical] == raw)
        for raw, canonical in [(row["home"], official["home"]), (row["away"], official["away"])]
    )


def infer_identity(rows, official, zone):
    """Infer one candidate zone's oriented aliases, without using score/name hints."""
    times, rejected = [], []
    for row in rows:
        try:
            if not row["home"] or not row["away"] or row["home"] == row["away"]:
                raise ValueError("Missing or identical oriented team names")
            times.append(local_kickoff(row["date"], row["time"], zone))
        except (ValueError, KeyError, OverflowError) as exc:
            times.append(None)
            rejected.append(dict(fd_row=row["row"], reason=str(exc)))
    official_times = [_utc(game["kickoff_utc"]) for game in official]
    base = [
        (i, k, (instant - kickoff).total_seconds() / 60)
        for i, instant in enumerate(times)
        if instant is not None
        for k, kickoff in enumerate(official_times)
        if abs((instant - kickoff).total_seconds()) <= WINDOW_SECONDS
    ]
    aliases, origin, conflicts = {}, {}, []
    blocked_rows, blocked_official = set(), set()
    iteration = 0
    while True:
        edges = [
            (i, k, delta)
            for i, k, delta in base
            if i not in blocked_rows
            and k not in blocked_official
            and _compatible(rows[i], official[k], aliases)
        ]
        forward = Counter(i for i, _, _ in edges)
        reverse = Counter(k for _, k, _ in edges)
        unique = [(i, k, d) for i, k, d in edges if forward[i] == reverse[k] == 1]
        proposals, canonical_proposals = defaultdict(set), defaultdict(set)
        for i, k, _ in unique:
            for raw, canonical in [
                (rows[i]["home"], official[k]["home"]),
                (rows[i]["away"], official[k]["away"]),
            ]:
                proposals[raw].add(canonical)
                canonical_proposals[canonical].add(raw)
        bad_raw = {raw for raw, names in proposals.items() if len(names) > 1}
        bad_canonical = {name for name, raw in canonical_proposals.items() if len(raw) > 1}
        accepted = []
        for i, k, delta in unique:
            pairs = [(rows[i]["home"], official[k]["home"]), (rows[i]["away"], official[k]["away"])]
            if any(raw in bad_raw or name in bad_canonical for raw, name in pairs):
                # Reject the whole pair, including its otherwise unambiguous opponent.
                blocked_rows.add(i)
                blocked_official.add(k)
                conflicts.append(
                    dict(
                        fd_row=rows[i]["row"],
                        jc_match_id=official[k]["match_id"],
                        iteration=iteration,
                        reason="conflicting oriented name mapping",
                    )
                )
            else:
                accepted.append((i, k, delta))
        new = {}
        for i, k, delta in accepted:
            row, game = rows[i], official[k]
            for raw, canonical in [(row["home"], game["home"]), (row["away"], game["away"])]:
                if raw not in aliases:
                    new[raw] = canonical
                    witness = dict(
                        fd_row=row["row"],
                        jc_match_id=game["match_id"],
                        difference_minutes=delta,
                        home_known_before=row["home"] in aliases,
                        away_known_before=row["away"] in aliases,
                    )
                    record = origin.setdefault(
                        raw,
                        dict(
                            iteration=iteration,
                            unique_fd_candidates=1,
                            unique_jc_candidates=1,
                            witnesses=[],
                        ),
                    )
                    record["witnesses"].append(witness)
        if not new:
            break
        aliases.update(new)
        iteration += 1
    comparisons, evidence = [], defaultdict(list)
    for i, k, delta in accepted:
        row, game = rows[i], official[k]
        if aliases.get(row["home"]) != game["home"] or aliases.get(row["away"]) != game["away"]:
            continue
        score_known = game.get("hg") is not None and game.get("ag") is not None
        item = dict(
            jc_match_id=game["match_id"],
            fd_row=row["row"],
            fd_home=row["home"],
            fd_away=row["away"],
            jc_home=game["home"],
            jc_away=game["away"],
            fd_local=times[i].astimezone(ZoneInfo(zone)).replace(tzinfo=None).isoformat(),
            fd_utc=times[i].isoformat(),
            jc_kickoff_utc=game["kickoff_utc"],
            difference_minutes=delta,
            fd_full=[row["hg"], row["ag"]],
            jc_full=[game.get("hg"), game.get("ag")],
            score_agrees=(row["hg"], row["ag"]) == (game["hg"], game["ag"])
            if score_known
            else None,
        )
        comparisons.append(item)
        for raw in (row["home"], row["away"]):
            evidence[raw].append(item)
    return dict(
        zone=zone,
        aliases=aliases,
        alias_origin=origin,
        evidence=dict(evidence),
        comparison_rows=comparisons,
        exact=sum(e["difference_minutes"] == 0 for e in comparisons),
        matched=len(comparisons),
        difference_histogram=dict(Counter(str(e["difference_minutes"]) for e in comparisons)),
        conflicts=conflicts,
        iterations=iteration,
        times=times,
        rejected_rows=rejected,
    )


def _public_zone(result):
    return {
        key: value
        for key, value in result.items()
        if key not in {"times", "aliases", "alias_origin", "evidence"}
    }


def verify_league(rows, official, config, *, raw_sha256="", verified_utc=None):
    """Select a uniquely supported UTC conversion; mismatching scores veto it."""
    zones = list(dict.fromkeys(config["candidate_zones"]))
    if not zones:
        raise ValueError("At least one candidate zone is required")
    results = [infer_identity(rows, official, zone) for zone in zones]
    results.sort(key=lambda result: (result["exact"], result["matched"]), reverse=True)
    best = results[0]
    equivalent = [r["zone"] for r in results if r["times"] == best["times"]]
    rivals = [r for r in results if r["zone"] not in equivalent]
    no_score_conflict = all(e["score_agrees"] is not False for e in best["comparison_rows"])
    verified = (
        best["exact"] >= 2
        and no_score_conflict
        and all(best["exact"] > rival["exact"] for rival in rivals)
    )
    comparisons = (
        [
            dict(e, raw_sha256=raw_sha256, div_code=config["div_code"])
            for e in best["comparison_rows"]
        ]
        if verified
        else []
    )
    aliases = {}
    if verified:
        for raw, canonical in best["aliases"].items():
            evidence = [e for e in comparisons if raw in (e["fd_home"], e["fd_away"])]
            if evidence:
                aliases[raw] = dict(
                    canonical=canonical,
                    status="auto_verified",
                    inference=best["alias_origin"][raw],
                    evidence=evidence,
                )
    return dict(
        div_code=config["div_code"],
        league=config["league"],
        time_basis=best["zone"] if verified else None,
        status="verified" if verified else "unverified",
        verified_by=best["exact"] if verified else 0,
        verified_utc=(verified_utc or datetime.now(UTC).isoformat()) if verified else None,
        comparison_rows=comparisons,
        aliases=aliases,
        equivalent_time_bases_in_observed_sample=equivalent,
        verification_rule="Full +/-180-minute mutually unique oriented fixture graph; "
        "iterative bijective aliases; at least two exact UTC witnesses; strictly more "
        "than nonequivalent rivals; any matched score disagreement vetoes verification.",
        zone_comparisons=[_public_zone(r) for r in results],
        selected_candidate=best,
    )


def _read_official(db):
    with sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        return [
            dict(row)
            for row in connection.execute(
                "SELECT m.match_id,m.league,m.kickoff_utc,h.name home,a.name away,"
                "r.home_goals hg,r.away_goals ag FROM matches m "
                "JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id "
                "LEFT JOIN results r ON r.match_id=m.match_id WHERE m.source='jc'"
            )
        ]


def _coverage(rows, official, result):
    best, records = result["selected_candidate"], result["comparison_rows"]
    valid_times = [instant for instant in best["times"] if instant is not None]
    low, high = (min(valid_times), max(valid_times)) if valid_times else (None, None)
    comparable = [j for j in official if low is not None and low <= _utc(j["kickoff_utc"]) <= high]
    matched_ids = {e["jc_match_id"] for e in records}
    matched_rows = {e["fd_row"] for e in records}
    missing = [j for j in comparable if j["match_id"] not in matched_ids]
    official_times = [_utc(j["kickoff_utc"]) for j in official]
    fd_missing = [
        dict(
            fd_row=row["row"],
            home=row["home"],
            away=row["away"],
            kickoff_utc=instant.isoformat(),
            known_home=result["aliases"].get(row["home"], {}).get("canonical"),
            known_away=result["aliases"].get(row["away"], {}).get("canonical"),
        )
        for row, instant in zip(rows, best["times"], strict=True)
        if instant is not None
        and official_times
        and min(official_times) <= instant <= max(official_times)
        and row["row"] not in matched_rows
    ]
    summary = dict(
        league=result["league"],
        status=result["status"],
        current_rows=len(rows),
        aliases=len(result["aliases"]),
        all_jc=len(official),
        fd_coverage_start_utc=low.isoformat() if low else None,
        fd_coverage_end_utc=high.isoformat() if high else None,
        comparable_jc=len(comparable),
        matched_jc=len(comparable) - len(missing),
        unmatched_jc=len(missing),
        excluded_outside_fd_coverage=len(official) - len(comparable),
        rate=(len(comparable) - len(missing)) / len(comparable) if comparable else None,
        fd_unmatched_in_jc_window=len(fd_missing),
    )
    unmatched = dict(
        unmatched_jc=missing,
        unmatched_fd_in_jc_window=fd_missing,
        unverified_raw_names=sorted(
            {r[s] for r in rows for s in ("home", "away")} - result["aliases"].keys()
        ),
    )
    return summary, unmatched


def _gap_diagnostics(rows, official, result, unmatched):
    best = result["selected_candidate"]
    aliases = best["aliases"]
    candidates = defaultdict(list)
    reverse = defaultdict(list)
    for row, instant in zip(rows, best["times"], strict=True):
        if instant is None:
            continue
        for game in official:
            delta = (instant - _utc(game["kickoff_utc"])).total_seconds() / 60
            if abs(delta) <= 180:
                compatible = _compatible(row, game, aliases)
                candidates[game["match_id"]].append(
                    dict(
                        fd_row=row["row"],
                        fd_home=row["home"],
                        fd_away=row["away"],
                        difference_minutes=delta,
                        compatible_with_verified_aliases=compatible,
                        known_home=aliases.get(row["home"]),
                        known_away=aliases.get(row["away"]),
                    )
                )
                if compatible:
                    reverse[row["row"]].append(game["match_id"])
    records = []
    for game in unmatched["unmatched_jc"]:
        options = candidates[game["match_id"]]
        for option in options:
            option["reverse_compatible_jc_ids"] = reverse[option["fd_row"]]
        count = sum(o["compatible_with_verified_aliases"] for o in options)
        reason = (
            "multiple compatible FD fixtures within full +/-180-minute window"
            if count > 1
            else "FD fixture has multiple compatible official candidates"
            if count == 1
            else "no FD fixture in +/-180-minute window compatible with oriented identities"
        )
        records.append(
            dict(jc=game, reason=reason, compatible_fd_count=count, all_temporal_candidates=options)
        )
    return records


def verify_inputs(db, index, config, year, output):
    """Read-only database/original inputs; write audit artifacts only to output."""
    official = _read_official(db)
    leagues, aliases, coverage, zones, unmatched, gaps = [], {}, {}, {}, {}, {}
    verified_utc = datetime.now(UTC).isoformat()
    entries = config.values() if isinstance(config, dict) else config
    for entry in entries:
        code, league = entry["div_code"], entry["league"]
        rows = load_rows(index[code], year)
        games = [game for game in official if game["league"] == league]
        result = verify_league(
            rows, games, entry, raw_sha256=index[code]["sha256"], verified_utc=verified_utc
        )
        leagues.append(
            {
                k: v
                for k, v in result.items()
                if k not in {"aliases", "zone_comparisons", "selected_candidate"}
            }
        )
        aliases[league] = result["aliases"]
        zones[code] = result["zone_comparisons"]
        coverage[code], unmatched[code] = _coverage(rows, games, result)
        gaps[code] = _gap_diagnostics(rows, games, result, unmatched[code])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    artifacts = dict(
        leagues_evidence=leagues,
        aliases_evidence=aliases,
        coverage=coverage,
        zone_comparisons=zones,
        unmatched=unmatched,
        gap_diagnostics=gaps,
    )
    for name, payload in artifacts.items():
        (output / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
    lines = [
        "# Offline FD identity verification",
        "",
        f"Season start year: {year}. Official database opened read-only. No network requests.",
        "",
        "|Code|Status|Time basis|Exact UTC|Aliases|Linked / comparable JC|",
        "|---|---|---|---:|---:|---:|",
    ]
    for league in leagues:
        item = coverage[league["div_code"]]
        lines.append(
            f"|{league['div_code']}|{league['status']}|{league['time_basis'] or ''}|"
            f"{league['verified_by']}|{item['aliases']}|"
            f"{item['matched_jc']}/{item['comparable_jc']}|"
        )
    lines += [
        "",
        "Coverage excludes official dates beyond the FD file's observed coverage. "
        "Unmatched FD rows may be fixtures outside the bookmaker-selected official archive.",
        "",
        "See gap_diagnostics.json for every remaining candidate and reverse competitor. "
        "Equivalent time bases are disclosed in leagues_evidence.json; equivalence applies "
        "only to this sample. Current-season timezone evidence does not prove historical "
        "publisher conventions. No proposed name aliases are generated.",
    ]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf8")
    return artifacts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    index = json.loads(args.index.read_text(encoding="utf-8-sig"))
    for item in index.values():
        if not Path(item["path"]).is_absolute():
            item["path"] = str(args.index.parent / item["path"])
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    verify_inputs(args.db, index, config, args.year, args.output)
    logging.getLogger(__name__).info("Offline identity evidence saved to %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
