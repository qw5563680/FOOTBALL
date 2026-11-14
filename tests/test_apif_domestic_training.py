"""T60: real archived identities/results, with explicit adversarial mutations."""

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from zucai.cloud_feeds import export_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.predict.backtest import read_history
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.seasons import latest_complete_seasons, season_bounds, season_for

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "data/fixtures/t60"
NOW = datetime(2026, 11, 7, tzinfo=UTC)
PAIRS = {"韩职": ("jc:2042585", "apif:1647394"), "澳超": ("jc:2042586", "apif:1593701")}


@pytest.fixture
def domestic_db(tmp_path):
    path = tmp_path / "source.db"
    engine = get_engine(path)
    init_db(engine)
    rows = [json.loads(line) for line in (FIXTURE / "history.jsonl").read_text().splitlines()]
    with session_scope(engine) as session:
        teams = {name: Team(name=name) for row in rows for name in (row["home"], row["away"])}
        session.add_all(teams.values())
        session.flush()
        for row in rows:
            session.add(
                Match(
                    **{
                        k: row[k]
                        for k in (
                            "match_id",
                            "source",
                            "league",
                            "counterpart_id",
                            "kickoff_utc",
                            "status",
                        )
                    },
                    home_team_id=teams[row["home"]].id,
                    away_team_id=teams[row["away"]].id,
                )
            )
        session.flush()
        for row in rows:
            if row["home_goals"] is not None:
                session.add(
                    Result(
                        **{
                            k: row[k]
                            for k in (
                                "match_id",
                                "home_goals",
                                "away_goals",
                                "available_utc",
                                "available_source",
                            )
                        }
                    )
                )
    yield engine, path
    engine.dispose()


def _complete_counterpart(session, league):
    """Simulate a late APIF completion using the actual linked JC score/time."""
    jc, apif = PAIRS[league]
    result = session.get(Result, jc)
    session.get(Match, apif).status = "finished"
    session.add(
        Result(
            match_id=apif,
            home_goals=result.home_goals,
            away_goals=result.away_goals,
            available_utc=result.available_utc,
            available_source=result.available_source,
        )
    )


def test_committed_source_policy_is_explicit_and_independent_of_collector_registry():
    from zucai.predict.training_sources import apif_history_sources

    assert apif_history_sources() == {"韩职": {"apif", "jc"}, "澳超": {"apif", "jc"}}
    registry = json.loads((ROOT / "data/api_football/leagues.json").read_text())
    assert all(
        item["league"] not in apif_history_sources()
        for item in registry.values()
        if item.get("identity_only")
    )
    assert "中超" not in apif_history_sources() and "丹超" not in apif_history_sources()


@pytest.mark.parametrize(
    "sources", [["apif", "unknown"], ["apif", "jc", "fd"], ["apif"], ["apif", ["jc"]]]
)
def test_source_policy_fails_closed_on_invalid_combination(tmp_path, monkeypatch, sources):
    from zucai.predict import training_sources

    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema_version": 1, "leagues": {"韩职": {"sources": sources}}}))
    monkeypatch.setattr(training_sources, "APIF_HISTORY_PATH", policy)
    with pytest.raises(ValueError, match="训练来源配置"):
        training_sources.apif_history_sources()


def test_new_domestic_opt_in_needs_configuration_only(domestic_db, tmp_path, monkeypatch):
    from zucai.predict import training_sources

    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {"schema_version": 1, "leagues": {"explicit-next-league": {"sources": ["apif", "jc"]}}}
        )
    )
    monkeypatch.setattr(training_sources, "APIF_HISTORY_PATH", policy)
    engine, _ = domestic_db
    with session_scope(engine) as session:
        _complete_counterpart(session, "韩职")
        for match in session.scalars(select(Match).where(Match.league == "韩职")):
            match.league = "explicit-next-league"
    with session_scope(engine) as session:
        history = read_history(session, "explicit-next-league", "apif,jc")
    assert history.attrs["linked_rows_excluded"] == [PAIRS["韩职"][1]]


@pytest.mark.parametrize("league", PAIRS)
def test_only_existing_linked_representative_is_retained(domestic_db, league):
    engine, _ = domestic_db
    with session_scope(engine) as session:
        _complete_counterpart(session, league)
    with session_scope(engine) as session:
        history = read_history(session, league, "apif,jc")
    jc, apif = PAIRS[league]
    assert jc in set(history.match_id) and apif not in set(history.match_id)
    assert history.attrs["linked_rows_excluded"] == [apif]
    assert history.attrs["sources"] == ["apif", "jc"]


@pytest.mark.parametrize("league", PAIRS)
@pytest.mark.parametrize("mutation", ["unlinked", "wrong_identity", "opaque_identity"])
def test_unlinked_or_inconsistent_overlap_is_still_rejected(domestic_db, league, mutation):
    engine, _ = domestic_db
    with session_scope(engine) as session:
        _complete_counterpart(session, league)
        jc, apif = PAIRS[league]
        if mutation == "unlinked":
            session.get(Match, jc).counterpart_id = None
        else:
            other = Team(
                name="apif:team:987654321" if mutation == "opaque_identity" else "different"
            )
            session.add(other)
            session.flush()
            session.get(Match, apif).home_team_id = other.id
    with session_scope(engine) as session, pytest.raises(ValueError, match="跨来源重复|身份重叠"):
        read_history(session, league, "apif,jc")


@pytest.mark.parametrize("source", ["apif,jc,other", "apif,jc,fd", "apif,jc,fdorg"])
def test_configured_domestic_history_rejects_undeclared_sources(domestic_db, source):
    engine, _ = domestic_db
    with session_scope(engine) as session, pytest.raises(ValueError, match="来源"):
        read_history(session, "韩职", source)


def test_inferred_source_does_not_admit_an_unknown_provider(domestic_db):
    engine, _ = domestic_db
    with session_scope(engine) as session:
        matches = list(session.scalars(select(Match).where(Match.league == "韩职")))
        original = matches[0]
        score = session.get(Result, original.match_id)
        for match in matches:
            match.league = "fixture-other"
        session.add(
            Match(
                match_id="unknown:fixture",
                source="unknown",
                league="韩职",
                home_team_id=original.home_team_id,
                away_team_id=original.away_team_id,
                kickoff_utc=original.kickoff_utc,
                status="finished",
            )
        )
        session.flush()
        session.add(
            Result(
                match_id="unknown:fixture",
                home_goals=score.home_goals,
                away_goals=score.away_goals,
                available_utc=score.available_utc,
                available_source=score.available_source,
            )
        )
    with session_scope(engine) as session, pytest.raises(ValueError, match="来源"):
        read_history(session, "韩职")


@pytest.mark.parametrize("league", PAIRS)
def test_unknown_and_cutoff_or_later_outcomes_are_excluded(domestic_db, league):
    engine, _ = domestic_db
    with session_scope(engine) as session:
        history = read_history(session, league, "apif,jc")
    cutoff = "2023-01-01T00:00:00Z"
    eligible = history.loc[(history.kickoff_utc < cutoff) & (history.available_utc < cutoff)].copy()
    original = DixonColes().fit(eligible, as_of=cutoff)
    poison = eligible.iloc[:3].copy()
    poison["match_id"] = ["apif:late", "apif:equal", "apif:unknown"]
    poison["home_goals"] = 99
    import pandas as pd

    poison["available_utc"] = pd.to_datetime(
        ["2023-01-02T00:00Z", cutoff, None], utc=True, format="ISO8601"
    )
    combined = pd.concat([eligible, poison], ignore_index=True)
    combined.attrs = eligible.attrs
    fitted = DixonColes().fit(combined, as_of=cutoff)
    assert fitted.training_rows == original.training_rows
    assert fitted.engine.get_params() == original.engine.get_params()
    assert fitted.unknown_team_prior is True


def test_real_archive_season_boundaries_and_fixture_provenance():
    proof = json.loads((FIXTURE / "provenance.json").read_text())
    assert (
        hashlib.sha256((FIXTURE / "history.jsonl").read_bytes()).hexdigest()
        == proof["history_sha256"]
    )
    for league, seasons in proof["season_bounds"].items():
        for season, summary in seasons.items():
            assert season_for(summary["min_utc"], league) == season
            assert season_for(summary["max_utc"], league) == season
    assert str(season_bounds("2025", "韩职")[0]) == "2025-01-01 00:00:00+00:00"
    assert str(season_bounds("2526", "澳超")[0]) == "2025-07-01 00:00:00+00:00"
    assert latest_complete_seasons(NOW, "韩职") == ["2024", "2025"]
    assert latest_complete_seasons(NOW, "澳超") == ["2425", "2526"]


@pytest.mark.parametrize(
    "league",
    ["英超", "西甲", "意甲", "德甲", "法甲", "荷甲", "葡超", "日职", "中超", "丹超", "未配置联赛"],
)
def test_daily_rejects_apif_outside_explicit_domestic_list(tmp_path, league):
    from zucai import cloud_predict

    params = tmp_path / "params.json"
    params.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "leagues": {
                    league: {
                        "half_life_days": 365,
                        "selection_as_of_utc": "2026-01-01T00:00Z",
                        "sources": ["apif", "jc"],
                    }
                },
            }
        )
    )
    with pytest.raises(ValueError, match="训练来源"):
        cloud_predict.run(tmp_path / "absent_archive", params, "must-reject")


def test_daily_restored_real_identities_predict_both_domestic_leagues(
    domestic_db, tmp_path, monkeypatch
):
    from zucai import cloud_predict

    engine, path = domestic_db
    archive = tmp_path / "archive"
    export_feeds(path, archive, date(2026, 11, 7), "fixture-replay")
    restore = cloud_predict.restore_feeds

    def restore_historical_slate(source, destination):
        feed = restore(source, destination)
        replay_engine = get_engine(destination)
        try:
            # A test-only historical-flow replay after normal archive validation:
            # retain all score/availability fields and change only these statuses.
            with session_scope(replay_engine) as session:
                for jc, _ in PAIRS.values():
                    session.get(Match, jc).status = "scheduled"
        finally:
            replay_engine.dispose()
        return feed

    monkeypatch.setattr(cloud_predict, "restore_feeds", restore_historical_slate)
    params = tmp_path / "params.json"
    params.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "leagues": {
                    league: {
                        "half_life_days": 365,
                        "selection_as_of_utc": "2026-01-01T00:00Z",
                        "sources": ["apif", "jc"],
                        "p2_ready": False,
                    }
                    for league in PAIRS
                },
            }
        )
    )
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    result = cloud_predict.run(archive, params, "offline-domestic", sporttery_status="skipped")
    assert result["status"] == "complete"
    for outcome in result["leagues"].values():
        assert outcome["sources"] == ["apif", "jc"]
        assert outcome["p2_label"] == "P2 实验" and outcome["p2_ready"] is False
        model = DixonColes.load(archive / outcome["model_path"])
        assert model.unknown_team_prior is True
        assert model.sources == ["apif", "jc"]
    batch = json.loads(
        (archive / result["forecasts"]["2026-11-07"]["path"] / "forecast.json").read_bytes()
    )
    predictions = {row["match_id"]: row for row in batch["predictions"]}
    for jc, _ in PAIRS.values():
        assert predictions[jc]["status"] == "predicted"
        assert predictions[jc]["p2_label"] == "P2 实验"
    with session_scope(engine) as session:
        assert len(list(session.scalars(select(Result)))) == 120
