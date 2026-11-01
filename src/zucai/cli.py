"""命令行入口。子命令在各任务单中逐步实现。"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zucai", description="中国足彩比赛分析与预测")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("version", help="显示版本")
    db = sub.add_parser("db", help="数据库管理")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    init = db_sub.add_parser("init", help="创建缺失的数据库表")
    init.add_argument("--path", help="SQLite 文件路径，默认读取 ZUCAI_DB 或 data/zucai.db")

    train = sub.add_parser("train", help="训练常规时间比分模型，并按周回测")
    train.add_argument("--model", choices=["dixon-coles"], default="dixon-coles")
    train.add_argument("--db", help="SQLite 文件，默认读取 ZUCAI_DB 或 data/zucai.db")
    train.add_argument("--league", help="联赛，库内有多个联赛时指定")
    train.add_argument("--source", help="数据来源，避免同场比赛被重复统计")
    train.add_argument("--since", help="回测起始日（含），之前的数据仍用于训练")
    train.add_argument("--as-of", help="数据可知截点，日期按 UTC 零时解释，默认当前时刻")
    train.add_argument(
        "--half-life-days", type=float, default=90, help="时间衰减半衰期，默认 90 天"
    )
    train.add_argument("--output", default="models/dixon-coles.pkl", help="模型输出文件")

    # 以下子命令由 docs/tasks 中的任务逐个实现：
    # fetch jc / fetch results / fetch sfc   (T02, T03)
    # features build                          (T04)
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
    if args.command == "train":
        try:
            from zucai.predict.command import parse_cutoff, train

            result = train(
                db=args.db,
                league=args.league,
                source=args.source,
                since=parse_cutoff(args.since) if args.since else None,
                as_of=parse_cutoff(args.as_of) if args.as_of else None,
                output=args.output,
                half_life_days=args.half_life_days,
            )
        except ModuleNotFoundError as exc:
            if exc.name != "penaltyblog":
                raise
            logging.getLogger(__name__).error('请先运行 python -m pip install -e ".[model]"')
            return 2
        except ValueError as exc:
            logging.getLogger(__name__).error("%s", exc)
            return 2
        print(json.dumps(result, ensure_ascii=False))
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
