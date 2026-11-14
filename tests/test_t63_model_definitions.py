"""Stable definition identity is distinct from daily fitted-artifact identity."""

import copy
import csv
import importlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.parameters import TRAINING_POLICY, model_readiness
from zucai.report.batch import predict_batch
from zucai.report.render import _csv_bytes, _validate_batch

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t16/train_history.csv"
CUTOFF = datetime(2026, 11, 2, tzinfo=UTC)
DEFINITION_FIELDS = ("model_definition_id", "model_definition", "source_variant", "paper_eligible")
OLD = ("英超", "西甲", "意甲", "德甲", "法甲", "葡超", "巴甲")
NEW = ("英冠", "法乙", "荷甲", "日职", "瑞超", "挪超", "芬超", "美职")


def definitions():
    return importlib.import_module("zucai.predict.model_definitions")


def entry(league="英超", **changes):
    value = dict(
        half_life_days=365,
        selection_as_of_utc="2026-07-01T00:00:00Z",
        generated_utc="2026-07-01T00:00:00Z",
        database_sha256="a" * 64,
        sources=["fd", "jc"],
        training_policy=copy.deepcopy(TRAINING_POLICY),
        unknown_team_prior="weakest_quartile_half_shrinkage",
        draw_calibration=False,
        selection_basis=(
            "minimum season-S walk-forward model log loss; complete equal samples; "
            "tie: shorter half-life"
        ),
        calibration_policy=(
            "enable in S+1 only if selected uncalibrated monthly S draw bias exceeds 3pp"
        ),
        p2_ready=True,
        p2_evidence={"as_of_utc": "2026-07-01T00:00:00Z", "seasons": ["2425", "2526"]},
    )
    if league in {"巴甲", "日职", "瑞超", "挪超", "芬超", "美职"}:
        value["p2_evidence"]["seasons"] = ["2024", "2025"]
    value.update(changes)
    return value


def model(league="英超", **changes):
    value = dict(
        league=league,
        model_family="dixon-coles",
        sources=["fd", "jc"],
        fitting_method="penaltyblog_slsqp",
        as_of="2026-11-01T00:00:00Z",
        half_life_days=365,
        training_policy=copy.deepcopy(TRAINING_POLICY),
        availability_basis="mixed",
        replayed_training_share=0,
        unknown_team_prior=True,
        prior_parameters={"method": "weakest_quartile_half_shrinkage"},
        draw_calibration=False,
    )
    value.update(changes)
    return SimpleNamespace(**value)


def config(tmp_path, league, values):
    path = tmp_path / "params.json"
    path.write_text(
        json.dumps({"schema_version": 1, "leagues": {league: values}}), encoding="utf-8"
    )
    return path


def test_daily_artifact_and_selected_annual_parameters_do_not_change_definition():
    first = definitions().definition_metadata(model(), entry())
    second = definitions().definition_metadata(
        model(
            as_of="2027-11-01T00:00:00Z",
            half_life_days=90,
            draw_calibration=True,
            artifact_sha256="f" * 64,
            training_rows=999,
            database_sha256="b" * 64,
            sources=["jc", "fd"],
        ),
        entry(
            half_life_days=90,
            draw_calibration=True,
            selection_season="2627",
            selection_as_of_utc="2027-07-01T00:00:00Z",
            database_sha256="c" * 64,
        ),
    )
    assert first == second
    assert len(first["model_definition_id"]) == 64
    assert set(first["model_definition"]) == {
        "league",
        "model_family",
        "training_sources",
        "fitting_method",
        "prior_method",
        "calibration_method",
        "selection_protocol",
    }
    assert first["model_definition"]["training_sources"] == ["fd", "jc"]
    assert first["source_variant"] == "legacy"


@pytest.mark.parametrize(
    "change", ["league", "family", "sources", "fit", "prior", "calibration", "selection"]
)
def test_changed_method_or_source_gets_a_distinct_definition(change):
    candidate, policy = model(), entry()
    if change == "league":
        candidate.league = "西甲"
    elif change == "family":
        candidate.model_family = "another-goal-model"
    elif change == "sources":
        candidate.sources = ["apif", "fd", "jc"]
    elif change == "fit":
        candidate.fitting_method = "another-optimizer"
    elif change == "prior":
        candidate.prior_parameters = {"method": "another-prior"}
    elif change == "calibration":
        candidate.calibration_method = "another-calibration"
    else:
        policy["selection_basis"] = "another-selection-method"
    assert (
        definitions().definition_metadata(candidate, policy)["model_definition_id"]
        != (definitions().definition_metadata(model(), entry())["model_definition_id"])
    )


def test_entry_definition_matches_artifact_and_missing_protocol_is_explicit():
    assert definitions().entry_definition_metadata("英超", entry()) == (
        definitions().definition_metadata(model(), entry())
    )
    missing = entry()
    missing.pop("selection_basis")
    missing.pop("calibration_policy")
    metadata = definitions().definition_metadata(model(), missing)
    protocol = metadata["model_definition"]["selection_protocol"]
    assert protocol["selection_basis"] == protocol["calibration_policy"] == "unknown"


@pytest.mark.parametrize("league", OLD)
def test_old_seven_retains_policy_checks_and_cautious_label(tmp_path, league):
    path = config(tmp_path, league, entry(league))
    ready = model_readiness(model(league), as_of=CUTOFF, params_file=path)
    assert ready["p2_ready"] is True
    assert ready["p2_label"] == "可用（旧规，考核中）"
    assert ready["paper_eligible"] is True
    assert ready["source_variant"] == "legacy"
    assert (
        model_readiness(model(league, half_life_days=90), as_of=CUTOFF, params_file=path)[
            "p2_ready"
        ]
        is False
    )


@pytest.mark.parametrize("league", OLD + NEW)
def test_expanded_is_experimental_even_when_old_p2_evidence_claims_ready(tmp_path, league):
    sources = ["apif", "fd", "jc"]
    path = config(tmp_path, league, entry(league, sources=sources))
    ready = model_readiness(model(league, sources=sources), as_of=CUTOFF, params_file=path)
    assert ready["p2_ready"] is False
    assert ready["p2_label"] == "实验"
    assert ready["paper_eligible"] is False
    assert ready["source_variant"] == "expanded"


def test_non_t63_current_keeps_existing_paper_eligibility(tmp_path):
    path = config(tmp_path, "测试联赛", entry(p2_ready=False))
    ready = model_readiness(model("测试联赛"), as_of=CUTOFF, params_file=path)
    assert ready["source_variant"] == "current"
    assert ready["p2_label"] == "P2 实验"
    assert ready["paper_eligible"] is True


@pytest.mark.parametrize("changed", ["family", "fit", "prior", "calibration", "selection"])
def test_unchanged_sources_cannot_inherit_legacy_qualification_after_method_change(
    tmp_path, changed
):
    candidate, policy = model(), entry()
    if changed == "family":
        candidate.model_family = policy["model_family"] = "another-family"
    elif changed == "fit":
        candidate.fitting_method = policy["fitting_method"] = "another-fit"
    elif changed == "prior":
        candidate.prior_parameters = {"method": "another-prior"}
        policy["unknown_team_prior"] = "another-prior"
    elif changed == "calibration":
        candidate.calibration_method = policy["calibration_method"] = "another-calibration"
    else:
        policy["selection_basis"] = "another-selection-protocol"
    ready = model_readiness(candidate, as_of=CUTOFF, params_file=config(tmp_path, "英超", policy))
    assert ready["p2_ready"] is ready["paper_eligible"] is False
    assert ready["p2_label"] == "实验"


@pytest.mark.parametrize("old_gate", [True, False])
def test_legacy_definition_does_not_expire_or_recertify_from_old_gate(tmp_path, old_gate):
    policy = entry(p2_ready=old_gate)
    ready = model_readiness(
        model(), as_of="2028-11-02T00:00:00Z", params_file=config(tmp_path, "英超", policy)
    )
    assert ready["p2_ready"] is ready["paper_eligible"] is True
    assert ready["p2_label"] == "可用（旧规，考核中）"


@pytest.mark.parametrize("field", ["parameter_source", "parameter_provenance"])
def test_manual_cli_selection_is_not_the_annual_selection_definition(tmp_path, field):
    candidate = model()
    setattr(candidate, field, "cli" if field == "parameter_source" else {"source": "cli"})
    metadata = definitions().definition_metadata(candidate, entry())
    assert (
        metadata["model_definition"]["selection_protocol"]["selection_method"] == "manual_override"
    )
    assert (
        metadata["model_definition_id"]
        != definitions().entry_definition_metadata("英超", entry())["model_definition_id"]
    )
    ready = model_readiness(candidate, as_of=CUTOFF, params_file=config(tmp_path, "英超", entry()))
    assert ready["p2_ready"] is ready["paper_eligible"] is False


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_calibration_cli_override_never_inherits_annual_definition(tmp_path, selected, override):
    policy = entry(draw_calibration=selected)
    candidate = model(
        draw_calibration=override,
        parameter_source="params.json",
        parameter_provenance={"source": "params.json", "draw_calibration_source": "cli"},
    )
    metadata = definitions().definition_metadata(candidate, policy)
    protocol = metadata["model_definition"]["selection_protocol"]
    assert protocol["selection_method"] == "manual_override"
    assert protocol["selection_basis"] == "explicit_manual_draw_calibration"
    assert protocol["calibration_policy"] == "explicit_manual_draw_calibration"
    assert (
        metadata["model_definition_id"]
        != definitions().legacy_definition_metadata("英超")["model_definition_id"]
    )
    ready = model_readiness(candidate, as_of=CUTOFF, params_file=config(tmp_path, "英超", policy))
    assert ready["p2_ready"] is ready["paper_eligible"] is False
    assert ready["p2_label"] == "实验"


@pytest.mark.parametrize("selected", [False, True])
def test_annual_calibration_selection_keeps_the_same_definition(tmp_path, selected):
    policy = entry(draw_calibration=selected)
    candidate = model(
        draw_calibration=selected,
        parameter_source="params.json",
        parameter_provenance={"source": "params.json", "draw_calibration_source": "params.json"},
    )
    metadata = definitions().definition_metadata(candidate, policy)
    assert (
        metadata["model_definition_id"]
        == definitions().legacy_definition_metadata("英超")["model_definition_id"]
    )
    ready = model_readiness(candidate, as_of=CUTOFF, params_file=config(tmp_path, "英超", policy))
    assert ready["p2_ready"] is ready["paper_eligible"] is True


def test_combined_cli_overrides_name_both_selection_dimensions():
    candidate = model(
        parameter_source="cli",
        parameter_provenance={"source": "cli", "draw_calibration_source": "cli"},
    )
    metadata = definitions().definition_metadata(candidate, entry())
    protocol = metadata["model_definition"]["selection_protocol"]
    assert protocol["selection_basis"] == "explicit_manual_half_life_and_draw_calibration"
    for key, value in metadata.items():
        setattr(candidate, key, value)
    assert definitions().definition_metadata(candidate, entry()) == metadata


def test_saved_annual_definition_cannot_mask_calibration_cli_override():
    candidate = model()
    for key, value in definitions().definition_metadata(candidate, entry()).items():
        setattr(candidate, key, value)
    candidate.parameter_provenance = {"source": "params.json", "draw_calibration_source": "cli"}
    with pytest.raises(ValueError):
        definitions().definition_metadata(candidate, entry())


@pytest.fixture
def frozen(tmp_path):
    history = pd.read_csv(FIXTURE)
    for field in ("kickoff_utc", "available_utc"):
        history[field] = pd.to_datetime(history[field], utc=True)
    history.attrs["sources"] = ["fd", "jc"]
    fitted = DixonColes(365).fit(history, as_of=CUTOFF - timedelta(days=1))
    fitted.training_policy = copy.deepcopy(TRAINING_POLICY)
    artifact = tmp_path / "daily.pkl"
    fitted.save(artifact)
    params = config(tmp_path, "英超", entry())
    engine = get_engine(tmp_path / "matches.db")
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all([Team(id=1, name="A"), Team(id=2, name="B")])
        session.flush()
        for key, kickoff in [("jc:1", CUTOFF + timedelta(hours=2)), ("jc:2", CUTOFF)]:
            session.add(
                Match(
                    match_id=key,
                    source="jc",
                    league="英超",
                    home_team_id=1,
                    away_team_id=2,
                    kickoff_utc=kickoff,
                    status="scheduled",
                )
            )
    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[artifact],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:1", "jc:2", "jc:missing"],
            params_file=params,
            persist=False,
        )
    yield batch, fitted, artifact, params, engine
    engine.dispose()


def test_batch_propagates_definition_to_predictions_and_known_exclusions(frozen):
    batch, _, _, _, _ = frozen
    assert batch["model_definition_schema"] == 1
    provenance = batch["models"][0]
    for prediction in batch["predictions"][:2]:
        for key in DEFINITION_FIELDS:
            assert prediction[key] == provenance[key]
    assert not any(key in batch["predictions"][2] for key in DEFINITION_FIELDS)
    _validate_batch(batch)
    rows = list(csv.DictReader(io.StringIO(_csv_bytes(batch).decode("utf-8-sig"))))
    assert rows[0]["model_definition_id"] == provenance["model_definition_id"]
    assert rows[1]["model_definition_id"] == provenance["model_definition_id"]
    assert rows[2]["model_definition_id"] == ""


def test_changed_pickle_sha_retains_definition(frozen):
    batch, fitted, artifact, params, engine = frozen
    fitted.parameter_provenance = {"daily_fixture_note": "second artifact"}
    fitted.save(artifact)
    with session_scope(engine) as session:
        newer = predict_batch(
            session,
            model_files=[artifact],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:1"],
            params_file=params,
            persist=False,
        )
    assert newer["models"][0]["model_version"] != batch["models"][0]["model_version"]
    assert newer["models"][0]["model_definition_id"] == batch["models"][0]["model_definition_id"]


def test_new_empty_model_batch_still_exports_definition_columns(frozen):
    _, _, _, params, engine = frozen
    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:missing"],
            params_file=params,
            persist=False,
            allow_empty=True,
        )
    _validate_batch(batch)
    row = next(csv.DictReader(io.StringIO(_csv_bytes(batch).decode("utf-8-sig"))))
    assert row["model_definition_id"] == row["source_variant"] == ""


def test_render_rejects_coherently_rehashed_changed_method_claiming_legacy_ready(frozen):
    batch = copy.deepcopy(frozen[0])
    altered = definitions().definition_metadata(model(fitting_method="another-fit"), entry())
    for row in [batch["models"][0], *batch["predictions"][:2]]:
        row.update(altered)
    with pytest.raises(ValueError):
        _validate_batch(batch)


@pytest.mark.parametrize(
    "damage", ["id", "payload", "variant", "paper", "partial", "model_partial", "excluded"]
)
def test_render_rejects_mismatched_or_partial_definition_metadata(frozen, damage):
    batch = copy.deepcopy(frozen[0])
    prediction = batch["predictions"][0]
    if damage == "id":
        prediction["model_definition_id"] = "f" * 64
    elif damage == "payload":
        prediction["model_definition"]["fitting_method"] = "tampered"
    elif damage == "variant":
        prediction["source_variant"] = "expanded"
    elif damage == "paper":
        prediction["paper_eligible"] = False
    elif damage == "partial":
        del prediction["source_variant"]
    elif damage == "model_partial":
        del batch["models"][0]["paper_eligible"]
    else:
        del batch["predictions"][1]["paper_eligible"]
    with pytest.raises(ValueError):
        _validate_batch(batch)


def test_old_frozen_records_without_definition_fields_remain_compatible(frozen):
    batch = copy.deepcopy(frozen[0])
    batch.pop("model_definition_schema", None)
    for row in [*batch["models"], *batch["predictions"]]:
        for key in DEFINITION_FIELDS:
            row.pop(key, None)
        if row.get("p2_ready"):
            row["p2_label"] = "P2 可用"
    _validate_batch(batch)


def test_parent_marker_prevents_wholesale_downgrade_to_legacy(frozen):
    batch = copy.deepcopy(frozen[0])
    batch["model_definition_schema"] = 1
    for row in [*batch["models"], *batch["predictions"]]:
        for key in DEFINITION_FIELDS:
            row.pop(key, None)
        if row.get("p2_ready"):
            row["p2_label"] = "P2 可用"
    with pytest.raises(ValueError):
        _validate_batch(batch)


@pytest.mark.parametrize("context", [{"source_variant": "expanded"}, {"paper_eligible": False}])
def test_candidate_batch_context_cannot_disagree_with_models_and_rows(frozen, context):
    batch = copy.deepcopy(frozen[0])
    batch.update(context)
    with pytest.raises(ValueError):
        _validate_batch(batch)


def test_known_scope_without_artifact_keeps_explicit_expected_definition(frozen):
    _, _, _, params, engine = frozen
    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:1"],
            params_file=params,
            persist=False,
            allow_empty=True,
        )
    prediction = batch["predictions"][0]
    assert prediction["status"] == "excluded"
    assert prediction["expected_definition"] is True
    assert prediction["model_version"] is None
    assert prediction["paper_eligible"] is prediction["p2_ready"] is False
    assert (
        prediction["model_definition_id"]
        == definitions().entry_definition_metadata("英超", entry())["model_definition_id"]
    )
    assert batch["models"] == []
    _validate_batch(batch)
    for key in DEFINITION_FIELDS:
        prediction.pop(key)
    prediction.pop("expected_definition")
    with pytest.raises(ValueError):
        _validate_batch(batch)


def test_known_scope_without_entry_states_definition_unavailable(frozen, tmp_path):
    _, _, _, _, engine = frozen
    params = tmp_path / "empty-params.json"
    params.write_text(json.dumps({"schema_version": 1, "leagues": {}}), encoding="utf-8")
    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:1"],
            params_file=params,
            persist=False,
            allow_empty=True,
        )
    prediction = batch["predictions"][0]
    assert prediction["definition_unavailable_reason"]
    assert prediction["paper_eligible"] is False
    assert "model_definition_id" not in prediction
    _validate_batch(batch)


@pytest.mark.parametrize("escape", ["expected", "unavailable"])
def test_available_artifact_cannot_be_downgraded_to_unbound_exclusion(frozen, escape):
    batch = copy.deepcopy(frozen[0])
    prediction = batch["predictions"][0]
    prediction.update(
        status="excluded",
        reason="failure",
        model_version=None,
        model_probabilities=None,
        score_matrix=None,
        p2_ready=False,
        p2_label="实验",
        paper_eligible=False,
    )
    if escape == "expected":
        prediction["expected_definition"] = True
    else:
        for key in DEFINITION_FIELDS[:-1]:
            prediction.pop(key)
        prediction["definition_unavailable_reason"] = "unavailable"
    with pytest.raises(ValueError):
        _validate_batch(batch)
