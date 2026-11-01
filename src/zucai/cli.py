"""命令行入口。子命令在各任务单中逐步实现。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zucai", description="中国足彩比赛分析与预测")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("version", help="显示版本")
    db = sub.add_parser("db", help="数据库管理")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    init = db_sub.add_parser("init", help="创建缺失的数据库表")
    init.add_argument("--path", help="SQLite 文件路径，默认读取 ZUCAI_DB 或 data/zucai.db")

    fetch = sub.add_parser("fetch", help="采集赛程、赔率及赛果")
    feeds = fetch.add_subparsers(dest="feed", required=True)
    jc = feeds.add_parser("jc", help="采集在售竞彩赛程和固定奖金")
    jc.add_argument("--date", type=date.fromisoformat, help="北京时间竞彩编号日期 YYYY-MM-DD")

    # 以下子命令由 docs/tasks 中的任务逐个实现：
    # fetch jc / fetch results / fetch sfc   (T02, T03)
    # features build                          (T04)
    # train                                   (T05)
    # predict / report                        (T06)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "version":
        from zucai import __version__

        print(__version__)
        return 0
    if args.command == "fetch" and args.feed == "jc":
        from requests import RequestException

        from zucai.sources.sporttery import fetch_schedule

        try:
            fetch_schedule(args.date)
        except (RequestException, ValueError, KeyError) as exc:
            logging.getLogger(__name__).error("赛程采集失败：%s", exc)
            return 1
        return 0
    if args.command == "db" and args.db_command == "init":
        from zucai.db import get_engine, init_db

        engine = get_engine(args.path)
        try:
            init_db(engine)
            logging.getLogger(__name__).info("数据库就绪 %s", engine.url.database)
        finally:
            engine.dispose()
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
