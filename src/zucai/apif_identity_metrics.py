"""Read-only identity metrics and compact match metadata for later offline review."""

import csv
import io
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from zucai.league_registry import provider_config
from zucai.sources.base import _atomic_write
from zucai.sources.cup_identity import alignment_counts
from zucai.sources.fd_ingest import WINDOW
from zucai.sources.fd_verify import _read_official, _utc
from zucai.sources.football_data import CONFIG_ROOT, read_json

FIELDS = (
    "provider_match_id",
    "league_id",
    "league",
    "season",
    "kickoff_utc",
    "home_provider_id",
    "away_provider_id",
    "home",
    "away",
    "raw_sha256",
    "complete",
    "scope_size",
)
SCOPE_FIELDS = (
    "league_id",
    "league",
    "season",
    "observed_utc",
    "raw_sha256",
    "complete",
    "scope_size",
)


def record_scope(path, code, league, season, *, raw_sha256="", complete=False, scope_size=0):
    """Latest attempt per season, including failed/empty observations and hash replays."""
    records = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(SCOPE_FIELDS):
                raise ValueError("身份范围审计字段无效")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("身份范围审计行无效")
                records[row["league_id"], row["season"]] = row
    records[str(code), str(season)] = dict(
        league_id=code,
        league=league,
        season=season,
        observed_utc=datetime.now(UTC).isoformat(),
        raw_sha256=raw_sha256,
        complete=str(complete).lower(),
        scope_size=scope_size,
    )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, SCOPE_FIELDS)
    writer.writeheader()
    writer.writerows(records.values())
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, buffer.getvalue().encode("utf-8"))


def record_matches(path, captures, *, leagues_path=None):
    from zucai.sources.api_football import LEAGUES

    configured_path = Path(leagues_path if leagues_path is not None else LEAGUES)
    config = (
        provider_config("apif")
        if configured_path.resolve() == (CONFIG_ROOT / "api_football/leagues.json").resolve()
        else read_json(configured_path)
    )
    captures = tuple(captures)
    # Validate every route before writing either audit; a new reviewed mapping
    # must not depend on duplicating its ID into the obsolete legacy config file.
    for capture in captures:
        code = str(capture["scope"]["league_id"])
        if code not in config:
            raise ValueError(f"{code} 身份审计赛事尚未核准")
        if any(
            str(game.competition_code) != code or game.league != config[code]["league"]
            for game in capture["games"]
        ):
            raise ValueError("身份审计比赛与核准赛事范围不符")
    records = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(FIELDS):
                raise ValueError("身份比赛审计字段无效")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("身份比赛审计行无效")
                records[row["provider_match_id"], row["raw_sha256"]] = row
    for capture in captures:
        for game in capture["games"]:
            row = dict(
                provider_match_id=game.provider_match_id,
                league_id=game.competition_code,
                league=game.league,
                season=capture["scope"].get("season", ""),
                kickoff_utc=game.kickoff_utc.isoformat(),
                home_provider_id=game.home_provider_id,
                away_provider_id=game.away_provider_id,
                home=game.home_raw,
                away=game.away_raw,
                raw_sha256=capture["raw_sha256"],
                complete=str(capture["complete"]).lower(),
                scope_size=len(capture["games"]),
            )
            records[str(game.provider_match_id), capture["raw_sha256"]] = row
    if records:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, FIELDS)
        writer.writeheader()
        writer.writerows(records.values())
        encoded = buffer.getvalue().encode("utf-8")
        if not path.exists() or path.read_bytes() != encoded:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, encoded)
    for capture in captures:
        code = str(capture["scope"]["league_id"])
        record_scope(
            path.with_name("apif_identity_scopes.csv"),
            code,
            config[code]["league"],
            capture["scope"]["season"],
            raw_sha256=capture["raw_sha256"],
            complete=capture["complete"],
            scope_size=len(capture["games"]),
        )


def jc_links(database):
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as con:
        return {
            row[0]
            for row in con.execute(
                "SELECT counterpart_id FROM matches "
                "WHERE source='jc' AND counterpart_id LIKE 'apif:%'"
            )
        }


def jc_window_metrics(database, games, aliases):
    official = _read_official(database)
    selected = [
        g
        for g in games
        if any(
            j["league"] == g.league and abs(_utc(j["kickoff_utc"]) - g.kickoff_utc) <= WINDOW
            for j in official
        )
    ]
    counts = list(alignment_counts(selected, aliases).values())
    total, aligned = sum(c["teams"] for c in counts), sum(c["aligned_teams"] for c in counts)
    return dict(
        jc_window_teams=total,
        jc_window_aligned_teams=aligned,
        jc_window_alignment_rate=aligned / total if total else None,
    )
