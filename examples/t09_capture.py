"""Capture live T09 evidence in an explicitly new private directory (never a unit test)."""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zucai.sources.base import RawStore
from zucai.sources.fdorg_command import COMPETITIONS
from zucai.sources.football_data import read_json
from zucai.sources.football_data_org import FdorgHttpError, FootballDataOrg


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _crop(source, path, dest):
    # Inspect the original bytes first: an echoed credential must never reach a fixture.
    token = os.environ["FOOTBALL_DATA_ORG_TOKEN"].strip().encode()
    raw = path.read_bytes()
    if not token or token in raw:
        raise ValueError("Unsafe fixture input")
    payload = json.loads(raw)
    rows = payload["matches"]
    priorities = ["EXTRA_TIME", "PENALTY_SHOOTOUT", "POSTPONED", "FINISHED", "TIMED", "SCHEDULED"]
    selected = []
    for label in priorities:
        for row in rows:
            if label in (row.get("status"), (row.get("score") or {}).get("duration")):
                if row not in selected:
                    selected.append(row)
                break
    if not selected and rows:
        selected.append(rows[0])
    # Only publish fields the model adapter actually needs. Never invent a missing edge case.
    trimmed = []
    for row in selected:
        item = {k: row[k] for k in ("id", "utcDate", "status")}
        for name, fields in (
            ("competition", ("code",)),
            ("season", ("startDate", "endDate")),
            ("homeTeam", ("id", "name")),
            ("awayTeam", ("id", "name")),
            ("score", ("duration", "fullTime", "halfTime", "regularTime")),
        ):
            item[name] = {k: row[name][k] for k in fields if k in (row.get(name) or {})}
        trimmed.append(item)
    crop = {"resultSet": {"count": len(trimmed)}, "matches": trimmed}
    encoded = json.dumps(crop, ensure_ascii=False, indent=2).encode()
    if token in encoded:
        raise ValueError("Unsafe cropped fixture")
    dest.write_bytes(encoded + b"\n")
    return dict(
        fixture_sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        source_rows=len(rows),
        copied_ids=[r["id"] for r in trimmed],
        captured=source.store.load_metadata(path),
    )


def capture(folder, season, floor):
    if not os.environ.get("FOOTBALL_DATA_ORG_TOKEN"):
        raise ValueError("真实验收需要在采集环境配置 FOOTBALL_DATA_ORG_TOKEN")
    if not 1900 <= floor <= season <= 9999:
        raise ValueError("invalid season range")
    folder.mkdir(parents=True, exist_ok=False)
    mapping = read_json(COMPETITIONS)
    source = FootballDataOrg(store=RawStore(folder / "raw"), competitions=mapping)
    evidence = {"started_utc": datetime.now(UTC).isoformat(), "codes": {}, "fixtures": {}}
    try:
        for code in mapping:
            events = evidence["codes"][code] = []
            for year in range(season, floor - 1, -1):
                query = dict(
                    competition=code, season=year, status=None if year == season else "FINISHED"
                )
                try:
                    path = source.fetch_raw(**query)
                except FdorgHttpError as exc:
                    events.append(dict(year=year, status=exc.status_code))
                    if exc.status_code in (403, 404):
                        break  # observed boundary, not proof that every older season is unavailable
                    raise
                payload = json.loads(path.read_bytes())
                rows = payload["matches"]
                durations = {}
                for row in rows:
                    duration = (row.get("score") or {}).get("duration") or "missing"
                    durations[duration] = durations.get(duration, 0) + 1
                events.append(
                    dict(
                        year=year,
                        rows=len(rows),
                        durations=durations,
                        meta=source.store.load_metadata(path),
                    )
                )
                if code == "PL" and year == season:
                    evidence["fixtures"]["season"] = _crop(
                        source, path, folder / "live_pl_season.json"
                    )
            _write(folder / "capture.json", evidence)
        day = datetime.now(UTC).date()
        path = source.fetch_raw(
            competition="PL,PD,SA",
            date_from=(day - timedelta(days=7)).isoformat(),
            date_to=(day + timedelta(days=8)).isoformat(),
        )
        evidence["fixtures"]["window"] = _crop(source, path, folder / "live_window.json")
    finally:
        evidence.update(
            ended_utc=datetime.now(UTC).isoformat(),
            requests=source.request_count,
            remaining_quota=source.remaining_quota,
            rate_events=source.rate_events,
        )
        _write(folder / "capture.json", evidence)
        source.session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new private directory")
    parser.add_argument("--season", required=True, type=int, help="current season start year")
    parser.add_argument("--floor", required=True, type=int, help="oldest year to probe, inclusive")
    args = parser.parse_args()
    capture(args.output, args.season, args.floor)
