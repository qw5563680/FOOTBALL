import json
import runpy
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.db import get_engine, init_db
from zucai.models import Match, Result, Team
from zucai.predict.base import utc
from zucai.predict.experiments.global_rating import RatingResult

_RUNNER = runpy.run_path(Path(__file__).resolve().parents[1] / "examples/t100_rating_replay.py")
TRAINING, VALIDATION = _RUNNER["TRAINING"], _RUNNER["VALIDATION"]
ReplayRow = _RUNNER["ReplayRow"]
evaluation_cutoff = _RUNNER["evaluation_cutoff"]
run_phases = _RUNNER["run_phases"]
validate_review_url = _RUNNER["validate_review_url"]


def test_reject_other_project_review_url():
    assert validate_review_url("https://github.com/qw5563680/FOOTBALL/pull/123#issuecomment-456")
    with pytest.raises(ValueError, match="审查"):
        validate_review_url("https://github.com/other/FOOTBALL/pull/123#issuecomment-456")


def test_weekly_cutoff_respects_league_season_boundaries():
    assert evaluation_cutoff(utc("2026-08-02T09:00Z"), "日职") == utc("2026-07-31T23:59:59.999999Z")
    assert evaluation_cutoff(utc("2026-08-02T09:00Z"), "英超") == utc("2026-07-26T23:59:59.999999Z")


def test_training_connection_cannot_read_validation_labels(tmp_path):
    database = tmp_path / "scores.db"
    engine = get_engine(database)
    init_db(engine)
    with Session(engine) as session:
        session.add_all([Team(id=1, name="甲"), Team(id=2, name="乙")])
        session.flush()
        for i, aware in enumerate(("2026-06-30T15:59:59.999999Z", "2026-06-30T16:00:00Z")):
            match_id = f"jc:{i}"
            session.add(
                Match(
                    match_id=match_id,
                    source="jc",
                    league="英超",
                    home_team_id=1,
                    away_team_id=2,
                    kickoff_utc="2026-06-30T12:00Z",
                    status="finished",
                )
            )
            session.flush()
            session.add(Result(match_id=match_id, home_goals=1, away_goals=0, available_utc=aware))
        session.commit()
    engine.dispose()
    with _RUNNER["score_session"](database, TRAINING.end) as session:
        assert list(session.scalars(select(Result.match_id))) == ["jc:0"]
    with _RUNNER["score_session"](database, VALIDATION.end) as session:
        assert list(session.scalars(select(Result.match_id).order_by(Result.match_id))) == [
            "jc:0",
            "jc:1",
        ]


def _row(game_id, kickoff, *, away_id=2, league="英超", prior=False):
    return ReplayRow(
        match_id=game_id,
        league=league,
        kickoff=utc(kickoff),
        cutoff=evaluation_cutoff(utc(kickoff), league),
        home_id=1,
        away_id=away_id,
        actual=0,
        official=(0.61, 0.25, 0.14),
        default_strength=prior,
    )


def test_tuning_is_frozen_before_holdout_and_common_cases_are_counted(tmp_path):
    history = (
        RatingResult.from_row(
            dict(
                match_id="jc:history",
                source="jc",
                league="英超",
                home_id=1,
                away_id=2,
                kickoff_utc="2023-12-01T12:00Z",
                available_utc="2023-12-01T15:00Z",
                available_source="captured",
                home_goals=2,
                away_goals=1,
            )
        ),
    )
    loaded = []

    def load_phase(phase):
        loaded.append(phase)
        if phase is TRAINING:
            assert not (tmp_path / "parameter-selection.json").exists()
            return [_row("jc:training", "2025-02-02T12:00Z")], lambda _: history, {}
        assert phase is VALIDATION
        freeze = json.loads((tmp_path / "parameter-selection.json").read_text())
        assert len(freeze["grid"]) == 36
        assert freeze["common"] == 1
        return (
            [
                _row("jc:before-freeze", "2026-06-30T18:00Z"),
                _row("jc:missing-team", "2026-07-09T12:00Z", away_id=3),
                _row("jc:holdout1", "2026-07-10T12:00Z", prior=True),
                _row("jc:holdout2", "2026-07-10T13:00Z", league="英冠"),
            ],
            lambda _: history,
            {},
        )

    report = run_phases(load_phase, tmp_path)
    assert loaded == [TRAINING, VALIDATION]
    assert report["validation"]["overall"]["common"] == 2
    assert report["validation"]["default_strength"]["common"] == 1
    assert set(report["validation"]["by_league"]) == {"英冠", "英超"}
    assert report["validation"]["excluded"] == {
        "cutoff_before_parameter_freeze": 1,
        "rating_unavailable": 1,
    }
    rows = [
        json.loads(line) for line in Path(tmp_path / "validation.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 4
    assert all("blend" not in row for row in rows)


def test_restored_archive_replays_official_model_and_counts_unconfigured_only(tmp_path):
    source = tmp_path / "source/scores.db"
    archive = tmp_path / "archive"
    archive.mkdir()
    restored = tmp_path / "restored/scores.db"
    result = tmp_path / "report"
    result.mkdir()
    engine = get_engine(source)
    init_db(engine)
    with Session(engine) as session:
        session.add_all([Team(id=1, name="甲"), Team(id=2, name="乙")])
        session.flush()
        fixtures = [
            (f"jc:warmup-{i}", "英超", utc("2024-01-01T12:00Z") + timedelta(days=i * 7))
            for i in range(9)
        ]
        fixtures.extend(
            [
                ("jc:tuning", "英超", utc("2025-03-05T12:00Z")),
                ("jc:validation", "英超", utc("2026-07-15T12:00Z")),
                ("jc:count-only", "英足总杯", utc("2026-07-15T12:00Z")),
            ]
        )
        for i, (match_id, league, kickoff) in enumerate(fixtures):
            session.add(
                Match(
                    match_id=match_id,
                    source="jc",
                    league=league,
                    home_team_id=1,
                    away_team_id=2,
                    kickoff_utc=kickoff.to_pydatetime(),
                    status="finished",
                )
            )
            session.flush()
            session.add(
                Result(
                    match_id=match_id,
                    home_goals=i % 3,
                    away_goals=1,
                    available_source="captured",
                    available_utc=(kickoff + timedelta(hours=3)).to_pydatetime(),
                )
            )
        session.commit()
    engine.dispose()
    export_feeds(source, archive, date(2026, 11, 27), "fixture")
    restore_feeds(archive, restored)
    entries = {"英超": {"sources": ["jc"]}}
    report = run_phases(lambda phase: _RUNNER["_load_phase"](restored, phase, entries), result)
    validation = report["validation"]
    assert validation["overall"]["common"] == 1
    assert validation["diagnostics"]["unconfigured_jc"] == {
        "total": 1,
        "both_rated": 1,
        "cutoff_before_parameter_freeze": 0,
    }
    row = json.loads((result / "validation.jsonl").read_text())
    assert row["match_id"] == "jc:validation"
    assert row["actual"] == 1
    assert row["cutoff_utc"] == "2026-07-12T23:59:59.999999+00:00"
    assert row["official"] and row["rating"]
