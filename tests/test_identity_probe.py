"""Reviewer-only league metadata confirmation, replayed from offline fixtures."""

import csv
import json
from pathlib import Path

import pytest
import responses

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((ROOT / "data/fixtures/t23/identity.json").read_bytes())
QUOTA = json.loads((ROOT / "data/fixtures/t22/quota_cases.json").read_bytes())


@pytest.fixture
def actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-identity-placeholder")
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)


def test_metadata_probe_confirms_seven_ids_without_fixture_or_status_requests(actions, tmp_path):
    from zucai.cloud_identity_probe import collect_league_probe, require_confirmed_leagues

    database = tmp_path / "data/zucai.db"
    with responses.RequestsMock() as http:
        for league in FIXTURE["leagues"]:
            doc = json.loads(json.dumps(FIXTURE["metadata"]))
            doc["parameters"]["id"] = league["code"]
            doc["response"][0]["league"].update(id=int(league["code"]), name=league["api_name"])
            doc["response"][0]["country"]["name"] = league["country"]
            http.get(
                "https://v3.football.api-sports.io/leagues?id=" + league["code"],
                json=doc,
                headers=QUOTA["paid_headers"],
            )
        report = collect_league_probe(database, "metadata-1")
    assert report["status"] == "complete" and report["requests"] == 7
    assert report["limit_day"] == 7500
    assert not database.exists()
    rows = list(csv.DictReader((database.parent / "state/audit/apif_league_probes.csv").open()))
    assert len(rows) == 7 and all(row["outcome"] == "ok" for row in rows)
    assert all(len(row["raw_sha256"]) == 64 for row in rows)
    proof = require_confirmed_leagues(database, [league["code"] for league in FIXTURE["leagues"]])
    assert len(proof) == 7


@pytest.mark.parametrize("problem", ["id", "country", "name", "type", "paging", "errors"])
def test_mismatched_metadata_is_audited_and_cannot_authorize_identity(actions, tmp_path, problem):
    from zucai.cloud_identity_probe import collect_league_probe, require_confirmed_leagues

    doc = json.loads(json.dumps(FIXTURE["metadata"]))
    if problem in {"id", "name", "type"}:
        doc["response"][0]["league"][problem] = 99 if problem == "id" else "wrong"
    elif problem == "country":
        doc["response"][0]["country"]["name"] = "wrong"
    elif problem == "paging":
        doc["paging"]["total"] = 2
    else:
        doc["errors"] = {"access": QUOTA["errors"]["access"]}
    database = tmp_path / "data/zucai.db"
    with responses.RequestsMock() as http:
        http.get("https://v3.football.api-sports.io/leagues?id=39", json=doc)
        report = collect_league_probe(database, "metadata-bad", codes="39")
    assert report["status"] == "partial" and report["requests"] == 1
    rows = list(csv.DictReader((database.parent / "state/audit/apif_league_probes.csv").open()))
    assert rows[0]["outcome"] == "error" and rows[0]["http"] == "200"
    with pytest.raises(ValueError, match="probe"):
        require_confirmed_leagues(database, ["39"])


def test_missing_metadata_proof_cannot_authorize_identity(tmp_path):
    from zucai.cloud_identity_probe import require_confirmed_leagues

    with pytest.raises(ValueError, match="probe"):
        require_confirmed_leagues(tmp_path / "data/zucai.db", ["39"])
