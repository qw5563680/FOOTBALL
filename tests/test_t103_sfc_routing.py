import hashlib
import pickle
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Result, SfcIssue, Team
from zucai.parlay.single_double import build as build_paper_parlay
from zucai.predict.backtest import read_history
from zucai.predict.dixon_coles import DixonColes
from zucai.prediction_disclosure import uses_unknown_team_prior
from zucai.report.batch import predict_batch
from zucai.report.render import _json_bytes, load_batch, write_report
from zucai.report.sfc import build_sfc_report, pick_nine_multi_plan
from zucai.scoreboard.paper import advance_ledger
from zucai.scoreboard.score import prematch_rows

CUTOFF = datetime(2026, 11, 27, 8, tzinfo=UTC)
KICKOFF = CUTOFF + timedelta(hours=2)


@pytest.fixture
def schedule(tmp_path):
    engine = get_engine(tmp_path / "games.db")
    init_db(engine)
    try:
        with session_scope(engine) as session:
            teams = {name: Team(name=name, aliases=[]) for name in ("A", "B", "fdorg:team:1")}
            session.add_all(teams.values())
            session.flush()

            def match(key, *, home="A", away="B", kickoff=KICKOFF, status="scheduled"):
                session.add(
                    Match(
                        match_id=key,
                        source=key.split(":")[0],
                        league="L",
                        home_team_id=teams[home].id,
                        away_team_id=teams[away].id,
                        kickoff_utc=kickoff,
                        status=status,
                    )
                )

            def result(key, kickoff, home, away, score):
                match(key, home=home, away=away, kickoff=kickoff, status="finished")
                session.add(
                    Result(
                        match_id=key,
                        home_goals=score[0],
                        away_goals=score[1],
                        available_utc=kickoff + timedelta(hours=3),
                        available_source="captured",
                    )
                )

            for index, score in enumerate([(2, 1), (1, 0), (0, 0), (0, 1), (1, 2), (3, 0)] * 4):
                home, away = ("B", "A") if index % 2 else ("A", "B")
                source = "jc" if index % 4 == 0 else "fd"
                result(
                    f"{source}:past-{index}", CUTOFF - timedelta(days=index + 2), home, away, score
                )
            session.flush()
            history = read_history(session, "L", "fd,jc")
            path = tmp_path / "model.pkl"
            DixonColes().fit(history, as_of=CUTOFF - timedelta(days=1)).save(path)
            for key in ("jc:forecast", "fdorg:forecast", "c500:forecast", "other:forecast"):
                match(key)
            match("fdorg:unknown", away="fdorg:team:1")
            match("c500:unknown", away="fdorg:team:1")
            match("fdorg:started", kickoff=CUTOFF)
            for i in range(1, 12):
                match(f"c500:other-{i}", kickoff=KICKOFF + timedelta(minutes=i))
            for key in ("jc:forecast", "fdorg:forecast"):
                for selection, price in zip("hda", (2.0, 3.0, 4.0), strict=True):
                    session.add(
                        Odds(
                            match_id=key,
                            pool="had",
                            selection=selection,
                            line=0,
                            price=price,
                            snapshot_utc=CUTOFF - timedelta(minutes=3),
                        )
                    )
            session.flush()
            yield session, path, history, result
    finally:
        engine.dispose()


def _predict(session, path, keys, *, sfc_only=()):
    return predict_batch(
        session,
        model_files=[path],
        as_of=CUTOFF,
        generated=CUTOFF,
        match_ids=keys,
        sfc_only_ids=sfc_only,
        persist=False,
    )


def test_only_scoped_rows_gain_access_to_the_league_model(schedule):
    session, path, _, _ = schedule
    before = _predict(session, path, ["jc:forecast", "fdorg:forecast"])
    sold_before, unmatched = before["predictions"]
    assert unmatched["status"] == "excluded"
    assert unmatched["reason"] == "缺少同来源同联赛的模型"
    sfc = ["fdorg:forecast", "c500:forecast", "other:forecast", "fdorg:started"]
    after = _predict(session, path, ["jc:forecast", "fdorg:unknown", *sfc], sfc_only=sfc)
    sold, unrelated, fdorg, own, other, started = after["predictions"]
    assert _json_bytes(sold) == _json_bytes(sold_before)
    assert sold["market"] is not None
    assert unrelated["status"] == "excluded"
    assert after["models"] == before["models"]
    assert after["models"][0]["sources"] == ["fd", "jc"]
    for row, source in ((fdorg, "fdorg"), (other, "other")):
        assert (row["status"], row["source"], row["sfc_only"]) == ("predicted", source, True)
        assert row["market"] is row["market_reference"] is None
        for field in ("model_version", "model_probabilities", "score_matrix"):
            assert _json_bytes(row[field]) == _json_bytes(own[field])
    assert (started["status"], started["reason"]) == ("excluded", "预测截点时已开赛")


def test_scoped_rows_use_the_same_unknown_team_prior_as_own_fixtures(schedule):
    session, path, _, _ = schedule
    keys = ["fdorg:unknown", "c500:unknown"]
    mapped, own = _predict(session, path, keys, sfc_only=keys)["predictions"]
    assert mapped["status"] == "predicted"
    assert uses_unknown_team_prior(mapped)
    assert mapped["team_strengths"]["away_default_strength"] is True
    for field in ("model_probabilities", "score_matrix", "team_strengths", "metadata"):
        assert _json_bytes(mapped[field]) == _json_bytes(own[field])


def test_mapped_rows_survive_freeze_and_enter_sfc_combinations(schedule, tmp_path):
    session, path, _, _ = schedule
    keys = ["fdorg:forecast", "other:forecast", "c500:forecast"]
    keys.extend(f"c500:other-{i}" for i in range(1, 12))
    batch = _predict(session, path, keys, sfc_only=keys)
    frozen = write_report(batch, tmp_path / "forecast")
    assert load_batch(frozen) == batch
    unscoped = deepcopy(batch)
    unscoped["predictions"][0].pop("sfc_only")
    with pytest.raises(ValueError, match="逐场定义"):
        write_report(unscoped, tmp_path / "unscoped")
    issue = SfcIssue(
        issue_no="synthetic-t103",
        observed_utc=CUTOFF.isoformat(),
        sale_end_utc=(KICKOFF - timedelta(minutes=30)).isoformat(),
        snapshot=[
            dict(
                seq=seq,
                match_id=row["match_id"],
                kickoff_utc=row["kickoff_utc"],
                matched_identity={
                    k: row[k] for k in ("home", "away", "source", "league", "kickoff_utc")
                },
            )
            for seq, row in enumerate(batch["predictions"], 1)
        ],
    )
    report = build_sfc_report(
        session, issue, batch, generated=CUTOFF, forecast_sha256=_sha256(frozen)
    )
    assert report["rows"][0]["matched_identity"]["source"] == "fdorg"
    assert report["rows"][0]["probabilities"] == report["rows"][2]["probabilities"]
    assert report["combo"]["covers"] == "all_14_results"
    report.update(
        sale_state="published_before_deadline",
        sale_end_utc=issue.sale_end_utc,
        published_utc=CUTOFF.isoformat(),
    )
    assert 1 in [row["seq"] for row in pick_nine_multi_plan(report)["selections"]]
    forecasts = [
        {
            **row,
            **{k: batch[k] for k in ("batch_id", "generated_utc", "as_of_utc", "mode")},
            "published_utc": CUTOFF.isoformat(),
        }
        for row in batch["predictions"]
    ]
    assert {row["match_id"] for row in prematch_rows(forecasts, CUTOFF)} == set(keys)
    assert advance_ledger(forecasts, {}, [], CUTOFF) == []
    paper = build_paper_parlay(batch, {}, {}, [], CUTOFF)
    assert paper["groups"] == []
    assert paper["diagnostics"] == [
        {"match_id": key, "reasons": ["not_jc_or_missing_kickoff"]} for key in sorted(keys)
    ]


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_forecast_scope_does_not_change_training_bytes(schedule, tmp_path):
    session, path, history, result = schedule
    before = _sha256(path)
    _predict(session, path, ["fdorg:forecast"], sfc_only=["fdorg:forecast"])
    result("fdorg:past", CUTOFF - timedelta(days=80), "A", "B", (9, 0))
    session.flush()
    after = read_history(session, "L", "fd,jc")
    assert pickle.dumps(after) == pickle.dumps(history)
    refitted = tmp_path / "refitted.pkl"
    DixonColes().fit(after, as_of=CUTOFF - timedelta(days=1)).save(refitted)
    assert _sha256(refitted) == _sha256(path) == before
