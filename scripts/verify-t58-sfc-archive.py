"""Replay T58 mappings and disclosure on an immutable real archive, entirely offline.

PYTHONPATH=src python scripts/verify-t58-sfc-archive.py --archive ARCHIVE --output NEW_DIR
Uses only original frozen forecasts. No model calculation, collection or publication.
"""

import argparse
import copy
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.orm import Session

from zucai import nations
from zucai.cloud_feeds import restore_feeds
from zucai.db import get_engine
from zucai.models import SfcIssue
from zucai.report.render import _json_bytes
from zucai.report.sfc import build_sfc_report, write_sfc_report
from zucai.sfc_reports import _batch, read_report
from zucai.sources.sfc import refresh_snapshot
from zucai.sources.sporttery import BEIJING

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/fixtures/t58"
EXPECTED = {2: "apif:1528973", 6: "apif:1528977", 10: "apif:1528981"}
MISSING_IDS = {"77", "55", "26", "37", "157"}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(folder):
    return {
        p.relative_to(folder).as_posix(): sha(p)
        for p in sorted(folder.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(folder).parts
    }


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def rejected_identities(session, issue, mapped):
    """Each changed identity must fail, even with a fully spelled team name."""
    rejected = []
    for seq in EXPECTED:
        for kind in ("home_away_reversed", "kickoff_plus_one_minute", "unknown_id_full_name"):
            rows = copy.deepcopy(issue.snapshot)
            row = rows[seq - 1]
            if kind == "home_away_reversed":
                row["home"], row["away"] = row["away"], row["home"]
                row["provider_home_id"], row["provider_away_id"] = (
                    row["provider_away_id"],
                    row["provider_home_id"],
                )
            elif kind == "kickoff_plus_one_minute":
                changed = datetime.fromisoformat(row["source_kickoff_utc"]) + timedelta(minutes=1)
                row["source_kickoff_utc"] = row["kickoff_utc"] = changed.isoformat()
            else:
                row["provider_home_id"] = "999999999"
                row["home"] = mapped[seq - 1]["matched_identity"]["home"]
            changed_issue = SimpleNamespace(
                issue_no=issue.issue_no, date_basis=issue.date_basis, snapshot=rows
            )
            result = refresh_snapshot(session, changed_issue)
            changed = result[seq - 1]
            assert changed["match_id"] is None and changed["mapping"] != "matched"
            assert all(r == mapped[i] for i, r in enumerate(result) if i != seq - 1)
            rejected.append(dict(case=kind, seq=seq, input=row, result=changed))
    return rejected


def reports(session, issue, archive, output, generated):
    original = read_report(archive, "sfc/26164")
    # Reassemble exactly the original frozen files; no model entry point is called.
    run = {
        "run_id": original["run_id"],
        "forecasts": {
            str(index): {"path": source["path"].removesuffix("/forecast.json")}
            for index, source in enumerate(original["forecast_sources"])
        },
    }
    batch, sources = _batch(archive, run)
    assert batch == original["forecast_batch"]
    assert sources == original["forecast_sources"]
    digest = hashlib.sha256(_json_bytes(batch)).hexdigest()
    assert digest == original["forecast_sha256"]
    original_snapshot = copy.deepcopy(issue.snapshot)
    mapped = refresh_snapshot(session, issue)
    changed = [row["seq"] for old, row in zip(original_snapshot, mapped, strict=True) if old != row]
    assert changed == list(EXPECTED)
    assert len(mapped) == 14
    for seq, match_id in EXPECTED.items():
        row = mapped[seq - 1]
        assert row["mapping"] == "matched" and row["match_id"] == match_id
        assert row["mapping_evidence"]["basis"] == "national_team_ids_and_exact_kickoff"
    assert [row["seq"] for row in mapped if row["mapping"] != "matched"] == [1, 14]
    write_json(output / "mapped-after.json", mapped)
    write_json(output / "rejected-identities.json", rejected_identities(session, issue, mapped))

    def render():
        return build_sfc_report(
            session,
            issue,
            batch,
            forecast_sha256=digest,
            generated=generated,
            max_lines=original["max_lines"],
            allow_missing=True,
        )

    # Remove only the newly reviewed entries responsible for the original gap.
    # Other full-name IDs stay approved: their historical success is not regressed.
    missing = copy.deepcopy(nations.sfc_aliases())
    for provider_id in MISSING_IDS:
        assert provider_id in missing[nations.LEAGUE]
        del missing[nations.LEAGUE][provider_id]
    with patch.object(nations, "sfc_aliases", return_value=missing):
        before = render()
        write_json(output / "mapped-with-missing-names.json", refresh_snapshot(session, issue))
    after = render()
    before_missing = {row["seq"]: row["reason"] for row in before["rows"] if not row["match_id"]}
    after_missing = {row["seq"]: row["reason"] for row in after["rows"] if not row["match_id"]}
    assert list(before_missing) == [1, 2, 6, 10, 14]
    assert list(after_missing) == [1, 14]
    for seq in EXPECTED:
        assert "队名对照表缺该队" in before_missing[seq]
    for reasons in (before_missing, after_missing):
        assert "友谊赛" in reasons[1] and "开球时间为占位" in reasons[1]
        assert "待场次桥接" in reasons[14] and "开售状态未核实" in reasons[14]
    predictions = {row["match_id"]: row for row in batch["predictions"]}
    for seq, match_id in EXPECTED.items():
        # Fixing an identity does not retroactively create a frozen prediction.
        assert match_id not in predictions
        row = after["rows"][seq - 1]
        assert row["probabilities"] is None
        assert row["reason"] == "无模型：该冻结批次没有此比赛的球队模型概率"
    for row in after["rows"]:
        if row["probabilities"] is not None:
            source = predictions[row["match_id"]]
            assert row["probabilities"] == source["model_probabilities"]
            assert row["model_version"] == source["model_version"]
        if row["seq"] not in EXPECTED:
            assert row["probabilities"] == original["rows"][row["seq"] - 1]["probabilities"]
    for label, report in (("before", before), ("after", after)):
        report["forecast_sources"] = sources
        write_sfc_report(report, output / label)
    assert before["missing_models"] == after["missing_models"] == original["missing_models"] == 5
    assert issue.snapshot == original_snapshot
    assert batch == original["forecast_batch"]
    assert not session.new and not session.dirty and not session.deleted
    return {
        "changed_mapping_rows": changed,
        "expected_matches": EXPECTED,
        "other_eleven_mapping_rows_exactly_unchanged": True,
        "before_unmapped_reasons": before_missing,
        "after_unmapped_reasons": after_missing,
        "mapped_before": 9,
        "mapped_after": 12,
        "missing_frozen_predictions_before": before["missing_models"],
        "missing_frozen_predictions_after": after["missing_models"],
        "newly_mapped_without_frozen_predictions": [
            {"seq": seq, "match_id": match_id, "reason": after["rows"][seq - 1]["reason"]}
            for seq, match_id in EXPECTED.items()
        ],
        "negative_cases_rejected": 9,
        "frozen_prediction_sources": [{"path": s["path"], "sha256": s["sha256"]} for s in sources],
        "forecast_batch_sha256": digest,
        "all_displayed_probabilities_equal_original_frozen_values": True,
        "historical_data_cutoff_utc": batch["as_of_utc"],
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
    actual = datetime.now(UTC)
    original = json.loads((archive / "sfc/26164/sfc.json").read_bytes())
    evaluation = datetime.fromisoformat(original["generated_utc"])
    try:
        # The restored DB is disposable and must never enter the evidence archive.
        with TemporaryDirectory(prefix="t58-sfc-acceptance-") as temporary:
            database = Path(temporary) / "restored.db"
            restored = restore_feeds(archive, database)
            database_before = sha(database)
            engine = get_engine(database)
            try:
                with engine.connect() as connection:
                    connection.exec_driver_sql("PRAGMA query_only=ON")
                    connection.exec_driver_sql("BEGIN")
                    with Session(bind=connection) as session:
                        issue = session.get(SfcIssue, "26164")
                        assert issue is not None
                        result = reports(session, issue, archive, output, evaluation)
            finally:
                engine.dispose()
                database_after = sha(database)
                write_json(
                    output / "restored-database-sha256.json",
                    {"before": database_before, "after": database_after},
                )
                assert database_before == database_after
    finally:
        after = inventory(archive)
        write_json(output / "archive-after-sha256.json", after)
        assert before == after
    result.update(
        archive_commit=provenance["archive_commit"],
        actual_execution_utc=actual.isoformat(),
        actual_execution_beijing=actual.astimezone(BEIJING).isoformat(),
        historical_evaluation_at_utc=evaluation.isoformat(),
        historical_evaluation_at_beijing=evaluation.astimezone(BEIJING).isoformat(),
        mode="offline_historical_report_replay",
        restored_counts=restored["counts"],
        archive_files_sha256_unchanged=len(before),
        frozen_26164_report_md_csv_sha256_unchanged={
            name: value
            for name, value in before.items()
            if name.startswith("sfc/26164/") and name.endswith(("report.md", "report.csv"))
        },
        restored_database_sha256_unchanged=database_before,
        source_code_sha256={
            name: sha(ROOT / name)
            for name in (
                "scripts/verify-t58-sfc-archive.py",
                "src/zucai/nations.py",
                "src/zucai/sources/sfc.py",
                "src/zucai/report/sfc.py",
                "data/team_aliases/sfc.json",
            )
        },
        model_recomputed=False,
        production_written=False,
        note="仅离线重放原时点映射与报告；实际执行时间单列，不冒充原时点已新增预测。",
    )
    write_json(output / "summary.json", result)
    LOG.info("T58 SFC offline acceptance passed: %s", json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.archive, args.output)
