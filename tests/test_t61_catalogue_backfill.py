"""Catalogue backfill acceptance uses checked-in catalogue/game fixtures only."""

import hashlib
import json
from pathlib import Path

import pytest
import responses

from test_cloud_apis import environment as environment
from test_t22_backfill import mock_paid_season, paid_status
from zucai.cloud_catalogue_backfill import RequestBudget, catalogue_scopes
from zucai.league_registry import load_registry

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "data/fixtures/t61"


def catalogue(tmp_path, provider="apif"):
    base = tmp_path / "state/catalogue"
    folder = base / "offline-catalogue"
    folder.mkdir(parents=True)
    doc = json.loads((FIXTURES / "backfill-catalogue.json").read_bytes())[provider]
    payload = json.dumps(doc).encode()
    (folder / f"{provider}.json").write_bytes(payload)
    (base / "latest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {
                    provider: {
                        "path": f"state/catalogue/offline-catalogue/{provider}.json",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "raw_sha256": "a" * 64,
                        "run_id": "offline-catalogue",
                    }
                },
            }
        )
    )
    return base


def test_scopes_use_explicit_catalogue_years_not_min_max_range(tmp_path):
    catalogue(tmp_path)
    plan = catalogue_scopes(tmp_path, "apif", "292")
    assert [s[1]["season"] for s in plan["scopes"]] == [2022, 2024]
    assert plan["provider"] == "apif" and plan["catalogue_sha256"]
    assert plan["access_is_verified"] is False


def test_candidate_rejected_before_catalogue_or_network(tmp_path):
    registry = load_registry()
    registry["providers"]["apif"]["292"].update(status="candidate", daily=False, backfill=False)
    # Testing planner's gate, without editing any repository configuration.
    with pytest.raises(ValueError, match="verified"):
        catalogue_scopes(tmp_path, "apif", "292", registry=registry)


@pytest.mark.parametrize("code", ["2", "39", "5", "999999", "292,292", ""])
def test_ineligible_route_never_falls_through(tmp_path, code):
    with pytest.raises(ValueError):
        catalogue_scopes(tmp_path, "apif", code)


def test_tampered_catalogue_fails_closed(tmp_path):
    folder = catalogue(tmp_path) / "offline-catalogue"
    (folder / "apif.json").write_text("{}")
    with pytest.raises(ValueError, match="SHA"):
        catalogue_scopes(tmp_path, "apif", "292")


def test_unlisted_requested_season_is_not_fabricated(tmp_path):
    catalogue(tmp_path)
    with pytest.raises(ValueError, match="赛季"):
        catalogue_scopes(tmp_path, "apif", "292", first="2023", last="2023")


def test_fdorg_does_not_expand_available_season_count(tmp_path):
    catalogue(tmp_path, "fdorg")
    plan = catalogue_scopes(tmp_path, "fdorg", "PL")
    assert [s[1]["season"] for s in plan["scopes"]] == [2026]


def test_fdorg_unrelated_null_code_does_not_block_reviewed_route(tmp_path):
    base = catalogue(tmp_path, "fdorg")
    path = base / "offline-catalogue/fdorg.json"
    doc = json.loads(path.read_bytes())
    doc["leagues"].append(dict(id="9999", code=None, name="Unrouteable", seasons=[]))
    payload = json.dumps(doc).encode()
    path.write_bytes(payload)
    latest = json.loads((base / "latest.json").read_bytes())
    latest["providers"]["fdorg"]["sha256"] = hashlib.sha256(payload).hexdigest()
    (base / "latest.json").write_text(json.dumps(latest))
    assert catalogue_scopes(tmp_path, "fdorg", "PL")["scopes"] == [
        ("PL", {"competition": "PL", "season": 2026})
    ]


def test_fd_missing_years_reports_gap_instead_of_inventing_range(tmp_path):
    catalogue(tmp_path, "fd")
    plan = catalogue_scopes(tmp_path, "fd", "E0")
    assert not plan["scopes"]
    assert plan["missing"][0]["code"] == "E0"


def test_hard_budget_counts_failed_requests_and_stops_before_dispatch():
    budget = RequestBudget(2)
    calls = []

    def send():
        calls.append(1)
        raise OSError("offline test")

    for _ in range(2):
        with pytest.raises(OSError):
            budget.call(send)
    with pytest.raises(ValueError, match="请求上限"):
        budget.call(send)
    assert budget.used == len(calls) == 2


def test_real_import_budget_resume_and_zero_second_insert(environment, tmp_path, monkeypatch):
    from datetime import date

    from zucai.cloud_catalogue_backfill import collect_catalogue_backfill
    from zucai.cloud_feeds import export_feeds, restore_feeds

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    paid_status(environment)
    database = tmp_path / "backfill/zucai.db"
    restore_feeds(environment, database)
    catalogue(database.parent)
    with responses.RequestsMock() as http:
        mock_paid_season(http, "292", 2022)
        first = collect_catalogue_backfill(
            database, "first", provider="apif", codes="292", max_requests=1
        )
        assert first["status"] == "partial"
        assert first["requests"] == len(http.calls) == 1
        assert first["inserted"] > 0
    with responses.RequestsMock() as http:
        mock_paid_season(http, "292", 2024)
        second = collect_catalogue_backfill(
            database, "second", provider="apif", codes="292", max_requests=1
        )
        assert second["status"] == "complete"
        assert second["requests"] == len(http.calls) == 1
        assert second["resumed_scopes"] == 1
    with responses.RequestsMock() as http:
        third = collect_catalogue_backfill(database, "third", provider="apif", codes="292")
        assert third["status"] == "complete" and third["resumed_scopes"] == 2
        assert third["inserted"] == third["requests"] == len(http.calls) == 0
    with responses.RequestsMock() as http:
        for year in (2022, 2024):
            mock_paid_season(http, "292", year)
        replay = collect_catalogue_backfill(
            database, "replay", provider="apif", codes="292", resume=False
        )
        assert replay["requests"] == len(http.calls) == 2
        assert replay["inserted"] == 0 and replay["unchanged"] > 0
    exported = tmp_path / "after-archive"
    manifest = export_feeds(database, exported, date(2026, 11, 2), "t61-replay")
    assert "state/catalogue/backfill-checkpoint.json" in manifest["files"]
    restored = tmp_path / "after-restore/zucai.db"
    restore_feeds(exported, restored)
    with responses.RequestsMock() as http:
        resumed = collect_catalogue_backfill(restored, "restored", provider="apif", codes="292")
        assert resumed["requests"] == len(http.calls) == 0
        assert resumed["resumed_scopes"] == 2


def test_fdorg_hard_limit_counts_retry_and_can_replay_later(environment, tmp_path, monkeypatch):
    from zucai.cloud_catalogue_backfill import collect_catalogue_backfill
    from zucai.cloud_feeds import restore_feeds

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setattr("zucai.sources.football_data_org._reset_seconds", lambda *a: 0)
    database = tmp_path / "fdorg/zucai.db"
    restore_feeds(environment, database)
    catalogue(database.parent, "fdorg")
    with responses.RequestsMock() as http:
        http.get(
            "https://api.football-data.org/v4/competitions/PL/matches?season=2026",
            status=429,
            json={"message": "offline rate limit"},
            headers={"X-RequestCounter-Reset": "1"},
        )
        report = collect_catalogue_backfill(
            database, "retry-limit", provider="fdorg", codes="PL", max_requests=1
        )
        assert report["status"] == "partial"
        assert report["requests"] == len(http.calls) == 1
        assert report["inserted"] == 0
