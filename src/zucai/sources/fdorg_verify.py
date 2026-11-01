"""Replay full v4 season snapshots against the official JC archive, without network or DB writes."""

import argparse
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from zucai.sources.base import RawStore
from zucai.sources.fd_ingest import _canonical
from zucai.sources.fd_verify import _compatible, _read_official, _utc, infer_oriented_pairs
from zucai.sources.football_data import CONFIG_ROOT, read_json
from zucai.sources.football_data_org import (
    COMPETITIONS_PATH,
    FootballDataOrg,
    _code,
    _id,
    _kickoff,
    _season,
)

# A 200 from a season URL can still be a cut-down sample. Verify every regular-season
# home/away pairing for these domestic formats before any fixture can become an anchor.
CLUB_COUNTS = {
    "PL": 20,
    "ELC": 24,
    "PD": 20,
    "SA": 20,
    "BL1": 18,
    "FL1": 18,
    "DED": 18,
    "PPL": 18,
    "BSA": 20,
}
DEFAULT_ALIASES = CONFIG_ROOT / "team_aliases/football_data_org.json"


@dataclass(frozen=True)
class _Fixture:
    """Schedule identity only. No score or result availability can enter the graph."""

    provider_match_id: int
    home_provider_id: int
    away_provider_id: int
    kickoff_utc: datetime
    home_raw: str | None
    away_raw: str | None
    provider_status: str | None
    import_rejection: str | None


def _identity_team(team):
    if not isinstance(team, dict) or _id(team.get("id")) is None:
        raise ValueError("missing or invalid provider team ID")
    # Display names never select an edge; the same ID may have a label on another match.
    label = team.get("name")
    return team["id"], label.strip() if isinstance(label, str) and label.strip() else None


def _fixtures(document, code, season, rejected):
    fallback_code = _code(document)
    fallback_season = str(document.get("filters", {}).get("season", ""))
    reasons = {r["row_number"]: r["reason"] for r in rejected}
    fixtures = {}
    for n, row in enumerate(document["matches"], 1):
        key = _id(row.get("id")) if isinstance(row, dict) else None
        if key is None or key in fixtures:
            raise ValueError(f"missing, invalid or duplicate match ID at row {n}")
        if _code(row, fallback_code) != code or (_season(row) or fallback_season) != str(season):
            raise ValueError(f"unconfirmed competition or season at row {n}")
        home, home_raw = _identity_team(row.get("homeTeam"))
        away, away_raw = _identity_team(row.get("awayTeam"))
        if home == away:
            raise ValueError(f"identical home and away IDs at row {n}")
        fixtures[key] = _Fixture(
            provider_match_id=key,
            home_provider_id=home,
            away_provider_id=away,
            kickoff_utc=_kickoff(row.get("utcDate")),
            home_raw=home_raw,
            away_raw=away_raw,
            provider_status=row.get("status") if isinstance(row.get("status"), str) else None,
            import_rejection=reasons.get(n),
        )
    return list(fixtures.values())


def _index(store, source, season):
    """Select the latest successful unfiltered season observation, never mix observations."""
    if not store.root.is_dir():
        raise ValueError("RawStore directory does not exist")
    provider_dir = store.root / "football-data-org"
    search_root = provider_dir if provider_dir.is_dir() else store.root
    captures = defaultdict(list)
    for sidecar in sorted(search_root.rglob("*.meta.json")):
        relpath = sidecar.relative_to(store.root).as_posix()
        meta = json.loads(store.load(relpath))
        if meta.get("status_code") != 200:
            continue
        url = meta.get("url")
        match = re.fullmatch(r".*/competitions/([A-Z0-9]+)/matches\?season=(\d{4})", url or "")
        if not match:
            continue  # Date windows and FINISHED-only files cannot prove uniqueness.
        code, year = match[1], int(match[2])
        if code not in source.competitions or (season is not None and year != season):
            continue
        if url != source.request_url(competition=code, season=year):
            continue
        try:
            seen = datetime.fromisoformat(meta["fetched_at_utc"])
            if seen.utcoffset() != timedelta(0):
                raise ValueError
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid UTC capture time in {relpath}") from exc
        provenance = dict(
            sidecar=relpath,
            raw_sha256=meta.get("sha256"),
            fetched_at_utc=meta["fetched_at_utc"],
            url=url,
        )
        captures[code, year].append((seen, provenance))
    if season is None:
        years = {year for _, year in captures}
        if len(years) != 1:
            raise ValueError("Specify --season when the archive has zero or multiple seasons")
        season = years.pop()
    selected = {}
    for (code, year), items in captures.items():
        latest = max(seen for seen, _ in items)
        tied = [item for seen, item in items if seen == latest]
        if len({item["raw_sha256"] for item in tied}) != 1:
            raise ValueError(f"Conflicting {code}/{year} observations at the same UTC instant")
        selected[code] = tied[0]
    return season, selected


def _full_schedule(source, code, season, archive):
    raw = source.store.load(archive["sidecar"].removesuffix(".meta.json"))
    if hashlib.sha256(raw).hexdigest() != archive["raw_sha256"]:
        raise ValueError(f"SHA256 mismatch for {archive['sidecar']}")
    try:
        source.parse(raw, competition=code, season=season)
    except ValueError as exc:
        return [], f"season response failed scope or structure checks: {exc}"
    document = json.loads(raw)
    filtered = {"status", "dateFrom", "dateTo", "matchday", "stage", "group", "limit", "offset"}
    if any(document.get("filters", {}).get(k) not in (None, "", []) for k in filtered):
        return [], "response filters restrict the full season"
    matches = document["matches"]
    count = document.get("resultSet", {}).get("count")
    if type(count) is not int or count != len(matches):
        return [], "resultSet.count does not confirm the archived row count"
    try:
        games = _fixtures(document, code, season, source.rejected)
    except ValueError as exc:
        return [], f"season fixture identity is incomplete: {exc}"
    regular_ids = {m["id"] for m in matches if m.get("stage") == "REGULAR_SEASON"}
    regular = [g for g in games if g.provider_match_id in regular_ids]
    clubs = {t for g in regular for t in (g.home_provider_id, g.away_provider_id)}
    pairs = Counter((g.home_provider_id, g.away_provider_id) for g in regular)
    expected = CLUB_COUNTS[code]
    if code == "FL1" and season < 2023:
        expected = 20
    if (
        len(clubs) != expected
        or len(pairs) != expected * (expected - 1)
        or set(pairs.values()) != {1}
    ):
        return [], f"incomplete regular season: {len(clubs)}/{expected} clubs, {len(pairs)} pairs"
    # Additional playoff fixtures in the response also participate in both directions.
    return sorted(games, key=lambda g: (g.kickoff_utc, g.provider_match_id)), None


def _active(aliases, league):
    active, reverse = {}, {}
    for key in aliases[league]:
        if not re.fullmatch(r"[1-9][0-9]*", key):
            raise ValueError(f"Invalid provider team ID: {key}")
        canonical = _canonical(aliases, league, key)
        if canonical is None:
            continue
        if canonical.casefold() in reverse:
            raise ValueError(f"Existing alias conflict for {canonical}")
        active[key] = canonical
        reverse[canonical.casefold()] = key
    return active


def _graph(games, official, active):
    rows = [
        dict(row=g.provider_match_id, home=str(g.home_provider_id), away=str(g.away_provider_id))
        for g in games
    ]
    instants = [g.kickoff_utc for g in games]
    independent = infer_oriented_pairs(rows, official, instants)
    # An active anchor can resolve an otherwise ambiguous fixture early enough to conceal
    # contradictory evidence reached through independent fixtures after several iterations.
    result = (
        infer_oriented_pairs(rows, official, instants, known_aliases=active)
        if active
        else independent
    )
    combined = result["aliases"]
    reverse = {name.casefold(): key for key, name in combined.items()}
    for key, name in independent["aliases"].items():
        if (key in combined and combined[key] != name) or reverse.get(name.casefold(), key) != key:
            raise ValueError(f"Independent fixture evidence conflicts with alias: {key}/{name}")
    by_id = {row["row"]: row for row in rows}
    by_jc = {game["match_id"]: game for game in official}
    # A previously blocked, contradictory fixture must not be forgotten if another
    # iteration subsequently assigns one of its identifiers.
    for conflict in independent["conflicts"] + result["conflicts"]:
        if not _compatible(by_id[conflict["fd_row"]], by_jc[conflict["jc_match_id"]], combined):
            raise ValueError(f"Oriented fixture evidence conflicts with alias: {conflict}")
    return result


def _witness(g, j, delta, archive):
    return dict(
        fdorg_id=f"fdorg:{g.provider_match_id}",
        jc_id=j["match_id"],
        home_id=g.home_provider_id,
        away_id=g.away_provider_id,
        jc_home=j["home"],
        jc_away=j["away"],
        fdorg_kickoff_utc=g.kickoff_utc.isoformat(),
        jc_kickoff_utc=j["kickoff_utc"],
        difference_minutes=delta,
        raw_sha256=archive["raw_sha256"],
        sidecar=archive["sidecar"],
        **_import_note(g),
    )


def _import_note(g):
    return (
        dict(provider_status=g.provider_status, import_rejection=g.import_rejection)
        if g.import_rejection
        else {}
    )


def _new_aliases(games, official, entries, graph, archive):
    by_id = {g.provider_match_id: g for g in games}
    by_jc = {j["match_id"]: j for j in official}
    labels = {}
    for game in games:
        for team, name in (
            (game.home_provider_id, game.home_raw),
            (game.away_provider_id, game.away_raw),
        ):
            if name:
                labels.setdefault(team, name)
    updated = dict(entries)
    for key, origin in graph["alias_origin"].items():
        witness = origin["witnesses"][0]
        g, j = by_id[witness["fd_row"]], by_jc[witness["jc_match_id"]]
        evidence = _witness(g, j, witness["difference_minutes"], archive)
        evidence["iteration"] = origin["iteration"]
        updated[key] = dict(
            name=(g.home_raw if key == str(g.home_provider_id) else g.away_raw)
            or labels.get(int(key)),
            canonical=graph["aliases"][key],
            status="auto_verified",
            evidence=[evidence],
        )
    return updated


def _jc_identity(j):
    return dict(jc_id=j["match_id"], home=j["home"], away=j["away"], kickoff_utc=j["kickoff_utc"])


def _coverage(games, official, graph, archive):
    # Compare the observed season's UTC dates, also including boundary fixtures whose
    # +/-180-minute overlap crosses midnight. This does not depend on whether identities link.
    low, high = games[0].kickoff_utc.date(), games[-1].kickoff_utc.date()
    temporal = {k for _, k, _ in graph["temporal_edges"]}
    comparable = [
        k
        for k, j in enumerate(official)
        if k in temporal or low <= _utc(j["kickoff_utc"]).date() <= high
    ]
    paired = {k for _, k, _ in graph["pairs"]}
    candidates, reverse = defaultdict(list), defaultdict(list)
    eligible = {(i, k) for i, k, _ in graph["compatible_edges"]}
    for i, k, _ in graph["compatible_edges"]:
        reverse[i].append(official[k]["match_id"])
    for i, k, delta in graph["temporal_edges"]:
        g = games[i]
        candidates[k].append(
            dict(
                fdorg_id=f"fdorg:{g.provider_match_id}",
                home_id=g.home_provider_id,
                away_id=g.away_provider_id,
                known_home=graph["aliases"].get(str(g.home_provider_id)),
                known_away=graph["aliases"].get(str(g.away_provider_id)),
                difference_minutes=delta,
                compatible=(i, k) in eligible,
                reverse_jc_ids=reverse[i],
                **_import_note(g),
            )
        )
    conflicts = {c["jc_match_id"] for c in graph["conflicts"]}
    gaps = []
    for k in comparable:
        if k in paired:
            continue
        j, options = official[k], candidates[k]
        viable = [opt for opt in options if opt["compatible"]]
        if j["match_id"] in conflicts:
            reason = "conflicting oriented fixture identities"
        elif len(viable) > 1:
            reason = "multiple compatible provider fixtures"
        elif viable:
            reason = "provider fixture has multiple compatible JC candidates"
        elif options:
            reason = "no compatible oriented team identities"
        else:
            reason = "no provider fixture within +/-180 minutes"
        gaps.append(dict(_jc_identity(j), reason=reason, candidates=options))
    links = [_witness(games[i], official[k], delta, archive) for i, k, delta in graph["pairs"]]
    matched = len(links)
    return (
        dict(
            status="complete",
            coverage_start_date=low.isoformat(),
            coverage_end_date=high.isoformat(),
            fdorg_rows=len(games),
            identity_only_rows=[
                dict(fdorg_id=f"fdorg:{g.provider_match_id}", **_import_note(g))
                for g in games
                if g.import_rejection
            ],
            all_jc=len(official),
            comparable_jc=len(comparable),
            matched_jc=matched,
            unmatched_jc=len(gaps),
            excluded_outside_fdorg_coverage=len(official) - len(comparable),
            rate=matched / len(comparable) if comparable else None,
        ),
        gaps,
        links,
    )


def verify_inputs(db, raw_dir, aliases_file, competitions_file, output, season=None):
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    competitions = read_json(competitions_file)
    aliases = read_json(aliases_file)
    official = sorted(
        _read_official(db), key=lambda j: (j["league"], j["kickoff_utc"], j["match_id"])
    )
    source = FootballDataOrg(store=RawStore(raw_dir), competitions=competitions)
    coverage, unlinked, links = {}, {}, {}
    try:
        season, archives = _index(source.store, source, season)
        for code, config in competitions.items():
            league = config["league"]
            jc = [game for game in official if game["league"] == league]
            entries = aliases.setdefault(league, {})
            active = _active(aliases, league)
            archive = archives.get(code)
            games, reason = (
                _full_schedule(source, code, season, archive)
                if archive
                else ([], "no successful unfiltered season archive")
            )
            if reason:
                coverage[code] = dict(
                    status="incomplete",
                    reason=reason,
                    coverage_start_date=None,
                    coverage_end_date=None,
                    fdorg_rows=None,
                    identity_only_rows=None,
                    all_jc=len(jc),
                    comparable_jc=None,
                    matched_jc=None,
                    unmatched_jc=None,
                    rate=None,
                    excluded_outside_fdorg_coverage=None,
                )
                unlinked[code] = [
                    dict(_jc_identity(j), assessed=False, reason=reason, candidates=[]) for j in jc
                ]
                links[code] = []
            else:
                graph = _graph(games, jc, active)
                aliases[league] = _new_aliases(games, jc, entries, graph, archive)
                coverage[code], unlinked[code], links[code] = _coverage(games, jc, graph, archive)
            coverage[code].update(league=league, season=season)
    finally:
        source.session.close()
    artifacts = dict(
        aliases=aliases, coverage=coverage, links=links, unlinked=unlinked, archives=archives
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as tmp:
        staged = Path(tmp) / "audit"
        staged.mkdir()
        for name, artifact in artifacts.items():
            (staged / f"{name}.json").write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
            )
        staged.rename(output)
    return artifacts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument(
        "--season", type=int, help="Season start year; required with mixed archives"
    )
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES)
    parser.add_argument("--competitions-file", type=Path, default=COMPETITIONS_PATH)
    parser.add_argument("--output", type=Path, required=True, help="New temporary audit directory")
    args = parser.parse_args(argv)
    verify_inputs(
        args.db, args.raw_dir, args.aliases, args.competitions_file, args.output, args.season
    )
    logging.getLogger(__name__).info("Offline FDORG identity evidence saved to %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
