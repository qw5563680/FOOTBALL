"""Run T53 offline acceptance on immutable archived inputs and isolated outputs.

PYTHONPATH=src python scripts/verify-t53-archive.py --archive ARCHIVE --output NEW_DIR
The archived model is loaded only after its fixed provenance SHA256 is verified.
Synthetic qualification controls are explicitly distinguished from historical matches.
"""

import argparse
import copy
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from zucai.cloud_feeds import restore_feeds
from zucai.db import get_engine
from zucai.models import SfcIssue
from zucai.parlay.single_double import build
from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes
from zucai.prediction_disclosure import prior_disclosure, prior_label, uses_unknown_team_prior
from zucai.report.render import _markdown as match_markdown
from zucai.report.sfc import _markdown as sfc_markdown
from zucai.report.sfc import build_sfc_report, pick_nine_lines
from zucai.scoreboard.score import load_forecasts, summarize_scores

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/fixtures/t53"
BASE = "eb335c7"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def inventory(folder):
    return {
        p.relative_to(folder).as_posix(): sha(p) for p in sorted(folder.rglob("*")) if p.is_file()
    }


def baseline_source(output):
    """Export tracked baseline source locally; do not fetch or install anything."""
    destination = output / "baseline-code"
    raw = subprocess.check_output(["git", "archive", BASE, "src"], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for item in archive.getmembers():
            if not item.isfile():
                continue
            target = (destination / item.name).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError("基线源码包含越界路径")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(item) as source:
                target.write_bytes(source.read())
    return destination


def baseline_probe(source, output, operation, inputs):
    """Execute actual baseline code in a separate interpreter/import path."""
    input_path = output / f"baseline-{operation}-input.json"
    output_path = output / f"baseline-{operation}-output.json"
    write_json(input_path, inputs)
    code = """
import json,sys
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import Session
import zucai
from zucai.db import get_engine
from zucai.models import SfcIssue
from zucai.parlay.single_double import build
from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes
from zucai.report.sfc import build_sfc_report
assert Path(zucai.__file__).resolve().is_relative_to(Path(sys.argv[4]).resolve())
data=json.loads(Path(sys.argv[2]).read_text())
if sys.argv[1]=='parlay':
    result=build(**data)
elif sys.argv[1]=='model':
    model=DixonColes.load(data['model'])
    result={}
    for row in data['rows']:
        grid,metadata=model.predict_with_metadata(row)
        result[row['match_id']]={'model_probabilities':had(grid),'metadata':metadata}
else:
    engine=get_engine(data['database'])
    with Session(engine) as session:
        result=build_sfc_report(session,session.get(SfcIssue,'26159'),data['batch'],
            forecast_sha256=data['forecast_sha256'],
            generated=datetime.fromisoformat(data['generated']),allow_missing=True)
    engine.dispose()
Path(sys.argv[3]).write_text(json.dumps(result,ensure_ascii=False,sort_keys=True))
"""
    environment = dict(os.environ, PYTHONPATH=str(source / "src"))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            operation,
            str(input_path),
            str(output_path),
            str(source / "src"),
        ],
        env=environment,
        cwd=output,
        capture_output=True,
        text=True,
        check=True,
    )
    (output / f"baseline-{operation}.log").write_text(result.stdout + result.stderr)
    return json.loads(output_path.read_text())


def eligibility_projection(document):
    return {
        "matches": {
            row["match_id"]: {
                key: row[key]
                for key in (
                    "probabilities",
                    "model_probabilities",
                    "eligibility",
                    "single",
                    "double",
                )
            }
            for board in document["boards"]
            for row in board["matches"]
        },
        "groups": [
            {
                key: group[key]
                for key in (
                    "group_id",
                    "match_ids",
                    "single_selection",
                    "double_selections",
                    "coverage_probability_exact",
                )
            }
            for group in document["groups"]
        ],
    }


def qualification_control(source, output, historical_row):
    """T45 supplies an explicitly synthetic complete qualification control."""
    sample = json.loads((ROOT / "data/fixtures/t45/input.json").read_bytes())
    for row in sample["batch"]["predictions"]:
        row["metadata"] = {
            "unknown_team_prior": False,
            "home_unknown_team": False,
            "away_unknown_team": False,
            "prior_method": None,
        }
    before = canonical(sample)
    old = baseline_probe(source, output, "parlay", sample)
    known = build(**sample)
    old_projection, new_projection = eligibility_projection(old), eligibility_projection(known)
    assert canonical(old_projection) == canonical(new_projection)
    assert len(known["groups"]) == 3
    write_json(output / "known-qualification-before.json", old_projection)
    write_json(output / "known-qualification-after.json", new_projection)
    assert canonical(sample) == before
    changed = copy.deepcopy(sample)
    first = changed["batch"]["predictions"][0]
    first["metadata"] = copy.deepcopy(historical_row["metadata"])
    affected = first["match_id"]
    research_probability = canonical(first["model_probabilities"])
    guarded = build(**changed)
    assert not any(affected in group["match_ids"] for group in guarded["groups"])
    for board in guarded["boards"]:
        for leader in ("highest_coverage", "highest_profit"):
            assert not board.get(leader) or affected not in board[leader]["match_ids"]
    prepared = next(
        row
        for board in guarded["boards"]
        for row in board["matches"]
        if row["match_id"] == affected
    )
    assert canonical(prepared["model_probabilities"]) == research_probability
    assert prepared["eligibility"]["status"] != "eligible"
    assert "unknown_team_prior" in prepared["eligibility"]["reasons"]
    assert any(row["match_id"] == affected for row in guarded["diagnostics"])
    write_json(output / "synthetic-unknown-qualification.json", guarded)
    return {
        "scope": "明确合成的T45准入控制；使用真实未知先验元信息，不冒充历史销售资格",
        "known_match_count": len(sample["batch"]["predictions"]),
        "known_groups_before_and_after": len(known["groups"]),
        "known_projection_byte_equal": True,
        "known_projection_sha256": hashlib.sha256(canonical(new_projection)).hexdigest(),
        "affected_control_id": affected,
        "affected_absent_from_main_groups_and_leaders": True,
        "research_probabilities_retained": True,
    }


def verify(archive, output):
    archive, output = archive.resolve(), output.resolve()
    if output.is_relative_to(archive) or archive.is_relative_to(output):
        raise ValueError("验收输出必须与输入原件分离")
    output.mkdir(parents=True, exist_ok=False)
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    for name, expected in provenance["source_files_sha256"].items():
        assert sha(archive / name) == expected, name
    before = inventory(archive)
    write_json(output / "archive-before-sha256.json", before)
    source = baseline_source(output)
    batch = json.loads((archive / "forecasts/2026-11-06/42024862563-1/forecast.json").read_text())
    frozen_batch_bytes = canonical(batch)
    model_path = archive / provenance["model_artifact"]
    model = DixonColes.load(model_path)
    expected_sides = {
        "jc:2042519": (False, False),
        "jc:2042520": (True, False),
        "jc:2042523": (False, True),
    }
    baseline_model = baseline_probe(
        source,
        output,
        "model",
        {
            "model": str(model_path),
            "rows": [row for row in batch["predictions"] if row["match_id"] in expected_sides],
        },
    )
    cases, real_predictions = {}, {}
    for row in batch["predictions"]:
        if row["match_id"] not in expected_sides:
            continue
        grid, metadata = model.predict_with_metadata(row)
        probability = had(grid)
        base_probability = baseline_model[row["match_id"]]["model_probabilities"]
        assert canonical(probability) == canonical(base_probability)
        home, away = expected_sides[row["match_id"]]
        assert metadata["home_unknown_team"] is home and metadata["away_unknown_team"] is away
        updated = {**row, "metadata": metadata}
        disclosure = prior_disclosure(updated)
        assert disclosure["home"] is home and disclosure["away"] is away
        assert prior_disclosure(row)["used"] == disclosure["used"]
        cases[row["match_id"]] = {
            "home": row["home"],
            "away": row["away"],
            "disclosure": disclosure,
            "base_to_patched_probability_bytes_equal": True,
            "frozen_probability_bytes_equal": (
                canonical(probability) == canonical(row["model_probabilities"])
            ),
            "frozen_model_probabilities": row["model_probabilities"],
            "baseline_recalculation_minus_frozen": {
                key: base_probability[key] - row["model_probabilities"][key] for key in "hda"
            },
            "model_probabilities": probability,
            "label": prior_label(updated),
        }
        real_predictions[row["match_id"]] = updated
    historical_row = next(row for row in batch["predictions"] if row["match_id"] == "jc:2042523")
    synthetic = {
        **historical_row,
        "match_id": "synthetic:t53-both-unknown",
        "home": "T53合成未见主队",
        "away": "T53合成未见客队",
    }
    assert synthetic["home"] not in model.teams and synthetic["away"] not in model.teams
    grid, metadata = model.predict_with_metadata(synthetic)
    assert metadata["home_unknown_team"] is True and metadata["away_unknown_team"] is True
    synthetic.update(metadata=metadata, model_probabilities=had(grid), score_matrix=grid.tolist())
    disclosure = prior_disclosure(synthetic)
    assert disclosure["used"] and disclosure["home"] and disclosure["away"]
    write_json(output / "synthetic-both-unknown-prediction.json", synthetic)
    cases[synthetic["match_id"]] = {
        "kind": "synthetic_inputs_actual_archived_model",
        "disclosure": disclosure,
        "label": prior_label(synthetic),
    }
    write_json(output / "actual-model-side-cases.json", cases)
    single_text = match_markdown(batch)
    ac_line = next(
        line for line in single_text.splitlines() if "jc:2042523" in line and "AC米兰" in line
    )
    assert "默认实力" in ac_line and "费伦茨" in ac_line and "96.29%" in ac_line
    assert "weakest_quartile_half_shrinkage" in ac_line.replace("\\_", "_")
    (output / "single-match-research-report.md").write_text(single_text)
    qualification = qualification_control(source, output, historical_row)
    database = output / "restored/football.db"
    restored = restore_feeds(archive, database)
    db_sha = sha(database)
    old_report = json.loads((archive / "sfc/26159/sfc.json").read_text())
    report_inputs = {
        "database": str(database),
        "batch": old_report["forecast_batch"],
        "forecast_sha256": old_report["forecast_sha256"],
        "generated": old_report["generated_utc"],
    }
    old_sfc = baseline_probe(source, output, "sfc", report_inputs)
    engine = get_engine(database)
    try:
        with Session(engine) as session:
            report = build_sfc_report(
                session,
                session.get(SfcIssue, "26159"),
                old_report["forecast_batch"],
                forecast_sha256=old_report["forecast_sha256"],
                generated=datetime.fromisoformat(old_report["generated_utc"]),
                allow_missing=True,
            )
            assert not session.new and not session.dirty and not session.deleted
    finally:
        engine.dispose()
    assert sha(database) == db_sha
    predicted_by_id = {row["match_id"]: row for row in old_report["forecast_batch"]["predictions"]}
    excluded_seq = {
        row["seq"]
        for row in report["rows"]
        if uses_unknown_team_prior(predicted_by_id.get(row["match_id"], {}))
    }
    assert 1 in excluded_seq
    assert not excluded_seq.intersection(report["confidence_order"])
    assert report["confidence_order"] == [
        seq for seq in old_sfc["confidence_order"] if seq not in excluded_seq
    ]

    def probabilities(value):
        # Compare raw probabilities, not formatted percentages.
        return [
            {"match_id": row["match_id"], "probabilities": row["probabilities"]}
            for row in value["rows"]
        ]

    assert canonical(probabilities(old_sfc)) == canonical(probabilities(report))
    write_json(output / "sfc-replay.json", report)
    (output / "sfc-research-report.md").write_bytes(sfc_markdown(report))
    legacy_report_before = canonical(old_report)
    legacy_text = sfc_markdown(old_report).decode()
    assert "默认实力" in legacy_text and "费伦茨" in legacy_text
    assert canonical(old_report) == legacy_report_before
    (output / "legacy-sfc-read-only.md").write_text(legacy_text)
    # Explicit synthetic publication receipt exercises selection, not a publication claim.
    pick_control = copy.deepcopy(report)
    pick_control.update(
        sale_state="published_before_deadline", published_utc=report["generated_utc"]
    )
    pick_text = "\n".join(pick_nine_lines(pick_control))
    assert "默认实力" in pick_text
    (output / "synthetic-publication-pick-nine.md").write_text(pick_text)
    original_score_summary = json.loads((archive / "scoreboard/summary.json").read_text())
    as_of = datetime.fromisoformat(original_score_summary["as_of_utc"])
    forecasts = load_forecasts(archive, as_of)
    scored = [
        json.loads(line) for line in (archive / "scoreboard/scored.jsonl").read_text().splitlines()
    ]
    scored_bytes = canonical(scored)
    # Mirror scoreboard.command: experimental rows have a separate evaluation path.
    production_scored = [
        row for row in scored if row.get("validation_status") != "experimental_unadopted"
    ]
    active_keys = original_score_summary["audit"]["active_keys"]
    scores = summarize_scores(
        production_scored, as_of, active_keys=active_keys, forecast_rows=forecasts
    )
    assert scores["total"]["n"] == original_score_summary["probability"]["total"]["n"]
    assert scores["prior_strata"]["default_strength"]["n"] == 4
    assert scores["prior_strata"]["known_teams"]["n"] == 92
    assert scores["prior_strata"]["unrecorded"]["n"] == 0
    assert sum(row["n"] for row in scores["prior_strata"].values()) == scores["total"]["n"]
    assert canonical(scored) == scored_bytes
    write_json(output / "actual-score-strata.json", scores["prior_strata"])
    assert canonical(batch) == frozen_batch_bytes
    after = inventory(archive)
    assert before == after
    write_json(output / "archive-after-sha256.json", after)
    summary = {
        "archive_commit": provenance["archive_commit"],
        "baseline_code": BASE,
        "executed_utc": datetime.now(UTC).isoformat(),
        "model_artifact": provenance["model_artifact"],
        "model_sha256": sha(model_path),
        "real_model_side_cases": cases,
        "synthetic_qualification_control": qualification,
        "sfc_original_confidence_order": old_sfc["confidence_order"],
        "sfc_filtered_confidence_order": report["confidence_order"],
        "sfc_prior_sequences_excluded": sorted(excluded_seq),
        "sfc_all_research_probabilities_unchanged": True,
        "score_strata_counts": {key: row["n"] for key, row in scores["prior_strata"].items()},
        "score_cohort": {
            "as_of_utc": as_of.isoformat(),
            "raw_scored_rows": len(scored),
            "experimental_rows_excluded": len(scored) - len(production_scored),
            "production_rows": len(production_scored),
            "active_keys": len(active_keys),
            "active_scored": scores["total"]["n"],
            "selection": "scoreboard.command production-only rows and original audit.active_keys",
        },
        "archive_files_unchanged": len(before),
        "frozen_file_categories": {
            name: sum(key.startswith(name + "/") for key in before)
            for name in ("forecasts", "sfc", "daily", "scoreboard")
        },
        "restored_counts": restored["counts"],
        "restored_database_sha256_unchanged": db_sha,
        "production_written": False,
        "note": (
            "全量pytest/Ruff另有日志；合成双方未知及准入/发布控制不冒充真实历史比赛或线上发布。"
        ),
    }
    write_json(output / "summary.json", summary)
    LOG.info("T53 offline acceptance passed: %s", json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.archive, args.output)
