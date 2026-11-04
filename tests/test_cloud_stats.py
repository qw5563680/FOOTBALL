"""The Actions entry point validates inputs before constructing any network client."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zucai import cloud_stats
from zucai.stats_schedule import coverage, empty_cursor, record, unit

FIX = Path(__file__).parents[1] / "data/fixtures/t32/runtime.json"


@pytest.mark.parametrize(
    "mode,event,actions",
    [
        ("backfill", "schedule", "true"),
        ("daily", "pull_request", "true"),
        ("daily", "schedule", "false"),
    ],
)
def test_only_authorized_actions_can_collect(monkeypatch, mode, event, actions):
    monkeypatch.setenv("GITHUB_ACTIONS", actions)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setattr(
        cloud_stats, "ReleaseStore", lambda: pytest.fail("must not create network client")
    )
    assert cloud_stats.main([mode, "--archive", "missing", "--run-id", "run"]) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["--max-requests", "7001"],
        ["--endpoints", "odds"],
        ["--leagues", "1"],
        ["--seasons", "2021"],
        ["--run-id", "../invalid"],
    ],
)
def test_invalid_selection_stops_before_restore_or_client(monkeypatch, arguments):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setattr(cloud_stats, "ReleaseStore", lambda: pytest.fail("network client created"))
    monkeypatch.setattr(cloud_stats, "restore_feeds", lambda *a: pytest.fail("restore started"))
    assert (
        cloud_stats.main(["backfill", "--archive", "missing", "--run-id", "run", *arguments]) == 1
    )


@pytest.mark.parametrize(
    "mode,missing,failed",
    [("backfill", True, False), ("daily", False, False), ("daily", False, True)],
)
def test_cli_restore_selection_and_archive_failure_exit(
    tmp_path, monkeypatch, mode, missing, failed
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv(
        "GITHUB_EVENT_NAME", "workflow_dispatch" if mode == "backfill" else "schedule"
    )
    path = tmp_path / "feeds.db"
    if not missing:
        path.touch()
    restored, calls = [], []
    monkeypatch.setattr(cloud_stats, "restore_feeds", lambda a, d: restored.append((a, d)))
    monkeypatch.setattr(
        cloud_stats,
        "ReleaseStore",
        lambda: SimpleNamespace(session=SimpleNamespace(close=lambda: None)),
    )

    def collect(db, archive, selected, **kwargs):
        calls.append((selected, kwargs))
        return dict(
            status="partial" if failed else "complete",
            requests=0,
            inserted=0,
            archive_failed=failed,
        )

    monkeypatch.setattr(cloud_stats, "collect_batch", collect)
    assert cloud_stats.main(
        [mode, "--archive", str(tmp_path / "archive"), "--db", str(path), "--run-id", "run"]
    ) == int(failed)
    assert bool(restored) == missing and len(calls) == 1
    selection, kwargs = calls[0]
    assert kwargs["daily"] == (mode == "daily")
    assert kwargs["max_requests"] == (300 if mode == "daily" else 7000)
    assert ("index" in selection["endpoints"]) == (mode == "backfill")


def test_summary_reports_frozen_coverage_and_partial(tmp_path, monkeypatch):
    from datetime import datetime

    now = datetime.fromisoformat(json.loads(FIX.read_text())["now"])
    cursor = empty_cursor()
    record(
        cursor,
        unit("statistics", 39, 2024, "102", league="英超"),
        "empty",
        now,
        requested=True,
        received=True,
    )
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    cloud_stats.write_summary(
        dict(
            status="partial",
            requests=1,
            remaining_quota=7499,
            coverage=coverage(cursor, now),
            errors=["等待补抓"],
        )
    )
    text = path.read_text()
    assert "| 英超 | 2024 | statistics | history | 1 | 1 | 0 | 1 | 0 | 0 |" in text
    assert "剩余配额 7499" in text and "等待补抓" in text
