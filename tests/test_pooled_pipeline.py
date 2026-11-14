"""T21 integration boundaries, using only labeled offline fixture inputs."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Result, SfcIssue, Team
from zucai.predict import backtest, parameters, tuning
from zucai.predict.command import load_corpus
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.pooled import CUPS, POOL, POOL_FITTING_METHOD, select_training_history
from zucai.report.batch import _models, predict_batch
from zucai.report.render import _validate_batch, load_batch, write_report
from zucai.report.sfc import build_sfc_report
from zucai.result_source_identity import record_result_identity
from zucai.result_source_links import record_result_link

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t21"
MODEL_CUTOFF = pd.Timestamp("2024-02-15T00:00:00Z")
GATE_CUTOFF = "2026-11-03T00:00:00+00:00"


@pytest.fixture
def contract():
    return json.loads((FIXTURES / "pipeline-contract.json").read_bytes())


@pytest.fixture
def history():
    data = pd.read_csv(
        FIXTURES / "model-history.csv", comment="#", parse_dates=["kickoff_utc", "available_utc"]
    )
    data["counterpart_id"] = None
    # Exercise all three target labels without manufacturing extra observations.
    data.loc[data.index % 8 == 7, "league"] = "欧协联"
    data.attrs["sources"] = ["apif", "fd", "fdorg", "jc"]
    data.attrs["training_leagues"] = sorted(set(data.league))
    return data


def _config(path, entry):
    path.write_text(
        json.dumps({"schema_version": 1, "leagues": {POOL: entry}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _record_synthetic_api_identity(session, match, verified_utc):
    """Explicit invented identity evidence for the labeled T21 fixture only."""
    teams = [session.get(Team, team_id) for team_id in (match.home_team_id, match.away_team_id)]
    if any(team.name.startswith(("apif:", "fdorg:", "fd:", "jc:")) for team in teams):
        return  # The fixture deliberately keeps these provisional identities unverified.
    for team in teams:
        alias = f"{match.source}:{match.league}:team:{team.id}"
        if alias not in team.aliases:
            team.aliases = [*team.aliases, alias]
    verified = pd.Timestamp(verified_utc)
    if verified.tzinfo is None:
        verified = verified.tz_localize("UTC")
    record_result_identity(session, match, verified.to_pydatetime())


@pytest.fixture
def database(tmp_path, history, contract):
    path = tmp_path / "fixture.db"
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        teams = {name: Team(name=name) for name in sorted(set(history.home) | set(history.away))}
        session.add_all(teams.values())
        session.flush()
        for row in history.to_dict("records") + contract["upcoming"]:
            session.add(
                Match(
                    match_id=row["match_id"],
                    source=row["source"],
                    league=row["league"],
                    home_team_id=teams[row["home"]].id,
                    away_team_id=teams[row["away"]].id,
                    kickoff_utc=row["kickoff_utc"],
                    status="finished" if "home_goals" in row else "scheduled",
                )
            )
            session.flush()
            if "home_goals" in row:
                session.add(
                    Result(
                        match_id=row["match_id"],
                        home_goals=row["home_goals"],
                        away_goals=row["away_goals"],
                        available_utc=row["available_utc"],
                        available_source=row["available_source"],
                    )
                )
                if row["source"] in {"apif", "fdorg"}:
                    _record_synthetic_api_identity(
                        session, session.get(Match, row["match_id"]), row["available_utc"]
                    )
        for quote in contract["quotes"]:
            session.add_all(
                Odds(
                    match_id=quote["match_id"],
                    pool="had",
                    line=0,
                    selection=selection,
                    price=price,
                    snapshot_utc=quote["snapshot_utc"],
                )
                for selection, price in zip("hda", quote["prices"], strict=False)
            )
    engine.dispose()
    return path


def test_training_contains_domestic_but_scoring_only_cups_and_missing_market_is_null(history):
    report = backtest.evaluate(
        history,
        {},
        since="2024-02-01T00:00:00Z",
        as_of="2024-03-01T00:00:00Z",
        fold="monthly",
        target_leagues=list(CUPS),
    )
    expected = history.loc[
        (history.kickoff_utc >= pd.Timestamp("2024-02-01T00:00Z"))
        & (history.kickoff_utc < pd.Timestamp("2024-03-01T00:00Z"))
        & history.league.isin(CUPS)
    ]
    assert {row["match_id"] for row in report["predictions"]} == set(expected.match_id)
    assert {row["league"] for row in report["predictions"]} == set(CUPS)
    assert report["model"]["n"] == report["in_period"] == len(expected)
    assert report["folds"][0]["training_leagues"]["英超"] == 8
    assert report["folds"][0]["training_leagues"]["德甲"] == 8
    assert report["market"]["n"] == report["paired_model"]["n"] == 0
    assert report["market"]["log_loss"] is report["market"]["brier"] is None
    assert all(row["market"] is None for row in report["predictions"])
    assert sum(report["by_league"][league]["2324"]["model"]["n"] for league in CUPS) == len(
        expected
    )


def test_only_jc_latest_complete_strictly_prekickoff_quote_is_comparison(database, history):
    engine = get_engine(database)
    try:
        with Session(engine) as session:
            actual = backtest.read_market(session, history, jc_only=True)
            unrestricted = backtest.read_market(session, history)
    finally:
        engine.dispose()
    assert actual == {"jc:synthetic-t21-2": (0.5, 0.25, 0.25)}
    assert "apif:synthetic-t21-3" in unrestricted


def test_no_successful_fold_does_not_claim_completed_pooled_fitting(history):
    report = backtest.evaluate(
        history.head(4),
        {},
        since="2024-01-01T00:00:00Z",
        as_of="2024-02-01T00:00:00Z",
        fold="monthly",
        target_leagues=list(CUPS),
    )
    assert report["model"]["n"] == 0
    assert report["skipped"]["warmup"] > 0
    assert report["fitting_method"] == "mixed_or_no_fit"


def test_selection_is_physically_before_next_season_and_market_never_selects(history, monkeypatch):
    future = history.copy()
    future["kickoff_utc"] += pd.DateOffset(years=1)
    future["available_utc"] += pd.DateOffset(years=1)
    future["match_id"] += "-next-season"
    combined = pd.concat([history, future], ignore_index=True)
    # A season-S match learned only in S+1 must also be absent from the selection frame.
    combined.loc[0, "available_utc"] = pd.Timestamp("2024-07-02T00:00Z")
    calls = []

    def measured(data, market, **kwargs):
        calls.append((data.copy(), market, kwargs))
        return dict(
            model=dict(n=20, log_loss=1 if kwargs["half_life_days"] == 90 else 1.1, draw_bias=0.04),
            in_period=20,
            unavailable_labels=0,
            skipped={},
            folds=[],
        )

    monkeypatch.setattr(tuning, "evaluate", measured)
    kwargs = dict(
        grid=[90, 365], seasons=["2324"], as_of="2025-07-02T00:00Z", target_leagues=list(CUPS)
    )
    first = tuning.rolling_origin(combined, {}, market={"jc:example": (0.1, 0.2, 0.7)}, **kwargs)
    second = tuning.rolling_origin(combined, {}, market={"jc:example": (0.8, 0.1, 0.1)}, **kwargs)
    assert first["league"] == POOL and first["half_life_days"] == 90
    assert first["windows"][0]["candidates"] == second["windows"][0]["candidates"]
    for data, market, options in [calls[i] for i in (0, 1, 3, 4)]:
        assert data.kickoff_utc.max() < pd.Timestamp("2024-07-01T00:00Z")
        assert data.available_utc.max() < pd.Timestamp("2024-07-01T00:00Z")
        assert options["target_leagues"] == list(CUPS)
        assert market == {} and options["draw_calibration"] is False
    assert calls[2][1] != calls[5][1]
    assert calls[2][2]["draw_calibration"] is True
    assert calls[2][2]["seasons"] == ["2425"]


def test_pooled_gate_accepts_exact_500_and_300_and_preserves_domestic_threshold(contract):
    report = contract["gate_report"]
    gate = parameters.p2_gate(report, league=POOL, as_of=GATE_CUTOFF)
    assert gate["p2_ready"] is True and gate["bet_allowed"] is False
    assert gate["metrics"]["model"]["n"] == 500
    assert gate["metrics"]["market"]["n"] == 300
    assert gate["seasons"] == ["2425", "2526"]
    domestic = copy.deepcopy(report)
    for row in domestic["seasons"].values():
        row["closing"] = row.pop("market")
        row["closing_paired_model"] = row.pop("paired_model")
    old_gate = parameters.p2_gate(domestic, league="英超", as_of=GATE_CUTOFF)
    assert old_gate["p2_ready"] is False
    assert "closing_paired_n_below_500_or_mismatch" in old_gate["reasons"]


@pytest.mark.parametrize(
    "failure", ["model_n", "market_n", "market_gap", "gain", "season", "skip", "labels"]
)
def test_pooled_gate_rejects_insufficient_or_incompatible_evidence(contract, failure):
    report = contract["gate_report"]
    row = report["seasons"]["2526"]
    if failure == "model_n":
        row["model"]["n"] -= 1
        row["uniform"]["n"] -= 1
        row["in_period"] -= 1
    elif failure == "market_n":
        row["market"]["n"] -= 1
        row["paired_model"]["n"] -= 1
    elif failure == "market_gap":
        row["paired_model"]["log_loss"] = 1.1
    elif failure == "gain":
        row["model"]["log_loss"] = 1.2
    elif failure == "season":
        report["seasons"]["2324"] = report["seasons"].pop("2526")
    elif failure == "skip":
        row["skipped"] = {"fit_failed": 1}
    else:
        row["unavailable_labels"] = 1
    result = parameters.p2_gate(report, league=POOL, as_of=GATE_CUTOFF)
    assert not result["p2_ready"]
    expected = {
        "model_n": "evaluation_n_below_500",
        "market_n": "market_paired_n_below_300_or_mismatch",
        "market_gap": "market_gap_above_0.05_or_missing",
        "gain": "uniform_gain_below_0.05",
        "season": "missing_complete_seasons",
        "skip": "skipped_evaluation_games",
        "labels": "unavailable_evaluation_labels",
    }
    assert expected[failure] in result["reasons"]


@pytest.mark.parametrize(
    "mismatch", [None, "sources", "scope", "targets", "seasons", "gate", "method"]
)
def test_pooled_readiness_binds_scope_sources_targets_and_latest_evidence(
    tmp_path, contract, mismatch
):
    entry = contract["parameters"]
    entry["p2_ready"] = True
    entry["p2_evidence"] = copy.deepcopy(
        parameters.p2_gate(contract["gate_report"], league=POOL, as_of=GATE_CUTOFF)
    )
    model = SimpleNamespace(
        league=POOL,
        pooled=True,
        fitting_method=POOL_FITTING_METHOD,
        prediction_leagues=list(CUPS),
        training_league_scope=entry["training_leagues"].copy(),
        as_of="2026-11-03T00:00:00+00:00",
        half_life_days=90,
        sources=entry["sources"].copy(),
        training_policy=entry["training_policy"],
        availability_basis="mixed",
        replayed_training_share=0,
        unknown_team_prior=True,
        prior_parameters={"method": entry["unknown_team_prior"]},
        draw_calibration=False,
        database_sha256="b" * 64,
    )
    if mismatch == "sources":
        model.sources.remove("apif")
    elif mismatch == "scope":
        model.training_league_scope.remove("英超")
    elif mismatch == "targets":
        model.prediction_leagues.remove("欧协联")
    elif mismatch == "seasons":
        entry["p2_evidence"]["seasons"] = ["2324", "2425"]
    elif mismatch == "gate":
        entry["p2_evidence"]["gate"]["min_market_paired"] = 200
    elif mismatch == "method":
        model.fitting_method = "penaltyblog_slsqp"
    result = parameters.model_readiness(
        model, as_of=GATE_CUTOFF, params_file=_config(tmp_path / "params.json", entry)
    )
    assert result["p2_ready"] is (mismatch is None)
    assert result["training_database_sha256"] != result["evaluation_database_sha256"]
    assert result["bet_allowed"] is False


def test_registry_batch_render_and_sfc_preserve_actual_cup_names(
    database, tmp_path, history, contract
):
    model = DixonColes(pooled=True).fit(history, as_of=MODEL_CUTOFF)
    artifact = tmp_path / "cups.pkl"
    model.save(artifact)
    params = _config(tmp_path / "params.json", contract["parameters"])
    registry = _models([artifact], MODEL_CUTOFF, params)
    assert set(registry) == {("jc", league) for league in CUPS}
    assert len({id(item[0]) for item in registry.values()}) == 1
    engine = get_engine(database)
    try:
        with Session(engine) as session:
            batch = predict_batch(
                session,
                model_files=[artifact],
                as_of=MODEL_CUTOFF,
                generated=MODEL_CUTOFF,
                match_ids=[row["match_id"] for row in contract["upcoming"]],
                params_file=params,
                persist=False,
            )
            assert len(batch["models"]) == 1 and batch["models"][0]["league"] == POOL
            assert {row["league"] for row in batch["predictions"]} == set(CUPS)
            assert all(row["status"] == "predicted" for row in batch["predictions"])
            assert all(len(row["top_scores"]) >= 5 for row in batch["predictions"])
            frozen = write_report(batch, tmp_path / "report")
            assert load_batch(frozen) == batch
            wrong_sources = copy.deepcopy(batch)
            wrong_sources["models"][0]["sources"] = ["fdorg"]
            with pytest.raises(ValueError, match="P2门槛"):
                _validate_batch(wrong_sources)
            wrong_targets = copy.deepcopy(batch)
            wrong_targets["predictions"] = []
            wrong_targets["models"][0]["prediction_leagues"] = ["英超"]
            with pytest.raises(ValueError, match="目标赛事"):
                _validate_batch(wrong_targets)
            issue = SfcIssue(
                issue_no="synthetic-t21",
                observed_utc=MODEL_CUTOFF.isoformat(),
                sale_end_utc="2024-02-19T20:00:00+00:00",
                snapshot=[
                    dict(
                        seq=i,
                        match_id=row["match_id"],
                        kickoff_utc=row["kickoff_utc"],
                        matched_identity={
                            key: row[key]
                            for key in ("home", "away", "source", "league", "kickoff_utc")
                        },
                    )
                    for i, row in enumerate(contract["upcoming"], start=1)
                ],
            )
            report = build_sfc_report(
                session,
                issue,
                batch,
                generated=MODEL_CUTOFF,
                forecast_sha256=hashlib.sha256(frozen.read_bytes()).hexdigest(),
            )
            assert all(row["probabilities"] is not None for row in report["rows"])
            assert report["combo"]["lines"] <= 64 and report["bet_allowed"] is False
            model.sources = ["fdorg"]
            model.save(tmp_path / "no-jc.pkl")
            assert _models([tmp_path / "no-jc.pkl"], MODEL_CUTOFF, params, allow_empty=True) == {}
    finally:
        engine.dispose()


@pytest.mark.parametrize("linked", [True, False])
@pytest.mark.parametrize("minutes", [0, 60])
def test_pooled_corpus_uses_existing_priority_link_without_mutating_database(
    database, tmp_path, contract, linked, minutes
):
    engine = get_engine(database)
    with session_scope(engine) as session:
        original = session.get(Match, "fd:synthetic-t21-0")
        result = session.get(Result, original.match_id)
        duplicate_id = "fdorg:t21-existing-counterpart"
        original.counterpart_id = duplicate_id if linked else None
        session.add(
            Match(
                match_id=duplicate_id,
                source="fdorg",
                league=original.league,
                home_team_id=original.home_team_id,
                away_team_id=original.away_team_id,
                kickoff_utc=pd.Timestamp(original.kickoff_utc) + pd.Timedelta(minutes=minutes),
                status="finished",
                counterpart_id=original.match_id if linked else None,
            )
        )
        session.flush()
        session.add(
            Result(
                match_id=duplicate_id,
                home_goals=result.home_goals,
                away_goals=result.away_goals,
                available_utc=result.available_utc,
                available_source=result.available_source,
            )
        )
        duplicate = session.get(Match, duplicate_id)
        _record_synthetic_api_identity(session, duplicate, result.available_utc)
        if linked:
            record_result_link(
                session,
                duplicate,
                original,
                pd.Timestamp(result.available_utc).to_pydatetime(),
            )
    engine.dispose()
    before = database.read_bytes()
    kwargs = dict(
        params_file=_config(tmp_path / "params.json", contract["parameters"]), as_of=MODEL_CUTOFF
    )
    if linked:
        games, market, closing, digest = load_corpus(database, POOL, **kwargs)
        # Read keeps both observations so an earlier fold can select its own
        # available version. The cutoff view still uses the existing priority.
        assert {"fd:synthetic-t21-0", duplicate_id} <= set(games.match_id)
        selected = select_training_history(games, MODEL_CUTOFF)
        assert "fd:synthetic-t21-0" in set(selected.match_id)
        assert duplicate_id not in set(selected.match_id)
        assert selected.attrs["linked_rows_excluded"] == [duplicate_id]
        assert market == {"jc:synthetic-t21-2": (0.5, 0.25, 0.25)} and closing == {}
        assert digest == hashlib.sha256(before).hexdigest()
    else:
        with pytest.raises(ValueError, match="跨来源重复"):
            load_corpus(database, POOL, **kwargs)
    assert database.read_bytes() == before


def test_default_training_scope_only_adds_configured_domestic_participants(
    database, tmp_path, contract
):
    entry = contract["parameters"]
    del entry["training_leagues"]
    config = _config(tmp_path / "params.json", entry)
    document = json.loads(config.read_bytes())
    domestic = {"half_life_days": 90, "selection_as_of_utc": "2023-07-01T00:00Z"}
    document["leagues"].update({"英超": domestic, "德甲": domestic, "日职": domestic})
    config.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    games, _, _, _ = load_corpus(database, POOL, params_file=config, as_of=MODEL_CUTOFF)
    assert games.attrs["training_leagues"] == sorted([*CUPS, "英超", "德甲"])
    assert "日职" not in set(games.league)


def test_daily_training_counts_cup_predictions_under_one_pool_and_freezes_two_days(
    database, tmp_path, contract, monkeypatch
):
    from zucai import cloud_predict
    from zucai.cloud_feeds import export_feeds
    from zucai.forecast_archive import verify_forecasts

    now = pd.Timestamp("2024-02-19T18:00:00Z")
    archive = tmp_path / "archive"
    export_feeds(database, archive, now.tz_convert("Asia/Shanghai").date(), "synthetic-t21")
    config = _config(tmp_path / "params.json", contract["parameters"])
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(cloud_predict, "_now", lambda: now)
    original = cloud_predict.train
    trained = []

    def tracked(**kwargs):
        trained.append(kwargs["league"])
        return original(**kwargs)

    monkeypatch.setattr(cloud_predict, "train", tracked)
    outcome = cloud_predict.run(archive, config, "offline-t21", sporttery_status="success")
    assert trained == [POOL]
    assert outcome["status"] == "complete"
    assert outcome["leagues"][POOL]["predicted"] == 13
    assert set(outcome["forecasts"]) == {"2024-02-20", "2024-02-21"}
    assert set(outcome["leagues"][POOL]["training_leagues"]) == {*CUPS, "英超", "德甲"}
    assert verify_forecasts(archive)["runs"]["offline-t21"] == outcome
    text = summary.read_text(encoding="utf-8")
    assert "欧战训练语料" in text and "比分前五" in text
    assert all(name in text for name in CUPS)
    for forecast in outcome["forecasts"].values():
        batch = load_batch(archive / forecast["path"] / "forecast.json")
        assert len(batch["models"]) == 1 and batch["models"][0]["league"] == POOL
        assert all(row["league"] in CUPS for row in batch["predictions"])


def test_daily_pool_requires_jc_before_restoring_or_training(tmp_path, contract, monkeypatch):
    from zucai import cloud_predict

    entry = contract["parameters"]
    entry["sources"] = ["fdorg"]
    config = _config(tmp_path / "params.json", entry)

    def unexpected_restore(*args, **kwargs):
        pytest.fail("invalid registration sources must fail before restoring")

    monkeypatch.setattr(cloud_predict, "restore_feeds", unexpected_restore)
    with pytest.raises(ValueError, match="来源须包含 jc"):
        cloud_predict.run(tmp_path / "archive", config, "invalid-sources")
