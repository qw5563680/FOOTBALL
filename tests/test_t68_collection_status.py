"""Offline collection states preserve retryable work and genuine conflicts."""

import json
import sqlite3
from datetime import timedelta

import pytest
import responses

from test_apif_ingest import KEY, WHEN, game, ingest, seed
from test_apif_ingest import db as db
from test_cloud_apis import environment as environment
from test_t22_backfill import mock_paid_season, paid_status
from test_t61_catalogue_backfill import catalogue
from zucai.cloud_apis import _record
from zucai.cloud_catalogue_backfill import RequestBudget, collect_catalogue_backfill
from zucai.cloud_feeds import restore_feeds
from zucai.db import session_scope
from zucai.models import Match, Result


def test_budget_is_specific_and_447_unrequested_scopes_remain_distinct():
    from zucai.request_budget import DeferredRequestBudget

    budget = RequestBudget(200)
    calls, deferred, failures = [], [], []
    for index in range(647):

        def send(index=index):
            calls.append(index)
            if index < 39:
                raise ValueError("fixture validation failure after request")

        try:
            budget.call(send)
        except DeferredRequestBudget:
            deferred.append(index)
        except ValueError:
            failures.append(index)
    assert budget.used == len(calls) == 200
    assert deferred == list(range(200, 647))
    assert failures == list(range(39))


def test_catalogue_budget_does_not_consume_quota_or_checkpoint(environment, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    paid_status(environment)
    database = tmp_path / "budget/zucai.db"
    restore_feeds(environment, database)
    catalogue(database.parent)
    quota_path = database.parent / "state/api_football_quota.json"
    prior_used = json.loads(quota_path.read_bytes())["used"]
    with responses.RequestsMock() as http:
        mock_paid_season(http, "292", 2022)
        report = collect_catalogue_backfill(
            database, "budget", provider="apif", codes="292", max_requests=1
        )
        assert report["requests"] == len(http.calls) == 1
    assert len(report["deferred_scopes"]) == 1
    assert report["deferred_scopes"][0]["reason"] == "deferred_request_budget"
    assert report["failed_scopes"] == report["errors"] == []
    assert report["scope_counts"] == dict(completed=1, partial=0, failed=0, deferred=1, held=0)
    checkpoint = json.loads(
        (database.parent / "state/catalogue/backfill-checkpoint.json").read_bytes()
    )
    assert len(checkpoint["completed"]) == 1
    quota = json.loads((database.parent / "state/api_football_quota.json").read_bytes())
    assert quota["used"] == prior_used + 1


@pytest.mark.parametrize("hours", [-3, 0, 3])
def test_only_unbound_jc_overlap_is_held_and_not_anomaly(db, hours):
    with session_scope(db) as session:
        seed(session, "jc:1", when=WHEN + timedelta(hours=hours))
        session.get(Result, "jc:1").home_goals = 2
        session.get(Result, "jc:1").away_goals = 1
        item = ingest(session, game(), mapping={})
        assert item["held_unbound"] == 1
        assert item["suspected_duplicates"] == item["rejected_rows"] == 0
        assert item["held"][0]["reason_code"] == "held_unbound"
        assert session.get(Match, KEY) is None
    report = dict(status="complete", errors=[])
    _record(report, item)
    assert report["status"] == "complete" and report["errors"] == []


@pytest.mark.parametrize("risk", ["multiple", "occupied", "score", "reverse", "apif", "row"])
def test_unbound_does_not_hide_real_conflicts(db, risk):
    with session_scope(db) as session:
        seed(session, "jc:1", counterpart="apif:other" if risk == "occupied" else None)
        if risk != "score":
            session.get(Result, "jc:1").home_goals = 2
            session.get(Result, "jc:1").away_goals = 1
        mapping = {}
        if risk == "multiple":
            seed(session, "jc:2", home="南川", away="西山")
        elif risk == "apif":
            seed(session, "apif:other")
        elif risk == "reverse":
            mapping = {"日职": {"551": {"canonical": "东岸", "status": "approved"}}}
        item = ingest(session, game(full=(-1, 1)) if risk == "row" else game(), mapping=mapping)
        assert item.get("held_unbound", 0) == 0
        assert item["suspected_duplicates"] + item["rejected_rows"] == 1
        report = dict(status="complete", errors=[], held_unbound=5)
        _record(report, item)
        assert report["status"] == "partial" and report["errors"]


def test_held_backfill_never_checkpoints_and_audit_survives_restore(
    environment, tmp_path, monkeypatch
):
    from datetime import date

    from zucai.cloud_feeds import export_feeds
    from zucai.daily_digest import _anomaly_sections

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    paid_status(environment)
    database = tmp_path / "held/zucai.db"
    restore_feeds(environment, database)
    with sqlite3.connect(database) as con:
        con.execute("UPDATE matches SET league='韩职',kickoff_utc='2022-05-18T12:00:00.000000Z'")
    catalogue(database.parent)
    with responses.RequestsMock() as http:
        mock_paid_season(http, "292", 2022)
        report = collect_catalogue_backfill(
            database, "held", provider="apif", codes="292", first="2022", last="2022"
        )
    assert report["status"] == "complete"
    assert report["held_unbound"] == 1
    assert report["scopes"][0]["status"] == "held"
    checkpoint = json.loads(
        (database.parent / "state/catalogue/backfill-checkpoint.json").read_bytes()
    )
    assert checkpoint["completed"] == {}
    text = "\n".join(_anomaly_sections({"collection": {"apif": report}}, {}, {}, "example/repo"))
    assert "apif：未记录" in text
    assert "未绑定暂缓 1（不计异常）" not in text
    archive = tmp_path / "held-export"
    exported = export_feeds(database, archive, date(2026, 11, 2), "held")
    key = "state/audit/apif_held_unbound.csv"
    assert key in exported["files"]
    restored = tmp_path / "held-restored/zucai.db"
    restore_feeds(archive, restored)
    assert (restored.parent / key).read_bytes() == (database.parent / key).read_bytes()


def test_http_failure_is_not_budget_deferral(environment, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    paid_status(environment)
    database = tmp_path / "http/zucai.db"
    restore_feeds(environment, database)
    catalogue(database.parent)
    with responses.RequestsMock() as http:
        http.get(
            "https://v3.football.api-sports.io/fixtures",
            status=500,
            json={"error": "offline failure"},
        )
        report = collect_catalogue_backfill(
            database, "http", provider="apif", codes="292", max_requests=2
        )
    assert len(report["failed_scopes"]) == 2
    assert report["deferred_scopes"] == []
    assert all("HTTP 500" in row["reason"] for row in report["failed_scopes"])


def test_normal_actions_summary_displays_held_and_duplicate_counts(
    environment, tmp_path, monkeypatch
):
    from datetime import date

    from test_cloud_apis import mock_apis, run_apis
    from zucai.cloud_feeds import export_feeds

    database = tmp_path / "summary-seed/zucai.db"
    restore_feeds(environment, database)
    with sqlite3.connect(database) as con:
        con.execute("UPDATE matches SET league='日职',kickoff_utc='2026-11-01T00:00:00.000000Z'")
    archive = tmp_path / "summary-archive"
    export_feeds(database, archive, date(2026, 11, 2), "summary-seed")
    with responses.RequestsMock() as http:
        mock_apis(http)
        work = run_apis(tmp_path, monkeypatch, archive, "summary-held")
    text = (work / "summary.md").read_text()
    assert "| 拒绝 | 疑似重复 | 未绑定暂缓 | 请求/文件 |" in text
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    report = manifest["collection"]["apif"]
    assert report["status"] == "complete" and report["held_unbound"] == 1
    row = next(line for line in text.splitlines() if line.startswith("| apif |"))
    cells = [cell.strip() for cell in row.split("|")]
    assert cells[5:8] == ["0", "0", "1"]


@pytest.mark.parametrize("rejected,display", [(1, "1"), (None, "未记录")])
def test_daily_digest_renders_rejected_count_separately_from_held(rejected, display):
    from zucai.daily_digest import _anomaly_sections

    report = dict(
        status="partial",
        suspected_duplicates=0,
        held_unbound=1,
        errors=["单行拒绝或疑似重复，详见 state/audit"],
    )
    if rejected is not None:
        report["rejected_rows"] = rejected
    rendered = "\n".join(
        _anomaly_sections({"collection": {"apif": report}}, {}, {}, "example/repo")
    )
    assert f"单行拒收 {display}" in rendered
    assert "未绑定暂缓 1（不计异常）" in rendered
