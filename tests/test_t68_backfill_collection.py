"""Real offline CLI runs keep historical backfills separate from daily collection."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest
import responses

from test_cloud_apis import environment as environment
from test_cloud_apis import mock_apis, run_apis
from test_cloud_backfill import robots
from test_cloud_cups import mock_season
from test_cloud_feeds import mock_calendar, mock_result_day, run_cloud_sync, seed_cloud_archive
from test_daily_digest import NOW, snapshot
from test_daily_digest import digest_archive as digest_archive
from test_t22_backfill import mock_paid_season, paid_status
from test_t61_catalogue_backfill import catalogue
from test_t61_fd_catalogue_backfill import catalogue as fd_catalogue
from zucai.cloud_feeds import archive_info, export_feeds, main, restore_feeds
from zucai.daily_digest import _anomaly_sections

FIXTURES = Path(__file__).parents[1] / "data/fixtures"


def set_collection(archive, collection):
    path = archive / "feed_manifest.json"
    doc = json.loads(path.read_bytes())
    doc["collection"] = collection
    path.write_text(json.dumps(doc))


def historical(provider, operation="backfill"):
    return dict(
        source=provider,
        operation=operation,
        status="partial",
        run_id="historical",
        suspected_duplicates=523,
        rejected_rows=7,
        errors=["historical backfill only"],
    )


@pytest.mark.parametrize("provider", ["apif", "fdorg", "fd"])
@pytest.mark.parametrize("use_catalogue", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_backfill_cli_preserves_daily_rows_and_writes_own_summary(
    environment, tmp_path, monkeypatch, provider, use_catalogue, fail
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    daily = {
        name: dict(status="complete", run_id="last-daily", suspected_duplicates=0)
        for name in ("jc", "sfc", "apif", "fdorg", "fd")
    }
    # Another provider's historical partial must not become this operation's status.
    other = "fdorg" if provider != "fdorg" else "apif"
    previous = {**daily, f"{other}_backfill": historical(other)}
    if use_catalogue and provider == "apif":
        paid_status(environment)
    if use_catalogue:
        database = tmp_path / "catalogue/zucai.db"
        restore_feeds(environment, database)
        if provider == "fd":
            fd_catalogue(database.parent, "E0", [2026])
        else:
            catalogue(database.parent, provider)
        export_feeds(database, environment, date(2026, 11, 2), "catalogue-seed")
    set_collection(environment, previous)
    code = {
        "apif": "292" if use_catalogue else "3",
        "fdorg": "PL" if use_catalogue else "CL",
        "fd": "E0",
    }[provider]
    year = (
        2022
        if use_catalogue and provider == "apif"
        else 2026
        if provider == "fd" or use_catalogue
        else 2024
    )
    work = tmp_path / "backfill-run"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))
    monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
    argv = [
        "feeds",
        "backfill",
        "--archive",
        str(environment),
        "--run-id",
        "new-backfill",
        "--source",
        provider,
        "--leagues",
        code,
        "--from",
        str(year),
        "--to",
        str(year),
    ]
    if use_catalogue:
        argv += ["--catalogue", "--max-requests", "2"]
    monkeypatch.setattr(sys, "argv", argv)
    with responses.RequestsMock() as http:
        if provider == "fd":
            robots(http)
            http.get(
                "https://www.football-data.co.uk/mmz4281/2627/E0.csv",
                body="invalid,header\n1,2\n" if fail else (FIXTURES / "t14/fd.csv").read_bytes(),
            )
        elif fail:
            url = (
                f"https://api.football-data.org/v4/competitions/{code}/matches"
                if provider == "fdorg"
                else "https://v3.football.api-sports.io/fixtures"
            )
            http.get(url, status=403, json={"errors": {"access": "offline rejection"}})
        elif provider == "fdorg" and use_catalogue:
            http.get(
                "https://api.football-data.org/v4/competitions/PL/matches",
                json=json.loads((FIXTURES / "t14/fdorg.json").read_bytes()),
            )
        elif provider == "apif" and use_catalogue:
            mock_paid_season(http, code, year)
        else:
            mock_season(http, provider, code, year)
        main()
        assert http.calls
    after = archive_info(environment)
    assert all(after["collection"].get(name) == row for name, row in previous.items())
    report = after["collection"][f"{provider}_backfill"]
    assert report["run_id"] == "new-backfill"
    assert report["operation"] == ("catalogue-backfill" if use_catalogue else "backfill")
    expected_status = "partial" if fail else "complete"
    assert report["status"] == after["collection_status"] == expected_status
    assert report["processed_files"] == (0 if fail else 1)
    summary = (work / "summary.md").read_text()
    assert f"历史回填：{expected_status}" in summary
    assert f"新增 {report['inserted']}" in summary
    restored = tmp_path / "restored/data/zucai.db"
    assert restore_feeds(environment, restored)["collection"] == after["collection"]
    digest = "\n".join(_anomaly_sections(after, {}, {}, "example/repo"))
    assert "523" not in digest and "historical backfill only" not in digest
    if provider == "apif" and fail and not use_catalogue:
        next_summary = tmp_path / "next-sync-summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(next_summary))
        with responses.RequestsMock() as http:
            mock_calendar(http)
            for day in ("2026-10-01", "2026-10-31", "2026-11-01", "2026-11-02"):
                mock_result_day(http, day, [])
            run_cloud_sync(monkeypatch, tmp_path / "next-sync", environment, "2026-11-02")
            assert not any("api-sports.io" in call.request.url for call in http.calls)
        following = archive_info(environment)
        assert following["collection"]["apif"] == daily["apif"]
        assert following["collection"]["apif_backfill"] == report
        assert following["collection_status"] == "complete"
        assert "Football feed archive: complete" in next_summary.read_text()


@pytest.mark.parametrize("operation", ["backfill", "catalogue-backfill"])
@pytest.mark.parametrize("command", ["sync", "apis"])
def test_daily_cli_separates_known_legacy_history_and_ignores_its_partial(
    environment, tmp_path, monkeypatch, operation, command
):
    archive = seed_cloud_archive(tmp_path / "official") if command == "sync" else environment
    previous = {provider: historical(provider, operation) for provider in ("fd", "fdorg", "apif")}
    set_collection(archive, previous)
    with responses.RequestsMock() as http:
        if command == "sync":
            summary = tmp_path / "summary.md"
            monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
            mock_calendar(http)
            for day in ("2026-10-30", "2026-10-31", "2026-11-01"):
                mock_result_day(http, day, [])
            run_cloud_sync(monkeypatch, tmp_path / "sync", archive, "2026-11-01")
        else:
            mock_apis(http)
            work = run_apis(tmp_path, monkeypatch, archive, "daily-api")
            summary = work / "summary.md"
    after = archive_info(archive)
    for provider, row in previous.items():
        assert after["collection"].get(f"{provider}_backfill") == row
        if command == "sync":
            assert provider not in after["collection"]
        else:
            assert after["collection"][provider]["run_id"] == "daily-api"
    assert after["collection_status"] == "complete"
    assert "Football feed archive: complete" in summary.read_text()
    digest = "\n".join(_anomaly_sections(after, {}, {}, "example/repo"))
    assert "523" not in digest and "historical backfill only" not in digest


def test_sync_preserves_unidentifiable_old_daily_value(tmp_path, monkeypatch):
    archive = seed_cloud_archive(tmp_path)
    ambiguous = historical("apif")
    ambiguous.pop("operation")
    set_collection(archive, {"apif": ambiguous, "fd_backfill": historical("fd")})
    with responses.RequestsMock() as http:
        mock_calendar(http)
        for day in ("2026-10-30", "2026-10-31", "2026-11-01"):
            mock_result_day(http, day, [])
        run_cloud_sync(monkeypatch, tmp_path / "sync", archive, "2026-11-01")
    after = archive_info(archive)
    assert after["collection"]["apif"] == ambiguous
    assert "apif_backfill" not in after["collection"]
    assert after["collection_status"] == "partial"


@pytest.mark.parametrize("provider", ["apif", "fdorg", "fd"])
def test_digest_does_not_label_explicit_legacy_backfill_as_daily(provider):
    row = historical(provider)
    text = "\n".join(_anomaly_sections({"collection": {provider: row}}, {}, {}, "example/repo"))
    assert f"{provider}：未记录" in text
    assert "523" not in text and "historical backfill only" not in text


def test_legacy_collision_keeps_both_backfills_without_daily_state():
    from zucai.cloud_feeds import daily_collection, separate_backfill_collection

    old = historical("apif")
    known = {**old, "run_id": "already-separated"}
    original = {"apif": old, "apif_backfill": known}
    separated = separate_backfill_collection(original)
    assert original == {"apif": old, "apif_backfill": known}
    assert len(separated) == 2 and separated["apif_backfill"] == known
    assert old in separated.values()
    assert daily_collection(separated) == {}


@pytest.mark.parametrize("provider", ["apif", "fdorg", "fd"])
@pytest.mark.parametrize("operation", ["backfill", "catalogue-backfill", None])
def test_full_digest_header_and_anomalies_use_same_daily_state(
    digest_archive, tmp_path, provider, operation
):
    from zucai.daily_digest import generate

    record = historical(provider)
    if operation is None:
        record.pop("operation")
    else:
        record["operation"] = operation
    if provider == "fd":
        record.pop("source")  # Legacy ordinary FD reports have no source field.
    set_collection(digest_archive, {provider: record})
    before = snapshot(digest_archive)
    text = generate(digest_archive, tmp_path / "digest", now=NOW).read_text()
    expected = "未记录" if operation else "partial"
    states = next(line for line in text.splitlines() if line.startswith("采集状态："))
    anomaly = next(line for line in text.splitlines() if line.startswith(f"- {provider}："))
    assert f"{provider}={expected}" in states
    assert f"{provider}：{expected}" in anomaly
    assert ("523" in anomaly) == (operation is None)
    assert snapshot(digest_archive) == before
