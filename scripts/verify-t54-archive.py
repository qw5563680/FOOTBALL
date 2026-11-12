"""Replay T54 against the immutable real 2026-11-12 archive, entirely offline.

PYTHONPATH=src python scripts/verify-t54-archive.py --archive ARCHIVE --output NEW_DIR
Only an isolated restored database and the separate output directory are written.
"""

import argparse
import copy
import hashlib
import json
import logging
import platform
import sqlite3
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from sqlalchemy.orm import Session

from zucai.cloud_feeds import restore_feeds
from zucai.db import get_engine
from zucai.models import Match, SfcIssue
from zucai.report.batch import predict_batch
from zucai.report.sfc import build_sfc_report
from zucai.sources.jc_identity import reconcile
from zucai.sources.sfc import refresh_snapshot

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/fixtures/t54"
TARGETS = ("jc:2042694", "apif:1528963")


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


def database_state(database):
    """Hash all non-target records, not only the frozen files outside the database."""
    state = {}
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (name,) in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            sql = f"SELECT * FROM {quoted}"
            args = ()
            if name == "matches":
                sql += " WHERE match_id NOT IN (?, ?)"
                args = TARGETS
            digest = hashlib.sha256()
            for row in connection.execute(sql + " ORDER BY rowid", args):
                digest.update(canonical(row) + b"\n")
            state[name] = digest.hexdigest()
    return state


def projection(rows):
    return [
        {k: row[k] for k in ("seq", "match_id", "probabilities")} for row in rows if row["seq"] != 3
    ]


def verify(archive, output):
    archive, output = archive.resolve(), output.resolve()
    if output.is_relative_to(archive) or archive.is_relative_to(output):
        raise ValueError("验收输出目录必须与归档目录分离")
    output.mkdir(parents=True, exist_ok=False)
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    for name, expected in provenance["source_files_sha256"].items():
        if sha(archive / name) != expected:
            raise ValueError(f"原始快照文件SHA不符: {name}")
    before = inventory(archive)
    write_json(output / "archive-before-sha256.json", before)
    frozen = json.loads((archive / provenance["frozen_report"]).read_text())
    assert frozen["missing_models"] == 1
    assert frozen["rows"][2]["probabilities"] is None
    assert frozen["rows"][2]["reason"] == "未映射：conflicting_identity"
    cutoff = datetime.fromisoformat(frozen["forecast_batch"]["as_of_utc"])
    assert cutoff.isoformat() == provenance["historical_as_of_utc"]
    generated = datetime.now(UTC)
    model = archive / provenance["model_artifact"]
    national = next(m for m in frozen["forecast_batch"]["models"] if m["league"] == "欧国联")
    assert sha(model) == national["artifact_sha256"] == provenance["model_sha256"]
    params = ROOT / "data/model/params.json"
    assert sha(params) == national["params_sha256"]
    database = output / "restored/football.db"
    restored = restore_feeds(archive, database)
    db_before = database_state(database)
    engine = get_engine(database)
    try:
        with Session(engine) as session:
            issue = session.get(SfcIssue, "26163")
            snapshot_before = canonical(issue.snapshot)
            baseline = refresh_snapshot(session, issue)
            assert sum(r["mapping"] == "matched" for r in baseline) == 13
            assert baseline[2]["reason"] == "conflicting_identity"
            assert session.get(Match, TARGETS[0]).home_team_id == 1156
            assert session.get(Match, TARGETS[1]).home_team_id == 1149
            write_json(output / "baseline-26163.json", baseline)
            baseline_batch = predict_batch(
                session,
                model_files=[model],
                as_of=cutoff,
                generated=generated,
                replay=True,
                match_ids=[r["match_id"] for r in baseline if r["seq"] != 3],
                params_file=params,
                market_weight=0,
                persist=False,
            )
            assert all(p["status"] == "predicted" for p in baseline_batch["predictions"])
            write_json(output / "baseline-thirteen-recomputed-batch.json", baseline_batch)
    finally:
        engine.dispose()
    repair = reconcile(database, "t54-offline-acceptance")
    assert repair["requests"] == 0 and repair["rekeyed_matches"] == 1
    write_json(output / "reconciliation.json", repair)
    engine = get_engine(database)
    try:
        with Session(engine) as session:
            issue = session.get(SfcIssue, "26163")
            mapped = refresh_snapshot(session, issue)
            assert len(mapped) == 14 and all(r["mapping"] == "matched" for r in mapped)
            assert mapped[2]["match_id"] == TARGETS[0]
            assert mapped[2]["matched_identity"]["home"] == "阿尔巴尼亚"
            jc, apif = (session.get(Match, key) for key in TARGETS)
            assert (jc.home_team_id, jc.away_team_id, jc.kickoff_utc, jc.league) == (
                apif.home_team_id,
                apif.away_team_id,
                apif.kickoff_utc,
                apif.league,
            )
            assert jc.home_team_id == 1149
            write_json(output / "mapped-26163.json", mapped)
            batch = predict_batch(
                session,
                model_files=[model],
                as_of=cutoff,
                generated=generated,
                replay=True,
                match_ids=[r["match_id"] for r in mapped],
                params_file=params,
                market_weight=0,
                persist=False,
            )
            assert len(batch["predictions"]) == 14
            assert all(r["status"] == "predicted" for r in batch["predictions"])
            assert batch["mode"] == "historical_replay" and batch["bet_allowed"] is False
            write_json(output / "all14-recomputed-batch.json", batch)
            by_recomputed = {p["match_id"]: p for p in batch["predictions"]}
            same_runtime_before = canonical(
                [
                    {k: p[k] for k in ("match_id", "model_probabilities")}
                    for p in baseline_batch["predictions"]
                ]
            )
            same_runtime_after = canonical(
                [
                    {
                        k: by_recomputed[p["match_id"]][k]
                        for k in ("match_id", "model_probabilities")
                    }
                    for p in baseline_batch["predictions"]
                ]
            )
            assert same_runtime_before == same_runtime_after
            (output / "same-runtime-before-thirteen.json").write_bytes(same_runtime_before)
            (output / "same-runtime-after-thirteen.json").write_bytes(same_runtime_after)
            differences = [
                {
                    "seq": row["seq"],
                    "match_id": row["match_id"],
                    "archived": row["probabilities"],
                    "recomputed": by_recomputed[row["match_id"]]["model_probabilities"],
                }
                for row in frozen["rows"]
                if row["seq"] != 3
                and row["probabilities"] != by_recomputed[row["match_id"]]["model_probabilities"]
            ]
            write_json(
                output / "archived-versus-recomputed-diagnostic.json",
                {
                    "different_rows": differences,
                    "same_runtime_before_after_byte_equal": True,
                    "runtime": {
                        "python": platform.python_version(),
                        "platform": platform.platform(),
                        "numpy": version("numpy"),
                        "scipy": version("scipy"),
                        "penaltyblog": version("penaltyblog"),
                    },
                    "note": (
                        "如存在差异，原归档与当前重算不能声称逐字节一致。"
                        "同一运行环境修复前后的13场精确一致，排除本次身份修复造成数值改变；"
                        "不舍入或修改原概率来隐藏浮点低位差异。"
                    ),
                },
            )
            # Preserve the already-frozen 13 predictions verbatim. Only the previously
            # excluded third row is generated using the original model and cutoff.
            # This is a separate historical replay, never a replacement frozen batch.
            isolated = copy.deepcopy(frozen["forecast_batch"])
            isolated.update(
                batch_id="t54-isolated-historical-replay",
                mode="historical_replay",
                generated_utc=generated.isoformat(),
            )
            isolated["models"] = [copy.deepcopy(national)]
            original_predictions = {
                p["match_id"]: p for p in frozen["forecast_batch"]["predictions"]
            }
            isolated["predictions"] = [
                copy.deepcopy(
                    by_recomputed[row["match_id"]]
                    if row["seq"] == 3
                    else original_predictions[row["match_id"]]
                )
                for row in mapped
            ]
            isolated["input"] = {
                "match_ids": [r["match_id"] for r in mapped],
                "market_weight": 0,
                "original_frozen_report": provenance["frozen_report"],
                "original_frozen_report_sha256": sha(archive / provenance["frozen_report"]),
                "reused_frozen_match_ids": [r["match_id"] for r in mapped if r["seq"] != 3],
                "newly_recomputed_match_ids": [TARGETS[0]],
                "reference_only": True,
            }
            for prediction in isolated["predictions"]:
                if prediction["match_id"] != TARGETS[0]:
                    assert canonical(prediction) == canonical(
                        original_predictions[prediction["match_id"]]
                    )
            write_json(output / "isolated-replay-batch.json", isolated)
            report = build_sfc_report(
                session,
                issue,
                isolated,
                forecast_sha256=hashlib.sha256(canonical(isolated)).hexdigest(),
                generated=generated,
                allow_missing=True,
            )
            assert report["missing_models"] == 0
            assert all(r["probabilities"] is not None for r in report["rows"])
            write_json(output / "isolated-replay-sfc-report.json", report)
            original_bytes = canonical(projection(frozen["rows"]))
            replay_bytes = canonical(projection(report["rows"]))
            assert original_bytes == replay_bytes
            (output / "original-thirteen-probability-bytes.json").write_bytes(original_bytes)
            (output / "replay-thirteen-probability-bytes.json").write_bytes(replay_bytes)
            assert canonical(issue.snapshot) == snapshot_before
            assert not session.new and not session.dirty and not session.deleted
    finally:
        engine.dispose()
    assert database_state(database) == db_before
    after = inventory(archive)
    write_json(output / "archive-after-sha256.json", after)
    assert before == after
    investigation = json.loads((FIXTURE / "team-investigation.json").read_text())
    assert {r["team_id"] for r in investigation["entries"]} == {
        1153,
        1154,
        1155,
        1157,
        1158,
        1159,
        1160,
    }
    assert all(r["status"] == "pending" for r in investigation["entries"])
    write_json(output / "team-investigation.json", investigation)
    summary = {
        "archive_commit": provenance["archive_commit"],
        "frozen_report": provenance["frozen_report"],
        "historical_as_of_utc": cutoff.isoformat(),
        "actual_replay_generated_utc": generated.isoformat(),
        "mode": "historical_replay",
        "restored_counts": restored["counts"],
        "baseline_mapped": 13,
        "baseline_third_reason": "conflicting_identity",
        "repaired_mapped": 14,
        "all14_predicted": 14,
        "same_runtime_thirteen_before_after_byte_equal": True,
        "same_runtime_thirteen_sha256": hashlib.sha256(same_runtime_before).hexdigest(),
        "archived_vs_full_recompute_byte_equal": not differences,
        "archived_vs_full_recompute_different_match_ids": [r["match_id"] for r in differences],
        "isolated_report_probability_sources": {
            "reused_frozen": 13,
            "newly_recomputed_with_original_model": [TARGETS[0]],
        },
        "third_match_id": mapped[2]["match_id"],
        "third_identity": mapped[2]["matched_identity"],
        "third_probabilities": report["rows"][2]["probabilities"],
        "original_thirteen_byte_equal": True,
        "probability_comparison": (
            "Original frozen 13 versus separate replay report reusing those 13 exact predictions; "
            "UTF-8 canonical JSON of seq/match_id/probabilities, no rounding or tolerance. "
            "Independent same-runtime pre/post repair recomputation also compares exact bytes."
        ),
        "thirteen_probability_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "model_artifact": provenance["model_artifact"],
        "model_sha256": sha(model),
        "params_sha256": sha(params),
        "archive_files_unchanged": len(before),
        "frozen_file_categories": {
            name: sum(k.startswith(name + "/") for k in before)
            for name in ("forecasts", "sfc", "daily", "scoreboard", "models")
        },
        "non_target_database_tables_sha256_unchanged": db_before,
        "issue_snapshot_unchanged": True,
        "registered_teams": [r["team_id"] for r in investigation["entries"]],
        "production_written": False,
        "network_requests": 0,
        "note": "全量pytest/ruff另行记录；这是独立历史重放，不替换原冻结预测、报告或账本。",
    }
    write_json(output / "summary.json", summary)
    LOG.info("T54 offline acceptance passed: %s", json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.archive, args.output)
