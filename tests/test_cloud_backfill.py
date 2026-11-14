"""Offline FD history orchestration with unchanged parser and import policy."""

import csv
import hashlib
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest
import requests
import responses

from zucai.cloud_feeds import archive_info, export_feeds, main, restore_feeds
from zucai.db import get_engine, init_db
from zucai.sources.base import RateLimiter
from zucai.sources.football_data import LEAGUES_PATH, read_json

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "data/fixtures/t18"
HOST = "https://www.football-data.co.uk"


def test_default_scope_has_all_fifteen_verified_leagues_and_sixty_csvs():
    from zucai.cloud_backfill import backfill_scopes

    scopes = backfill_scopes(read_json(LEAGUES_PATH))
    assert len(scopes) == 60
    assert len({code for code, _ in scopes}) == 15
    assert not any(code == "D2" for code, _ in scopes)
    assert [s["seasons"] for c, s in scopes if c == "E0"] == [
        ["2122"],
        ["2223"],
        ["2324"],
        ["2425"],
        ["2526"],
        ["2627"],
    ]
    assert [s for c, s in scopes if c == "JPN"] == [
        {"country": "JPN", "from_season": 2021, "to_season": 2026}
    ]


@pytest.mark.parametrize(("first", "last"), [("2425", "2526"), ("2024", "2025")])
def test_scope_filters_map_season_codes_and_years_to_the_same_start_years(first, last):
    from zucai.cloud_backfill import backfill_scopes

    scopes = backfill_scopes(read_json(LEAGUES_PATH), "JPN,E0", first, last)
    assert scopes == [
        ("E0", {"div": "E0", "seasons": ["2425"]}),
        ("E0", {"div": "E0", "seasons": ["2526"]}),
        ("JPN", {"country": "JPN", "from_season": 2024, "to_season": 2025}),
    ]


@pytest.mark.parametrize(
    ("codes", "first", "last"),
    [
        ("D2", None, None),
        ("ZZZ", None, None),
        ("E0,", None, None),
        ("E0,E0", None, None),
        ("", None, None),
        (None, "2123", "2627"),
        (None, "2627", "2122"),
        (None, "foo", None),
        (None, "21", None),
    ],
)
def test_invalid_scope_is_rejected_before_network(codes, first, last):
    from zucai.cloud_backfill import backfill_scopes

    with pytest.raises(ValueError):
        backfill_scopes(read_json(LEAGUES_PATH), codes, first, last)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    database = tmp_path / "seed.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO teams VALUES (1,'阿森纳','[]'),(2,'考文垂','[]')")
        db.execute(
            "INSERT INTO matches (match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES "
            "('jc:t18','jc','英超',1,2,'2026-08-21T14:00:00.000000Z','finished')"
        )
        db.execute(
            "INSERT INTO results (match_id,home_goals,away_goals,available_utc,available_source) "
            "VALUES ('jc:t18',3,1,'2026-08-21T17:00:00.000000Z','captured')"
        )
    target = tmp_path / "archive"
    export_feeds(database, target, date(2026, 11, 1), "seed")
    tick = [100.0]
    monkeypatch.setattr("zucai.sources.base.time.monotonic", lambda: tick[0])
    monkeypatch.setattr(
        "zucai.sources.base.time.sleep", lambda seconds: tick.__setitem__(0, tick[0] + seconds)
    )
    monkeypatch.setattr("zucai.sources.football_data.FootballDataCoUk.limiter", RateLimiter(2))
    return target


def run_backfill(archive, work, monkeypatch, *extra):
    work.mkdir()
    monkeypatch.chdir(work)
    # cloud_feeds sets this process variable; register it for fixture teardown too.
    monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))
    monkeypatch.setattr(
        sys, "argv", ["feeds", "backfill", "--archive", str(archive), "--run-id", work.name, *extra]
    )
    main()
    return archive_info(archive)["collection"]["fd_backfill"]


def robots(api, body=None):
    api.get(HOST + "/robots.txt", body=body or (FIXTURES / "robots.txt").read_bytes())


def csv_response(api, path, fixture="main.csv", *, redirect=False):
    url = HOST + path
    if redirect:
        target = url.replace("//www.", "//")
        api.get(url, status=302, headers={"Location": target})
        url = target
    api.get(url, body=(FIXTURES / fixture).read_bytes())


def test_restore_backfill_export_is_idempotent_and_roundtrips_exact_rows(
    archive, tmp_path, monkeypatch
):
    initial = archive_info(archive)
    config_sha = hashlib.sha256((ROOT / "data/model/params.json").read_bytes()).hexdigest()
    for name in ("first", "second"):
        with responses.RequestsMock() as api:
            robots(api)
            csv_response(api, "/mmz4281/2122/E0.csv", redirect=True)
            csv_response(api, "/new/JPN.csv", "combined.csv")
            result = run_backfill(
                archive,
                tmp_path / name,
                monkeypatch,
                "--leagues",
                "E0,JPN",
                "--from",
                "2122",
                "--to",
                "2122",
            )
            assert len(api.calls) == 4
        assert result["status"] == "complete"
        assert result["inserted"] == (3 if name == "first" else 0)
        assert result["csv_requests"] == 3 and result["robots_requests"] == 1
        assert result["csv_min_interval_seconds"] >= 2
        assert result["leagues"]["E0"]["results_after"] == 2
        assert result["leagues"]["JPN"]["results_after"] == 1
        assert result["leagues"]["E0"]["results_added"] == (2 if name == "first" else 0)
        assert result["robots"]["allowed"] is True
        assert result["robots"]["sha256"]
        raw = tmp_path / name / "data/raw"
        assert len(list(raw.rglob("*.csv"))) == 3  # Includes archived redirect response.
        for path in raw.rglob("*.csv"):
            meta = json.loads(path.with_suffix(".csv.meta.json").read_bytes())
            assert meta["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        audit = list(csv.DictReader((archive / "state/audit/fd_backfill_requests.csv").open()))
        assert len(audit) == (4 if name == "first" else 8)
        assert all(float(r["interval_seconds"]) >= 2 for r in audit if r["interval_seconds"])
        summary = (tmp_path / name / "summary.md").read_text()
        assert "一次性历史回填" in summary and "最短" in summary and "E0" in summary
    final = archive_info(archive)
    assert final["through_beijing"] == initial["through_beijing"]
    assert final["sources"]["jc"] == initial["sources"]["jc"]
    assert final["sources"]["fdorg"] == initial["sources"]["fdorg"]
    assert final["sources"]["apif"] == initial["sources"]["apif"]
    assert final["counts"]["matches"] == final["counts"]["results"] == 4
    assert final["counts"]["closing_odds"] == 3
    restored = tmp_path / "restored/data/zucai.db"
    restore_feeds(archive, restored)
    with sqlite3.connect(restored) as a, sqlite3.connect(tmp_path / "second/data/zucai.db") as b:
        for table in ("teams", "matches", "results", "odds", "closing_odds"):
            assert (
                a.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                == b.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            )
        assert a.execute(
            "SELECT DISTINCT available_source FROM results r JOIN matches m USING(match_id) "
            "WHERE m.source='fd'"
        ).fetchall() == [("assumed",)]
    assert hashlib.sha256((ROOT / "data/model/params.json").read_bytes()).hexdigest() == config_sha


@pytest.mark.parametrize("failure", ["404", "network", "header"])
def test_single_file_failure_is_partial_and_later_seasons_continue(
    archive, tmp_path, monkeypatch, failure
):
    with responses.RequestsMock() as api:
        robots(api)
        url = HOST + "/mmz4281/2122/E0.csv"
        if failure == "404":
            api.get(url, status=404)
        elif failure == "network":
            api.get(url, body=requests.ConnectionError("offline connection failed"))
        else:
            api.get(url, body="wrong,header\n1,2\n")
        csv_response(api, "/mmz4281/2223/E0.csv")
        result = run_backfill(
            archive,
            tmp_path / failure,
            monkeypatch,
            "--leagues",
            "E0",
            "--from",
            "2122",
            "--to",
            "2223",
        )
    assert result["status"] == archive_info(archive)["collection_status"] == "partial"
    assert result["inserted"] == 2 and result["csv_requests"] == 2
    assert result["failed_scopes"][0]["scope"] == "E0/2122"
    assert result["scopes"][1]["status"] == "complete"
    assert result["csv_min_interval_seconds"] >= 2


def test_rejected_and_suspected_rows_are_partial_and_official_result_is_preserved(
    archive, tmp_path, monkeypatch
):
    with responses.RequestsMock() as api:
        robots(api)
        csv_response(api, "/mmz4281/2627/E0.csv", "partial.csv")
        result = run_backfill(
            archive,
            tmp_path / "partial",
            monkeypatch,
            "--leagues",
            "E0",
            "--from",
            "2627",
            "--to",
            "2627",
        )
    assert result["status"] == "partial"
    assert result["inserted"] == 1 and result["linked_existing"] == 1
    assert result["rejected_rows"] == result["suspected_duplicates"] == 1
    assert (archive / "state/audit/fd_rejected_rows.csv").is_file()
    assert (archive / "state/audit/fd_suspected_duplicates.csv").is_file()
    with sqlite3.connect(tmp_path / "partial/data/zucai.db") as db:
        assert db.execute(
            "SELECT home_goals,available_source FROM results WHERE match_id='jc:t18'"
        ).fetchone() == (3, "captured")


def test_robots_denial_stops_before_any_csv_and_leaves_archive_unchanged(
    archive, tmp_path, monkeypatch
):
    before = {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    with responses.RequestsMock() as api:
        robots(api, "User-agent: ZucaiResearch\nDisallow: /mmz4281/\n")
        with pytest.raises(ValueError, match="robots"):
            run_backfill(archive, tmp_path / "denied", monkeypatch, "--leagues", "E0")
        assert len(api.calls) == 1
    assert {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("status", [404, 503])
def test_unavailable_robots_does_not_start_csv_requests(archive, tmp_path, monkeypatch, status):
    with responses.RequestsMock() as api:
        api.get(HOST + "/robots.txt", status=status)
        with pytest.raises(requests.HTTPError):
            run_backfill(archive, tmp_path / f"robots-{status}", monkeypatch, "--leagues", "E0")
        assert len(api.calls) == 1  # No automatic retries bypassing the measured limiter.
    assert archive_info(archive)["run"] == "seed"


def test_backfill_preserves_existing_frozen_forecasts(archive, tmp_path, monkeypatch):
    from test_forecast_archive import staged_run
    from zucai.forecast_archive import publish_forecasts, verify_forecasts

    stage, run = staged_run(tmp_path / "stage", "previous-forecast")
    manifest = publish_forecasts(archive, stage, run)
    frozen = {
        name: (archive / name).read_bytes()
        for name in [*manifest["files"], "forecast_manifest.json"]
    }
    with responses.RequestsMock() as api:
        robots(api)
        csv_response(api, "/mmz4281/2122/E0.csv")
        run_backfill(
            archive,
            tmp_path / "with-forecast",
            monkeypatch,
            "--leagues",
            "E0",
            "--from",
            "2122",
            "--to",
            "2122",
        )
    assert verify_forecasts(archive) == manifest
    assert all((archive / name).read_bytes() == raw for name, raw in frozen.items())


@pytest.mark.parametrize("run_id", ["bad..id", "bad.", "../bad", ".bad"])
def test_invalid_run_id_fails_before_requests_or_imports(archive, tmp_path, monkeypatch, run_id):
    with responses.RequestsMock() as api:
        with pytest.raises(ValueError, match="run ID"):
            run_backfill(
                archive, tmp_path / "invalid-id", monkeypatch, "--run-id", run_id, "--leagues", "E0"
            )
        assert len(api.calls) == 0
    assert archive_info(archive)["run"] == "seed"
