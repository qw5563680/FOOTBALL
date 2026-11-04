import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import zstandard
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.features.stats import StatsFeatures
from zucai.models import Match, MatchStat, Odds, Result, Team
from zucai.predict.backtest import read_history
from zucai.predict.base import utc
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.experiments.command import attach_market, run, sha256
from zucai.predict.experiments.evaluate import WalkForwardExperiment, load_protocol

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t33"
PROTOCOL = Path(__file__).parents[1] / "docs/experiments/T33-evaluation-protocol.json"


@pytest.fixture
def experiment_db(tmp_path):
    database = tmp_path / "experiment.db"
    engine = get_engine(database)
    init_db(engine)
    samples = json.loads((FIXTURES / "intensity.json").read_bytes())["rows"]
    names = json.loads((FIXTURES / "timeline.json").read_bytes())["teams"]
    with session_scope(engine) as session:
        teams = {name: Team(name=name) for name in names}
        session.add_all(teams.values())
        session.flush()
        for i, sample in enumerate(samples):
            kickoff = utc("2023-01-01T15:00:00Z") + np.timedelta64(i, "D")
            home, away = names[i % 3], names[(i + 1) % 3]
            key = f"fd:synthetic-{i}"
            session.add(
                Match(
                    match_id=key,
                    source="fd",
                    league="英超",
                    status="finished",
                    home_team_id=teams[home].id,
                    away_team_id=teams[away].id,
                    kickoff_utc=kickoff,
                )
            )
            session.flush()
            session.add(
                Result(
                    match_id=key,
                    home_goals=sample["home_goals"],
                    away_goals=sample["away_goals"],
                    available_source="assumed",
                    available_utc=kickoff + np.timedelta64(3, "h"),
                )
            )
            for side, team in (("home", home), ("away", away)):
                value = sample[f"{side}_for"]
                session.add(
                    MatchStat(
                        provider="fd",
                        provider_fixture_id=key,
                        endpoint="csv",
                        record_key=f"{side}:shots_on_target",
                        match_id=key,
                        team_id=teams[team].id,
                        side=side,
                        period_basis="90",
                        extra_time_included=False,
                        eligible_90=True,
                        captured_utc="2026-11-04T00:00Z",
                        available_utc=None,
                        availability_basis="backfilled",
                        raw_sha256="a" * 64,
                        raw_location="fixture",
                        parser_version="test",
                        missing_reason=None,
                        version=1,
                        stat_key="shots_on_target",
                        raw_value=value,
                        value=value,
                        unit="count",
                    )
                )
    protocol = load_protocol(PROTOCOL)
    protocol.update(
        training_start="2022-07-01T00:00Z",
        selection_start="2023-03-01T00:00Z",
        selection_end="2023-04-01T00:00Z",
        evaluation_start="2023-04-01T00:00Z",
        evaluation_end="2023-05-01T00:00Z",
    )
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    yield database, engine, path
    engine.dispose()


def test_offline_command_restores_no_tables_and_preserves_frozen_output(experiment_db, tmp_path):
    database, engine, protocol = experiment_db
    before = sha256(database)
    output = tmp_path / "result"
    run(
        database,
        output,
        protocol_path=protocol,
        providers=["fd"],
        leagues=["英超"],
        code_commit="test",
    )
    assert sha256(database) == before
    summary = json.loads((output / "summary.json").read_bytes())["scopes"]["fd-英超"]
    strict = summary["strict"]
    replay = summary["backfill_replay"]
    assert strict["models"]["candidate_raw"]["experimental_n"] == 0
    assert strict["selection_evidence_sufficient"] is False
    assert replay["models"]["candidate_raw"]["experimental_n"] > 0
    assert replay["selection_evidence_sufficient"] is True
    assert replay["models"]["market"]["n"] == 0
    assert replay["models"]["candidate_raw"]["n"] == 30
    group_report = json.loads((output / "group_comparisons.json").read_bytes())
    assert group_report["refitted"] is False and group_report["reselected"] is False
    assert group_report["prediction_code_commit"] == group_report["generator_commit"] == "test"
    for name, digest in group_report["predictions_sha256"].items():
        assert sha256(output / name) == digest
    with (output / "fd-英超-backfill_replay-predictions.jsonl.zst").open("rb") as stream:
        with zstandard.ZstdDecompressor().stream_reader(stream) as reader:
            rows = [json.loads(line) for line in reader.read().decode().splitlines()]
    for row in rows:
        assert utc(row["fit_cutoff"]) < utc(row["feature_cutoff"]) == utc(row["kickoff_utc"])
        for name in ("candidate_raw", "ablation_raw"):
            assert row["variants"][name]["model_version"]
            assert row["variants"][name]["model_type"] == "TEAM"
        assert row["bet_allowed"] is False
    for line in (output / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert sha256(output / name) == digest
    with pytest.raises(FileExistsError):
        run(database, output, protocol_path=protocol, providers=["fd"], code_commit="test")
    assert sha256(database) == before


def test_selection_and_earlier_models_ignore_future_scores(experiment_db):
    _, engine, path = experiment_db
    protocol = load_protocol(path)
    with session_scope(engine) as session:
        features = StatsFeatures(session, provider="fd")
        history = read_history(session, league="英超", source="fd")
    options = dict(
        features=features, protocol=protocol, stat_key="shots_on_target", mode="backfill_replay"
    )
    original = WalkForwardExperiment(history, **options).select()
    altered = history.copy()
    altered.loc[
        altered.kickoff_utc >= utc(protocol["evaluation_start"]), ["home_goals", "away_goals"]
    ] = 9
    changed = WalkForwardExperiment(altered, **options).select()
    assert original == changed
    selected_ids = {
        key
        for t in original["trials"].values()
        for fold in t["folds"]
        for model in fold["models"].values()
        for key in model["training_ids"]
    }
    future = set(history.loc[history.kickoff_utc >= utc(protocol["evaluation_start"]), "match_id"])
    assert not (selected_ids & future)


def test_unknown_result_availability_retains_target_without_scoring(experiment_db):
    _, engine, path = experiment_db
    with session_scope(engine) as session:
        result = session.get(Result, "fd:synthetic-90")
        result.available_utc = None
    with session_scope(engine) as session:
        history = read_history(session, league="英超", source="fd")
        features = StatsFeatures(session, provider="fd")
    experiment = WalkForwardExperiment(
        history,
        features,
        protocol=load_protocol(path),
        stat_key="shots_on_target",
        mode="backfill_replay",
    )
    rows, _ = experiment.run(
        window=5, start="2023-04-01T00:00Z", end="2023-04-02T00:00Z", fold="weekly"
    )
    assert len(rows) == 1 and rows[0]["actual"] is None
    assert rows[0]["fallback_reason"] == "unavailable_result_label"
    assert not rows[0]["experimental"] and not rows[0]["variants"]


def test_baseline_probabilities_are_the_unchanged_fixed90_implementation(experiment_db):
    _, engine, path = experiment_db
    with session_scope(engine) as session:
        history = read_history(session, league="英超", source="fd")
        features = StatsFeatures(session, provider="fd")
    experiment = WalkForwardExperiment(
        history,
        features,
        protocol=load_protocol(path),
        stat_key="shots_on_target",
        mode="backfill_replay",
    )
    rows, _ = experiment.run(
        window=5, start="2023-04-01T00:00Z", end="2023-04-02T00:00Z", fold="weekly"
    )
    baseline = DixonColes(90, unknown_team_prior=True, draw_calibration=True).fit(
        history, as_of=rows[0]["fit_cutoff"]
    )
    fixture = history.loc[history.match_id == rows[0]["match_id"]].iloc[0]
    for suffix, calibrated in (("raw", False), ("calibrated", True)):
        baseline.draw_calibration = calibrated
        grid, _ = baseline.predict_with_metadata(fixture)
        assert np.array_equal(grid, rows[0]["variants"][f"baseline_{suffix}"]["score_grid"])


def test_missing_stat_falls_back_both_arms_with_identical_baseline_version(experiment_db):
    _, engine, path = experiment_db
    with session_scope(engine) as session:
        row = session.scalar(
            select(MatchStat).where(
                MatchStat.match_id == "fd:synthetic-90", MatchStat.side == "home"
            )
        )
        row.value = None
        row.missing_reason = "null"
    with session_scope(engine) as session:
        features = StatsFeatures(session, provider="fd")
        history = read_history(session, league="英超", source="fd")
    experiment = WalkForwardExperiment(
        history,
        features,
        protocol=load_protocol(path),
        stat_key="shots_on_target",
        mode="backfill_replay",
    )
    rows, _ = experiment.run(
        window=5, start="2023-04-01T00:00Z", end="2023-04-10T00:00Z", fold="weekly"
    )
    missing = [r for r in rows if r["statistics_missing"]]
    assert missing
    for row in missing:
        assert row["experimental"] is False
        assert row["missing_reasons"]
        for arm in ("candidate_raw", "ablation_raw"):
            assert (
                row["variants"][arm]["probabilities"]
                == row["variants"]["baseline_raw"]["probabilities"]
            )
            assert (
                row["variants"][arm]["model_version"]
                == row["variants"]["baseline_raw"]["model_version"]
            )
            assert row["variants"][arm]["fallback_reason"] == "statistics_missing"


def test_market_is_only_post_fit_comparison_and_requires_trusted_link(experiment_db):
    _, engine, path = experiment_db
    with session_scope(engine) as session:
        game = session.get(Match, "fd:synthetic-90")
        session.add(
            Match(
                match_id="jc:test",
                source="jc",
                league=game.league,
                status="finished",
                home_team_id=game.home_team_id,
                away_team_id=game.away_team_id,
                kickoff_utc=game.kickoff_utc,
                counterpart_id=game.match_id,
            )
        )
        session.flush()
        for selection, price in zip("hda", [2, 3, 4], strict=True):
            session.add(
                Odds(
                    match_id="jc:test",
                    pool="had",
                    line=0,
                    selection=selection,
                    snapshot_utc=utc(game.kickoff_utc) - np.timedelta64(1, "h"),
                    price=price,
                )
            )
    with session_scope(engine) as session:
        features = StatsFeatures(session, provider="fd")
        history = read_history(session, league="英超", source="fd")
        experiment = WalkForwardExperiment(
            history,
            features,
            protocol=load_protocol(path),
            stat_key="shots_on_target",
            mode="backfill_replay",
        )
        rows, _ = experiment.run(
            window=5, start="2023-04-01T00:00Z", end="2023-04-02T00:00Z", fold="weekly"
        )
        assert rows[0]["variants"]["market"] is None
        before = deepcopy(rows[0]["variants"])
        attach_market(session, rows, features)
        market = rows[0]["variants"]["market"]
        assert market["model_type"] == "MARKET"
        assert market["probabilities"] == pytest.approx(
            np.array([1 / 2, 1 / 3, 1 / 4]) / (1 / 2 + 1 / 3 + 1 / 4)
        )
        for name, entry in before.items():
            if name != "market":
                assert rows[0]["variants"][name] == entry
