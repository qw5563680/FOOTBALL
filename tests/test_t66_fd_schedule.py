"""Successful FD collection freshness, using offline T14 HTTP and real archives."""

import json
import sqlite3
from datetime import datetime

import pytest
import requests
import responses

from test_cloud_apis import (
    FIXTURES,
    ROOT,
    configure_fd,
    forget_fd_success,
    mock_apis,
    run_apis,
)
from test_cloud_apis import environment as environment
from test_stats_batch import NOW as STATS_NOW
from test_stats_batch import batch as batch
from test_stats_workflow import captured_fd_argv
from zucai.cloud_apis import collect_apis, fd_due
from zucai.cloud_feeds import archive_info, digest, export_feeds, restore_feeds

STATE = "state/fd_collection.json"
STARTED = "last_successful_run_started_utc"
FD_URL = "https://www.football-data.co.uk/mmz4281/2627/E0.csv"


def write_receipt(path, stamp="2026-11-02T23:59:59+00:00"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, STARTED: stamp}))


def archive_receipt(archive, stamp):
    write_receipt(archive / STATE, stamp)
    path = archive / "feed_manifest.json"
    meta = json.loads(path.read_bytes())
    meta["files"][STATE] = digest(archive / STATE)
    path.write_text(json.dumps(meta))


@pytest.mark.parametrize(
    ("previous", "instant", "expected"),
    [
        ("2026-11-02T23:59:59Z", "2026-11-02T23:59:59Z", False),
        ("2026-11-02T23:59:59Z", "2026-11-03T10:19:00Z", False),
        ("2026-11-02T23:59:59Z", "2026-11-08T23:59:59Z", False),
        ("2026-11-02T23:59:59Z", "2026-11-09T00:00:00Z", True),
        ("2026-11-14T10:19:00Z", "2026-11-15T23:59:59Z", False),
        ("2026-11-14T10:19:00Z", "2026-11-16T00:00:00Z", True),
        ("2026-11-14T10:19:00Z", "2026-11-16T07:59:59+08:00", False),
        ("2026-11-14T10:19:00Z", "2026-11-16T08:00:00+08:00", True),
        ("2025-12-31T10:19:00Z", "2026-01-01T00:00:00Z", False),
        ("2025-12-31T10:19:00Z", "2026-01-05T00:00:00Z", True),
        ("2026-12-31T10:19:00Z", "2027-01-04T07:59:59+08:00", False),
        ("2026-12-31T10:19:00Z", "2027-01-04T08:00:00+08:00", True),
        ("2025-12-31T10:19:00Z", "2027-01-04T00:00:00Z", True),
    ],
)
@pytest.mark.parametrize("slot", ["early", "late", "auto"])
def test_success_only_suppresses_its_monday_anchored_utc_week(
    tmp_path, previous, instant, expected, slot
):
    path = tmp_path / STATE
    write_receipt(path, previous)
    assert fd_due(datetime.fromisoformat(instant), slot, path) is expected


INVALID_RECEIPTS = [
    "damaged",
    "[]",
    "{}",
    json.dumps({"version": True, STARTED: "2026-11-01T02:40:00+00:00"}),
    json.dumps({"version": 2, STARTED: "2026-11-01T02:40:00+00:00"}),
    json.dumps({"version": 1, STARTED: "2026-11-01T02:40:00+00:00", "extra": 1}),
    *[
        json.dumps({"version": 1, STARTED: stamp})
        for stamp in (
            None,
            1793491200,
            "2026-11-01",
            "2026-11-01T02:40:00",
            "2026-11-01T02:40:00+08:00",
            "2026-11-01 02:40:00Z",
            "2026-02-30T02:40:00Z",
            "2026-11-02T02:40:01Z",  # Future evidence, even within the same UTC day.
        )
    ],
]


@pytest.mark.parametrize("raw", INVALID_RECEIPTS)
def test_invalid_or_future_success_never_suppresses_collection(tmp_path, raw):
    path = tmp_path / STATE
    path.parent.mkdir()
    path.write_text(raw)
    assert fd_due(datetime.fromisoformat("2026-11-02T02:40:00Z"), "late", path)


@pytest.mark.parametrize("raw", INVALID_RECEIPTS)
def test_export_rejects_invalid_or_future_success_before_changing_archive(
    environment, tmp_path, raw
):
    database = tmp_path / "work/data/zucai.db"
    restore_feeds(environment, database)
    before = (environment / "feed_manifest.json").read_bytes()
    (database.parent / STATE).write_text(raw)
    with pytest.raises(ValueError, match="FD"):
        export_feeds(database, environment, datetime(2026, 11, 2).date(), "invalid")
    assert (environment / "feed_manifest.json").read_bytes() == before


def test_missing_early_collects_late_and_success_roundtrips(environment, tmp_path, monkeypatch):
    forget_fd_success(environment)
    configure_fd(monkeypatch, tmp_path)
    monkeypatch.setattr("test_cloud_apis.CLOCK", datetime.fromisoformat("2026-11-02T10:19:00Z"))
    with responses.RequestsMock() as api:
        mock_apis(api, fd=True)
        first = run_apis(tmp_path, monkeypatch, environment, "late-recovery", slot="late")
    meta = archive_info(environment)
    assert meta["collection"]["fd"]["status"] == "complete"
    assert meta["collection"]["fd"]["downloaded_files"] == 1
    assert meta["collection"]["fd"]["inserted"] == 1
    expected = {"version": 1, STARTED: "2026-11-02T10:19:00+00:00"}
    assert json.loads((environment / STATE).read_bytes()) == expected
    assert meta["files"][STATE] == digest(first / "data" / STATE)
    for slot in ("early", "late", "auto"):
        with responses.RequestsMock() as api:
            mock_apis(api)
            later = run_apis(tmp_path, monkeypatch, environment, "skip-" + slot, slot=slot)
        assert archive_info(environment)["collection"]["fd"]["status"] == "skipped"
        assert json.loads((later / "data" / STATE).read_bytes()) == expected
    restored = tmp_path / "restored/data/zucai.db"
    restore_feeds(environment, restored)
    assert (restored.parent / STATE).read_bytes() == (environment / STATE).read_bytes()


@pytest.mark.parametrize("failure", ["http", "parse", "zero-scopes"])
def test_failed_early_preserves_previous_success_and_retries_late(
    environment, tmp_path, monkeypatch, failure
):
    configure_fd(monkeypatch, tmp_path)
    archive_receipt(environment, "2026-10-26T02:40:00+00:00")
    previous = (environment / STATE).read_bytes()
    database = tmp_path / "work/data/zucai.db"
    restore_feeds(environment, database)
    monkeypatch.setenv("ZUCAI_DB", str(database))
    if failure == "zero-scopes":
        config_path = tmp_path / "leagues.json"
        original = config_path.read_bytes()
        config_path.write_text("{}")
    with responses.RequestsMock() as api:
        mock_apis(api)
        if failure != "zero-scopes":
            raw = (FIXTURES / "fd.csv").read_bytes()
            if failure == "parse":
                raw = b"not a CSV response"
            api.get(FD_URL, body=raw, status=503 if failure == "http" else 200)
        early = collect_apis(database, datetime.fromisoformat("2026-11-02T02:40:00Z"), slot="early")
    assert early["fd"]["status"] == "partial"
    assert (database.parent / STATE).read_bytes() == previous
    assert fd_due(datetime.fromisoformat("2026-11-02T10:19:00Z"), "late", database.parent / STATE)
    if failure == "zero-scopes":
        config_path.write_bytes(original)
    monkeypatch.setattr("test_cloud_apis.CLOCK", datetime.fromisoformat("2026-11-02T10:19:00Z"))
    with responses.RequestsMock() as api:
        mock_apis(api, fd=True)
        late = collect_apis(database, datetime.fromisoformat("2026-11-02T10:19:00Z"), slot="late")
    assert late["fd"]["status"] == "complete"
    assert late["fd"]["downloaded_files"] == 1
    saved = json.loads((database.parent / STATE).read_bytes())
    assert saved[STARTED] == "2026-11-02T10:19:00+00:00"


@pytest.mark.parametrize("warning", ["rows", "duplicates", "both"])
def test_row_warnings_keep_partial_report_but_complete_the_weekly_batch(
    environment, tmp_path, monkeypatch, warning
):
    forget_fd_success(environment)
    configure_fd(monkeypatch, tmp_path)
    if warning in ("duplicates", "both"):
        database = tmp_path / "seed-warning/data/zucai.db"
        restore_feeds(environment, database)
        with sqlite3.connect(database) as db:
            db.execute(
                "UPDATE matches SET league='英超',kickoff_utc='2026-11-01T12:00:00.000000Z' "
                "WHERE match_id='jc:1'"
            )
        export_feeds(database, environment, datetime(2026, 11, 2).date(), "seed-warning")
    raw = (FIXTURES / "fd.csv").read_bytes()
    if warning == "rows":
        raw = raw.replace(b",2,1,H,", b",-1,1,H,")
    elif warning == "both":
        raw += raw.splitlines(keepends=True)[1].replace(b",2,1,H,", b",-1,1,H,")
    with responses.RequestsMock() as api:
        mock_apis(api)
        api.get(FD_URL, body=raw)
        first = run_apis(tmp_path, monkeypatch, environment, "row-warnings")
    meta = archive_info(environment)
    report = meta["collection"]["fd"]
    assert report["status"] == meta["collection_status"] == "partial"
    assert report["downloaded_files"] == report["processed_files"] == 1
    assert report["rejected_rows"] == (1 if warning in ("rows", "both") else 0)
    assert report["suspected_duplicates"] == (1 if warning in ("duplicates", "both") else 0)
    for key, audit in (
        ("rejected_rows", "fd_rejected_rows.csv"),
        ("suspected_duplicates", "fd_suspected_duplicates.csv"),
    ):
        if report[key]:
            assert (first / "data/state/audit" / audit).is_file()
            assert f"state/audit/{audit}" in meta["files"]
    expected = {"version": 1, STARTED: "2026-11-02T02:40:00+00:00"}
    assert (environment / STATE).is_file()
    assert json.loads((environment / STATE).read_bytes()) == expected
    assert meta["files"][STATE] == digest(first / "data" / STATE)
    assert list((first / "data/raw/football-data-co-uk").rglob("*.csv"))
    with responses.RequestsMock() as api:
        mock_apis(api)
        later = run_apis(tmp_path, monkeypatch, environment, "skip-warnings", slot="late")
        assert not any("football-data.co.uk" in call.request.url for call in api.calls)
    assert archive_info(environment)["collection"]["fd"]["status"] == "skipped"
    assert json.loads((later / "data" / STATE).read_bytes()) == expected
    assert not (later / "data/raw/football-data-co-uk").exists()
    assert captured_fd_argv(tmp_path, later) == []


def test_saturday_success_skips_sunday_but_collects_again_next_monday(
    environment, tmp_path, monkeypatch
):
    forget_fd_success(environment)
    configure_fd(monkeypatch, tmp_path)
    for name, instant, expected in (
        ("saturday", "2026-11-14T10:19:00Z", "complete"),
        ("sunday", "2026-11-15T23:59:59Z", "skipped"),
        ("monday", "2026-11-16T00:00:00Z", "complete"),
    ):
        monkeypatch.setattr("test_cloud_apis.CLOCK", datetime.fromisoformat(instant))
        with responses.RequestsMock(assert_all_requests_are_fired=False) as api:
            mock_apis(api, fd=expected == "complete", apif_mode="empty", empty_fdorg=True)
            work = run_apis(tmp_path, monkeypatch, environment, name)
            assert sum("football-data.co.uk" in call.request.url for call in api.calls) == (
                1 if expected == "complete" else 0
            )
        report = archive_info(environment)["collection"]["fd"]
        assert report["status"] == expected
        assert archive_info(environment)["collection"]["apif"]["status"] == "complete"
        assert archive_info(environment)["collection"]["fdorg"]["status"] == "complete"
        assert report["downloaded_files"] == (1 if expected == "complete" else 0)
        if expected == "complete":
            assert json.loads((environment / STATE).read_bytes())[STARTED] == instant.replace(
                "Z", "+00:00"
            )
        else:
            assert not (work / "data/raw/football-data-co-uk").exists()
            assert captured_fd_argv(tmp_path, work) == []


@pytest.mark.parametrize("failure", ["http", "parse"])
def test_later_scope_failure_preserves_retry_after_another_scope_succeeds(
    environment, tmp_path, monkeypatch, failure
):
    forget_fd_success(environment)
    configure_fd(monkeypatch, tmp_path)
    config_path = tmp_path / "leagues.json"
    config = json.loads(config_path.read_bytes())
    config["E1"] = json.loads((ROOT / "data/football_data_co_uk/leagues.json").read_bytes())["E1"]
    config_path.write_text(json.dumps(config))
    with responses.RequestsMock() as api:
        mock_apis(api, fd=True)
        api.get(
            "https://www.football-data.co.uk/mmz4281/2627/E1.csv",
            body=b"not a CSV response",
            status=503 if failure == "http" else 200,
        )
        work = run_apis(tmp_path, monkeypatch, environment, "partial-batch")
    report = archive_info(environment)["collection"]["fd"]
    assert report["status"] == "partial"
    assert report["downloaded_files"] == report["inserted"] == 1
    assert STATE not in archive_info(environment)["files"]
    assert not (work / "data" / STATE).exists()
    assert list((work / "data/raw/football-data-co-uk").rglob("*.csv"))
    assert captured_fd_argv(tmp_path, work)
    assert fd_due(datetime.fromisoformat("2026-11-02T10:19:00Z"), "late", work / "data" / STATE)
    monkeypatch.setattr("test_cloud_apis.CLOCK", datetime.fromisoformat("2026-11-02T10:19:00Z"))
    with responses.RequestsMock() as api:
        mock_apis(api, fd=True)
        api.get(
            "https://www.football-data.co.uk/mmz4281/2627/E1.csv",
            body=(FIXTURES / "fd.csv").read_bytes().replace(b"E0,", b"E1,"),
        )
        run_apis(tmp_path, monkeypatch, environment, "retry-batch", slot="late")
    report = archive_info(environment)["collection"]["fd"]
    assert report["status"] == "complete"
    assert report["downloaded_files"] == report["processed_files"] == 2
    assert json.loads((environment / STATE).read_bytes())[STARTED] == "2026-11-02T10:19:00+00:00"


@pytest.mark.parametrize("receipt", ["invalid", "future", "hash"])
def test_restore_rejects_bad_receipt_before_replacing_database(environment, tmp_path, receipt):
    if receipt == "invalid":
        archive_receipt(environment, "2026-11-01T02:40:00")
    elif receipt == "future":
        archive_receipt(environment, "2026-11-02T02:40:01Z")
    else:
        write_receipt(environment / STATE, "2026-11-01T03:00:00Z")
    target = tmp_path / "existing.db"
    target.write_bytes(b"must remain untouched")
    with pytest.raises(ValueError):
        restore_feeds(environment, target)
    assert target.read_bytes() == b"must remain untouched"


@pytest.mark.parametrize("prior_success", [None, "2026-10-26T02:40:00Z"])
def test_restore_discards_success_from_unpublished_failed_export(
    environment, tmp_path, monkeypatch, prior_success
):
    if prior_success:
        archive_receipt(environment, prior_success)
    else:
        forget_fd_success(environment)
    configure_fd(monkeypatch, tmp_path)
    database = tmp_path / "work/data/zucai.db"
    restore_feeds(environment, database)
    monkeypatch.setenv("ZUCAI_DB", str(database))
    with responses.RequestsMock() as api:
        mock_apis(api, fd=True)
        report = collect_apis(database, datetime.fromisoformat("2026-11-02T02:40:00Z"))
    assert report["fd"]["status"] == "complete"
    assert (database.parent / STATE).exists()
    # Force a real export validation failure after collection succeeded locally.
    with sqlite3.connect(database) as db:
        db.execute("UPDATE matches SET status='finished' WHERE match_id='jc:1'")
    previous_manifest = (environment / "feed_manifest.json").read_bytes()
    with pytest.raises(ValueError, match="完赛状态"):
        export_feeds(database, environment, datetime(2026, 11, 2).date(), "failed-export")
    assert (environment / "feed_manifest.json").read_bytes() == previous_manifest
    restore_feeds(environment, database)
    if prior_success:
        assert json.loads((database.parent / STATE).read_bytes())[STARTED] == prior_success
    else:
        assert not (database.parent / STATE).exists()
    assert fd_due(datetime.fromisoformat("2026-11-02T10:19:00Z"), "late", database.parent / STATE)


def test_captured_fd_recovery_after_api_stats_has_no_requests_or_asset_collision(
    batch, tmp_path, monkeypatch
):
    from zucai import cloud_stats
    from zucai.db import get_engine, session_scope
    from zucai.models import Match
    from zucai.sources.base import RawStore
    from zucai.stats_batch import collect_batch
    from zucai.stats_schedule import plan

    database, archive, release, client = batch
    work = tmp_path / "work"
    work.mkdir()
    database.parent.rename(work / "data")
    database = work / "data/zucai.db"
    (work / "data/feeds.db").rename(database)
    archive.rename(tmp_path / "cloud-data")
    archive = tmp_path / "cloud-data"
    fixture_root = ROOT / "data/fixtures"
    engine = get_engine(database)
    with session_scope(engine) as session:
        session.add(
            Match(**json.loads((fixture_root / "t31/store.json").read_bytes())["matches"][0])
        )
    engine.dispose()
    raw = RawStore(database.parent / "raw").save(
        "football-data-co-uk",
        "E0-2425",
        (fixture_root / "t31/fd.csv").read_bytes(),
        "csv",
        captured_at=STATS_NOW,
        url="https://www.football-data.co.uk/mmz4281/2425/E0.csv",
        status_code=200,
    )
    raw_before = raw.read_bytes()
    metadata_before = raw.with_suffix(".csv.meta.json").read_bytes()
    export_feeds(database, archive, STATS_NOW.date(), "before-late-stats")
    injury = json.loads((fixture_root / "t38/injuries.json").read_bytes())
    injury["parameters"]["season"] = "2026"
    kickoff = json.loads((fixture_root / "t38/archive.json").read_bytes())["match"]["kickoff_utc"]
    for row in injury["response"]:
        row["league"]["season"] = 2026
        row["fixture"]["date"] = kickoff
    with responses.RequestsMock() as http:
        http.get("https://v3.football.api-sports.io/injuries?league=39&season=2026", json=injury)
        earlier = collect_batch(
            database,
            archive,
            plan("39", "2026", "injuries", STATS_NOW),
            release=release,
            run_id="fd-recovery-3",
            now=STATS_NOW,
            daily=True,
            client_factory=client,
        )
    assert earlier["status"] == "complete" and earlier["inserted"] == 4
    assert earlier["requests"] == 1
    prior_checksum = release.root / "stats-raw-202611/SHA256SUMS-fd-recovery-3"
    checksum_before = prior_checksum.read_bytes()
    quota = database.parent / "state/api_football_quota.json"
    quota_before = quota.read_bytes()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return STATS_NOW

    monkeypatch.setattr(cloud_stats, "datetime", Clock)
    monkeypatch.setattr(cloud_stats, "ReleaseStore", lambda: release)
    release.session = requests.Session()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.delenv("API_FOOTBALL_KEY")
    monkeypatch.chdir(work)
    argv = captured_fd_argv(tmp_path, work)
    with responses.RequestsMock() as http:
        assert cloud_stats.main(argv[2:]) == 0
        assert len(http.calls) == 0
    report = archive_info(archive)["collection"]["stats"]
    assert report["status"] == "complete", report
    assert report["requests"] == report["downloaded_files"] == 0
    assert report["reused_files"] == 1 and report["inserted"] == 16
    assert quota.read_bytes() == quota_before
    assert prior_checksum.read_bytes() == checksum_before
    assert raw.read_bytes() == raw_before
    assert raw.with_suffix(".csv.meta.json").read_bytes() == metadata_before
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM match_injuries").fetchone() == (4,)
        assert db.execute("SELECT count(*) FROM match_stats WHERE provider='fd'").fetchone() == (
            16,
        )
        # The existing T31 fixture is scheduled: reuse must not invent a known result time.
        assert db.execute(
            "SELECT DISTINCT availability_basis,captured_utc,available_utc "
            "FROM match_stats WHERE provider='fd'"
        ).fetchall() == [("backfilled", "2026-11-04T09:00:10.000000Z", None)]
    # Published Release references restore the evidence, never prior local raw bytes.
    later = tmp_path / "later"
    restore_feeds(archive, later / "data/zucai.db")
    assert not (later / "data/raw/football-data-co-uk").exists()
    assert captured_fd_argv(later, later) == []
