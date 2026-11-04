"""Actions-only stats probing with fixture HTTP responses and real quota/raw storage."""

import csv
import json
import sqlite3
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
import responses

from test_stats_probe import fixture
from zucai.db import get_engine, init_db
from zucai.sources.api_football import API

NOW = datetime(2026, 11, 4, tzinfo=UTC)


def test_complete_default_scope_fits_500_request_budget():
    from zucai.cloud_stats_probe import DEFAULT_PER_SCOPE, stats_plan

    plan = stats_plan(None, None, DEFAULT_PER_SCOPE, NOW)
    assert plan["per_scope"] == 1 and plan["request_budget"] == 344
    assert len(plan["leagues"]) == 19 and len(plan["scopes"]) == 54


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-stats-placeholder")
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    db = tmp_path / "data/zucai.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    teams = fixture("fixtures")["response"][0]["teams"]
    with sqlite3.connect(db) as con:
        for side in teams.values():
            con.execute(
                "INSERT INTO teams (id,name,aliases) VALUES (?,?,'[]')", (side["id"], side["name"])
            )
        for key, mid in fixture("cases")["known_ids"].items():
            match = next(
                r for r in fixture("fixtures")["response"] if str(r["fixture"]["id"]) == key
            )
            finished = match["fixture"]["status"]["short"] in {"FT", "AET", "PEN"}
            con.execute(
                "INSERT INTO matches (match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status,counterpart_id) VALUES (?,?,?,1,2,?,?,?)",
                (
                    mid,
                    mid.split(":")[0],
                    "欧罗巴",
                    "2024-09-02T18:00:00Z",
                    "finished" if finished else "cancelled",
                    "apif:" + key if mid.startswith("jc:") else None,
                ),
            )
            if finished:
                score = match["score"]
                con.execute(
                    "INSERT INTO results (match_id,home_goals,away_goals,ht_home_goals,"
                    "ht_away_goals,available_utc,available_source) VALUES (?,?,?,?,?,?,?)",
                    (
                        mid,
                        score["fulltime"]["home"],
                        score["fulltime"]["away"],
                        score["halftime"]["home"],
                        score["halftime"]["away"],
                        "2024-09-03T00:00:00Z",
                        "captured",
                    ),
                )
    return db


def register(http, *, code="3", season="2024", sample=(102, 202), half=True, bad=None):
    headers = fixture("cases")["quota_headers"]
    doc = fixture("leagues")
    doc["parameters"]["id"] = code
    doc["response"][0]["league"]["id"] = int(code)
    http.get(API + "/leagues?id=" + code, json=doc, headers=headers)
    if code == "2":
        return
    doc = fixture("fixtures")
    doc["parameters"] = {"league": code, "season": season}
    for row in doc["response"]:
        row["league"].update(id=int(code), season=int(season))
    http.get(API + "/fixtures?" + urlencode(doc["parameters"]), json=doc, headers=headers)
    for fid in sample:
        for endpoint in ("statistics", "events", "lineups", "players"):
            doc = fixture(endpoint)
            doc["parameters"]["fixture"] = str(fid)
            if bad == endpoint:
                doc["errors"] = fixture("cases")["errors"]["plan"]
            http.get(
                API + "/fixtures/" + endpoint + "?fixture=" + str(fid), json=doc, headers=headers
            )
    if half:
        doc = fixture("statistics")
        doc["parameters"] = {"fixture": str(sample[0]), "half": "true"}
        http.get(
            API + "/fixtures/statistics?" + urlencode(doc["parameters"]), json=doc, headers=headers
        )
    doc = fixture("injuries")
    doc["parameters"] = {"league": code, "season": season}
    http.get(API + "/injuries?" + urlencode(doc["parameters"]), json=doc, headers=headers)


def test_collect_archives_all_endpoints_and_leaves_database_unchanged(
    database, tmp_path, monkeypatch
):
    from zucai.cloud_stats_probe import collect_stats_probe, write_stats_probe_summary

    before = database.read_bytes()
    with responses.RequestsMock() as http:
        register(http)
        report = collect_stats_probe(
            database, "stats-one", codes="3", seasons="2024", per_scope=2, now=NOW
        )
        assert len(http.calls) == 12
    assert report["status"] == "complete" and report["requests"] == 12
    assert report["request_budget"] == 12 and report["status_requests"] == 0
    assert database.read_bytes() == before
    rows = list(csv.DictReader((database.parent / "state/audit/apif_stats_probes.csv").open()))
    assert len(rows) == 12
    stats = [
        r
        for r in rows
        if r["endpoint"] == "fixtures/statistics" and '"half"' not in r["parameters"]
    ]
    assert {r["fixture_id"] for r in stats} == {"102", "202"}
    assert {r["period_basis"] for r in stats} == {"FT", "AET"}
    assert next(r for r in stats if r["fixture_id"] == "202")["match_id"] == "jc:fixture-202"
    assert all(len(r["raw_sha256"]) == 64 and float(r["elapsed_ms"]) >= 0 for r in rows)
    sidecars = list((database.parent / "raw/api-football").rglob("*.meta.json"))
    assert len(sidecars) == 12
    for path in sidecars:
        meta = json.loads(path.read_bytes())
        assert meta["quota_headers"]["x-ratelimit-limit"] == "300"
        assert "offline-stats-placeholder" not in path.read_text()
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    write_stats_probe_summary(report)
    text = summary.read_text()
    assert "fixtures/statistics" in text and "2024" in text
    assert "7490" in text and "metadata" in text
    assert "不是全季覆盖率" in text
    assert "有效率" in text and "100.0%" in text


def test_champions_league_only_requests_metadata(database):
    from zucai.cloud_stats_probe import collect_stats_probe

    with responses.RequestsMock() as http:
        register(http, code="2")
        report = collect_stats_probe(
            database, "cl", codes="2", seasons="2022,2024,2026", per_scope=2, now=NOW
        )
        assert len(http.calls) == 1
    assert report["requests"] == report["request_budget"] == 1
    assert report["probes"][0]["coverage"]


def test_collector_reaches_jc_linked_main_stage_before_provider_only_qualifier(database):
    from zucai.cloud_stats_probe import collect_stats_probe, known_fixture_ids
    from zucai.stats_probe import DETAIL_ENDPOINTS

    target = fixture("sampling_priority")["main_stage"]
    with sqlite3.connect(database) as con:
        for mid, source, counterpart in (
            (target["counterpart_id"], "apif", target["match_id"]),
            (target["match_id"], "jc", target["counterpart_id"]),
        ):
            con.execute(
                "INSERT INTO matches (match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status,counterpart_id) VALUES (?,?,?,1,2,?,?,?)",
                (mid, source, "欧罗巴", "2024-09-02T18:00:00Z", "finished", counterpart),
            )
    known = known_fixture_ids(database)
    assert known[str(target["fixture_id"])] == target["match_id"]
    before = database.read_bytes()
    with responses.RequestsMock() as http:
        register(http, sample=(target["fixture_id"],))
        report = collect_stats_probe(
            database, "jc-priority", codes="3", seasons="2024", per_scope=1, now=NOW
        )
    assert report["status"] == "complete" and report["requests"] == 8
    detail = [row for row in report["probes"] if row["endpoint"] in DETAIL_ENDPOINTS]
    assert {row["fixture_id"] for row in detail} == {target["fixture_id"]}
    assert {row["match_id"] for row in detail} == {target["match_id"]}
    assert database.read_bytes() == before


def test_errors_are_partial_and_next_endpoint_still_runs(database):
    from zucai.cloud_stats_probe import collect_stats_probe

    with responses.RequestsMock() as http:
        register(http, sample=(102,), bad="events")
        report = collect_stats_probe(
            database, "partial", codes="3", seasons="2024", per_scope=1, now=NOW
        )
    assert report["status"] == "partial" and report["requests"] == 8
    assert (
        next(r for r in report["probes"] if r["endpoint"] == "fixtures/events")["outcome"]
        == "error"
    )
    assert (
        report["probes"][-1]["endpoint"] == "injuries" and report["probes"][-1]["outcome"] == "ok"
    )


@pytest.mark.parametrize("codes,seasons,count", [(None, None, 2), ("3", "2024", 200)])
def test_budget_refusal_and_actions_guard_have_no_side_effects(
    tmp_path, monkeypatch, codes, seasons, count
):
    from zucai.cloud_stats_probe import collect_stats_probe

    db = tmp_path / "data/zucai.db"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    with responses.RequestsMock() as http:
        with pytest.raises(ValueError, match="500"):
            collect_stats_probe(db, "over", codes=codes, seasons=seasons, per_scope=count, now=NOW)
        assert not http.calls
    assert not list(tmp_path.iterdir())
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    with pytest.raises(ValueError, match="workflow_dispatch"):
        collect_stats_probe(db, "schedule", codes="3", seasons="2024", per_scope=1, now=NOW)
    assert not list(tmp_path.iterdir())


def test_reserve_floor_stops_all_remaining_requests_and_persists_partial_audit(database):
    from zucai.cloud_stats_probe import collect_stats_probe

    with responses.RequestsMock() as http:
        headers = {**fixture("cases")["quota_headers"], "x-ratelimit-requests-remaining": "10"}
        http.get(API + "/leagues?id=3", json=fixture("leagues"), headers=headers)
        report = collect_stats_probe(
            database, "quota-stop", codes="3,848", seasons="2024", per_scope=1, now=NOW
        )
        assert len(http.calls) == 1
    assert report["status"] == "partial" and report["requests"] == 1
    assert report["remaining_quota"] == 10 and report["stopped_reason"]
    assert len(report["probes"]) == 2  # metadata plus the blocked attempt, not all future scopes.
    assert report["probes"][-1]["requests"] == 0 and report["probes"][-1]["raw_sha256"] == ""


@pytest.mark.parametrize(
    "codes,seasons,count",
    [("3,3", "2024", 1), ("999", "2024", 1), ("3", "2027", 1), ("3", "2024", 0)],
)
def test_invalid_scope_is_rejected_before_any_requests(
    codes, seasons, count, tmp_path, monkeypatch
):
    from zucai.cloud_stats_probe import collect_stats_probe

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    with pytest.raises(ValueError):
        collect_stats_probe(
            tmp_path / "data/zucai.db",
            "bad",
            codes=codes,
            seasons=seasons,
            per_scope=count,
            now=NOW,
        )
    assert not list(tmp_path.iterdir())


def test_cli_stats_archive_roundtrip_preserves_tables_and_frozen_forecasts(
    database, tmp_path, monkeypatch
):
    import sys

    from test_cloud_archive import table_rows
    from test_forecast_archive import staged_run
    from zucai.cloud_feeds import archive_info, export_feeds, main, restore_feeds
    from zucai.forecast_archive import publish_forecasts

    archive = tmp_path / "archive"
    original = export_feeds(database, archive, NOW.date(), "seed")
    stage, run = staged_run(tmp_path / "staged", "frozen")
    manifest = publish_forecasts(archive, stage, run)
    frozen = {p: (archive / p).read_bytes() for p in [*manifest["files"], "forecast_manifest.json"]}
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "feeds",
            "probe",
            "--probe-kind",
            "stats",
            "--source",
            "apif",
            "--archive",
            str(archive),
            "--run-id",
            "stats-cli",
            "--leagues",
            "3",
            "--seasons",
            "2024",
        ],
    )
    with responses.RequestsMock() as http:
        register(http, sample=(102,))
        main()
    updated = archive_info(archive)
    assert updated["counts"] == original["counts"] and updated["sources"] == original["sources"]
    assert updated["through_beijing"] == original["through_beijing"]
    assert updated["collection"]["apif"]["requests"] == 8
    assert "state/audit/apif_stats_probes.csv" in updated["files"]
    restored = tmp_path / "restored/data/zucai.db"
    restore_feeds(archive, restored)
    assert table_rows(restored) == table_rows(database)
    assert all((archive / p).read_bytes() == content for p, content in frozen.items())


@pytest.mark.parametrize(
    "command,kind", [("apis", "stats"), ("identity", None), ("probe", "seasons")]
)
def test_per_scope_only_allowed_for_stats_probe(command, kind, tmp_path, monkeypatch):
    import sys

    from zucai.cloud_feeds import main

    argv = [
        "feeds",
        command,
        "--archive",
        str(tmp_path / "archive"),
        "--run-id",
        "bad",
        "--per-scope",
        "1",
    ]
    if kind:
        argv += ["--probe-kind", kind]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["429", "network", "unsafe", "archive"])
def test_failed_request_preserves_evidence_and_continues_without_import(
    database, monkeypatch, failure
):
    import requests

    from zucai.cloud_stats_probe import collect_stats_probe
    from zucai.sources.base import RawStore

    if failure == "archive":
        original = RawStore.save_response

        def failing_store(self, source, name, response, ext, **kwargs):
            if name == "stats-fixtures-events":
                raise OSError("synthetic archive write failure")
            return original(self, source, name, response, ext, **kwargs)

        monkeypatch.setattr(RawStore, "save_response", failing_store)
    with responses.RequestsMock() as http:
        register(http, sample=(102,))
        event_url = API + "/fixtures/events?fixture=102"
        doc = fixture("events")
        if failure == "429":
            doc["errors"] = fixture("cases")["errors"]["rate"]
            http.replace(
                responses.GET,
                event_url,
                json=doc,
                status=429,
                headers=fixture("cases")["quota_headers"],
            )
        elif failure == "network":
            http.replace(
                responses.GET, event_url, body=requests.Timeout("private exception detail")
            )
        elif failure == "unsafe":
            doc["response"][0]["comments"] = "offline-stats-placeholder"
            http.replace(responses.GET, event_url, json=doc)
        report = collect_stats_probe(
            database, failure, codes="3", seasons="2024", per_scope=1, now=NOW
        )
        assert len(http.calls) == 8
    row = next(r for r in report["probes"] if r["endpoint"] == "fixtures/events")
    assert report["status"] == "partial" and row["outcome"] == "error" and row["requests"] == 1
    assert report["probes"][-1]["outcome"] == "ok"
    if failure == "429":
        assert row["http"] == 429 and len(row["raw_sha256"]) == 64
    else:
        assert row["raw_sha256"] == ""
    assert "private exception detail" not in str(report)
    assert "offline-stats-placeholder" not in str(report)
    assert all(
        "offline-stats-placeholder" not in p.read_text() for p in database.parent.rglob("*.json")
    )


def test_no_eligible_fixture_records_zero_request_skips_for_each_detail_endpoint(
    database, tmp_path, monkeypatch
):
    from zucai.cloud_stats_probe import collect_stats_probe, write_stats_probe_summary
    from zucai.stats_probe import DETAIL_ENDPOINTS

    with responses.RequestsMock() as http:
        register(http, sample=(), half=False)
        doc = fixture("fixtures")
        for match in doc["response"]:
            match["fixture"]["status"]["short"] = "NS"
        http.replace(responses.GET, API + "/fixtures?league=3&season=2024", json=doc)
        report = collect_stats_probe(
            database, "no-samples", codes="3", seasons="2024", per_scope=2, now=NOW
        )
        assert len(http.calls) == 3
    assert report["status"] == "partial" and report["requests"] == 3
    skipped = [row for row in report["probes"] if row["outcome"] == "skipped"]
    assert {row["endpoint"] for row in skipped} == set(DETAIL_ENDPOINTS)
    assert all(
        row["requests"] == 0 and row["http"] == "" and row["raw_sha256"] == "" for row in skipped
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    write_stats_probe_summary(report)
    text = summary.read_text()
    assert "无可抽样" in text
    assert all(f"| {endpoint} | 0 | 0 | — | 0 | 0 | 0 |" in text for endpoint in DETAIL_ENDPOINTS)
