"""Paid permissions and backfill integration, using checked-in offline evidence only."""

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import responses

from test_cloud_apis import environment as environment
from test_cloud_cups import run_backfill
from zucai.cloud_api_backfill import api_backfill_scopes
from zucai.cloud_feeds import archive_info, digest

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "data/fixtures"
CASES = json.loads((FIXTURES / "t22/quota_cases.json").read_bytes())
DOMESTIC = "98,292,188,113,103,244,119,253,169"


def test_permissions_match_reviewed_actions_evidence():
    leagues = json.loads((ROOT / "data/api_football/leagues.json").read_bytes())
    rows = list(csv.DictReader((FIXTURES / "t22/paid_season_probes.csv").open()))
    assert len(rows) == 30
    for row in rows:
        config = leagues[row["league"]]
        probe = config["season_probes"][row["season"]]
        assert int(row["season"]) in config["seasons_available"]
        assert probe["accessible"] is True
        assert probe["results"] == int(row["results"])
        assert probe["http"] == int(row["http"]) == 200
        assert probe["raw_sha256"] == row["raw_sha256"]
        assert probe["verified_utc"] == row["observed_utc"]
        assert probe["run_id"] == row["run_id"]
        assert probe["paging"] == json.loads(row["paging"])
        assert "error" not in probe
    for code, config in leagues.items():
        assert config["verified_utc"] == max(r["observed_utc"] for r in rows if r["league"] == code)
        expected = list(range(2021, 2027)) if code in {"3", "848"} else list(range(2022, 2027))
        assert config["seasons_available"] == expected
    # Metadata still calls 2027 the current Japanese season. It was NOT probed under Pro.
    assert leagues["98"]["current_season"] == 2027
    assert leagues["98"]["current_season_accessible"] is False
    assert all(c["current_season_accessible"] for code, c in leagues.items() if code != "98")


def test_paid_scopes_are_config_driven_for_all_eleven_leagues(monkeypatch, tmp_path):
    assert len(api_backfill_scopes("apif")) == 12  # default remains the two cups
    assert len(api_backfill_scopes("apif", DOMESTIC, "2025", "2026")) == 18
    path = tmp_path / "leagues.json"
    config = json.loads((ROOT / "data/api_football/leagues.json").read_bytes())
    config["98"]["seasons_available"] = [2025]
    path.write_text(json.dumps(config))
    monkeypatch.setattr("zucai.cloud_api_backfill.LEAGUES", path)
    assert api_backfill_scopes("apif", "98") == [("98", {"league_id": "98", "season": 2025})]
    with pytest.raises(ValueError, match="已核验"):
        api_backfill_scopes("apif", "98", "2025", "2026")


def paid_status(archive):
    state = {
        **CASES["paid"],
        "date_utc": "2026-11-02",
        "status_free": True,
        "status_checked_utc": "2026-11-02T01:00:00Z",
        "status_evidence": ["a" * 64, "b" * 64],
    }
    path = archive / "state/api_football_quota.json"
    path.write_text(json.dumps(state))
    manifest = archive / "feed_manifest.json"
    doc = json.loads(manifest.read_bytes())
    doc["files"]["state/api_football_quota.json"] = digest(path)
    manifest.write_text(json.dumps(doc))


def mock_paid_season(http, code, year):
    name = f"t20/apif_{code}_2024.json" if code in {"3", "848"} else "t10/synthetic_page1.json"
    doc = json.loads((FIXTURES / name).read_bytes())
    doc["parameters"] = {"league": code, "season": str(year)}
    doc["paging"] = {"current": 1, "total": 1}
    for index, row in enumerate(doc["response"]):
        row["league"].update(id=int(code), season=year)
        row["fixture"]["id"] = 9000000 + int(code) * 1000 + (year - 2020) * 100 + index
        kickoff = datetime(year, 5, 18, 12, tzinfo=UTC)
        row["fixture"].update(timestamp=int(kickoff.timestamp()), date=kickoff.isoformat())
    http.get(
        "https://v3.football.api-sports.io/fixtures",
        json=doc,
        headers=CASES["paid_headers"],
        match=[responses.matchers.query_param_matcher(doc["parameters"])],
    )
    return len(doc["response"])


@pytest.mark.parametrize("codes", [DOMESTIC, "3,848"])
def test_paid_backfill_repeat_retains_quota_and_source_priority(
    environment, tmp_path, monkeypatch, codes
):
    initial = archive_info(environment)
    official_aliases = {p: p.read_bytes() for p in (ROOT / "data/team_aliases").glob("*.json")}
    paid_status(environment)
    for attempt in range(2):
        with responses.RequestsMock() as http:
            http.get(
                "https://v3.football.api-sports.io/status",
                json=CASES["status"],
                headers=CASES["paid_headers"],
            )
            for code in codes.split(","):
                for year in (2025, 2026):
                    mock_paid_season(http, code, year)
            report = run_backfill(
                environment, tmp_path / f"paid-{attempt}", monkeypatch, "apif", codes, 2025, 2026
            )
            assert report["requests"] == 2 * len(codes.split(","))
            assert report["status_requests"] == 1 and len(http.calls) == report["requests"] + 1
        assert report["status"] == "complete"
        assert report["plan"] == "Pro" and report["limit_day"] == 7500
        assert not report["quota_fallback"]
        assert report["inserted"] > 0 if attempt == 0 else report["inserted"] == 0
        assert report["rejected_rows"] == report["suspected_duplicates"] == 0
        assert set(report["leagues"]) == set(codes.split(","))
        for item in report["scopes"]:
            assert item["results_after"] > 0
            assert "alignment_rate_before" in item and "alignment_rate_after" in item
        current = archive_info(environment)
        assert current["sources"]["jc"] == initial["sources"]["jc"]
        assert current["through_beijing"] == initial["through_beijing"]
        summary = (tmp_path / f"paid-{attempt}/summary.md").read_text()
        assert "7500" in summary and "Pro" in summary and "对齐率（前→后）" in summary
    assert all(p.read_bytes() == raw for p, raw in official_aliases.items())
