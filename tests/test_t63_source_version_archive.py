"""Offline source candidates; target identities come from data/fixtures/t17/upcoming.json."""

import copy
import hashlib
import importlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sfc_publication_support import REF, commit_report, git, prepare_remote
from test_forecast_archive import staged_run
from zucai.forecast_archive import MANIFEST, attach_experiments, publish_forecasts, verify_forecasts
from zucai.predict.model_definitions import entry_definition_metadata
from zucai.report.render import write_report

TARGETS = json.loads((Path(__file__).parents[1] / "data/fixtures/t17/upcoming.json").read_bytes())
STAMP = "2026-11-02T22:00:00+00:00"
PUBLISHED = datetime.fromisoformat("2026-11-02T22:05:00+00:00")
FLAGS = {"source_variant": "expanded", "p2_ready": False, "paper_eligible": False}
DEFINITION_META = entry_definition_metadata("英超", {"sources": ["apif", "fd", "fdorg", "jc"]})
DEFINITION = DEFINITION_META["model_definition_id"]


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def _snapshot(archive):
    return {
        str(path.relative_to(archive)): path.read_bytes()
        for path in archive.rglob("*")
        if path.is_file()
    }


def prepared(tmp_path, *, failed=False, main_failed=False):
    stage, run = staged_run(tmp_path / "stage", "source-run", failed=main_failed)
    archive = tmp_path / "archive"
    candidate_model = "models/source-run/英超.source-expanded.pkl"
    candidate_train = "models/source-run/英超.source-expanded.train.json"
    params = "models/source-run/source-expanded.params.json"
    sources = ["apif", "fd", "fdorg", "jc"]
    outcome = {
        **FLAGS,
        **copy.deepcopy(DEFINITION_META),
        "training_json_path": candidate_train,
        "training_database_sha256": run["database_sha256"],
        "training_cutoff_utc": run["training_cutoff_utc"],
        "sources": sources,
        "p2_label": "实验",
        "status": "failed" if failed else "trained",
    }
    if failed:
        outcome["reason"] = "synthetic model unavailable"
    else:
        (stage / candidate_model).write_bytes(b"synthetic source expanded opaque model\x00")
        outcome.update(
            model_path=candidate_model,
            sha256=hashlib.sha256((stage / candidate_model).read_bytes()).hexdigest(),
        )
    _json(stage / candidate_train, outcome)
    _json(stage / params, {"leagues": {"英超": {"sources": sources, "half_life_days": 365.0}}})
    documents = {}
    for day, forecast in run["forecasts"].items():
        directory = stage / forecast["path"]
        main = json.loads((directory / "forecast.json").read_bytes())
        targets = [
            {
                key: row[key]
                for key in ("match_id", "source", "league", "home", "away", "kickoff_utc")
            }
            for row in TARGETS
            if row["source"] == "jc"
            and datetime.fromisoformat(row["kickoff_utc"])
            .astimezone(timezone(timedelta(hours=8)))
            .date()
            .isoformat()
            == day
        ]
        main["input"]["match_ids"] = [row["match_id"] for row in targets]
        main["predictions"] = [
            dict(row, status="excluded", reason="synthetic main fixture") for row in targets
        ]
        shutil.rmtree(directory)
        write_report(main, directory)
        doc = copy.deepcopy(main)
        doc.update(
            **FLAGS,
            model_definition_schema=1,
            validation_status="experimental_unadopted",
            generated_utc=STAMP,
        )
        doc["predictions"] = [row for row in doc["predictions"] if row["league"] == "英超"]
        doc["input"]["match_ids"] = [row["match_id"] for row in doc["predictions"]]
        if failed:
            doc["models"] = []
        else:
            doc["models"] = [
                {
                    **FLAGS,
                    **copy.deepcopy(DEFINITION_META),
                    "model_name": "dixon-coles",
                    "model_version": outcome["sha256"],
                    "sources": sources,
                    "league": "英超",
                    "model_as_of_utc": run["training_cutoff_utc"],
                    "last_available_utc": "2026-11-01T15:00:00+00:00",
                    "training_games": 84,
                    "availability_basis": "mixed",
                    "assumed_training_share": 0.25,
                    "p2_label": "实验",
                }
            ]
        for row in doc["predictions"]:
            row.update({**FLAGS, **copy.deepcopy(DEFINITION_META), "p2_label": "实验"})
            if failed:
                row["reason"] = outcome["reason"]
                row.update(expected_definition=True, model_version=None, model_name="dixon-coles")
            else:
                row.update(
                    status="predicted",
                    reason=None,
                    model_name="dixon-coles",
                    model_version=outcome["sha256"],
                    model_probabilities={"h": 0.4, "d": 0.4, "a": 0.2},
                    score_matrix=[[0.2, 0.2], [0.4, 0.2]],
                    top_scores=[],
                    market=None,
                    ev=None,
                    experimental_blend=None,
                )
        documents[day] = doc
    publish_forecasts(archive, stage, run)
    return archive, stage, run, {"英超": outcome}, documents


def attach(archive, stage, run, outcomes, documents):
    module = importlib.import_module("zucai.source_version_archive")
    return module.attach_source_versions(
        archive, stage, run["run_id"], outcomes, documents, clock=lambda: PUBLISHED
    )


def test_candidates_freeze_independent_models_params_and_complete_batches(tmp_path):
    archive, stage, run, outcomes, documents = prepared(tmp_path)
    experiments = {day: {"generated_utc": STAMP, "predictions": []} for day in run["forecasts"]}
    attach_experiments(archive, experiments, run["run_id"], clock=lambda: PUBLISHED)
    before = _snapshot(archive)
    assert not (archive / outcomes["英超"]["model_path"]).exists()
    attached = attach(archive, stage, run, outcomes, documents)
    manifest = verify_forecasts(archive)
    assert attached == manifest["runs"][run["run_id"]]
    assert attached["source_versions"]["leagues"] == outcomes
    params = attached["source_versions"]["params_path"]
    assert params == "models/source-run/source-expanded.params.json"
    assert attached["source_versions"]["params_sha256"] == manifest["files"][params]
    for day, document in documents.items():
        entry = attached["forecasts"][day]
        path = entry["source_versions_path"]
        assert path == f"forecasts/{day}/source-run/source_versions.json"
        assert json.loads((archive / path).read_bytes()) == document
        assert entry["source_versions_attached_utc"] == PUBLISHED.isoformat()
        assert "source_versions_published_utc" not in entry
        assert entry["experimental_path"] in manifest["files"]
    assert all(
        (archive / name).read_bytes() == raw for name, raw in before.items() if name != MANIFEST
    )
    latest = json.loads((archive / "models/latest.json").read_bytes())
    assert latest["models"]["英超"]["model_path"] == "models/source-run/英超.pkl"
    assert all(row["league"] == "英超" for doc in documents.values() for row in doc["predictions"])


@pytest.mark.parametrize("main_failed", [False, True])
def test_failed_candidate_has_outcome_and_exclusions_without_a_model(tmp_path, main_failed):
    archive, stage, run, outcomes, documents = prepared(
        tmp_path, failed=True, main_failed=main_failed
    )
    attached = attach(archive, stage, run, outcomes, documents)
    assert attached["source_versions"]["leagues"]["英超"]["status"] == "failed"
    manifest = verify_forecasts(archive)
    assert not any(name.endswith("source-expanded.pkl") for name in manifest["files"])
    assert all(
        row["status"] == "excluded" and row["reason"]
        for doc in documents.values()
        for row in doc["predictions"]
    )


def test_duplicate_attachment_is_rejected_without_touching_evidence(tmp_path):
    values = prepared(tmp_path)
    attach(*values)
    before = _snapshot(values[0])
    with pytest.raises(ValueError, match="冻结|存在"):
        attach(*values)
    assert _snapshot(values[0]) == before


def test_configured_legacy_league_cannot_be_omitted_from_candidates(tmp_path):
    from zucai.source_version_archive import source_version_files

    values = prepared(tmp_path)
    attached = attach(*values)
    attached["leagues"]["西甲"] = {"sources": ["fd", "jc"], "status": "failed"}
    with pytest.raises(ValueError, match="完整覆盖"):
        source_version_files(attached["run_id"], attached)


def test_failed_expected_definition_must_match_the_frozen_source_scope(tmp_path):
    archive, stage, run, outcomes, documents = prepared(tmp_path, failed=True)
    contradictory = entry_definition_metadata("英超", {"sources": ["apif", "jc"]})
    outcomes["英超"].update(contradictory)
    _json(stage / outcomes["英超"]["training_json_path"], outcomes["英超"])
    for document in documents.values():
        for row in document["predictions"]:
            row.update(copy.deepcopy(contradictory))
    before = _snapshot(archive)
    with pytest.raises(ValueError, match="定义|来源"):
        attach(archive, stage, run, outcomes, documents)
    assert _snapshot(archive) == before


@pytest.mark.parametrize(
    "attack",
    [
        "batch_id",
        "cutoff",
        "identity",
        "missing_target",
        "extra_target",
        "duplicate_target",
        "ready",
        "paper",
        "variant",
        "definition",
        "model_sha",
        "model_missing",
        "params_missing",
        "wrong_path",
        "unsupported_league",
        "model_registry_missing",
        "failure_predicted",
        "missing_definition_marker",
        "stripped_prediction_definition",
        "expected_existing_model",
        "training_definition",
    ],
)
def test_bad_candidate_contract_is_rejected_without_changing_main(tmp_path, attack):
    archive, stage, run, outcomes, documents = prepared(tmp_path)
    before = _snapshot(archive)
    doc = documents["2026-11-03"]
    row = doc["predictions"][0]
    if attack == "batch_id":
        doc["batch_id"] = "different-batch"
    elif attack == "cutoff":
        doc["as_of_utc"] = STAMP
    elif attack == "identity":
        row["home"], row["away"] = row["away"], row["home"]
    elif attack == "missing_target":
        doc["predictions"] = []
        doc["input"]["match_ids"] = []
    elif attack == "extra_target":
        extra = copy.deepcopy(row)
        extra["match_id"] = "jc:not-in-primary"
        doc["predictions"].append(extra)
        doc["input"]["match_ids"].append(extra["match_id"])
    elif attack == "duplicate_target":
        doc["predictions"].append(copy.deepcopy(row))
    elif attack == "ready":
        doc["p2_ready"] = True
    elif attack == "paper":
        row["paper_eligible"] = True
    elif attack == "variant":
        doc["models"][0]["source_variant"] = "baseline"
    elif attack == "definition":
        del row["model_definition_id"]
    elif attack == "model_sha":
        outcomes["英超"]["sha256"] = "f" * 64
    elif attack == "model_missing":
        (stage / outcomes["英超"]["model_path"]).unlink()
    elif attack == "params_missing":
        (stage / "models/source-run/source-expanded.params.json").unlink()
    elif attack == "wrong_path":
        outcomes["英超"]["model_path"] = "models/source-run/英超.pkl"
    elif attack == "unsupported_league":
        outcomes["荷甲"] = outcomes.pop("英超")
    elif attack == "model_registry_missing":
        doc["models"] = []
    elif attack == "failure_predicted":
        outcomes["英超"].update(status="failed", reason="synthetic failure")
        outcomes["英超"].pop("model_path")
        outcomes["英超"].pop("sha256")
    elif attack == "missing_definition_marker":
        del doc["model_definition_schema"]
    elif attack == "stripped_prediction_definition":
        for key in ("model_definition_id", "model_definition", "source_variant", "paper_eligible"):
            row.pop(key)
    elif attack == "expected_existing_model":
        row.update(status="excluded", reason="synthetic exclusion", expected_definition=True)
        row.pop("model_probabilities")
        row.pop("score_matrix")
    elif attack == "training_definition":
        path = stage / outcomes["英超"]["training_json_path"]
        training = json.loads(path.read_bytes())
        training["model_definition"]["training_sources"] = ["fd", "jc"]
        _json(path, training)
    with pytest.raises(ValueError):
        attach(archive, stage, run, outcomes, documents)
    assert _snapshot(archive) == before


@pytest.mark.parametrize(
    "attack",
    ["paper", "definition", "params_sources", "unlisted_file", "model_hash", "orphan_path"],
)
def test_verification_rechecks_candidate_semantics_after_hashes_are_updated(tmp_path, attack):
    values = prepared(tmp_path)
    archive = values[0]
    attach(*values)
    manifest = json.loads((archive / MANIFEST).read_bytes())
    run = manifest["runs"]["source-run"]
    path = run["forecasts"]["2026-11-03"]["source_versions_path"]
    if attack in {"paper", "definition"}:
        doc = json.loads((archive / path).read_bytes())
        doc["predictions"][0]["paper_eligible" if attack == "paper" else "model_definition_id"] = (
            True
        )
        _json(archive / path, doc)
        manifest["files"][path] = hashlib.sha256((archive / path).read_bytes()).hexdigest()
    elif attack == "params_sources":
        path = run["source_versions"]["params_path"]
        _json(archive / path, {"leagues": {"英超": {"sources": ["jc", "fd"]}}})
        sha = hashlib.sha256((archive / path).read_bytes()).hexdigest()
        manifest["files"][path] = run["source_versions"]["params_sha256"] = sha
    elif attack == "unlisted_file":
        manifest["files"]["models/source-run/extra.pkl"] = "a" * 64
    elif attack == "model_hash":
        run["source_versions"]["leagues"]["英超"]["sha256"] = "a" * 64
    elif attack == "orphan_path":
        del run["source_versions"]
    _json(archive / MANIFEST, manifest)
    with pytest.raises(ValueError):
        verify_forecasts(archive)


def test_manifest_failure_removes_only_candidate_files(tmp_path, monkeypatch):
    values = prepared(tmp_path)
    archive = values[0]
    before = _snapshot(archive)
    replace = Path.replace

    def fail_manifest(path, destination):
        if Path(destination) == archive / MANIFEST:
            raise OSError("synthetic candidate manifest failure")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail_manifest)
    with pytest.raises(OSError, match="candidate manifest failure"):
        attach(*values)
    assert _snapshot(archive) == before
    verify_forecasts(archive)


def test_final_verification_failure_restores_the_primary_manifest(tmp_path, monkeypatch):
    values = prepared(tmp_path)
    module = importlib.import_module("zucai.source_version_archive")
    archive = values[0]
    before = _snapshot(archive)
    original, calls = module.verify_forecasts, []

    def fail_after_commit(path):
        calls.append(path)
        if len(calls) == 2:
            raise ValueError("synthetic final verification failure")
        return original(path)

    monkeypatch.setattr(module, "verify_forecasts", fail_after_commit)
    with pytest.raises(ValueError, match="final verification failure"):
        attach(*values)
    assert _snapshot(archive) == before
    verify_forecasts(archive)


@pytest.mark.parametrize("target", ["model", "params", "destination"])
def test_candidate_paths_cannot_follow_symlinks_or_replace_existing_files(tmp_path, target):
    values = prepared(tmp_path)
    archive, stage, _, outcomes, _ = values
    model = outcomes["英超"]["model_path"]
    if target == "destination":
        (archive / model).write_bytes(b"unowned existing candidate file")
    else:
        path = stage / (
            model if target == "model" else "models/source-run/source-expanded.params.json"
        )
        other = tmp_path / "outside"
        path.rename(other)
        path.symlink_to(other)
    before = _snapshot(archive)
    with pytest.raises(ValueError):
        attach(*values)
    assert _snapshot(archive) == before


def test_actual_publication_clock_cannot_precede_candidate_generation(tmp_path):
    archive, stage, run, outcomes, documents = prepared(tmp_path)
    before = _snapshot(archive)
    module = importlib.import_module("zucai.source_version_archive")
    with pytest.raises(ValueError):
        module.attach_source_versions(
            archive,
            stage,
            run["run_id"],
            outcomes,
            documents,
            clock=lambda: datetime.fromisoformat(run["generated_utc"]),
        )
    assert _snapshot(archive) == before


def test_sidecar_needs_its_own_successful_remote_publication(tmp_path):
    from zucai.forecast_publication import confirm_publication, publication_times

    values = prepared(tmp_path)
    archive = values[0]
    prepare_remote(archive, "2026-11-02T20:00:00+00:00")
    primary_time = datetime.fromisoformat("2026-11-02T20:05:00+00:00")
    assert len(confirm_publication(archive, clock=lambda: primary_time)) == 2
    attached = attach(*values)
    candidate_paths = {entry["source_versions_path"] for entry in attached["forecasts"].values()}
    manifest = verify_forecasts(archive)
    assert not candidate_paths.intersection(publication_times(archive, manifest))
    commit_report(archive, "2026-11-02T23:00:00+00:00", push=False)
    after = datetime.fromisoformat("2026-11-02T23:05:00+00:00")
    assert confirm_publication(archive, clock=lambda: after) == []
    assert not candidate_paths.intersection(publication_times(archive, verify_forecasts(archive)))
    git(archive, "push", "origin", "HEAD:refs/heads/data/football-feeds")
    git(archive, "fetch", "origin", "refs/heads/data/football-feeds:" + REF)
    assert set(confirm_publication(archive, clock=lambda: after)) == candidate_paths
    confirmed = verify_forecasts(archive)
    resolved = publication_times(archive, confirmed)
    for entry in confirmed["runs"]["source-run"]["forecasts"].values():
        assert resolved[entry["source_versions_path"]]["published_utc"] == after.isoformat()
        assert (
            resolved[entry["path"] + "/forecast.json"]["published_utc"] == primary_time.isoformat()
        )
        assert entry["source_versions_attached_utc"] == PUBLISHED.isoformat()


def test_sidecar_confirmation_and_receipt_use_its_own_generation_time(tmp_path):
    from zucai.forecast_publication import confirm_publication, publication_times

    values = prepared(tmp_path)
    archive = values[0]
    attached = attach(*values)
    candidate_paths = {entry["source_versions_path"] for entry in attached["forecasts"].values()}
    prepare_remote(archive, "2026-11-02T23:00:00+00:00")
    early = datetime.fromisoformat("2026-11-02T21:00:00+00:00")
    assert not candidate_paths.intersection(confirm_publication(archive, clock=lambda: early))
    after = datetime.fromisoformat("2026-11-02T23:05:00+00:00")
    assert set(confirm_publication(archive, clock=lambda: after)) == candidate_paths
    manifest = verify_forecasts(archive)
    for entry in manifest["runs"]["source-run"]["forecasts"].values():
        entry["source_versions_published_utc"] = early.isoformat()
        entry["source_versions_publication_evidence"]["published_utc"] = early.isoformat()
    assert not candidate_paths.intersection(publication_times(archive, manifest))


def test_sidecar_never_uses_legacy_primary_commit_time(tmp_path):
    from zucai.forecast_publication import publication_times

    values = prepared(tmp_path)
    archive = values[0]
    attached = attach(*values)
    prepare_remote(archive, "2026-11-02T23:00:00+00:00")
    manifest = verify_forecasts(archive)
    for entry in manifest["runs"]["source-run"]["forecasts"].values():
        entry.pop("publication_required", None)
    resolved = publication_times(archive, manifest)
    candidate_paths = {entry["source_versions_path"] for entry in attached["forecasts"].values()}
    assert not candidate_paths.intersection(resolved)
    assert len(resolved) == 2


def test_failed_remote_check_does_not_create_candidate_receipts(tmp_path):
    from zucai.forecast_publication import confirm_publication

    values = prepared(tmp_path)
    archive = values[0]
    attach(*values)
    prepare_remote(archive, "2026-11-02T23:00:00+00:00")
    git(archive, "update-ref", "-d", REF)
    before = (archive / MANIFEST).read_bytes()
    with pytest.raises(ValueError, match="published_ref"):
        confirm_publication(archive, clock=lambda: PUBLISHED)
    assert (archive / MANIFEST).read_bytes() == before


def test_sidecar_published_at_kickoff_has_no_prematch_proof(tmp_path):
    from zucai.forecast_publication import confirm_publication, publication_times
    from zucai.scoreboard.score import publication_reason

    values = prepared(tmp_path)
    archive, documents = values[0], values[4]
    attached = attach(*values)
    prepare_remote(archive, "2026-11-02T23:00:00+00:00")
    doc = documents["2026-11-03"]
    row = doc["predictions"][0]
    kickoff = datetime.fromisoformat(row["kickoff_utc"])
    confirm_publication(archive, clock=lambda: kickoff)
    path = attached["forecasts"]["2026-11-03"]["source_versions_path"]
    receipt = publication_times(archive, verify_forecasts(archive))[path]
    assert (
        publication_reason(dict(row, published_utc=receipt["published_utc"]), kickoff)
        == "published_after_kickoff"
    )
