"""New source candidates cannot enter legacy paper or SFC paths."""

import copy
import json

import pytest

from test_parlay_paper import BEFORE
from test_parlay_paper import document as document
from test_parlay_single_double import FIX
from test_scoreboard_paper import DECISION, paper_samples
from test_sfc_report import build as build_sfc
from test_sfc_report import complete_issue as complete_issue
from zucai.db import session_scope
from zucai.models import Match
from zucai.parlay.paper import advance_ledger as advance_f
from zucai.parlay.single_double import build
from zucai.predict.model_definitions import (
    EXPERIMENTAL_LEAGUES,
    LEGACY_LEAGUES,
    T63_LEAGUES,
    entry_definition_metadata,
    legacy_definition_metadata,
)
from zucai.scoreboard.paper import advance_ledger, paper_version_allowed

FIELDS = ("model_definition_id", "model_definition", "source_variant", "paper_eligible")


def version(row, variant="legacy", eligible=True):
    return dict(
        model_definition_id="a" * 64,
        model_definition={"league": row["league"], "model_family": "synthetic-dc"},
        source_variant=variant,
        paper_eligible=eligible,
    )


def version_batch(batch, variant="legacy", eligible=True):
    batch["model_definition_schema"] = 1
    for row in batch["predictions"]:
        row.update(version(row, variant, eligible))
    models = {row["model_version"]: row for row in batch["predictions"]}
    batch["models"] = [
        {"model_version": key, **{field: row[field] for field in FIELDS}}
        for key, row in models.items()
    ]


def _source_version_issue(complete_issue, league, sources, *, ready=False):
    engine, issue, batch = complete_issue
    definition = (
        legacy_definition_metadata(league)
        if league in LEGACY_LEAGUES and set(sources) == {"fd", "jc"}
        else entry_definition_metadata(league, {"sources": sources})
    )
    qualification = dict(
        definition,
        paper_eligible=ready,
        p2_ready=ready,
        p2_label="可用（旧规，考核中）"
        if ready
        else "实验"
        if league in T63_LEAGUES
        else "P2 实验",
    )
    model = copy.deepcopy(batch["models"][0])
    model.pop("source", None)
    model.update(qualification, league=league, sources=sorted(sources))
    with session_scope(engine) as session:
        for row in issue.snapshot:
            row["league"] = league
            row["matched_identity"].update(source="jc", league=league)
            match = session.get(Match, row["match_id"])
            row["match_id"] = "jc:" + row["match_id"].partition(":")[2]
            session.add(
                Match(
                    match_id=row["match_id"],
                    source="jc",
                    league=league,
                    home_team_id=match.home_team_id,
                    away_team_id=match.away_team_id,
                    kickoff_utc=match.kickoff_utc,
                    status=match.status,
                )
            )
        for row in batch["predictions"]:
            row.update(
                copy.deepcopy(qualification),
                match_id="jc:" + row["match_id"].partition(":")[2],
                source="jc",
                league=league,
                model_version=model["model_version"],
            )
    batch.update(
        schema_version=2,
        model_definition_schema=1,
        validation_status="league_gated",
        models=[model],
    )
    return engine, issue, batch


@pytest.mark.parametrize(
    "fields",
    [
        {"source_variant": "expanded", "paper_eligible": True},
        {"source_variant": "legacy", "paper_eligible": False},
        {"model_definition_id": "a" * 64},
        {"model_definition": {"model_family": "synthetic-dc"}},
        {"model_definition_schema": 1},
    ],
)
def test_a_to_e_never_admit_expanded_or_partial_version_rows(fields):
    rows, _ = paper_samples()
    for row in rows:
        row.update(fields, p2_ready=True)
    ledger = advance_ledger(rows, {}, [], DECISION)
    assert not [row for row in ledger if row["record_type"] == "ticket"]
    assert any(row.get("reason") == "model_version_not_paper_eligible" for row in ledger)


def test_old_seven_new_tickets_preserve_definition_and_existing_tickets_stay_immutable():
    rows, _ = paper_samples()
    for row in rows:
        row.update(version(row), p2_ready=True)
    ledger = advance_ledger(rows, {}, [], DECISION)
    tickets = [row for row in ledger if row["record_type"] == "ticket"]
    assert {row["account"] for row in tickets} == set("ABCDE")
    assert all(leg["model_definition_id"] == "a" * 64 for row in tickets for leg in row["legs"])
    frozen = copy.deepcopy(ledger)
    for row in rows:
        row.update(source_variant="expanded", paper_eligible=False)
    assert advance_ledger(rows, {}, ledger, DECISION) == frozen


@pytest.mark.parametrize(
    "damage", ["expanded", "ineligible", "partial", "model", "batch", "stripped"]
)
def test_f_builder_rejects_candidates_even_with_a_forged_ready_flag(damage):
    sample = json.loads((FIX / "input.json").read_bytes())
    version_batch(sample["batch"])
    for row in sample["batch"]["predictions"]:
        row["p2_ready"] = True
        if damage == "expanded":
            row["source_variant"] = "expanded"
        elif damage == "ineligible":
            row["paper_eligible"] = False
        elif damage == "partial":
            del row["paper_eligible"]
    if damage == "model":
        for model in sample["batch"]["models"]:
            model["source_variant"] = "expanded"
    if damage == "batch":
        sample["batch"]["source_variant"] = "expanded"
    if damage == "stripped":
        sample["batch"]["model_definition_schema"] = 1
        for row in [*sample["batch"]["predictions"], *sample["batch"]["models"]]:
            for field in FIELDS:
                row.pop(field, None)
    report = build(**sample)
    assert report["groups"] == []
    assert any(
        "model_version_not_paper_eligible" in row["reasons"] for row in report["diagnostics"]
    )


def test_f_legacy_version_keeps_original_groups_and_definition_receipts():
    sample = json.loads((FIX / "input.json").read_bytes())
    original = build(**sample)
    version_batch(sample["batch"])
    report = build(**sample)
    assert [row["group_id"] for row in report["groups"]] == [
        row["group_id"] for row in original["groups"]
    ]
    assert all(
        row[side]["model_definition_id"] == "a" * 64
        for row in report["groups"]
        for side in ("single", "double")
    )


def _qualified_legacy_primary(batch):
    version_batch(batch)
    for item in [*batch["predictions"], *batch["models"]]:
        item.update(
            legacy_definition_metadata("英超"),
            paper_eligible=True,
            p2_ready=True,
            p2_label="可用（旧规，考核中）",
        )


@pytest.mark.parametrize("name", ["dc-sot-v1", "dc-xg-v1", "dc-sot-injuries-v2"])
def test_f_independent_models_never_inherit_the_primary_definition(name):
    sample = json.loads((FIX / "input.json").read_bytes())
    extra = copy.deepcopy(sample["batch"]["predictions"][0])
    extra.update(
        model_name=name,
        model_version=name + "-fixture",
        model_probabilities=dict(h=0.1, d=0.2, a=0.7),
    )
    independent = dict(
        batch_id=sample["batch"]["batch_id"],
        generated_utc=sample["frozen_utc"],
        as_of_utc=sample["batch"]["as_of_utc"],
        predictions=[extra],
    )
    original = build(**sample, experimental=independent)
    _qualified_legacy_primary(sample["batch"])
    report = build(**sample, experimental=independent)
    derived = report["experimental_boards"][0]["matches"][0]
    assert derived["model_name"] == name
    assert derived["model_version"] == extra["model_version"]
    assert not any(field in derived for field in FIELDS)
    assert report["experimental_boards"] == original["experimental_boards"]
    primary = next(
        row
        for board in report["boards"]
        for row in board["matches"]
        if row["match_id"] == extra["match_id"]
    )
    assert primary["independent_models_available"] == 1
    assert "independent_direction_disagrees" in primary["single"]["reasons"]
    assert primary["model_definition"] == legacy_definition_metadata("英超")["model_definition"]


def test_f_market_reference_never_inherits_the_primary_definition():
    sample = json.loads((FIX / "input.json").read_bytes())
    original = build(**sample)
    _qualified_legacy_primary(sample["batch"])
    report = build(**sample)
    for row in report["market_reference"]["matches"]:
        assert row["model_name"] == "market_inverse_sp"
        assert row["model_version"] == "inverse_sp_v1"
        assert not any(field in row for field in FIELDS)
    assert report["market_reference"] == original["market_reference"]


@pytest.mark.parametrize("parent", [{"source_variant": "expanded"}, {"model_definition_schema": 1}])
def test_f_cannot_use_candidate_parent_as_an_independent_vote(parent):
    sample = json.loads((FIX / "input.json").read_bytes())
    original = build(**sample)
    row = copy.deepcopy(sample["batch"]["predictions"][0])
    row.update(model_name="candidate-source-version", model_probabilities=dict(h=0.1, d=0.2, a=0.7))
    candidate = dict(
        batch_id=sample["batch"]["batch_id"],
        generated_utc=sample["frozen_utc"],
        as_of_utc=sample["batch"]["as_of_utc"],
        predictions=[row],
        **parent,
    )
    report = build(**sample, experimental=candidate)
    assert report["groups"] == original["groups"]
    assert report["experimental_boards"] == []


def test_f_ledger_admits_qualified_legacy_parent_marker_and_keeps_receipts(document):
    document["model_definition_schema"] = 1
    for group in document["groups"]:
        for side in ("single", "double"):
            group[side].update(version(group[side]))
    ledger = advance_f([document], {}, [], BEFORE)
    tickets = [row for row in ledger if row["record_type"] == "ticket"]
    assert len(tickets) == 2
    assert all(leg["model_definition_id"] == "a" * 64 for row in tickets for leg in row["legs"])


@pytest.mark.parametrize("where", ["document", "group", "single", "double"])
def test_f_ledger_rejects_forged_candidate_documents_at_every_boundary(document, where):
    target = document if where == "document" else document["groups"][0]
    if where in {"single", "double"}:
        target = target[where]
    target.update(source_variant="expanded", paper_eligible=True)
    ledger = advance_f([document], {}, [], BEFORE)
    assert not [row for row in ledger if row["record_type"] in {"ticket", "group"}]


@pytest.mark.parametrize("missing", ["partial", "whole"])
def test_f_ledger_cannot_treat_missing_new_qualification_as_legacy(document, missing):
    if missing == "partial":
        document["groups"][0]["double"]["model_definition_id"] = "a" * 64
    else:
        document["model_definition_schema"] = 1
    ledger = advance_f([document], {}, [], BEFORE)
    assert not [row for row in ledger if row["record_type"] in {"ticket", "group"}]


@pytest.mark.parametrize("where", ["row", "model", "batch", "stripped"])
def test_sfc_old_seven_reject_invalid_definition_context_independently(
    complete_issue, monkeypatch, where
):
    from zucai.report import sfc

    # Exercise the SFC boundary independently of the shared report validator.
    monkeypatch.setattr(sfc, "_validate_batch", lambda batch: None)
    engine, issue, batch = _source_version_issue(complete_issue, "英超", ["fd", "jc"], ready=True)
    first = batch["predictions"][0]
    if where == "row":
        first["source_variant"] = "expanded"
    elif where == "model":
        for model in batch["models"]:
            if model["model_version"] == first["model_version"]:
                model["source_variant"] = "expanded"
    elif where == "batch":
        batch["source_variant"] = "expanded"
    else:
        batch["model_definition_schema"] = 1
        for row in [*batch["predictions"], *batch["models"]]:
            for field in FIELDS:
                row.pop(field, None)
    report = build_sfc(engine, issue, batch)
    row = next(row for row in report["rows"] if row["match_id"] == first["match_id"])
    assert row["probabilities"] is None
    assert "版本" in row["reason"]
    assert report["combo"] is None


@pytest.mark.parametrize("league", sorted(LEGACY_LEAGUES))
def test_sfc_legacy_probabilities_remain_equal_and_carry_definition(complete_issue, league):
    from zucai.report import sfc

    engine, issue, batch = complete_issue
    original = build_sfc(engine, issue, batch)
    engine, issue, batch = _source_version_issue(complete_issue, league, ["fd", "jc"], ready=True)
    report = build_sfc(engine, issue, batch)
    assert report["combo"] == original["combo"]
    assert [row["probabilities"] for row in report["rows"]] == [
        row["probabilities"] for row in original["rows"]
    ]
    assert all(
        row["model_definition_id"] == legacy_definition_metadata(league)["model_definition_id"]
        for row in report["rows"]
    )
    assert all(row["p2_label"] == "可用（旧规，考核中）" for row in report["rows"])
    rendered = sfc._markdown(report).decode("utf-8")
    assert "可用（旧规，考核中）" in rendered
    assert "P2 实验" not in rendered
    assert "P2 未验证模型" not in rendered


@pytest.mark.parametrize(
    "change", ["manual_same", "manual_changed", "prior_changed", "unqualified"]
)
def test_sfc_fixed_legacy_rejects_changed_or_unqualified_methods(complete_issue, change):
    engine, issue, batch = _source_version_issue(complete_issue, "英超", ["fd", "jc"], ready=True)
    original = batch["models"][0]["model_definition"]
    policy = dict(
        sources=original["training_sources"],
        model_family=original["model_family"],
        fitting_method=original["fitting_method"],
        prior_method=original["prior_method"],
        calibration_method=original["calibration_method"],
        **original["selection_protocol"],
    )
    if change.startswith("manual_"):
        policy.update(
            selection_basis="explicit_manual_half_life", selection_method="manual_override"
        )
        batch["models"][0]["parameter_provenance"] = dict(
            source="cli", half_life_days=365 if change == "manual_same" else 90
        )
    elif change == "prior_changed":
        policy["prior_method"] = "league_average"
    definition = entry_definition_metadata("英超", policy)
    assert (
        definition["model_definition_id"]
        == legacy_definition_metadata("英超")["model_definition_id"]
    ) is (change == "unqualified")
    for value in [*batch["models"], *batch["predictions"]]:
        value.update(
            copy.deepcopy(definition), p2_ready=False, p2_label="实验", paper_eligible=False
        )
    report = build_sfc(engine, issue, batch)
    assert all(row["probabilities"] is None for row in report["rows"])
    assert report["combo"] is None


@pytest.mark.parametrize("league", sorted(EXPERIMENTAL_LEAGUES))
def test_sfc_eight_primary_experiments_keep_diagnostic_probabilities(complete_issue, league):
    from zucai.report import sfc

    original = build_sfc(*complete_issue)
    engine, issue, batch = _source_version_issue(complete_issue, league, ["apif", "fd", "jc"])
    report = build_sfc(engine, issue, batch)
    assert report["combo"] == original["combo"]
    assert [row["probabilities"] for row in report["rows"]] == [
        row["probabilities"] for row in original["rows"]
    ]
    assert all(row["p2_label"] == "实验" for row in report["rows"])
    assert all(row["paper_eligible"] is False for row in report["rows"])
    assert all(not paper_version_allowed(row) for row in batch["predictions"])
    assert "实验" in sfc._markdown(report).decode("utf-8")


def test_sfc_nations_experiment_keeps_original_diagnostic_probabilities(complete_issue):
    original = build_sfc(*complete_issue)
    engine, issue, batch = _source_version_issue(complete_issue, "欧国联", ["fdorg", "jc"])
    report = build_sfc(engine, issue, batch)
    assert report["combo"] == original["combo"]
    assert all(row["probabilities"] is not None for row in report["rows"])
    assert all(row["p2_label"] == "P2 实验" for row in report["rows"])


@pytest.mark.parametrize("league", sorted(LEGACY_LEAGUES))
@pytest.mark.parametrize("stripped_parent", [False, True])
def test_sfc_seven_expanded_candidates_cannot_replace_legacy_even_without_parent(
    complete_issue, league, stripped_parent
):
    engine, issue, batch = _source_version_issue(complete_issue, league, ["apif", "fd", "jc"])
    if stripped_parent:
        batch.pop("model_definition_schema")
    else:
        batch.update(source_variant="expanded", paper_eligible=False)
    report = build_sfc(engine, issue, batch)
    assert all(row["probabilities"] is None for row in report["rows"])
    assert report["combo"] is None


def test_sfc_explicit_candidate_document_cannot_be_used_as_eight_primary(complete_issue):
    engine, issue, batch = _source_version_issue(complete_issue, "日职", ["apif", "fd", "jc"])
    batch.update(source_variant="expanded", paper_eligible=False)
    report = build_sfc(engine, issue, batch)
    assert all(row["probabilities"] is None for row in report["rows"])
    assert report["combo"] is None


@pytest.mark.parametrize(
    "damage",
    [
        "wrong_variant",
        "wrong_label",
        "missing_row",
        "missing_model",
        "stripped_row",
        "stripped_model",
        "model_mismatch",
        "parent_mismatch",
        "bad_marker",
    ],
)
def test_sfc_primary_diagnostics_still_reject_invalid_definition_metadata(complete_issue, damage):
    engine, issue, batch = _source_version_issue(complete_issue, "日职", ["apif", "fd", "jc"])
    row, model = batch["predictions"][0], batch["models"][0]
    if damage == "wrong_variant":
        row["source_variant"] = "legacy"
    elif damage == "wrong_label":
        row["p2_label"] = "可用（旧规，考核中）"
    elif damage == "missing_row":
        row.pop("paper_eligible")
    elif damage == "missing_model":
        model.pop("model_definition")
    elif damage in {"stripped_row", "stripped_model"}:
        for field in FIELDS:
            (row if damage == "stripped_row" else model).pop(field)
    elif damage == "model_mismatch":
        model["model_definition_id"] = "0" * 64
    elif damage == "parent_mismatch":
        batch["source_variant"] = "legacy"
    else:
        batch["model_definition_schema"] = True
    with pytest.raises(ValueError):
        build_sfc(engine, issue, batch)


def test_forecast_loader_keeps_parent_definition_marker_for_paper_guard(tmp_path, monkeypatch):
    from zucai.scoreboard import score

    rows, _ = paper_samples()
    batch = {key: rows[0][key] for key in ("batch_id", "generated_utc", "as_of_utc", "mode")}
    batch.update(model_definition_schema=1, predictions=[rows[0]])
    relative = "forecasts/2026-11-01/late/forecast.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(batch), encoding="utf-8")
    monkeypatch.setattr(score, "verify_forecasts", lambda archive: {"files": {relative: "test"}})
    monkeypatch.setattr(score, "load_batch", lambda path: batch)
    loaded = score.load_forecasts(tmp_path, DECISION, publications={})
    assert loaded[0]["model_definition_schema"] == 1
