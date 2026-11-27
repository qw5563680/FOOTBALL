"""Daily generation uses immutable primary fixtures and isolates rating artifacts."""

import json
import zipfile
from datetime import date, timedelta

from sqlalchemy import select

from test_cloud_predict import NOW
from test_cloud_predict import scheduled_archive as scheduled_archive
from test_stats_rows import LocalRelease
from zucai import cloud_predict, experimental_archive
from zucai.cloud_feeds import export_feeds
from zucai.db import get_engine, session_scope
from zucai.forecast_archive import verify_forecasts
from zucai.models import Match, Result, Team
from zucai.predict.experiments.global_rating_runtime import fit_and_forecast

MODEL = "elo-global-v1"
VERSION = "t100-cross-league-rating-v1"


def _local_release(monkeypatch, tmp_path):
    release = LocalRelease(tmp_path / "release")
    freeze = experimental_archive.freeze_parallel
    monkeypatch.setattr(
        experimental_archive,
        "freeze_parallel",
        lambda *args, **kwargs: freeze(*args, **kwargs, release_factory=lambda: release),
    )
    return release


def _forecast_paths(archive, run):
    return {
        path: path.read_bytes()
        for entry in run["forecasts"].values()
        for path in (archive / entry["path"]).iterdir()
    }


def _by_day(archive, run, key):
    return [json.loads((archive / entry[key]).read_bytes()) for entry in run["forecasts"].values()]


def _formal_targets(archive, run):
    return {
        row["match_id"]
        for entry in run["forecasts"].values()
        for row in json.loads((archive / entry["path"] / "forecast.json").read_bytes())[
            "predictions"
        ]
        if (row["model_name"], row["status"]) == ("dixon-coles", "predicted")
    }


def test_elo_only_freezes_for_exact_formal_targets(scheduled_archive, tmp_path, monkeypatch):
    archive, params = scheduled_archive
    release = _local_release(monkeypatch, tmp_path)
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    database = tmp_path / "seed.db"
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            match = session.scalars(
                select(Match)
                .where(Match.league == "英超", Match.kickoff_utc > NOW, Match.source == "jc")
                .order_by(Match.kickoff_utc)
            ).first()
            new_team = Team(name="没有历史的球队")
            session.add(new_team)
            session.flush()
            match.away_team_id = new_team.id
            unseen_match, unseen_team = match.match_id, new_team.id
    finally:
        engine.dispose()
    export_feeds(database, archive, date(2026, 11, 2), "seed-rating")

    state = tmp_path / "production-state"
    formal = cloud_predict.run(archive, params, "t100-formal", production_state=state)
    before = _forecast_paths(archive, formal)
    frozen = cloud_predict.run_experiments(archive, params, "t100-formal", state)
    assert frozen["global_rating_version"] == VERSION
    assert all(path.read_bytes() == original for path, original in before.items())
    verified = verify_forecasts(archive)["runs"]["t100-formal"]
    assert verified == frozen
    rows = {
        row["match_id"]: row
        for doc in _by_day(archive, verified, "experimental_path")
        for row in doc["predictions"]
        if row["model_name"] == MODEL
    }
    assert set(rows) == _formal_targets(archive, formal)
    assert len(rows) == 2 and unseen_match in rows
    unavailable = rows.pop(unseen_match)
    (rated,) = rows.values()
    assert unavailable["status"] == "missing"
    assert unavailable["fallback_reason"] == "missing_team_rating"
    assert unavailable["model_probabilities"] is None
    assert unavailable["missing_details"] == [
        {"team_id": unseen_team, "side": "away", "team": "没有历史的球队"}
    ]
    assert rated["status"] == "predicted"
    assert set(rated["model_probabilities"]) == set("hda")
    for row in (rated, unavailable):
        assert row["model_type"] == "TEAM" and row["model_version"] == VERSION
        assert row["p2_ready"] is row["bet_allowed"] is row["paper_eligible"] is False
        assert row["top_score"] is None
        assert row["feature_cutoff"] == row["training_cutoff_utc"] == formal["training_cutoff_utc"]
        assert "market" not in row and "market_reference" not in row
    asset = frozen["experimental_release"]["asset"]
    with zipfile.ZipFile(release.root / asset["tag"] / asset["name"]) as bundle:
        fits = json.loads(bundle.read("training.json"))
        (rating_fit,) = [fit for fit in fits if fit["model_name"] == MODEL]
        assert rating_fit["model_version"] == VERSION
        assert rating_fit["snapshot"]["parameters"] == dict(
            update_scale=16, home_advantage=65, logistic_scale=200, draw_ratio=0.6
        )
        assert rating_fit["snapshot"]["training_rows"] == 84


def test_elo_activation_survives_absent_attachment_without_retroactive_ratings(
    scheduled_archive, tmp_path, monkeypatch
):
    archive, params = scheduled_archive
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    state = tmp_path / "production-state"
    run_id = "t100-absent"
    run = cloud_predict.run(archive, params, run_id, production_state=state)
    assert run["global_rating_version"] == VERSION
    before = _forecast_paths(archive, run)
    assert all("experimental_path" not in f for f in run["forecasts"].values())
    manifest_file = archive / "forecast_manifest.json"
    manifest = verify_forecasts(archive)
    manifest["runs"][run_id].pop("global_rating_version")
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    legacy = cloud_predict.record_experiment_unavailable(
        archive, run_id, state, reason="experiment_timeout"
    )
    assert "global_rating_version" not in legacy
    assert all(path.read_bytes() == original for path, original in before.items())
    assert all(
        row["model_name"] != MODEL
        for doc in _by_day(archive, legacy, "experimental_path")
        for row in doc["predictions"]
    )


def test_daily_score_view_cannot_be_changed_by_future_cross_source_conflict(
    scheduled_archive, tmp_path
):
    database = tmp_path / "seed.db"
    engine = get_engine(database)
    try:
        before = fit_and_forecast(database, [], cutoff=NOW.isoformat())
        with session_scope(engine) as session:
            initial = session.scalars(
                select(Match)
                .where(Match.status == "finished", Match.source == "fd")
                .order_by(Match.match_id)
            ).first()
            session.add(
                Match(
                    match_id="jc:late-conflicting-label",
                    source="jc",
                    league=initial.league,
                    home_team_id=initial.home_team_id,
                    away_team_id=initial.away_team_id,
                    kickoff_utc=initial.kickoff_utc,
                    status="finished",
                )
            )
            session.flush()
            session.add(
                Result(
                    match_id="jc:late-conflicting-label",
                    home_goals=9,
                    away_goals=2,
                    available_utc=NOW + timedelta(hours=1),
                    available_source="captured",
                )
            )
        assert fit_and_forecast(database, [], cutoff=NOW.isoformat()) == before
    finally:
        engine.dispose()
