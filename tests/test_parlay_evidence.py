"""T38 capture evidence and backwards-compatible sporttery audit fields."""

import csv
import json
from datetime import datetime
from pathlib import Path

import pytest

from test_injuries_storage import CAPTURE, KICKOFF, ingest
from test_injuries_storage import injury_database as injury_database
from zucai.parlay.archive import capture_injuries
from zucai.sources import sporttery

FIX = Path(__file__).parents[1] / "data/fixtures"


@pytest.mark.parametrize("cutoff,expected", [(KICKOFF, 2), (CAPTURE, None)])
def test_injury_fact_uses_actual_capture_before_cutoff_and_keeps_missing(
    injury_database, cutoff, expected
):
    ingest(injury_database, daily=False)
    batches = [dict(as_of_utc=cutoff, predictions=[dict(match_id="fd:target")])]
    facts = capture_injuries(injury_database, batches)["fd:target"]
    assert facts["home"]["confirmed_injured_out"] == expected
    assert facts["home"]["availability_mode"] == "strict_capture_time"
    if expected is not None:
        assert facts["home"]["availability_basis"] == "backfilled"
        assert facts["home"]["captured_utc"]
    else:
        assert facts["home"]["missing_reason"] == "backfilled_not_known"


def parse_catalog(document=None):
    feed = sporttery.SportteryCalculator()
    raw = (FIX / "t39/jc_catalog_12.json").read_bytes()
    feed.parse(
        json.dumps(document).encode() if document else raw,
        captured=datetime.fromisoformat("2026-11-04T03:30:00Z"),
    )
    return feed


def test_pool_flags_preserved_no_deadline_invented_and_present_raw_fields_retained():
    feed = parse_catalog()
    assert all(row["sale_end_fields"] == "" for row in feed.catalog)
    document = json.loads((FIX / "t39/jc_catalog_12.json").read_bytes())
    # Append a synthetic possible provider field: not evidence of live endpoint support.
    match = document["value"]["matchInfoList"][0]["subMatchList"][0]
    match["sellEndTime"] = "2026-11-04 22:05:00"
    match["poolList"][0]["saleDeadline"] = "2026-11-04 22:00:00"
    updated = parse_catalog(document).catalog[0]
    raw = json.loads(updated["sale_end_fields"])
    assert raw["sellEndTime"] == match["sellEndTime"]
    assert raw["poolList[0].saleDeadline"] == "2026-11-04 22:00:00"
    assert json.loads(updated["poolList"])[0] == match["poolList"][0]
    assert [r["retained"] for r in parse_catalog(document).catalog] == [
        r["retained"] for r in feed.catalog
    ]


@pytest.mark.parametrize("legacy", [False, True])
def test_old_catalog_header_upgrade_preserves_old_fields_and_legacy_bytes(tmp_path, legacy):
    feed = parse_catalog()
    path = tmp_path / ("jc_catalog.csv" if legacy else "jc_catalog/2026-11.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    old_fields = [k for k in sporttery.CATALOG_FIELDS if k not in sporttery.PARLAY_CATALOG_FIELDS]
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=old_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(feed.catalog)
    before = path.read_bytes()
    sporttery.save_schedule_audit(tmp_path, feed)
    if legacy:
        assert path.read_bytes() == before
    else:
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert rows == feed.catalog
    first = {p: p.read_bytes() for p in tmp_path.rglob("*.csv")}
    sporttery.save_schedule_audit(tmp_path, feed)
    assert all(p.read_bytes() == raw for p, raw in first.items())


def test_monthly_old_unknown_columns_can_be_filled_after_a_new_capture(tmp_path):
    old = parse_catalog()
    path = tmp_path / "jc_catalog/2026-11.csv"
    path.parent.mkdir(parents=True)
    old_fields = [k for k in sporttery.CATALOG_FIELDS if k not in sporttery.PARLAY_CATALOG_FIELDS]
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=old_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(old.catalog)
    new = sporttery.SportteryCalculator()
    new.parse(
        (FIX / "t39/jc_catalog_12.json").read_bytes(),
        captured=datetime.fromisoformat("2026-11-04T04:30:00Z"),
    )
    sporttery.save_schedule_audit(tmp_path, new)
    sporttery.save_schedule_audit(tmp_path, old)
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 24
    assert all(row["poolList"] for row in rows)
    raw = path.read_bytes()
    sporttery.save_schedule_audit(tmp_path, old)
    assert path.read_bytes() == raw
    old.catalog[0]["poolList"] = "[]"
    with pytest.raises(ValueError, match="审计冲突"):
        sporttery.save_schedule_audit(tmp_path, old)
