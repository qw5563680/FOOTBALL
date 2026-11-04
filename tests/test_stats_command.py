"""CLI paths are offline by default; even index-only acquisition requires Actions."""

import json
from pathlib import Path

import pytest

from zucai.cli import build_parser, main
from zucai.sources.api_football import LEAGUES, ApiFootball
from zucai.sources.football_data import read_json
from zucai.stats_command import fetch_index
from zucai.stats_link import INDEX_REQUEST_LEAGUES


def test_stats_cli_exposes_offline_replays():
    args = build_parser().parse_args(
        [
            "stats",
            "ingest-fd",
            "--db",
            "feeds.db",
            "--raw-file",
            "E0.csv",
            "--div",
            "E0",
            "--season",
            "2425",
        ]
    )
    assert args.stats_command == "ingest-fd" and args.raw_file == Path("E0.csv")
    args = build_parser().parse_args(
        [
            "stats",
            "link",
            "--db",
            "feeds.db",
            "--raw-file",
            "fixtures.json",
            "--league",
            "2",
            "--season",
            "2024",
        ]
    )
    assert args.fetch is False
    assert (
        main(
            [
                "stats",
                "ingest-fd",
                "--db",
                "missing.db",
                "--raw-file",
                "missing.csv",
                "--div",
                "E0",
                "--season",
                "2425",
            ]
        )
        == 2
    )


def test_index_fetch_blocked_locally_before_client_creation(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ValueError, match="Actions"):
        fetch_index(tmp_path / "feeds.db", league_id=2, season=2024)


def test_index_only_never_enters_normal_or_identity_importer(tmp_path):
    config = read_json(LEAGUES)
    assert config["2"]["index_only"] is True
    assert INDEX_REQUEST_LEAGUES == {2, 39, 140, 135, 78, 61, 88, 94}
    for identity in (False, True):
        client = ApiFootball(
            leagues=config, quota_path=tmp_path / "quota.json", identity_only=identity
        )
        assert "2" not in client.leagues
        with pytest.raises(ValueError):
            client._scope(league_id=2, season=2024)
        client.session.close()


def test_unsupported_players_qualification_does_not_exclude_league_phase():
    config = json.loads(
        (Path(__file__).parents[1] / "data/api_football/stats_coverage.json").read_text()
    )
    cups = [
        r
        for r in config["unsupported"]
        if r["endpoint"] == "fixtures/players" and 3 in r["leagues"]
    ]
    assert len(cups) == 1 and cups[0]["stage"] == "qualification"


def test_actions_index_fetch_is_one_fixture_page_with_quota(tmp_path, monkeypatch):
    import responses

    from zucai.sources.base import RawStore

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-test-placeholder")
    fixture = json.loads((Path(__file__).parents[1] / "data/fixtures/t31/fixture.json").read_text())
    raw = dict(
        get="fixtures",
        parameters={"league": "2", "season": "2024"},
        errors=[],
        results=1,
        paging={"current": 1, "total": 1},
        response=[fixture],
    )
    database = tmp_path / "feeds.db"
    database.touch()
    with responses.RequestsMock() as http:
        http.get(
            "https://v3.football.api-sports.io/fixtures?league=2&season=2024",
            json=raw,
            headers={
                "x-ratelimit-requests-limit": "7500",
                "x-ratelimit-requests-remaining": "7499",
            },
        )
        result = fetch_index(database, league_id=2, season=2024)
        assert len(http.calls) == 1
    meta = RawStore(tmp_path / "raw").load_metadata(result)
    assert meta["quota_headers"]["x-ratelimit-requests-limit"] == "7500"
    assert json.loads(result.read_text()) == raw
    with pytest.raises(ValueError, match="索引请求"):
        fetch_index(database, league_id=3, season=2024)
