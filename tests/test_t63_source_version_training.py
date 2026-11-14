"""Synthetic orchestration boundaries; no claims about historical accuracy."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from zucai import source_version_training as parallel
from zucai.predict.model_definitions import entry_definition_metadata

ROOT = Path(__file__).parents[1]
CUTOFF = datetime(2026, 11, 2, 18, tzinfo=UTC)


@pytest.fixture
def configuration():
    document = json.loads((ROOT / "data/model/params.json").read_bytes())
    document["leagues"] = {"英超": document["leagues"]["英超"]}
    return document


def test_candidate_params_leave_original_methods_and_numbers_unchanged(configuration):
    original = copy.deepcopy(configuration)
    candidate = parallel.candidate_parameters(configuration)
    assert configuration == original
    old = original["leagues"]["英超"]
    new = candidate["leagues"]["英超"]
    assert new["sources"] == ["apif", "fd", "fdorg", "jc"]
    assert new["p2_ready"] is False
    for key in set(old) - {"sources", "p2_ready", "p2_evidence", "database_sha256"}:
        assert old[key] == new[key]


def test_failure_after_fit_cannot_claim_or_reuse_candidate_artifact(
    configuration, tmp_path, monkeypatch
):
    database = tmp_path / "db"
    database.write_bytes(b"synthetic read-only source")
    metadata = entry_definition_metadata(
        "英超", parallel.candidate_parameters(configuration)["leagues"]["英超"]
    )

    def trainer(**kwargs):
        assert kwargs["as_of"] == CUTOFF
        kwargs["output"].write_bytes(b"synthetic model")
        return {"training_rows": 42}

    monkeypatch.setattr(
        parallel.DixonColes,
        "load",
        lambda path: SimpleNamespace(model_definition_id=metadata["model_definition_id"]),
    )
    write = parallel._write
    failed = False

    def fail_training_report(path, value):
        nonlocal failed
        if path.name.endswith(".train.json") and not failed:
            failed = True
            raise OSError("synthetic disk-write failure")
        write(path, value)

    monkeypatch.setattr(parallel, "_write", fail_training_report)
    outcomes, fitted, _ = parallel.train_candidates(
        database, tmp_path, "run", CUTOFF, configuration, trainer=trainer
    )
    outcome = outcomes["英超"]
    assert outcome["status"] == "failed"
    assert not fitted
    assert "model_path" not in outcome and "sha256" not in outcome
    assert not (tmp_path / "models/run/英超.source-expanded.pkl").exists()
    assert json.loads((tmp_path / outcome["training_json_path"]).read_bytes()) == outcome


@pytest.mark.parametrize("late", [False, True])
def test_candidate_keeps_main_sfc_scope_and_cutoff_without_late_probabilities(
    configuration, tmp_path, monkeypatch, late
):
    params = tmp_path / "run/source-expanded.params.json"
    params.parent.mkdir()
    params.write_text(json.dumps(parallel.candidate_parameters(configuration)))
    metadata = entry_definition_metadata(
        "英超", parallel.candidate_parameters(configuration)["leagues"]["英超"]
    )
    kickoff = CUTOFF + timedelta(hours=2)
    target = {
        "match_id": "jc:synthetic-sfc-only",
        "league": "英超",
        "kickoff_utc": kickoff.isoformat(),
        "sfc_only": True,
    }
    primary = {
        "batch_id": "same-batch",
        "as_of_utc": CUTOFF.isoformat(),
        "input": {"kickoff_date_beijing": "2026-11-03", "scope": "sfc:synthetic"},
        "predictions": [target],
    }

    def predict(session, **kwargs):
        assert kwargs["as_of"] == CUTOFF
        assert kwargs["match_ids"] == [target["match_id"]]
        return {
            "as_of_utc": CUTOFF.isoformat(),
            "input": {},
            "predictions": [
                {
                    **{k: v for k, v in target.items() if k != "sfc_only"},
                    "status": "predicted",
                    "model_probabilities": {"h": 0.5, "d": 0.3, "a": 0.2},
                }
            ],
        }

    monkeypatch.setattr(parallel, "predict_batch", predict)
    outcomes = {"英超": {**metadata, "status": "trained", "sha256": "a" * 64, "predicted": 0}}
    generated = kickoff if late else CUTOFF + timedelta(minutes=1)
    result = parallel.predict_candidates(
        None,
        primary=primary,
        outcomes=outcomes,
        fitted={},
        params_path=params,
        clock=lambda: generated,
    )
    row = result["predictions"][0]
    assert result["input"]["scope"] == primary["input"]["scope"]
    assert row["sfc_only"] is True
    assert result["batch_id"] == primary["batch_id"]
    assert result["as_of_utc"] == primary["as_of_utc"]
    assert result["generated_utc"] == generated.isoformat()
    assert row["paper_eligible"] is False and row["source_variant"] == "expanded"
    assert row["status"] == ("excluded" if late else "predicted")
    assert ("model_probabilities" in row) is not late
    assert outcomes["英超"]["predicted"] == (0 if late else 1)
