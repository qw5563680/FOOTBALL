"""Run the real collector, parser, cursor and archive against offline HTTP/Release mocks."""

import copy
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import responses

from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.sources import api_football
from zucai.sources.base import RawStore
from zucai.stats_batch import collect_batch
from zucai.stats_client import StatsClient
from zucai.stats_schedule import plan
from zucai.stats_storage import link_archive

FIX = Path(__file__).parents[1] / "data/fixtures"
RUNTIME = json.loads((FIX / "t32/runtime.json").read_text())
DATA = json.loads((FIX / "t32/batch.json").read_text())
NOW = datetime.fromisoformat(RUNTIME["now"])


class Release:
    def __init__(self, root, fail=None):
        self.root, self.fail = root, fail
        self.uploads = []
        self.downloads = []

    def upload(self, tag, path):
        if self.fail and self.fail in path.name:
            raise OSError("offline archive failure")
        data = path.read_bytes()
        if not data:
            raise ValueError("HTTP 422: GitHub Release rejects empty assets")
        dest = self.root / tag / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.read_bytes() != data:
            raise ValueError("immutable collision")
        dest.write_bytes(data)
        self.uploads.append(path.name)
        return dict(
            tag=tag, name=path.name, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )

    def download(self, tag, name, dest):
        self.downloads.append((tag, name))
        shutil.copyfile(self.root / tag / name, dest)
        return dest


@pytest.fixture
def batch(tmp_path, monkeypatch):
    database = tmp_path / "data/feeds.db"
    database.parent.mkdir()
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(**t) for t in DATA["teams"])
        session.flush()
        session.add_all(Match(**m) for m in DATA["matches"])
        session.flush()
        session.add_all(Result(**r) for r in DATA["results"])
    engine.dispose()
    fixture = RawStore(tmp_path / "seed").save(
        "api-football",
        "fixtures",
        json.dumps(
            dict(
                get="fixtures",
                parameters={"league": "2", "season": "2024"},
                errors=[],
                results=2,
                paging={"current": 1, "total": 1},
                response=DATA["fixtures"],
            )
        ).encode(),
        "json",
        captured_at=NOW,
        url=api_football.API + "/fixtures?league=2&season=2024",
        status_code=200,
    )
    assert (
        link_archive(
            database, fixture, league_id=2, season=2024, aliases_path=FIX / "t31/aliases.json"
        )["linked"]
        == 2
    )
    quota = database.parent / "state/api_football_quota.json"
    quota.parent.mkdir(exist_ok=True)
    quota.write_text(json.dumps(RUNTIME["quota"]))
    archive = tmp_path / "archive"
    export_feeds(
        database, archive, date(2026, 11, 4), "seed", collection={"jc": {"status": "complete"}}
    )
    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-placeholder")

    clock = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(api_football, "datetime", Clock)

    def client(**kwargs):
        return StatsClient(**kwargs, now=lambda: clock[0], sleep=lambda seconds: None)

    client.clock = clock

    return database, archive, Release(tmp_path / "release"), client


def response(fixture_id, *, empty=False, errors=None):
    doc = copy.deepcopy(
        DATA["empty"] if empty else json.loads((FIX / "t30/statistics.json").read_text())
    )
    doc["parameters"]["fixture"] = str(fixture_id)
    if errors:
        doc["errors"] = errors
    return doc


def register(http, fixture_id, **options):
    http.get(
        api_football.API + f"/fixtures/statistics?fixture={fixture_id}",
        json=response(fixture_id, **options),
    )


def run(batch, run_id="one", *, max_requests=7000, **kwargs):
    database, archive, release, client = batch
    now = kwargs.pop("now", NOW)
    client.clock[0] = now
    return collect_batch(
        database,
        archive,
        plan("2", "2024", "statistics", NOW),
        release=release,
        run_id=run_id,
        now=now,
        client_factory=client,
        max_requests=max_requests,
        **kwargs,
    )


def table(database, name):
    with sqlite3.connect(database) as db:
        order = "id" if name == "match_stats" else "1"
        return db.execute(f"SELECT * FROM {name} ORDER BY {order}").fetchall()


def test_resume_and_repeat_zero_requests_keep_rows_and_base_facts(batch):
    database, archive, release, _ = batch
    base = {t: table(database, t) for t in ("matches", "results", "teams")}
    with responses.RequestsMock() as http:
        register(http, 102)
        first = run(batch, max_requests=1)
        assert first["status"] == "partial" and first["inserted"] == 36, first
        assert first["requests"] == 1 and len(http.calls) == 1
    saved = table(database, "match_stats")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT DISTINCT provider FROM match_stats").fetchall() == [("apif",)]
        assert db.execute(
            "SELECT value,missing_reason FROM match_stats WHERE stat_key='saves' AND side='home'"
        ).fetchone() == (None, "null")
    assert first["coverage"]["rows"][0]["expected"] == 2
    assert not any("state/match_stats" in k for k in archive_info(archive)["files"])
    shards = archive_info(archive)["technical_data"]["row_shards"]
    assert release.downloads == []
    with responses.RequestsMock() as http:
        register(http, 103)
        second = run(batch, "two")
        assert second["status"] == "complete" and second["inserted"] == 36, second
        assert second["requests"] == 1 and len(http.calls) == 1
    assert release.downloads == [(shard["tag"], shard["name"]) for shard in shards]
    assert table(database, "match_stats")[:36] == saved
    before, uploaded = database.read_bytes(), len(release.uploads)
    with responses.RequestsMock() as http:
        third = run(batch, "three", resume=False)
        assert third["requests"] == third["inserted"] == len(http.calls) == 0, third
    assert database.read_bytes() == before
    assert len(release.uploads) == uploaded
    assert {t: table(database, t) for t in base} == base
    meta = archive_info(archive)
    assert meta["collection"]["jc"] == {"status": "complete"}
    assert meta["technical_data"]["counts"]["match_stats"] == 72
    restored = database.parent.parent / "restored/feeds.db"
    restore_feeds(archive, restored)
    assert table(restored, "match_stats") == []


@pytest.mark.parametrize("failure", ["stats-raw-", "stats-rows-", "SHA256SUMS-one"])
def test_archive_failure_rolls_back_database_audits_cursor_but_preserves_quota(batch, failure):
    database, archive, release, _ = batch
    release.fail = failure
    before = database.read_bytes()
    audit_path = database.parent / "state/audit/stats_link_audit.csv"
    audit = audit_path.read_bytes() if audit_path.exists() else None
    with responses.RequestsMock() as http:
        register(http, 102)
        register(http, 103)
        result = run(batch)
    assert result.get("archive_failed") and result["inserted"] == 0, result
    assert database.read_bytes() == before
    assert (audit_path.read_bytes() if audit_path.exists() else None) == audit
    assert not (database.parent / "state/stats_backfill_cursor.json").exists()
    assert not (database.parent / "state/audit/stats_ingest_audit.csv").exists()
    assert json.loads((database.parent / "state/api_football_quota.json").read_bytes())["used"] == 2
    assert archive_info(archive)["collection"]["stats"]["archive_failed"]
    assert list((database.parent / "raw").rglob("*.meta.json"))


def test_release_http_failure_is_persisted_with_status_and_safe_response(batch):
    from zucai.stats_release import ReleaseStore

    cases = json.loads((FIX / "t32/release_errors.json").read_text())
    case = cases["cases"][1]
    database, archive, _, client = batch
    before = database.read_bytes()
    root = "https://api.github.com/repos/owner/repo/releases"
    upload = "https://uploads.github.com/repos/owner/repo/releases/1/assets"
    with responses.RequestsMock() as http:
        register(http, 102)
        register(http, 103)
        http.get(root + "/tags/stats-raw-202611", json={"id": 1, "upload_url": upload})
        http.get(root + "/1/assets", json=[])
        http.post(upload, status=case["status"], body=case["body"])
        result = run((database, archive, ReleaseStore("owner/repo", cases["token"]), client))
    assert result["archive_failed"] and result["requests"] == 2, result
    assert database.read_bytes() == before
    expected = case["body"].replace(cases["token"], "[REDACTED]")[:200]
    expected = expected.replace("\r", "\\r").replace("\n", "\\n")
    assert result["errors"] == [
        f"技术归档失败，该批数据库/游标未提交：Release HTTP {case['status']}: {expected}"
    ]
    assert archive_info(archive)["collection"]["stats"]["errors"] == result["errors"]
    assert cases["token"] not in (archive / "feed_manifest.json").read_text()


def test_permission_error_marks_scope_unsupported_and_never_retries(batch):
    with responses.RequestsMock() as http:
        register(http, 102, errors=DATA["errors"])
        result = run(batch)
        assert result["requests"] == 1 and result["inserted"] == 0, result
    row = result["coverage"]["rows"][0]
    assert row["unsupported"] == 2 and row["valid"] == 0
    with responses.RequestsMock() as http:
        assert run(batch, "two")["requests"] == len(http.calls) == 0


def test_http_200_parameter_errors_fail_without_disabling_other_fixtures(batch):
    errors = json.loads((FIX / "t30/cases.json").read_text())["errors"]["parameter"]
    with responses.RequestsMock() as http:
        register(http, 102, errors=errors)
        register(http, 103)
        result = run(batch)
        assert result["requests"] == len(http.calls) == 2
    assert result["inserted"] == 36 and result["status"] == "partial", result
    row = result["coverage"]["rows"][0]
    assert [row[key] for key in ("failed", "valid", "unsupported")] == [1, 1, 0]


def test_empty_responses_wait_seven_days_and_at_most_two_retries(batch):
    for attempt in range(3):
        with responses.RequestsMock() as http:
            register(http, 102, empty=True)
            register(http, 103, empty=True)
            result = run(batch, str(attempt), now=NOW + timedelta(days=7 * attempt))
            # T31 materializes explicit missing markers on the first empty statistics
            # response; these must stay null and must never count as valid coverage.
            assert result["requests"] == 2 and result["inserted"] == (72 if attempt == 0 else 0), (
                result
            )
            assert all(
                value is None
                for (value,) in sqlite3.connect(batch[0]).execute("SELECT value FROM match_stats")
            )
        with responses.RequestsMock() as http:
            assert (
                run(batch, f"same-{attempt}", now=NOW + timedelta(days=7 * attempt))["requests"]
                == 0
            )
    with responses.RequestsMock() as http:
        assert run(batch, "last", now=NOW + timedelta(days=100))["requests"] == 0
    row = result["coverage"]["rows"][0]
    assert row["null"] == 2 and row["requests"] == 6


def test_reserve_stops_without_requests_and_retains_pending_work(batch):
    database, archive, _, _ = batch
    quota = database.parent / "state/api_football_quota.json"
    doc = json.loads(quota.read_bytes())
    doc["remaining"] = 310
    quota.write_text(json.dumps(doc))
    with responses.RequestsMock() as http:
        result = run(batch)
        assert result["status"] == "partial" and result["requests"] == len(http.calls) == 0
    assert result["coverage"]["rows"][0]["expected"] == 2
    assert archive_info(archive)["collection"]["stats"]["requests"] == 0


@pytest.mark.parametrize("status", [401, 403, 429])
def test_non_json_transport_failure_stops_or_disables_scope(batch, status):
    with responses.RequestsMock() as http:
        http.get(
            api_football.API + "/fixtures/statistics?fixture=102",
            body=RUNTIME["non_json_error"],
            status=status,
        )
        result = run(batch)
        assert result["requests"] == len(http.calls) == 1, result
    assert result["inserted"] == 0 and not result.get("archive_failed"), result
    if status in {401, 403}:
        assert result["coverage"]["rows"][0]["unsupported"] == 2
        with responses.RequestsMock() as http:
            assert run(batch, "two")["requests"] == len(http.calls) == 0


def test_index_entry_uses_existing_matches_then_publishes_index_only(batch, monkeypatch, tmp_path):
    from zucai import stats_batch, stats_context

    database, archive, release, client = batch
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM apif_fixture_index")
    export_feeds(database, archive, date(2026, 11, 4), "no-index")
    config = tmp_path / "config/team_aliases"
    config.mkdir(parents=True)
    (config / "api_football.json").write_bytes((FIX / "t31/aliases.json").read_bytes())
    monkeypatch.setattr(stats_batch, "CONFIG_ROOT", config.parent)
    monkeypatch.setattr(stats_context, "CONFIG_ROOT", config.parent)
    doc = dict(
        get="fixtures",
        parameters={"league": "2", "season": "2024"},
        errors=[],
        results=2,
        paging={"current": 1, "total": 1},
        response=DATA["fixtures"],
    )
    with responses.RequestsMock() as http:
        http.get(api_football.API + "/fixtures?league=2&season=2024", json=doc)
        result = collect_batch(
            database,
            archive,
            plan("2", "2024", "index", NOW),
            release=release,
            run_id="index",
            now=NOW,
            client_factory=client,
        )
        assert result["requests"] == len(http.calls) == 1, result
    assert result["linked_existing"] == 2 and result["inserted"] == 0, result
    assert result["status"] == "complete" and not result.get("archive_failed"), result
    assert len(table(database, "matches")) == 2
    meta = archive_info(archive)
    technical = meta["technical_data"]
    assert technical["counts"] == dict(
        apif_fixture_index=2, match_stats=0, match_events=0, match_lineups=0
    )
    assert technical["row_shards"] == []
    assert "checksum_asset" not in technical
    assert release.uploads == ["stats-raw-index.zip", "SHA256SUMS-raw-index"]
    assert not (database.parent / "stats_assets/SHA256SUMS-index").exists()

    from zucai.cloud_feeds import main
    from zucai.stats_rows import restore_rows

    restored = tmp_path / "restored/feeds.db"
    restore_feeds(archive, restored)
    assert table(restored, "apif_fixture_index") == table(database, "apif_fixture_index")
    assert restore_rows(restored, archive, release=release) == dict(
        match_stats=0, match_events=0, match_lineups=0
    )
    assert release.downloads == []
    verify_dir = tmp_path / "verify"
    verify_dir.mkdir()
    monkeypatch.chdir(verify_dir)
    monkeypatch.setattr(
        sys, "argv", ["feeds", "verify", "--archive", str(archive), "--run-id", "verify-index"]
    )
    main()
    assert table(verify_dir / "data/zucai.db", "apif_fixture_index") == table(
        database, "apif_fixture_index"
    )
    again = export_feeds(restored, tmp_path / "again", date(2026, 11, 4), "again")
    assert again["technical_data"] == technical


def test_fd_entry_ingests_free_file_without_api_requests(batch, monkeypatch):
    from zucai.sources.football_data import FootballDataCoUk

    database, archive, release, client = batch
    data = json.loads((FIX / "t31/store.json").read_text())
    engine = get_engine(database)
    with session_scope(engine) as session:
        session.add(Match(**data["matches"][0]))
    engine.dispose()
    export_feeds(database, archive, date(2026, 11, 4), "with-fd")
    monkeypatch.setattr(FootballDataCoUk.limiter, "wait", lambda: None)
    with responses.RequestsMock() as http:
        http.get(
            "https://www.football-data.co.uk/mmz4281/2425/E0.csv",
            body=(FIX / "t31/fd.csv").read_bytes(),
        )
        result = collect_batch(
            database,
            archive,
            plan("E0", "2024", "fd", NOW),
            release=release,
            run_id="fd",
            now=NOW,
            client_factory=client,
        )
        assert result["requests"] == 0 and len(http.calls) == 1, result
    assert result["downloaded_files"] == 1 and result["inserted"] == 16, result
    with responses.RequestsMock() as http:
        result = collect_batch(
            database,
            archive,
            plan("E0", "2024", "fd", NOW),
            release=release,
            run_id="fd2",
            now=NOW,
            client_factory=client,
        )
        assert result["downloaded_files"] == result["inserted"] == len(http.calls) == 0, result


def test_index_with_audited_rejection_is_complete_and_resume_does_not_request_again(
    batch, monkeypatch, tmp_path
):
    from zucai import stats_batch, stats_context
    from zucai.stats_storage import read_audit

    database, archive, release, client = batch
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM apif_fixture_index")
        db.execute("DELETE FROM results WHERE match_id='jc:2'")
        db.execute("DELETE FROM matches WHERE match_id='jc:2'")
    export_feeds(database, archive, date(2026, 11, 4), "seed-unmatched")
    config = tmp_path / "config/team_aliases"
    config.mkdir(parents=True)
    (config / "api_football.json").write_bytes((FIX / "t31/aliases.json").read_bytes())
    monkeypatch.setattr(stats_batch, "CONFIG_ROOT", config.parent)
    monkeypatch.setattr(stats_context, "CONFIG_ROOT", config.parent)
    doc = dict(
        get="fixtures",
        parameters={"league": "2", "season": "2024"},
        errors=[],
        results=2,
        paging={"current": 1, "total": 1},
        response=DATA["fixtures"],
    )

    def collect(run_id):
        return collect_batch(
            database,
            archive,
            plan("2", "2024", "index", NOW),
            release=release,
            run_id=run_id,
            now=NOW,
            client_factory=client,
            resume=True,
        )

    with responses.RequestsMock() as http:
        http.get(api_football.API + "/fixtures?league=2&season=2024", json=doc)
        first = collect("rejected-index")
        assert len(http.calls) == 1
    assert first["status"] == "complete" and first["linked_existing"] == first["rejected_rows"] == 1
    assert archive_info(archive)["collection_status"] == "complete"
    records = json.loads((database.parent / "state/stats_backfill_cursor.json").read_text())[
        "records"
    ]
    row = next(iter(records.values()))
    assert row["outcome"] == "ok" and row["rejected_rows"] == row["attempts"] == 1
    coverage = first["coverage"]["rows"][0]
    assert [coverage[k] for k in ("valid", "failed", "rejected_rows")] == [1, 0, 1]
    audit = read_audit(database.parent / "state/audit/stats_link_audit.csv")
    assert len(audit) == 1 and audit[0]["reason"] == "候选不全"
    with responses.RequestsMock() as http:
        second = collect("resume-rejected-index")
        assert len(http.calls) == second["requests"] == 0
    assert (
        second["status"] == "complete" and second["coverage"]["rows"] == first["coverage"]["rows"]
    )


@pytest.mark.parametrize("invalid", [False, True])
def test_fd_unmatched_rows_are_counted_separately_but_parse_failure_is_partial(
    batch, monkeypatch, invalid
):
    from zucai.sources.football_data import FootballDataCoUk
    from zucai.stats_storage import read_audit

    database, archive, release, client = batch
    monkeypatch.setattr(FootballDataCoUk.limiter, "wait", lambda: None)

    def collect(run_id):
        return collect_batch(
            database,
            archive,
            plan("E0", "2024", "fd", NOW),
            release=release,
            run_id=run_id,
            now=NOW,
            client_factory=client,
        )

    with responses.RequestsMock() as http:
        http.get(
            "https://www.football-data.co.uk/mmz4281/2425/E0.csv",
            body=RUNTIME["non_json_error"] if invalid else (FIX / "t31/fd.csv").read_bytes(),
        )
        first = collect("fd-rejected")
    assert first["status"] == ("partial" if invalid else "complete")
    assert archive_info(archive)["collection_status"] == first["status"]
    coverage = first["coverage"]["rows"][0]
    assert coverage["failed"] == int(invalid)
    assert coverage["rejected_rows"] == int(not invalid)
    if not invalid:
        assert (
            read_audit(database.parent / "state/audit/stats_ingest_audit.csv")[0]["reason"]
            == "候选不全"
        )
        with responses.RequestsMock() as http:
            second = collect("fd-resume")
            assert len(http.calls) == second["downloaded_files"] == 0
        assert second["status"] == "complete"


@pytest.mark.parametrize("failure", ["response_errors", "invalid_json", "http_error"])
def test_index_response_failures_remain_partial(batch, failure):
    database, archive, release, client = batch
    options = dict(body=RUNTIME["non_json_error"], status=503 if failure == "http_error" else 200)
    if failure == "response_errors":
        doc = dict(
            get="fixtures",
            parameters={"league": "2", "season": "2024"},
            errors=json.loads((FIX / "t30/cases.json").read_text())["errors"]["parameter"],
            results=0,
            response=[],
        )
        options = dict(json=doc)
    with responses.RequestsMock() as http:
        http.get(api_football.API + "/fixtures?league=2&season=2024", **options)
        result = collect_batch(
            database,
            archive,
            plan("2", "2024", "index", NOW),
            release=release,
            run_id="failed-index",
            now=NOW,
            client_factory=client,
        )
        assert len(http.calls) == 1
    assert result["status"] == archive_info(archive)["collection_status"] == "partial"
    assert result["linked_existing"] == 0
