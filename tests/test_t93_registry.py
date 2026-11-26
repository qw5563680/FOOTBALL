from copy import deepcopy

import pytest
from sqlalchemy.orm import Session

from test_pooled_pipeline import MODEL_CUTOFF, _config
from test_pooled_pipeline import contract as contract
from test_pooled_pipeline import database as database
from test_pooled_pipeline import history as history
from zucai.db import get_engine
from zucai.models import Match, Odds, SfcIssue, Team
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.pooled import CUPS, POOL
from zucai.prediction_disclosure import uses_unknown_team_prior
from zucai.report.batch import _models, predict_batch
from zucai.report.render import _json_bytes, load_batch, write_report
from zucai.report.sfc import build_sfc_report, disclosed_rows


def test_pooled_scope_fits_both_reports_and_keeps_ordinary_jc_rows_identical(
    database, tmp_path, contract, history
):
    model = DixonColes(pooled=True).fit(history, as_of=MODEL_CUTOFF)
    artifact = tmp_path / "t93.pkl"
    model.save(artifact)
    params = _config(tmp_path / "params.json", contract["parameters"])
    ordinary = _models([artifact], MODEL_CUTOFF, params)
    scoped = _models([artifact], MODEL_CUTOFF, params, sfc_only=True)
    assert set(ordinary) == {("jc", cup) for cup in CUPS}
    assert set(scoped) == {(source, cup) for source in (*model.sources, "c500") for cup in CUPS}
    assert all(item[1]["sources"] == model.sources for item in scoped.values())

    fixture = next(match for match in contract["upcoming"] if match["league"] == "欧罗巴")
    engine = get_engine(database)
    try:
        with Session(engine) as session:
            jc = session.get(Match, fixture["match_id"])
            placeholder = session.query(Team).filter_by(name="apif:team:1").one()
            assert placeholder.name in model.teams
            extra = ("apif:t93", "c500:t93", "apif:t93-placeholder", "unsupported:t93")
            for key in extra:
                session.add(
                    Match(
                        match_id=key,
                        source=key.split(":")[0],
                        league=jc.league,
                        home_team_id=placeholder.id
                        if key.endswith("placeholder")
                        else jc.home_team_id,
                        away_team_id=jc.away_team_id,
                        kickoff_utc=jc.kickoff_utc,
                        status="scheduled",
                    )
                )
            session.flush()
            # The scoped route uses neither these offers nor any no-quote fallback.
            for index, selection in enumerate("hda"):
                session.add(
                    Odds(
                        match_id=extra[0],
                        pool="had",
                        selection=selection,
                        line=0,
                        price=2.0 + index,
                        snapshot_utc=MODEL_CUTOFF.isoformat(),
                    )
                )
            session.flush()
            options = dict(
                model_files=[artifact],
                as_of=MODEL_CUTOFF,
                generated=MODEL_CUTOFF,
                params_file=params,
                persist=False,
            )
            before = predict_batch(session, match_ids=[jc.match_id], **options)
            unscoped = predict_batch(session, match_ids=list(extra), **options)
            assert all(row["status"] == "excluded" for row in unscoped["predictions"])
            after = predict_batch(
                session, match_ids=[jc.match_id, *extra], sfc_only_ids=extra, **options
            )
            assert _json_bytes(after["predictions"][0]) == _json_bytes(before["predictions"][0])
            assert after["models"] == before["models"]
            apif, own, unknown, out_of_scope = after["predictions"][1:]
            for row, source in ((apif, "apif"), (own, "c500"), (unknown, "apif")):
                assert (row["status"], row["source"], row["sfc_only"]) == (
                    "predicted",
                    source,
                    True,
                )
                assert row["model_definition"]["league"] == POOL
                assert row["market"] is None and row["market_reference"] is None
            assert apif["model_probabilities"] == own["model_probabilities"]
            assert uses_unknown_team_prior(unknown)
            assert unknown["team_strengths"]["home_default_strength"] is True
            assert unknown["team_strengths"]["away_default_strength"] is False
            # A scoped placeholder is treated as unseen even if its opaque label has a fit.
            expected, _ = model.predict_with_metadata({**unknown, "home": "从未见过的球队"})
            assert unknown["score_matrix"] == expected.tolist()
            assert out_of_scope["status"] == "excluded"
            frozen = write_report(after, tmp_path / "forecast")
            assert load_batch(frozen) == after

            wrong = deepcopy(after)
            wrong["predictions"][1].pop("sfc_only")
            with pytest.raises(ValueError, match="P2门槛"):
                write_report(wrong, tmp_path / "bad")

            # Reuse source-qualified rows as 14 slots for the real SFC report builder.
            rows = [*after["predictions"][:4], *before["predictions"] * 10]
            for index, row in enumerate(rows[4:], 1):
                rows[3 + index] = dict(row, match_id=f"jc:t93-{index}")
                session.add(
                    Match(
                        match_id=rows[3 + index]["match_id"],
                        source="jc",
                        league=jc.league,
                        home_team_id=jc.home_team_id,
                        away_team_id=jc.away_team_id,
                        kickoff_utc=jc.kickoff_utc,
                        status="scheduled",
                    )
                )
            session.flush()
            sfc_batch = dict(after, predictions=rows)
            issue = SfcIssue(
                issue_no="synthetic-t93",
                observed_utc=MODEL_CUTOFF.isoformat(),
                sale_end_utc="2024-02-18T12:00:00+00:00",
                snapshot=[
                    dict(
                        seq=index,
                        match_id=row["match_id"],
                        kickoff_utc=row["kickoff_utc"],
                        matched_identity={
                            key: row[key]
                            for key in ("home", "away", "source", "league", "kickoff_utc")
                        },
                    )
                    for index, row in enumerate(rows, start=1)
                ],
            )
            report = build_sfc_report(
                session, issue, sfc_batch, generated=MODEL_CUTOFF, forecast_sha256="a" * 64
            )
            assert all(row["probabilities"] is not None for row in report["rows"])
            assert uses_unknown_team_prior(disclosed_rows(report)[3])
            assert 4 not in report["confidence_order"]
    finally:
        engine.dispose()


def test_pooled_model_without_jc_sources_is_not_attributed_to_ordinary_rows(
    database, tmp_path, contract, history
):
    history["source"] = "apif"
    history.attrs["sources"] = ["apif"]
    model = DixonColes(pooled=True).fit(history, as_of=MODEL_CUTOFF)
    artifact = tmp_path / "apif-only.pkl"
    model.save(artifact)
    params = _config(tmp_path / "params.json", contract["parameters"])
    fixture = contract["upcoming"][0]
    engine = get_engine(database)
    try:
        with Session(engine) as session:
            jc = session.get(Match, fixture["match_id"])
            apif = Match(
                match_id="apif:scope-only",
                source="apif",
                league=jc.league,
                home_team_id=jc.home_team_id,
                away_team_id=jc.away_team_id,
                kickoff_utc=jc.kickoff_utc,
                status="scheduled",
            )
            session.add(apif)
            session.flush()
            options = dict(
                model_files=[artifact],
                as_of=MODEL_CUTOFF,
                generated=MODEL_CUTOFF,
                params_file=params,
                persist=False,
                allow_empty=True,
            )
            before = predict_batch(session, match_ids=[jc.match_id], **options)
            after = predict_batch(
                session,
                match_ids=[jc.match_id, apif.match_id],
                sfc_only_ids={apif.match_id},
                **options,
            )
            assert before["predictions"] == after["predictions"][:1]
            assert before["models"] == []
            assert after["predictions"][0]["status"] == "excluded"
            assert after["predictions"][1]["status"] == "predicted"
            assert load_batch(write_report(after, tmp_path / "mixed")) == after
    finally:
        engine.dispose()


@pytest.mark.parametrize("selection", [{"match_ids": ["jc:1"]}, {"day": MODEL_CUTOFF.date()}])
def test_sfc_scope_requires_requested_explicit_match_ids(selection):
    with pytest.raises(ValueError, match="14 场.*比赛ID"):
        predict_batch(
            None,
            model_files=[],
            as_of=MODEL_CUTOFF,
            generated=MODEL_CUTOFF,
            sfc_only_ids={"apif:1"},
            **selection,
        )
