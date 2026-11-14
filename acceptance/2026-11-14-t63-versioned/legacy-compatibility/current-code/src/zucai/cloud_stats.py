"""Actions entry point for isolated technical-data backfill and daily observations."""

import argparse
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from zucai.cloud_apis import failure_reason
from zucai.cloud_feeds import restore_feeds
from zucai.stats_batch import collect_batch
from zucai.stats_release import ReleaseStore, validate_name
from zucai.stats_schedule import plan

LOG = logging.getLogger(__name__)


def require_actions(mode):
    event = os.environ.get("GITHUB_EVENT_NAME")
    if os.environ.get("GITHUB_ACTIONS") != "true" or event not in (
        {"workflow_dispatch"} if mode == "backfill" else {"workflow_dispatch", "schedule"}
    ):
        raise ValueError("技术采集仅允许 Actions；历史回填只允许 workflow_dispatch")


def write_summary(report):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    with Path(path).open("a", encoding="utf-8") as output:
        output.write("\n## 技术数据采集\n\n")
        output.write(
            f"状态：{cell(report['status'])}；API 请求 {report['requests']}；"
            f"fd 文件 {report.get('downloaded_files', 0)}；新增行 {report.get('inserted', 0)}；"
            f"剩余配额 {report.get('remaining_quota', '未知')}。\n\n"
        )
        output.write(
            "| 赛事 | 季 | 端点 | 范围 | 应取 | 已取 | 有效 | 空值 | 失败 | 未支持 | 拒绝行 |\n"
        )
        output.write("|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|\n")
        for row in report.get("coverage", {}).get("rows", []):
            fields = (
                "league",
                "season",
                "endpoint",
                "phase",
                "expected",
                "fetched",
                "valid",
                "null",
                "failed",
                "unsupported",
            )
            output.write(
                "| "
                + " | ".join(cell(row[key]) for key in fields)
                + f" | {row.get('rejected_rows', 0)} |\n"
            )
        output.write(
            "\n索引按页、fd 按文件、逐场端点按已链接比赛计数；拒绝行单列，空字段保留 null。\n"
        )
        injuries = report.get("coverage", {}).get("injuries")
        if injuries is not None:
            output.write(
                "\n伤停存储：captured "
                + cell(injuries["captured_rows"])
                + "；backfilled "
                + cell(injuries["backfilled_rows"])
                + "；已关联 / 总行数 "
                + cell(injuries["linked_rows"])
                + " / "
                + cell(injuries["total_rows"])
                + "；关联率 "
                + cell(injuries["linkage_rate"])
                + "（空集为缺失；不进入正式预测）。\n"
            )
        for error in report.get("errors", []):
            output.write("\n- " + cell(error) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backfill", "daily"))
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/zucai.db"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--leagues")
    parser.add_argument("--seasons")
    parser.add_argument("--endpoints")
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)
    release = None
    try:
        require_actions(args.command)
        validate_name(args.run_id)
        maximum = (
            args.max_requests
            if args.max_requests is not None
            else (300 if args.command == "daily" else 7000)
        )
        if not 1 <= maximum <= 7000:
            raise ValueError("max_requests 必须为 1–7000")
        now = datetime.now(UTC)
        endpoints = args.endpoints or (
            "statistics,events,lineups,players,injuries" if args.command == "daily" else None
        )
        selection = plan(args.leagues, args.seasons, endpoints, now)
        if args.command == "daily" and "index" in selection["endpoints"]:
            raise ValueError("daily 不额外请求索引页；fd 只复用本次已下载文件")
        if not args.db.exists():
            restore_feeds(args.archive, args.db)
        release = ReleaseStore()
        report = collect_batch(
            args.db,
            args.archive,
            selection,
            release=release,
            run_id=args.run_id,
            now=now,
            daily=args.command == "daily",
            resume=args.resume,
            max_requests=maximum,
        )
        write_summary(report)
        LOG.info(
            "技术数据 %s：请求 %s，新增 %s",
            report["status"],
            report["requests"],
            report["inserted"],
        )
        return int(report.get("archive_failed", False))
    except (ValueError, OSError) as exc:
        LOG.error("技术数据启动或归档失败：%s", failure_reason(exc))
        return 1
    finally:
        if release is not None:
            release.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
