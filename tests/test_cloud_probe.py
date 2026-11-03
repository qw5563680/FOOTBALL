import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import responses

from zucai.cloud_probe import collect_probe, probe_scopes, status_billing

ROOT = Path(__file__).parents[1] / "data/fixtures"
NOW = datetime(2026, 11, 3, tzinfo=UTC)
CASES = json.loads((ROOT / "t22/quota_cases.json").read_bytes())


def test_probe_ranges():
    assert len(probe_scopes(None, None, NOW)) == 12
    assert len(probe_scopes("98,292,188,113,103,244,119,253,169", "2025,2026", NOW)) == 18
    assert probe_scopes("3", "2026", NOW) == [(3, 2026)]


@pytest.mark.parametrize(
    "leagues,seasons",
    [
        ("39", "2026"),
        ("3,3", "2026"),
        ("3", "2026-2021"),
        ("3", "2027"),
        ("3", "2025,,2026"),
        ("3", "2026,2026"),
    ],
)
def test_reject_scope_before_requests(leagues, seasons):
    with pytest.raises(ValueError):
        probe_scopes(leagues, seasons, NOW)


def test_probe_requires_manual_actions(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ValueError, match="workflow_dispatch"):
        collect_probe(tmp_path / "data/zucai.db", "test", now=NOW)
    assert not list(tmp_path.rglob("*.json"))


def test_probe_only_first_page_no_import_and_audits_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-probe-placeholder")
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    database = tmp_path / "data/zucai.db"
    database.parent.mkdir()
    database.write_bytes(b"not opened by probe")
    doc = json.loads((ROOT / "t10/synthetic_page1.json").read_bytes())
    doc["parameters"].update(league="3", season="2025")
    with responses.RequestsMock() as http:
        http.get(
            "https://v3.football.api-sports.io/status",
            json=CASES["status"],
            headers=CASES["paid_headers"],
        )
        http.get(
            "https://v3.football.api-sports.io/fixtures?league=3&season=2025",
            json=doc,
            headers=CASES["paid_headers"],
        )
        doc = json.loads((ROOT / "t10/synthetic_errors.json").read_bytes())
        doc["errors"] = {"access": "Your account is suspended"}
        http.get(
            "https://v3.football.api-sports.io/fixtures?league=3&season=2026",
            json=doc,
            headers=CASES["paid_headers"],
        )
        result = collect_probe(database, "probe1", codes="3", seasons="2025-2026", now=NOW)
        assert len(http.calls) == 4  # two status observations and two first pages
    assert database.read_bytes() == b"not opened by probe"
    assert result["status"] == "partial" and result["requests"] == 2
    assert result["status_requests"] == 2 and result["status_billing"] == "unbilled"
    assert "Your account is suspended" in str(result["errors"])
    rows = list(csv.DictReader((database.parent / "state/audit/apif_season_probes.csv").open()))
    assert len(rows) == 2 and rows[0]["results"] == "1"
    assert json.loads(rows[0]["paging"])["total"] == 2
    assert json.loads(rows[1]["errors"])["access"] == "Your account is suspended"
    assert all(len(r["raw_sha256"]) == 64 for r in rows)
    saved = json.loads((database.parent / "state/api_football_quota.json").read_bytes())
    assert saved["status_free"] is True and saved["plan"] == "Pro"


def test_status_billing_requires_consistent_evidence():
    first = dict(
        observed_utc="2026-11-03T00:00:00Z",
        plan="Pro",
        limit_day=7500,
        current=2,
        headers={"x-ratelimit-requests-limit": "7500", "x-ratelimit-requests-remaining": "7498"},
    )
    assert status_billing(first, first) is None  # no elapsed observation interval
    second = {**first, "observed_utc": "2026-11-03T00:00:07Z"}
    assert status_billing(first, second) is True
    second.update(
        current=3,
        headers={"x-ratelimit-requests-limit": "7500", "x-ratelimit-requests-remaining": "7497"},
    )
    assert status_billing(first, second) is False
    assert status_billing(first, {**second, "headers": {}}) is None


def test_probe_archive_roundtrip_keeps_rows_forecasts_and_watermark(tmp_path, monkeypatch):
    import sys

    from test_cloud_archive import seed_database, table_rows
    from test_forecast_archive import staged_run
    from zucai.cloud_feeds import archive_info, export_feeds, main, restore_feeds
    from zucai.forecast_archive import publish_forecasts

    database = tmp_path / "seed/data/zucai.db"
    seed_database(database)
    archive = tmp_path / "archive"
    original = export_feeds(database, archive, NOW.date(), "seed")
    stage, run = staged_run(tmp_path / "stage", "existing-forecast")
    frozen_manifest = publish_forecasts(archive, stage, run)
    frozen = {
        name: (archive / name).read_bytes()
        for name in [*frozen_manifest["files"], "forecast_manifest.json"]
    }
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-probe-placeholder")
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    for attempt in range(2):
        work = tmp_path / f"probe-{attempt}"
        work.mkdir()
        monkeypatch.chdir(work)
        # cloud_feeds is a command process; restore its environment when invoking it in-process.
        monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "feeds",
                "probe",
                "--archive",
                str(archive),
                "--run-id",
                work.name,
                "--source",
                "apif",
                "--leagues",
                "98",
                "--seasons",
                "2024",
            ],
        )
        with responses.RequestsMock() as http:
            http.get(
                "https://v3.football.api-sports.io/status",
                json=CASES["status"],
                headers=CASES["paid_headers"],
            )
            http.get(
                "https://v3.football.api-sports.io/fixtures?league=98&season=2024",
                body=(ROOT / "t10/synthetic_page1.json").read_bytes(),
                headers=CASES["paid_headers"],
            )
            main()
            assert len(http.calls) == 3
        meta = archive_info(archive)
        assert (
            meta["sources"] == original["sources"]
            and meta["through_beijing"] == original["through_beijing"]
        )
        assert meta["collection"]["apif"]["requests"] == 1
        restore_feeds(archive, tmp_path / f"restored-{attempt}/data/zucai.db")
        assert table_rows(tmp_path / f"restored-{attempt}/data/zucai.db") == table_rows(database)
        assert all((archive / name).read_bytes() == raw for name, raw in frozen.items())
        assert "7500" in (work / "summary.md").read_text()
    rows = list(csv.DictReader((archive / "state/audit/apif_season_probes.csv").open()))
    assert len(rows) == 2
    assert json.loads((archive / "state/api_football_quota.json").read_bytes())["used"] == 6


@pytest.mark.parametrize(
    "bad", [None, {"limit_day": 0}, {"plan": []}, {"observed_utc": "invalid"}, {"remaining": 7501}]
)
def test_verify_paid_state(tmp_path, monkeypatch, bad):
    import sys

    from test_cloud_archive import seed_database
    from zucai.cloud_feeds import export_feeds, main

    database = tmp_path / "seed/data/zucai.db"
    seed_database(database)
    state = database.parent / "state"
    state.mkdir()
    (state / "api_football_quota.json").write_text(json.dumps({**CASES["paid"], **(bad or {})}))
    archive = tmp_path / "archive"
    export_feeds(database, archive, NOW.date(), "seed")
    work = tmp_path / "verify"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(
        sys, "argv", ["feeds", "verify", "--archive", str(archive), "--run-id", "verify"]
    )
    if bad:
        with pytest.raises(ValueError, match="配额"):
            main()
    else:
        main()


def test_quota_failure_still_records_each_requested_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-probe-placeholder")
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    database = tmp_path / "data/zucai.db"
    state = database.parent / "state"
    state.mkdir(parents=True)
    (state / "api_football_quota.json").write_text("{}")
    with responses.RequestsMock() as http:
        result = collect_probe(database, "broken", codes="3", seasons="2025-2026", now=NOW)
        assert not http.calls
    assert result["status"] == "partial" and len(result["probes"]) == 2
    assert result["status_requests"] == result["requests"] == 0
