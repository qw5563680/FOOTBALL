"""Malformed experiment files cannot change formal scores, cash history or other files."""
# ruff: noqa: F811

import hashlib
import json
import math
from pathlib import Path

import pytest

from scoreboard_summary_support import assert_same_summary
from test_scoreboard_command import scoreboard_archive  # noqa: F401
from test_scoreboard_score import NOW
from zucai.forecast_archive import attach_experiments, verify_forecasts
from zucai.scoreboard.command import run
from zucai.scoreboard.experimental import load_experiments
from zucai.scoreboard.score import instant

CASES = json.loads(
    (Path(__file__).parents[1] / "data/fixtures/t34a/invalid-experiments.json").read_bytes()
)


def attach_fixtures(archive):
    manifest = verify_forecasts(archive)
    for run_id, info in manifest["runs"].items():
        docs = {}
        for day, forecast in info["forecasts"].items():
            original = json.loads((archive / forecast["path"] / "forecast.json").read_bytes())
            rows = []
            for row in original["predictions"]:
                row = dict(
                    row,
                    model_name="dc-sot-v1",
                    model_version="offline-experiment",
                    p2_ready=False,
                    validation_status="experimental_unadopted",
                    availability_mode="strict",
                    fallback_reason=None if row["status"] == "predicted" else row["reason"],
                )
                row.pop("market", None)
                rows.append(row)
            docs[day] = dict(
                schema_version=1,
                availability_mode="strict",
                predictions=rows,
                **{k: original[k] for k in ("batch_id", "generated_utc", "as_of_utc", "mode")},
            )
        attach_experiments(
            archive, docs, run_id, clock=lambda stamp=info["generated_utc"]: instant(stamp)
        )

    from forecast_publication_support import confirm_fixture

    confirm_fixture(archive)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_bad_experiment_file_is_audited_without_blocking_formal_scoreboard(
    scoreboard_archive, case
):
    archive = scoreboard_archive
    before = run(archive, as_of=NOW)
    ledger = (archive / "scoreboard/ledger.jsonl").read_bytes()
    scores = (archive / "scoreboard/scored.jsonl").read_bytes()
    attach_fixtures(archive)
    manifest = verify_forecasts(archive)
    good_rows, audit = load_experiments(archive, manifest, NOW)
    assert audit == [] and good_rows
    path = good_rows[0]["forecast_path"]
    document = json.loads((archive / path).read_bytes())
    assert len(document["predictions"]) > 1
    if "remove" in case:
        document.pop(case["remove"])
    document.update(case.get("document_fields", {}))
    if "row_fields" in case:
        # Valid rows preceding a bad row must not partially enter the batch.
        document["predictions"][-1].update(case["row_fields"])
    if "remove_row" in case:
        document["predictions"][-1].pop(case["remove_row"])
    if "row_nan" in case:
        document["predictions"][-1][case["row_nan"]] = float("nan")
    if case.get("duplicate_prediction"):
        document["predictions"].append(
            dict(document["predictions"][0], model_probabilities={"h": 0.2, "d": 0.3, "a": 0.5})
        )
    raw = (
        case["raw"].encode()
        if "raw" in case
        else json.dumps(case.get("document", document)).encode()
    )
    (archive / path).write_bytes(raw)
    manifest["files"][path] = hashlib.sha256(raw).hexdigest()
    (archive / "forecast_manifest.json").write_text(json.dumps(manifest))
    verify_forecasts(archive)  # Test a malformed producer file, not a hash-integrity bypass.
    loaded, audit = load_experiments(archive, manifest, NOW)
    assert loaded == [r for r in good_rows if r["forecast_path"] != path]
    assert loaded
    assert len(audit) == 1
    assert audit[0]["forecast_path"] == path
    assert audit[0]["reason"] == "invalid_experimental_file"
    after = run(archive, as_of=NOW)
    assert after["experimental_audit"]["invalid_files"] == audit
    assert_same_summary(after["probability"], before["probability"])
    assert after["accounts"] == before["accounts"]
    assert (archive / "scoreboard/ledger.jsonl").read_bytes() == ledger
    scored = (archive / "scoreboard/scored.jsonl").read_bytes()
    assert scored.startswith(scores)
    run(archive, as_of=NOW)
    assert (archive / "scoreboard/scored.jsonl").read_bytes() == scored
    assert (archive / path).read_bytes() == raw


def test_summary_tolerance_accepts_only_last_bit_float_drift():
    expected = {"model": "team", "n": 2, "ready": True, "bins": [0.3, None]}
    assert_same_summary(dict(expected, bins=[math.nextafter(0.3, 1.0), None]), expected)
    changes = [
        dict(expected, bins=[0.300001, None]),
        dict(expected, n=3),
        dict(expected, n=2.0),
        dict(expected, ready=1),
        dict(expected, model="market"),
        dict(expected, bins=[None, 0.3]),
        {key: value for key, value in expected.items() if key != "ready"},
    ]
    for changed in changes:
        with pytest.raises(AssertionError):
            assert_same_summary(changed, expected)
