"""Experimental frozen attachments cannot rewrite production scores or money history."""
# ruff: noqa: F811

import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import timedelta

from test_experimental_scoreboard import scores
from test_scoreboard_command import scoreboard_archive  # noqa: F401
from test_scoreboard_score import NOW
from zucai.forecast_archive import attach_experiments, verify_forecasts
from zucai.scoreboard.command import run
from zucai.scoreboard.experimental import summarize_experiments
from zucai.scoreboard.score import instant, score_forecasts


def test_append_experimental_scores_preserves_original_scores_ledger_and_repeat_bytes(
    scoreboard_archive,
):
    archive = scoreboard_archive
    first = run(archive, as_of=NOW)
    ledger = (archive / "scoreboard/ledger.jsonl").read_bytes()
    original_scores = (archive / "scoreboard/scored.jsonl").read_bytes()
    manifest = verify_forecasts(archive)
    for run_id, info in manifest["runs"].items():
        docs = {}
        for day, forecast in info["forecasts"].items():
            batch = json.loads((archive / forecast["path"] / "forecast.json").read_bytes())
            predictions = []
            for original in batch["predictions"]:
                row = dict(
                    original,
                    model_name="dc-sot-v1",
                    model_version="fixture-experiment",
                    validation_status="experimental_unadopted",
                    availability_mode="strict",
                    p2_ready=False,
                    fallback_reason=None
                    if original["status"] == "predicted"
                    else original["reason"],
                )
                row.pop("market", None)
                predictions.append(row)
            docs[day] = dict(
                schema_version=1,
                batch_id=batch["batch_id"],
                as_of_utc=batch["as_of_utc"],
                generated_utc=batch["generated_utc"],
                mode=batch["mode"],
                availability_mode="strict",
                predictions=predictions,
            )
        attach_experiments(archive, docs, run_id)
    from forecast_publication_support import confirm_fixture

    confirm_fixture(archive)
    second = run(archive, as_of=NOW)
    assert first["probability"] == second["probability"]
    assert (archive / "scoreboard/ledger.jsonl").read_bytes() == ledger
    updated = (archive / "scoreboard/scored.jsonl").read_bytes()
    assert updated.startswith(original_scores) and len(updated) > len(original_scores)
    run(archive, as_of=NOW)
    assert (archive / "scoreboard/scored.jsonl").read_bytes() == updated
    assert (archive / "scoreboard/ledger.jsonl").read_bytes() == ledger


def test_utc_week_paired_bootstrap_is_reproducible():
    rows, results, experiments, _, _ = scores()
    for source in (rows, experiments):
        extra = deepcopy(source)
        for row in extra:
            row["match_id"] += ":week2"
            for key in ("generated_utc", "as_of_utc", "kickoff_utc"):
                row[key] = (instant(row[key]) + timedelta(days=7)).isoformat()
        source.extend(extra)
    for key, value in list(results.items()):
        row = dict(value, match_id=key + ":week2")
        for column in ("available_utc", "kickoff_utc"):
            row[column] = (instant(row[column]) + timedelta(days=7)).isoformat()
        results[row["match_id"]] = row
    at = NOW + timedelta(days=7)
    production, audit = score_forecasts(rows, results, [], at)
    scored, _ = score_forecasts(experiments, results, [], at, experimental=True)
    first = summarize_experiments(experiments, scored, production, audit["active_keys"], at)
    assert first == summarize_experiments(experiments, scored, production, audit["active_keys"], at)
    english = next(
        g for g in first["groups"] if g["model_name"] == "dc-sot-v1" and g["league"] == "英超"
    )
    assert english["paired"]["blocks"] == 2 and english["common_n"] == 4
    assert english["paired"]["log_loss"]["interval"] is not None
    assert english["paired"]["replicates"] == 2000


def test_scoreboard_and_digest_do_not_require_model_extra():
    code = """
import sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname == 'penaltyblog' or fullname.startswith('penaltyblog.'):
            raise ModuleNotFoundError('model extra intentionally unavailable')
sys.meta_path.insert(0, Block())
import zucai.scoreboard.command
import zucai.daily_digest
"""
    subprocess.run(
        [sys.executable, "-c", code], env=os.environ.copy(), check=True, capture_output=True
    )
