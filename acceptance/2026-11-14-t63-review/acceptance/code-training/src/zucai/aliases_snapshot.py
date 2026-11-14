"""Read a verified local data-branch checkout without changing its files."""

import csv
import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

from zucai.league_registry import provider_config
from zucai.sources.fd_ingest import _canonical
from zucai.sources.fdorg_verify import _active
from zucai.sources.football_data import CONFIG_ROOT, read_json

LEAGUES = CONFIG_ROOT / "api_football/leagues.json"
_DEFAULT_LEAGUES = LEAGUES
ALIASES = CONFIG_ROOT / "team_aliases/api_football.json"
FDORG_ALIASES = CONFIG_ROOT / "team_aliases/football_data_org.json"


def formal_aliases():
    from zucai.sources.apif_aliases import validate_approvals

    aliases = read_json(ALIASES)
    validate_approvals(aliases, require_metadata=True)
    return aliases


def fdorg_formal_aliases():
    from zucai.sources.apif_aliases import validate_approvals

    aliases = read_json(FDORG_ALIASES)
    validate_approvals(aliases)  # Pre-T23 fdorg approvals retain their existing provenance.
    return aliases


def csv_rows(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, strict=True))
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("身份审计 CSV 不完整")
    return rows


def snapshot(archive):
    from zucai.cloud_feeds import restore_feeds

    archive = Path(archive).resolve()
    with TemporaryDirectory(prefix="aliases-read-") as temp:
        database = Path(temp) / "data/zucai.db"
        manifest = restore_feeds(archive, database)
        state = database.parent / "state"
        audits = {
            name: csv_rows(state / f"audit/apif_{name}.csv")
            for name in (
                "cup_aliases",
                "identity",
                "suspected_duplicates",
                "rejected_rows",
                "identity_matches",
                "identity_scopes",
            )
        }
        audits["fdorg_unmatched_teams"] = csv_rows(state / "audit/fdorg_unmatched_teams.csv")
        path = state / "alias_candidates/apif.json"
        candidates = read_json(path) if path.exists() else {}
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            teams = [
                dict(row) for row in con.execute("SELECT id,name,aliases FROM teams ORDER BY id")
            ]
            matches = [
                dict(row)
                for row in con.execute(
                    "SELECT m.match_id,m.source,m.league,m.kickoff_utc,m.counterpart_id,"
                    "m.home_team_id,m.away_team_id,"
                    "h.name home,a.name away,h.aliases home_aliases,a.aliases away_aliases "
                    "FROM matches m JOIN teams h ON h.id=m.home_team_id "
                    "JOIN teams a ON a.id=m.away_team_id ORDER BY m.match_id"
                )
            ]
    return dict(
        audits=audits,
        candidates=candidates,
        teams=teams,
        matches=matches,
        manifest=manifest,
        manifest_sha256=hashlib.sha256((archive / "feed_manifest.json").read_bytes()).hexdigest(),
    )


def observed_games(data, aliases):
    games = {}
    reverse = defaultdict(dict)
    for league, entries in aliases.items():
        for key in entries:
            canonical = _canonical(aliases, league, key)
            if canonical:
                reverse[league].setdefault(canonical, set()).add(key)
    for match in data["matches"]:
        if match["source"] != "apif":
            continue
        row = dict(
            league=match["league"],
            provider_match_id=match["match_id"].removeprefix("apif:"),
            kickoff_utc=match["kickoff_utc"],
            season="",
            raw_sha256="",
            complete="false",
        )
        for side in ("home", "away"):
            name = match[side]
            prefix = f"apif:{match['league']}:team:"
            keys = {
                value.removeprefix(prefix)
                for value in json.loads(match[side + "_aliases"])
                if value.startswith(prefix)
            }
            if name.startswith("apif:team:"):
                keys.add(name.removeprefix("apif:team:"))
            keys.update(reverse[match["league"]].get(name, set()))
            row[side + "_provider_id"] = next(iter(keys)) if len(keys) == 1 else ""
            row[side] = name
        games[row["provider_match_id"]] = row
    for name in ("rejected_rows", "suspected_duplicates", "identity_matches"):
        for row in data["audits"][name]:
            if row.get("provider_match_id") and row.get("league"):
                games[row["provider_match_id"]] = dict(row)
    return list(games.values())


def selected_leagues(codes):
    config = provider_config("apif") if LEAGUES == _DEFAULT_LEAGUES else read_json(LEAGUES)
    ids = codes.split(",")
    if not ids or len(ids) != len(set(ids)) or any(code not in config for code in ids):
        raise ValueError("leagues 须为不重复的已配置 APIF 联赛 ID")
    return {code: config[code] for code in ids}


def proposed_entries(data, aliases, selected):
    names = {config["league"] for config in selected.values()}
    records = {
        (league, key): dict(entry)
        for league, entries in aliases.items()
        if league in names
        for key, entry in entries.items()
        if entry.get("status") == "proposed"
    }
    for source in (data["audits"]["cup_aliases"], data["audits"]["identity"]):
        for row in source:
            if row["league"] in names:
                records[row["league"], row["team_id"]] = row
    for league, entries in data["candidates"].items():
        if league in names:
            for key, entry in entries.items():
                records[league, key] = entry
    # Cup imports already bridge consistent active IDs across formal leagues.
    # A pending candidate in another league must never suppress owner review.
    bridge = defaultdict(set)
    for league, entries in aliases.items():
        _active(aliases, league)
        for key in entries:
            if canonical := _canonical(aliases, league, key):
                bridge[key].add(canonical)
    if any(len(names) != 1 for names in bridge.values()):
        raise ValueError("正式 APIF 同源球队 ID 存在跨联赛身份冲突")
    return {
        key: entry
        for key, entry in records.items()
        if entry.get("status") == "proposed"
        and _canonical(aliases, *key) is None
        and not (key[0] in {"欧罗巴", "欧协联"} and key[1] in bridge)
    }
