"""Offline T52 acceptance on a separate restore of the immutable real feed archive.

Run with PYTHONPATH=src python scripts/verify-t52-archive.py --archive ARCHIVE --output NEW_DIR.
No network, training, production writes, or historical-prediction replacement is performed.
"""

import argparse
import copy
import hashlib
import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.orm import Session

from zucai.cloud_feeds import restore_feeds
from zucai.cloud_predict import open_national_fixtures
from zucai.db import get_engine
from zucai.models import Match, SfcIssue
from zucai.report.batch import predict_batch
from zucai.report.sfc import build_sfc_report
from zucai.sources.sfc import refresh_snapshot

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/fixtures/t52"
EXPECTED = {
    "apif:1528958",
    "apif:1528959",
    "apif:1528961",
    "apif:1528962",
    "apif:1528963",
    "apif:1545613",
}


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
    old = json.loads((archive / "forecasts/2026-11-14/43067904858-1/forecast.json").read_text())
    cutoff = datetime.fromisoformat(old["as_of_utc"])
    generated = datetime.now(UTC)
    model = archive / provenance["model_artifact"]
    national_model = next(m for m in old["models"] if m["league"] == "欧国联")
    assert sha(model) == national_model["artifact_sha256"] == provenance["model_sha256"]
    params = ROOT / "data/model/params.json"
    assert sha(params) == national_model["params_sha256"]
    dbfile = output / "restored/football.db"
    restored = restore_feeds(archive, dbfile)
    db_before = sha(dbfile)
    engine = get_engine(dbfile)
    try:
        with Session(engine) as session:
            issue = session.get(SfcIssue, "26163")
            original_snapshot = canonical(issue.snapshot)
            mapped = refresh_snapshot(session, issue)
            assert len(mapped) == 14 and all(r["mapping"] == "matched" for r in mapped)
            assert {r["match_id"] for r in mapped[:6]} == EXPECTED
            for old_row, row in zip(issue.snapshot[:6], mapped[:6], strict=True):
                evidence = row["mapping_evidence"]
                assert evidence["basis"] == "national_team_ids_and_exact_kickoff"
                assert evidence["bridge_fallback_reason"] == "target_match_missing"
                assert evidence["bridge"] == old_row["mapping_evidence"]
                assert session.get(Match, evidence["bridge"]["jc_id"]) is None
            write_json(output / "mapped-26163.json", mapped)
            dates = open_national_fixtures(session, cutoff)
            ids = sorted(dates.get(date(2026, 11, 13), set()))
            assert set(ids) == EXPECTED
            batch6 = predict_batch(
                session,
                model_files=[model],
                as_of=cutoff,
                generated=generated,
                replay=True,
                match_ids=ids,
                params_file=params,
                market_weight=0,
                persist=False,
                allow_empty=True,
            )
            batch6["input"]["kickoff_date_beijing"] = "2026-11-13"
            assert set(batch6["input"]["match_ids"]) == EXPECTED
            assert all(r["status"] == "predicted" for r in batch6["predictions"])
            write_json(output / "nov13-replay-batch.json", batch6)
            all_ids = [r["match_id"] for r in mapped]
            batch14 = predict_batch(
                session,
                model_files=[model],
                as_of=cutoff,
                generated=generated,
                replay=True,
                match_ids=all_ids,
                params_file=params,
                market_weight=0,
                persist=False,
            )
            assert all(r["status"] == "predicted" for r in batch14["predictions"])
            write_json(output / "all14-replay-batch.json", batch14)
            by_old = {r["match_id"]: r for r in old["predictions"]}
            by_new = {r["match_id"]: r for r in batch14["predictions"]}
            frozen8 = [
                {
                    "match_id": r["match_id"],
                    "model_probabilities": by_old[r["match_id"]]["model_probabilities"],
                }
                for r in issue.snapshot[6:]
            ]
            replay8 = [
                {
                    "match_id": r["match_id"],
                    "model_probabilities": by_new[r["match_id"]]["model_probabilities"],
                }
                for r in mapped[6:]
            ]
            bytes_old, bytes_new = canonical(frozen8), canonical(replay8)
            assert bytes_old == bytes_new
            (output / "original-eight-probability-bytes.json").write_bytes(bytes_old)
            (output / "replay-eight-probability-bytes.json").write_bytes(bytes_new)
            report = build_sfc_report(
                session,
                issue,
                batch14,
                forecast_sha256=hashlib.sha256(canonical(batch14)).hexdigest(),
                generated=generated,
                allow_missing=True,
            )
            assert report["missing_models"] == 0
            assert all(r["probabilities"] is not None for r in report["rows"])
            write_json(output / "isolated-replay-sfc-report.json", report)
            wrong_issue = SimpleNamespace(
                issue_no=issue.issue_no,
                date_basis=issue.date_basis,
                snapshot=copy.deepcopy(issue.snapshot),
            )
            wrong = mapped[6]["match_id"]
            wrong_issue.snapshot[0]["mapping_evidence"]["jc_id"] = wrong
            assert session.get(Match, wrong) is not None
            conflict = refresh_snapshot(session, wrong_issue)[0]
            assert conflict["mapping"] == "ambiguous" and conflict["match_id"] is None
            assert (
                conflict["reason"] == "conflicting_identity" and wrong in conflict["candidate_ids"]
            )
            write_json(output / "existing-wrong-bridge-conflict.json", conflict)
            assert canonical(issue.snapshot) == original_snapshot
            assert not session.new and not session.dirty and not session.deleted
    finally:
        engine.dispose()
    after = inventory(archive)
    write_json(output / "archive-after-sha256.json", after)
    assert before == after
    assert sha(dbfile) == db_before
    categories = {
        name: sum(k.startswith(name + "/") for k in before)
        for name in ("forecasts", "sfc", "daily", "scoreboard")
    }
    summary = {
        "archive_commit": provenance["archive_commit"],
        "historical_as_of_utc": cutoff.isoformat(),
        "actual_replay_generated_utc": generated.isoformat(),
        "mode": "historical_replay",
        "restored_counts": restored["counts"],
        "mapped": 14,
        "total": 14,
        "nov13_input_match_ids": batch6["input"]["match_ids"],
        "nov13_predicted": 6,
        "all14_predicted": 14,
        "original_eight_byte_equal": True,
        "probability_comparison": (
            "UTF-8 canonical JSON of exact original match_id/model_probabilities "
            "versus actual model replay; no rounding or tolerance"
        ),
        "eight_probability_sha256": hashlib.sha256(bytes_old).hexdigest(),
        "wrong_existing_bridge_rejected": True,
        "wrong_bridge_candidate": wrong,
        "model_artifact": provenance["model_artifact"],
        "model_sha256": sha(model),
        "params_sha256": sha(params),
        "archive_files_unchanged": len(before),
        "frozen_file_categories": categories,
        "restored_database_sha256_unchanged": db_before,
        "issue_snapshot_unchanged": True,
        "production_written": False,
        "note": (
            "五条标准中的全量pytest/ruff由单独检查日志证明；"
            "本文件证明真实归档重放、概率逐字节比较、冲突拒绝和全部原档案SHA不变。"
        ),
    }
    write_json(output / "summary.json", summary)
    LOG.info("T52 offline acceptance passed: %s", json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.archive, args.output)
