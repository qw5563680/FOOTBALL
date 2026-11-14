"""Real FD import/export code over synthetic, intercepted HTTP fixture bytes only.

Production FD catalogue metadata has no inferred years. Explicit years below are
synthetic test setup, not new access evidence or production configuration changes.
"""

import hashlib
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest
import responses

from test_cloud_apis import environment as environment
from zucai.cloud_catalogue_backfill import catalogue_scopes, collect_catalogue_backfill
from zucai.cloud_feeds import archive_info, export_feeds, main, restore_feeds
from zucai.league_registry import load_registry

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data/fixtures"
HOST = "https://www.football-data.co.uk"


@pytest.fixture
def database(environment, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    target = tmp_path / "fd-catalogue/zucai.db"
    restore_feeds(environment, target)
    return target


def catalogue(root, code, years):
    """Add only explicitly selected synthetic years to the checked-in FD fixture."""
    document = json.loads((FIXTURES / "t61/backfill-catalogue.json").read_bytes())["fd"]
    row = document["leagues"][0]
    assert row["seasons"] == []  # The real default must not invent historic ranges.
    config = load_registry()["providers"]["fd"][code]["config"]
    row.update(
        id=code,
        code=code,
        name=config["league"],
        seasons=[
            {"year": year, "fixtures": None, "results": None, "access": "unverified"}
            for year in years
        ],
    )
    payload = (json.dumps(document) + "\n").encode()
    folder = root / "state/catalogue/offline-fd-catalogue"
    folder.mkdir(parents=True)
    (folder / "fd.json").write_bytes(payload)
    (folder.parent / "latest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {
                    "fd": {
                        "path": "state/catalogue/offline-fd-catalogue/fd.json",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                },
            }
        )
    )


def robots(http):
    http.get(HOST + "/robots.txt", body=(FIXTURES / "t18/robots.txt").read_bytes())


def fd_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT m.match_id,m.league,m.kickoff_utc,r.home_goals,r.away_goals,"
            "r.available_utc,r.available_source FROM matches m "
            "JOIN results r USING(match_id) WHERE m.source='fd' ORDER BY m.match_id"
        ).fetchall()


def test_fd_catalogue_budget_import_archive_resume_and_cli_no_resume(
    database, tmp_path, monkeypatch
):
    catalogue(database.parent, "E0", [2026])
    assert catalogue_scopes(database.parent, "fd", "E0")["scopes"] == [
        ("E0", {"div": "E0", "seasons": ["2627"]})
    ]
    with responses.RequestsMock() as http:
        robots(http)
        limited = collect_catalogue_backfill(
            database, "fd-robots-only", provider="fd", codes="E0", max_requests=1
        )
        assert limited["status"] == "partial"
        assert limited["requests"] == limited["robots_requests"] == len(http.calls) == 1
        assert limited["csv_requests"] == limited["inserted"] == limited["processed_files"] == 0
        assert http.calls[0].request.url == HOST + "/robots.txt"
        assert limited["failed_scopes"] == []
        assert limited["deferred_scopes"][0]["reason"] == "deferred_request_budget"
    checkpoint = database.parent / "state/catalogue/backfill-checkpoint.json"
    assert json.loads(checkpoint.read_bytes())["completed"] == {}
    assert fd_rows(database) == []

    original = (FIXTURES / "t14/fd.csv").read_bytes()
    url = HOST + "/mmz4281/2627/E0.csv"
    with responses.RequestsMock() as http:
        robots(http)
        http.get(url, body=original)
        imported = collect_catalogue_backfill(
            database, "fd-import", provider="fd", codes="E0", max_requests=2
        )
        assert imported["status"] == "complete"
        assert imported["requests"] == len(http.calls) == 2
        assert imported["robots_requests"] == imported["csv_requests"] == 1
        assert imported["inserted"] == imported["processed_files"] == 1
    rows = fd_rows(database)
    assert len(rows) == 1
    assert rows[0][1:] == (
        "英超",
        "2026-11-01T12:00:00.000000Z",
        2,
        1,
        "2026-11-01T15:00:00.000000Z",
        "assumed",
    )
    completed = json.loads(checkpoint.read_bytes())["completed"]
    assert len(completed) == 1
    proof = next(iter(completed.values()))
    csv_path = database.parent / proof["raw_path"]
    assert csv_path.read_bytes() == original
    assert proof["raw_sha256"] == hashlib.sha256(original).hexdigest()
    sidecar = json.loads(csv_path.with_suffix(".csv.meta.json").read_bytes())
    assert sidecar["url"] == url and sidecar["status_code"] == 200

    with responses.RequestsMock() as http:
        resumed = collect_catalogue_backfill(database, "fd-resume", provider="fd", codes="E0")
        assert resumed["status"] == "complete"
        assert resumed["resumed_scopes"] == 1
        assert resumed["requests"] == resumed["inserted"] == len(http.calls) == 0

    archive = tmp_path / "fd-export"
    manifest = export_feeds(database, archive, date(2026, 11, 2), "fd-roundtrip")
    raw_files = [key for key in manifest["files"] if "/backfill/" in key]
    assert proof["raw_path"] in raw_files
    assert proof["raw_path"] + ".meta.json" in raw_files
    assert any(key.endswith(".txt") for key in raw_files)  # The robots originals survive too.
    restored = tmp_path / "fd-restored/zucai.db"
    restore_feeds(archive, restored)
    for key in [*raw_files, "state/catalogue/backfill-checkpoint.json"]:
        payload = (database.parent / key).read_bytes()
        assert (archive / key).read_bytes() == (restored.parent / key).read_bytes() == payload
        assert manifest["files"][key] == hashlib.sha256(payload).hexdigest()
    assert fd_rows(restored) == rows
    with responses.RequestsMock() as http:
        resumed = collect_catalogue_backfill(restored, "fd-restored", provider="fd", codes="E0")
        assert resumed["requests"] == len(http.calls) == 0 and resumed["resumed_scopes"] == 1

    # Exercise the actual --no-resume CLI plumbing, then its ordinary export/restore checks.
    work = tmp_path / "fd-cli-replay"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "feeds",
            "backfill",
            "--archive",
            str(archive),
            "--source",
            "fd",
            "--catalogue",
            "--leagues",
            "E0",
            "--from",
            "2026",
            "--to",
            "2026",
            "--max-requests",
            "2",
            "--no-resume",
            "--run-id",
            "fd-cli-replay",
        ],
    )
    with responses.RequestsMock() as http:
        robots(http)
        http.get(url, body=original)
        main()
        replay = archive_info(archive)["collection"]["fd_backfill"]
        assert replay["status"] == "complete"
        assert replay["requests"] == len(http.calls) == 2
        assert replay["resumed_scopes"] == replay["inserted"] == 0
        assert replay["unchanged"] == 1
    assert fd_rows(work / "data/zucai.db") == rows


def test_fd_candidate_d2_is_rejected_even_with_an_explicit_catalogue_year(database):
    catalogue(database.parent, "D2", [2026])
    before = database.read_bytes()
    with responses.RequestsMock() as http:
        with pytest.raises(ValueError, match="verified"):
            collect_catalogue_backfill(database, "fd-d2", provider="fd", codes="D2")
        assert len(http.calls) == 0
    assert database.read_bytes() == before
    assert not (database.parent / "state/catalogue/fd-d2").exists()


def test_fd_combined_country_csv_filters_exact_contiguous_catalogue_years(database):
    catalogue(database.parent, "JPN", [2020, 2021])
    assert catalogue_scopes(database.parent, "fd", "JPN")["scopes"] == [
        ("JPN", {"country": "JPN", "from_season": 2020, "to_season": 2021})
    ]
    with responses.RequestsMock() as http:
        robots(http)
        http.get(HOST + "/new/JPN.csv", body=(FIXTURES / "t18/combined.csv").read_bytes())
        report = collect_catalogue_backfill(
            database, "fd-jpn", provider="fd", codes="JPN", max_requests=2
        )
        assert report["status"] == "complete"
        assert report["requests"] == len(http.calls) == 2
        assert report["inserted"] == 2 and report["rejected_rows"] == 0
    rows = fd_rows(database)
    assert len(rows) == 2 and {row[2][:4] for row in rows} == {"2020", "2021"}
    assert all(row[1] == "日职" and row[-1] == "assumed" for row in rows)
