"""Read-only identity metrics and compact match metadata for later offline review."""

import csv
import io
import sqlite3

from zucai.sources.base import _atomic_write
from zucai.sources.cup_identity import alignment_counts
from zucai.sources.fd_ingest import WINDOW
from zucai.sources.fd_verify import _read_official, _utc

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


def record_matches(path, captures):
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
    if not records:
        return
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, FIELDS)
    writer.writeheader()
    writer.writerows(records.values())
    encoded = buffer.getvalue().encode("utf-8")
    if not path.exists() or path.read_bytes() != encoded:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, encoded)


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
