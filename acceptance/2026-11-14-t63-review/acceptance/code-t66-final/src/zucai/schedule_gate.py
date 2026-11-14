"""Decide the extra 15:40 Beijing run using only local SFC schedule headers.

This module deliberately uses only the standard library: Actions runs it before
installing the project or allowing any sports source requests.
"""

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ScheduleDecision:
    run_early: bool
    scope: str
    reason: str


def _deadline(path: Path) -> datetime:
    if not re.fullmatch(r"[0-9]{5}\.json", path.name) or path.is_symlink():
        raise ValueError("invalid issue filename")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload.get("issue"), dict)
        or payload["issue"].get("issue_no") != path.stem
    ):
        raise ValueError("invalid issue schedule header")
    value = payload["issue"].get("sale_end_utc")
    if not isinstance(value, str):
        raise ValueError("missing deadline")
    deadline = datetime.fromisoformat(value)
    if deadline.utcoffset() is None:
        raise ValueError("deadline requires timezone")
    return deadline


def decide_early_run(
    archive: Path, *, now: datetime | None = None, archive_available: bool = True
) -> ScheduleDecision:
    """Skip only when every archived deadline is known and outside today/tomorrow."""
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("now requires timezone")
    if not archive_available:
        return ScheduleDecision(True, "unknown", "期次归档读取失败，保守执行")
    today = now.astimezone(BEIJING).date()
    tomorrow = today + timedelta(days=1)
    try:
        paths = sorted((Path(archive) / "state/sfc").iterdir())
    except OSError:
        return ScheduleDecision(True, "unknown", "期次范围缺失或无法读取，保守执行")
    if not paths:
        return ScheduleDecision(True, "unknown", "期次范围为空，保守执行")
    matched, unknown = [], []
    for path in paths:
        try:
            deadline_day = _deadline(path).astimezone(BEIJING).date()
        except (OSError, UnicodeError, ValueError, OverflowError):
            unknown.append(path.name)
            continue
        if today <= deadline_day <= tomorrow:
            matched.append(path.stem)
    if matched:
        return ScheduleDecision(True, "present", "当日或次日停售期次：" + "、".join(matched))
    if unknown:
        return ScheduleDecision(True, "unknown", "部分期次停售时间无法判定，保守执行")
    return ScheduleDecision(False, "absent", "归档期次均不在当日或次日停售，跳过提前班次")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--now", type=datetime.fromisoformat)
    parser.add_argument(
        "--archive-status",
        choices=("success", "failure", "cancelled", "skipped"),
        default="success",
    )
    parser.add_argument("--github-output", type=Path, default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--summary", type=Path, default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args(argv)
    now = args.now or datetime.now(UTC)
    decision = decide_early_run(
        args.archive, now=now, archive_available=args.archive_status == "success"
    )
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    message = (
        f"提前班次检查（北京时间 {now.astimezone(BEIJING):%Y-%m-%d %H:%M}）：{decision.reason}"
    )
    LOG.info(message)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"run_early={str(decision.run_early).lower()}\nscope={decision.scope}\n")
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write(message + "\n")


if __name__ == "__main__":
    main()
