"""Read-only counterpart evidence for APIF identity checks; no new matching rules."""

import hashlib
import sqlite3
from pathlib import Path

from zucai.sources.fd_ingest import WINDOW, _canonical
from zucai.sources.fd_verify import _read_official, _utc
from zucai.sources.football_data import read_json, verified_league


def read_counterparts(database, config, fd_leagues, fd_aliases):
    official = [row for row in _read_official(database) if row["league"] == config["league"]]
    leagues, aliases = read_json(fd_leagues), read_json(fd_aliases)
    code, league = config["fd_code"], config["league"]
    verified_league(leagues, code)
    if leagues[code]["league"] != league:
        raise ValueError("FD 桥接联赛配置不符")
    names = {name for key in aliases.get(league, {}) if (name := _canonical(aliases, league, key))}
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT m.match_id,m.league,m.kickoff_utc,h.name home,a.name away "
            "FROM matches m JOIN teams h ON h.id=m.home_team_id "
            "JOIN teams a ON a.id=m.away_team_id WHERE m.source='fd' AND m.league=?",
            (league,),
        )
        for row in rows:
            if (
                not row["match_id"].startswith(f"fd:{code}:")
                or not {row["home"], row["away"]} <= names
            ):
                continue
            # Already-known identities can identify duplicate counterpart observations.
            # Keep JC as the witness; duplicate FD copies must not create graph ambiguity.
            if any(
                item["match_id"].startswith("jc:")
                and row["home"] == item["home"]
                and row["away"] == item["away"]
                and abs(_utc(row["kickoff_utc"]) - _utc(item["kickoff_utc"])) <= WINDOW
                for item in official
            ):
                continue
            official.append(dict(row))
    return official, {
        "fd_leagues_sha256": hashlib.sha256(Path(fd_leagues).read_bytes()).hexdigest(),
        "fd_aliases_sha256": hashlib.sha256(Path(fd_aliases).read_bytes()).hexdigest(),
    }
