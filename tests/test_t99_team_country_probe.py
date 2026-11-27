"""Offline transport tests; the mock envelope has no provider team fields."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
import responses

from zucai.cloud_team_country_probe import collect_team_country_probe, write_summary

ROOT = Path(__file__).resolve().parents[1]
ENVELOPE = ROOT / "data/fixtures/t99/teams-transport-envelope.json"
QUOTA = json.loads((ROOT / "data/fixtures/t22/quota_cases.json").read_bytes())
NOW = datetime(2026, 11, 27, tzinfo=UTC)
API = "https://v3.football.api-sports.io/teams"


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-team-countries-placeholder")
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)


def payload(league):
    doc = json.loads(ENVELOPE.read_bytes())
    doc["parameters"]["league"] = str(league)
    return doc


def saved(state, item, expected):
    raw = state / item["raw_path"]
    meta = state / item["metadata_path"]
    assert raw.read_bytes() == expected
    assert item["raw_sha256"] == hashlib.sha256(expected).hexdigest()
    assert json.loads(meta.read_bytes())["sha256"] == item["raw_sha256"]


def test_complete_two_team_lists_archive_response_without_using_team_fields(
    hosted, tmp_path, monkeypatch
):
    database = tmp_path / "zucai.db"
    database.write_bytes(b"unchanged database")
    alias = tmp_path / "state/alias_candidates/apif.json"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"unchanged aliases")
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    expected = {league: json.dumps(payload(league)).encode() for league in (3, 848)}
    with responses.RequestsMock() as http:
        for league, body in expected.items():
            http.get(
                f"{API}?league={league}&season=2026",
                body=body,
                headers=QUOTA["paid_headers"],
            )
        report = collect_team_country_probe(database, "probe-1", now=NOW)
        assert [call.request.url for call in http.calls] == [
            f"{API}?league={league}&season=2026" for league in (3, 848)
        ]
    assert report["status"] == "complete"
    assert report["requests"] == 2
    assert report["collection"] == "apif_team_countries"
    assert [item["league_id"] for item in report["probes"]] == [3, 848]
    for item in report["probes"]:
        assert item["outcome"] == "complete" and item["results"] == 1
        saved(tmp_path, item, expected[item["league_id"]])
    assert report["evidence_index"] == "state/catalogue/probe-1/apif-team-countries/index.json"
    assert json.loads((tmp_path / report["evidence_index"]).read_bytes()) == report
    assert database.read_bytes() == b"unchanged database"
    assert alias.read_bytes() == b"unchanged aliases"
    write_summary(report)
    text = summary.read_text()
    assert all(item["raw_sha256"] in text for item in report["probes"])
    assert "2" in text and "offline-team-countries-placeholder" not in text


@pytest.mark.parametrize("problem", ["pagination", "network", "http"])
def test_incomplete_or_failed_reply_stops_at_one_request_and_retains_other_scope(
    hosted, tmp_path, problem
):
    bad = payload(3)
    bad["paging"]["total"] = 2 if problem == "pagination" else 1
    raw = json.dumps(bad).encode()
    body = requests.ConnectionError("diagnostic text") if problem == "network" else raw
    with responses.RequestsMock() as http:
        http.get(f"{API}?league=3&season=2026", body=body, status=503 if problem == "http" else 200)
        http.get(f"{API}?league=848&season=2026", json=payload(848))
        report = collect_team_country_probe(tmp_path / "new.db", "probe-2", now=NOW)
        assert len(http.calls) == 2
    first, second = report["probes"]
    assert report["status"] == "partial" and report["requests"] == 2
    assert first["outcome"] == "error" and first["reason"]
    assert second["outcome"] == "complete"
    if problem == "pagination":
        assert "分页" in first["reason"]
    if problem == "network":
        assert first["raw_path"] is None and first["raw_sha256"] is None
    else:
        saved(tmp_path, first, raw)
    assert "diagnostic text" not in json.dumps(report)
    assert not (tmp_path / "new.db").exists()


@pytest.mark.parametrize("runner", ["self-hosted", None])
def test_probe_refuses_other_runners_before_staging(hosted, tmp_path, monkeypatch, runner):
    if runner:
        monkeypatch.setenv("RUNNER_ENVIRONMENT", runner)
    else:
        monkeypatch.delenv("RUNNER_ENVIRONMENT")
    with responses.RequestsMock() as http, pytest.raises(ValueError, match="github-hosted"):
        collect_team_country_probe(tmp_path / "new.db", "bad", now=NOW)
    assert not http.calls
    assert not list(tmp_path.iterdir())
