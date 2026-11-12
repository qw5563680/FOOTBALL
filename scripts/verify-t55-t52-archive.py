"""Read-only T55 attached T52 digest acceptance on a fixed real feed snapshot.

Run with PYTHONPATH=src python scripts/verify-t55-t52-archive.py --archive ARCHIVE
--archive-commit SHA --as-of UTC --output NEW_DIR. No network or archive writes.
The counterpart transition itself is covered by tests/test_t52_sfc_bridge.py and
by the 26163 cropped-source regression in tests/test_t52_real_archive.py.
"""

import argparse
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from zucai.daily_digest import generate
from zucai.digest_views import _known_unstarted_lines

LOG = logging.getLogger(__name__)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(folder):
    return {
        path.relative_to(folder).as_posix(): sha(path)
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def verify(archive, output, *, archive_commit, as_of):
    archive, output = archive.resolve(), output.resolve()
    if output.is_relative_to(archive) or archive.is_relative_to(output):
        raise ValueError("验收输出目录必须与归档目录分离")
    if as_of.utcoffset() is None:
        raise ValueError("验收截点必须有时区")
    output.mkdir(parents=True, exist_ok=False)
    before = inventory(archive)
    write_json(output / "archive-before-sha256.json", before)
    source = "state/sfc/26163.json"
    issue = json.loads((archive / source).read_bytes())["issue"]
    assert len(issue["snapshot"]) == 14
    reminder = "\n".join(_known_unstarted_lines("2026-11-13", as_of, None, [], [(source, issue)]))
    assert "胜负彩 26163 期有 6 场未开球比赛未在本表展示" in reminder
    assert "已映射" not in reminder and "未映射" not in reminder
    assert "胜负彩专用冻结预测见下方 14 场胜负彩" in reminder
    (output / "26163-reminder.md").write_text(reminder)
    digest = generate(archive, output / "digest", now=as_of, run_id="t55-offline-acceptance")
    text = digest.read_text()
    assert "## 14 场胜负彩" in text and "期号：26163" in text
    # All reminders are free of stale counts even when forecasts show some rows.
    predictions = text.split("## 今日与明日预测", 1)[1].split("## 14 场胜负彩", 1)[0]
    assert "已映射" not in predictions and "未映射" not in predictions
    after = inventory(archive)
    write_json(output / "archive-after-sha256.json", after)
    assert before == after
    summary = {
        "archive_commit": archive_commit,
        "historical_as_of_utc": as_of.isoformat(),
        "actual_source": source,
        "source_sha256": sha(archive / source),
        "source_issue_rows": len(issue["snapshot"]),
        "nov13_unshown_reminder_rows": 6,
        "reminder_has_mapping_counts": False,
        "actual_digest_has_26163_section": True,
        "actual_digest_forecast_section_has_mapping_counts": False,
        "archive_files_unchanged": len(before),
        "production_written": False,
        "note": "只核验真实26163期提醒不再给出原始映射计数；不据此宣称概率或发布验收通过。",
    }
    write_json(output / "summary.json", summary)
    LOG.info("T55 attached T52 archive acceptance: %s", json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-commit", required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.archive, args.output, archive_commit=args.archive_commit, as_of=args.as_of)
