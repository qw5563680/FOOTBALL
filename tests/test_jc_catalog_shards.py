"""T42: capture-month shards and selective, backwards-compatible digest reads."""

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from zucai.cloud_feeds import archive_info, digest, export_feeds, restore_feeds
from zucai.db import get_engine, init_db
from zucai.digest_context import audit_rows, catalog_gaps
from zucai.sources import sporttery

FIX = Path(__file__).parents[1] / "data/fixtures"
RAW = (FIX / "t39/jc_catalog_12.json").read_bytes()
SAMPLE = json.loads((FIX / "t42/capture_windows.json").read_bytes())
NAMES = ("jc_catalog", "jc_skipped")


def capture(stamp):
    feed = sporttery.SportteryCalculator()
    feed.parse(RAW, captured=datetime.fromisoformat(stamp))
    return feed


def rows_at(feed, name):
    return feed.catalog if name == "jc_catalog" else feed.skipped


def write_csv(path, feed, name):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (*sporttery.CATALOG_FIELDS, *(["reason"] if name == "jc_skipped" else []))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows_at(feed, name))


def contents(directory):
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in directory.rglob("*.csv")
        if p.is_file()
    }


def test_cross_month_captures_use_utc_not_business_or_beijing_month(tmp_path):
    for stamp, month in zip(SAMPLE["captures"], SAMPLE["capture_months"], strict=True):
        feed = capture(stamp)
        assert {row["business_date"] for row in feed.catalog} == {"2026-11-04"}
        sporttery.save_schedule_audit(tmp_path, feed)
        for name in NAMES:
            path = tmp_path / name / f"{month}.csv"
            with path.open(encoding="utf-8", newline="") as stream:
                assert list(csv.DictReader(stream)) == rows_at(feed, name)
        before = contents(tmp_path)
        sporttery.save_schedule_audit(tmp_path, feed)
        assert contents(tmp_path) == before
    assert set(contents(tmp_path)) == {
        f"{name}/{month}.csv" for name in NAMES for month in SAMPLE["capture_months"]
    }


def test_old_november_files_remain_immutable_and_still_enforce_replay_keys(tmp_path):
    old = capture(SAMPLE["legacy_capture"])
    for name in NAMES:
        write_csv(tmp_path / f"{name}.csv", old, name)
    old_bytes = contents(tmp_path)
    fresh = capture(SAMPLE["next_capture"])
    sporttery.save_schedule_audit(tmp_path, fresh)
    for path, raw in old_bytes.items():
        assert (tmp_path / path).read_bytes() == raw
    before = contents(tmp_path)
    sporttery.save_schedule_audit(tmp_path, old)
    assert contents(tmp_path) == before  # Legacy records are not copied into the shard.
    old.catalog[2]["retained"] = "1"
    with pytest.raises(ValueError, match="审计冲突"):
        sporttery.save_schedule_audit(tmp_path, old)
    assert contents(tmp_path) == before


def test_empty_capture_has_monthly_headers_but_no_invented_records(tmp_path):
    feed = sporttery.SportteryCalculator()
    feed.parse(
        json.dumps(SAMPLE["empty_response"]).encode(),
        captured=datetime.fromisoformat(SAMPLE["next_capture"]),
    )
    sporttery.save_schedule_audit(tmp_path, feed)
    for name in NAMES:
        with (tmp_path / name / "2026-11.csv").open(encoding="utf-8") as stream:
            assert list(csv.DictReader(stream)) == []


def test_unknown_capture_time_does_not_choose_machine_month(tmp_path):
    feed = sporttery.SportteryCalculator()
    feed.parse(RAW)
    with pytest.raises(ValueError):
        sporttery.save_schedule_audit(tmp_path, feed)
    assert contents(tmp_path) == {}


@pytest.mark.parametrize("existing", [False, True])
def test_paired_monthly_audits_rollback_after_second_write(tmp_path, monkeypatch, existing):
    if existing:
        sporttery.save_schedule_audit(tmp_path, capture(SAMPLE["legacy_capture"]))
    before = contents(tmp_path)
    real_write = sporttery._atomic_write
    failed = False

    def fail_once(path, content):
        nonlocal failed
        real_write(path, content)
        if path.parent.name == "jc_skipped" and not failed:
            failed = True
            raise OSError("synthetic second write failure")

    monkeypatch.setattr(sporttery, "_atomic_write", fail_once)
    with pytest.raises(OSError, match="synthetic"):
        sporttery.save_schedule_audit(tmp_path, capture(SAMPLE["next_capture"]))
    assert contents(tmp_path) == before


def test_archive_hashes_restore_bytes_and_removes_only_obsolete_owned_shards(tmp_path):
    database = tmp_path / "source/data/zucai.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    audit = database.parent / "state/audit"
    for stamp in SAMPLE["captures"]:
        sporttery.save_schedule_audit(audit, capture(stamp))
    for name in NAMES:
        write_csv(audit / f"{name}.csv", capture(SAMPLE["legacy_capture"]), name)
    expected = contents(audit)
    archive = tmp_path / "archive"
    manifest = export_feeds(database, archive, date(2026, 11, 5), "monthly")
    assert archive_info(archive) == manifest
    assert all(
        manifest["files"][f"state/audit/{path}"] == digest(audit / path) for path in expected
    )
    restored = tmp_path / "restored/data/zucai.db"
    dest = restored.parent / "state/audit"
    for name in NAMES:
        write_csv(dest / name / "2026-12.csv", capture(SAMPLE["next_capture"]), name)
    note = dest / "jc_catalog/notes.csv"
    note.write_text("keep user note\n", encoding="utf-8")
    directory = dest / "jc_catalog/2027-03.csv"
    directory.mkdir()
    restore_feeds(archive, restored)
    assert contents(dest) == {**expected, "jc_catalog/notes.csv": b"keep user note\n"}
    assert directory.is_dir()
    # Re-export removes old owned shards, but leaves legacy bytes and unrelated files alone.
    for name in NAMES:
        (audit / name / "2026-10.csv").unlink()
    export_feeds(database, archive, date(2026, 11, 5), "without-october")
    restore_feeds(archive, restored)
    assert all(not (dest / name / "2026-10.csv").exists() for name in NAMES)
    assert all((dest / f"{name}.csv").read_bytes() == expected[f"{name}.csv"] for name in NAMES)


@pytest.mark.parametrize("case", SAMPLE["windows"], ids=lambda c: c["name"])
@pytest.mark.parametrize("name", NAMES)
def test_digest_opens_only_week_months_and_november_legacy(tmp_path, monkeypatch, case, name):
    feed = {"files": {}}
    for month in SAMPLE["months"]:
        path = tmp_path / f"state/audit/{name}/{month}.csv"
        write_csv(path, capture(f"{month}-01T00:00:00Z"), name)
        feed["files"][path.relative_to(tmp_path).as_posix()] = digest(path)
    legacy = tmp_path / f"state/audit/{name}.csv"
    write_csv(legacy, capture(SAMPLE["legacy_capture"]), name)
    feed["files"][legacy.relative_to(tmp_path).as_posix()] = digest(legacy)
    expected = {f"state/audit/{name}/{month}.csv" for month in case["months"]}
    if "2026-11" in case["months"]:
        expected.add(f"state/audit/{name}.csv")
    opened = []
    original = Path.open

    def tracking(path, *args, **kwargs):
        relative = path.relative_to(tmp_path).as_posix()
        opened.append(relative)
        assert relative in expected
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking)
    now = datetime.fromisoformat(case["now"])
    records = audit_rows(tmp_path, feed, name, now)
    assert records
    assert set(opened) == expected and len(opened) == len(expected)
    assert all(datetime.fromisoformat(row["captured_utc"]) <= now for row in records)


def test_legacy_only_and_mixed_archives_keep_catalog_gap_meaning(tmp_path):
    old = capture(SAMPLE["legacy_capture"])
    files = {}
    for name in NAMES:
        path = tmp_path / f"state/audit/{name}.csv"
        write_csv(path, old, name)
        files[path.relative_to(tmp_path).as_posix()] = digest(path)
    now = datetime.fromisoformat(SAMPLE["next_capture"])
    metadata = {"files": files}
    assert audit_rows(tmp_path, metadata, "jc_catalog", now) == old.catalog
    expected = catalog_gaps(old.catalog, old.skipped, set(), {})
    fresh = capture(SAMPLE["next_capture"])
    for name in NAMES:
        path = tmp_path / f"state/audit/{name}/2026-11.csv"
        write_csv(path, fresh, name)
        files[path.relative_to(tmp_path).as_posix()] = digest(path)
    actual = catalog_gaps(
        audit_rows(tmp_path, metadata, "jc_catalog", now),
        audit_rows(tmp_path, metadata, "jc_skipped", now),
        set(),
        {},
    )
    assert [(r["jc_num"], r["reason"], r["confirmed"]) for r in actual] == [
        (r["jc_num"], r["reason"], r["confirmed"]) for r in expected
    ]
    future = now + timedelta(hours=1)
    assert len(audit_rows(tmp_path, metadata, "jc_catalog", future)) == 24
    assert len(audit_rows(tmp_path, metadata, "jc_catalog", now - timedelta(seconds=1))) == 12
    assert audit_rows(tmp_path, {"files": {}}, "jc_catalog", now) is None
