import pickle
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

from test_prediction import CUTOFF
from test_sfc_report import NOW
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Result, Team
from zucai.predict.backtest import read_history, read_market
from zucai.predict.dixon_coles import DixonColes
from zucai.report.batch import _models, predict_batch
from zucai.report.render import load_batch, write_report

pytest_plugins = [
    "test_prediction",
    "test_prediction_batch",
    "test_report_render",
    "test_sfc_report",
]


@pytest.fixture
def mixed_db(games, tmp_path):
    games["source"] = ["fd", "jc"] * 60
    games["match_id"] = games.source + ":" + games.index.astype(str)
    engine = get_engine(tmp_path / "mixed.db")
    init_db(engine)
    with session_scope(engine) as s:
        teams = {name: Team(name=name) for name in "ABCD"}
        s.add_all(teams.values())
        s.flush()
        for r in games.itertuples():
            s.add(
                Match(
                    match_id=r.match_id,
                    source=r.source,
                    league=r.league,
                    kickoff_utc=r.kickoff_utc,
                    home_team_id=teams[r.home].id,
                    away_team_id=teams[r.away].id,
                    status="finished",
                )
            )
        s.flush()
        for r in games.itertuples():
            s.add(
                Result(
                    match_id=r.match_id,
                    home_goals=int(r.home_goals),
                    away_goals=int(r.away_goals),
                    available_utc=r.available_utc,
                )
            )
        for selection, price in zip("hda", [2.0, 3.0, 6.0], strict=True):
            s.add(
                Odds(
                    match_id=games.iloc[1].match_id,
                    pool="had",
                    line=0,
                    selection=selection,
                    price=price,
                    snapshot_utc=games.iloc[1].kickoff_utc - timedelta(hours=1),
                )
            )
    yield engine
    engine.dispose()


def test_read_fit_multi_source_and_fd_first_market(mixed_db):
    statements = []
    event.listen(
        mixed_db,
        "before_cursor_execute",
        lambda conn, cursor, statement, params, context, many: statements.append(statement),
    )
    with session_scope(mixed_db) as s:
        with pytest.raises(ValueError, match="来源"):
            read_history(s, "L")
        history = read_history(s, "L", " jc, fd, jc ")
        assert set(history.source) == {"jc", "fd"}
        assert history.iloc[0].source == "fd"
        market = read_market(s, history)
        assert history.iloc[1].match_id in market
    model = DixonColes().fit(history, as_of=CUTOFF)
    assert model.sources == ["fd", "jc"]
    assert model.training_source_counts == {"fd": 35, "jc": 35}
    assert not any("closing_odds" in query.lower() for query in statements)


@pytest.mark.parametrize("source", ["", " ", "jc,", ",jc", "jc,,fd", 2])
def test_source_selection_rejects_malformed(source, mixed_db):
    with session_scope(mixed_db) as s, pytest.raises(ValueError, match="来源"):
        read_history(s, "L", source)


def test_fit_keeps_declared_corpus_separate_from_eligible_sources(games):
    games["source"] = "fd"
    games.loc[games.kickoff_utc >= CUTOFF, "source"] = "jc"
    model = DixonColes().fit(games, as_of=CUTOFF)
    assert model.sources == ["fd", "jc"]
    assert model.training_source_counts == {"fd": 70}
    target = dict(home="A", away="B", league="L", kickoff_utc=CUTOFF + timedelta(hours=1))
    assert model.predict_score_matrix({**target, "source": "jc"}).sum() == pytest.approx(1)
    with pytest.raises(ValueError, match="来源"):
        model.predict_score_matrix({**target, "source": "other"})


def test_multiple_leagues_remain_rejected(games, mixed_db):
    games.loc[0, "league"] = "other"
    with pytest.raises(ValueError, match="联赛"):
        DixonColes().fit(games, as_of=CUTOFF)
    with session_scope(mixed_db) as s:
        s.get(Match, "fd:0").league = "other"
    with session_scope(mixed_db) as s, pytest.raises(ValueError, match="联赛"):
        read_history(s, source="jc,fd")


def test_explicit_selected_but_absent_source_is_preserved(mixed_db):
    with session_scope(mixed_db) as s:
        history = read_history(s, "L", "jc,fd,archive")
    model = DixonColes().fit(history, as_of=CUTOFF)
    assert model.sources == ["archive", "fd", "jc"]
    assert "archive" not in model.training_source_counts


def test_legacy_model_source_loads_as_singleton(setup_batch, tmp_path):
    _, _, model, _ = setup_batch
    model.source = "jc"
    model.__dict__.pop("sources", None)
    path = tmp_path / "legacy.pkl"
    path.write_bytes(pickle.dumps(model))
    restored = DixonColes.load(path)
    assert restored.sources == ["jc"]


def test_registry_membership_single_metadata_and_overlap(setup_batch, tmp_path):
    engine, path, model, _ = setup_batch
    from test_prediction_batch import CUTOFF as batch_cutoff

    model.sources = ["fd", "jc"]
    model.save(path)
    with session_scope(engine) as s:
        original = s.get(Match, "jc:1")
        for source in ["fd", "other"]:
            s.add(
                Match(
                    match_id=source + ":target",
                    source=source,
                    league="L",
                    home_team_id=original.home_team_id,
                    away_team_id=original.away_team_id,
                    kickoff_utc=original.kickoff_utc,
                    status="scheduled",
                )
            )
        s.flush()
        batch = predict_batch(
            s,
            model_files=[path],
            as_of=batch_cutoff,
            generated=batch_cutoff,
            match_ids=["jc:1", "fd:target", "other:target"],
        )
    assert [p["status"] for p in batch["predictions"]] == ["predicted", "predicted", "excluded"]
    assert len(batch["models"]) == 1
    assert batch["models"][0]["sources"] == ["fd", "jc"]
    assert "source" not in batch["models"][0]
    assert batch["schema_version"] == 2
    second = tmp_path / "second.pkl"
    model.sources = ["fd"]
    model.save(second)
    with pytest.raises(ValueError, match="来源|模型"):
        _models([path, second], batch_cutoff)


def test_current_sources_report_roundtrip_and_legacy_unchanged(frozen_batch, tmp_path):
    old = deepcopy(frozen_batch)
    frozen_batch["schema_version"] = 2
    model = frozen_batch["models"][0]
    model["sources"] = ["fd", model.pop("source")]
    path = write_report(frozen_batch, tmp_path / "v2")
    assert load_batch(path) == frozen_batch
    assert "fd,jc" in (path.parent / "report.md").read_text(encoding="utf-8")
    path = write_report(old, tmp_path / "v1")
    assert load_batch(path) == old
    root = Path(__file__).parents[1] / "reports/2026-11-01-t06-acceptance"
    for path in root.glob("*/forecast.json"):
        before = path.read_bytes()
        legacy = load_batch(path)
        from zucai.report.render import _markdown

        assert _markdown(legacy) == (path.parent / "report.md").read_text(encoding="utf-8")
        assert path.read_bytes() == before


def test_sfc_consumer_accepts_source_membership(complete_issue):
    from zucai.report.sfc import build_sfc_report

    engine, issue, batch = complete_issue
    batch["schema_version"] = 2
    for model in batch["models"]:
        model["sources"] = ["fd", model.pop("source")]
    with session_scope(engine) as s:
        report = build_sfc_report(
            s, issue, batch, forecast_sha256="a" * 64, generated=NOW, max_lines=64
        )
    assert all(row["probabilities"] for row in report["rows"])


def test_train_cli_accepts_multiple_sources_and_emits_counts(mixed_db, tmp_path, capsys):
    import json

    from zucai.cli import main

    output = tmp_path / "mixed.pkl"
    assert (
        main(
            [
                "train",
                "--db",
                mixed_db.url.database,
                "--league",
                "L",
                "--source",
                " jc,fd,jc ",
                "--since",
                "2020-02-03",
                "--as-of",
                CUTOFF.isoformat(),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["sources"] == ["fd", "jc"]
    assert report["training_source_counts"] == {"fd": 35, "jc": 35}
    assert "source" not in report
    assert DixonColes.load(output).sources == ["fd", "jc"]


@pytest.mark.parametrize("sources", [[], "jc,fd", ["jc", "jc"], ["jc", ""], [None]])
def test_v2_report_rejects_invalid_sources(frozen_batch, tmp_path, sources):
    frozen_batch["schema_version"] = 2
    model = frozen_batch["models"][0]
    model.pop("source")
    model["sources"] = sources
    with pytest.raises(ValueError, match="来源"):
        write_report(frozen_batch, tmp_path / "invalid")


@pytest.mark.parametrize(
    "duplicate_source,should_fail,minutes",
    [("jc", True, 120), ("jc", True, 180), ("jc", False, 181), ("fd", False, 120)],
)
def test_history_rejects_later_cross_source_identity_overlap(
    mixed_db, duplicate_source, should_fail, minutes
):
    with session_scope(mixed_db) as s:
        original = s.get(Match, "fd:0")
        result = s.get(Result, original.match_id)
        key = duplicate_source + ":duplicate"
        kickoff = datetime.fromisoformat(original.kickoff_utc.replace("Z", "+00:00"))
        s.add(
            Match(
                match_id=key,
                source=duplicate_source,
                league=original.league,
                home_team_id=original.home_team_id,
                away_team_id=original.away_team_id,
                kickoff_utc=kickoff + timedelta(minutes=minutes),
                status="finished",
            )
        )
        s.flush()
        s.add(
            Result(
                match_id=key,
                home_goals=result.home_goals,
                away_goals=result.away_goals,
                available_utc=kickoff + timedelta(hours=5),
            )
        )
    with session_scope(mixed_db) as s:
        if should_fail:
            with pytest.raises(ValueError, match="重复|重叠"):
                read_history(s, "L", "jc,fd")
        else:
            assert len(read_history(s, "L", "jc,fd")) == 121


@pytest.mark.parametrize("minutes,should_fail", [(0, True), (180, True), (181, False)])
def test_direct_fit_validates_unsorted_cross_source_overlap(games, minutes, should_fail):
    import pandas as pd

    games["source"] = "fd"
    games["match_id"] = "fd:" + games.index.astype(str)
    duplicate = games.iloc[[0]].copy()
    duplicate["source"] = "jc"
    duplicate["match_id"] = "jc:duplicate"
    duplicate["kickoff_utc"] += timedelta(minutes=minutes)
    duplicate["available_utc"] = duplicate.kickoff_utc + timedelta(hours=3)
    history = pd.concat([games, duplicate], ignore_index=True).sample(frac=1, random_state=9)
    before = history.copy(deep=True)
    if should_fail:
        with pytest.raises(ValueError, match="重复|重叠"):
            DixonColes().fit(history, as_of=CUTOFF)
    else:
        assert DixonColes().fit(history, as_of=CUTOFF).training_rows == 71
    pd.testing.assert_frame_equal(history, before)


def test_v2_null_sources_cannot_fallback_to_legacy_source(frozen_batch, tmp_path):
    frozen_batch["schema_version"] = 2
    frozen_batch["models"][0]["sources"] = None
    assert frozen_batch["models"][0]["source"] == "jc"
    with pytest.raises(ValueError, match="来源|sources"):
        write_report(frozen_batch, tmp_path / "hybrid-invalid")
    assert not (tmp_path / "hybrid-invalid").exists()
