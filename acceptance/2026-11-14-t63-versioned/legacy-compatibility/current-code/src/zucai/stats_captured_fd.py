"""Reuse this collection's fd bytes; this module never downloads a CSV."""

import json
import re
from urllib.parse import urlparse

from zucai.sources.apif_stats import instant
from zucai.sources.football_data import LEAGUES_PATH, read_json
from zucai.stats_schedule import record, unit
from zucai.stats_storage import ingest_fd


def ingest_captured_files(database, raw_root, selection, now, cursor, location):
    result = dict(inserted=0, rejected_rows=0, reused_files=0)
    if "fd" not in selection["endpoints"]:
        return result
    leagues = read_json(LEAGUES_PATH)
    for path in sorted((raw_root / "football-data-co-uk").rglob("*.csv")):
        meta = json.loads(path.with_suffix(path.suffix + ".meta.json").read_bytes())
        scope = re.fullmatch(
            r"/mmz4281/([0-9]{4})/([A-Z][0-9]|SP1|I1|N1|P1)\.csv", urlparse(meta["url"]).path
        )
        captured = instant(meta["fetched_at_utc"])
        if not scope or captured.date() != instant(now).date() or captured > instant(now):
            continue
        season, code = scope.groups()
        if code not in selection["fd_codes"] or 2000 + int(season[:2]) not in selection["seasons"]:
            continue
        item = unit(
            "fd",
            code,
            2000 + int(season[:2]),
            meta["sha256"],
            league=leagues[code]["league"],
            phase="daily",
        )
        if cursor["records"].get(item["key"], {}).get("outcome") == "ok":
            continue
        added = ingest_fd(
            database, path, div=code, season=season, raw_location=location(path), daily=True
        )
        result["inserted"] += added["inserted"]
        result["rejected_rows"] += added["rejected"]
        result["reused_files"] += 1
        record(
            cursor,
            item,
            "ok",
            captured,
            requested=False,
            received=True,
            rejected_rows=added["rejected"],
            raw_sha256=meta["sha256"],
            raw_location=location(path),
        )
    return result
