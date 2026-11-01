"""命令行入口。子命令在各任务单中逐步实现。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta


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
    sfc = feeds.add_parser("sfc", help="采集当前或指定14场胜负彩期次")
    sfc.add_argument("--issue", help="5位期号，不填写取来源当前期")
    sfc.add_argument("--db", help="数据库路径，默认 ZUCAI_DB 或 data/zucai.db")
    sfc.add_argument("--matches-out", help="另存可直接输入T06的CSV清单，不覆盖已有文件")
    from zucai.sources.results import assumed_delay

    fd = feeds.add_parser("fd", help="导入已核实时区的football-data.co.uk历史赛果")
    fd_kind = fd.add_mutually_exclusive_group(required=True)
    fd_kind.add_argument("--div", help="按季联赛代码，例如E0")
    fd_kind.add_argument("--country", help="合并文件国家代码，例如JPN")
    fd.add_argument("--season", action="append", help="按季文件四位赛季，可重复")
    fd.add_argument("--from-season", type=int, help="合并文件起始年份，含当年")
    fd.add_argument("--to-season", type=int, help="合并文件结束年份，含当年")
    fd.add_argument("--assume-known-after", type=assumed_delay, required=True, metavar="3h")
    fd.add_argument("--db", help="目标SQLite文件")
    fd.add_argument("--leagues-file", help="已验证时区配置JSON")
    fd.add_argument("--aliases-file", help="有状态的球队别名JSON")
    fd.add_argument("--raw-file", help="回放已有CSV及同名HTTP/SHA256 sidecar，不联网")
    fd.add_argument("--raw-root", default="data/raw", help="原始文件存放根目录")
    fd.add_argument("--audit-dir", default="data", help="拒绝/可疑行审计目录")

    fdorg = feeds.add_parser("fdorg", help="采集 football-data.org 的整轮赛程和常规时间赛果")
    fdorg.add_argument("--competition", required=True, help="联赛代码，以逗号分隔，例如 PL,PD,SA")
    window = fdorg.add_mutually_exclusive_group(required=True)
    window.add_argument("--season", type=int, action="append", help="赛季开始年份，可重复")
    window.add_argument(
        "--days", type=int, help="UTC 今天前后各 N 个自然日，含两端；按不超过10天分窗"
    )
    fdorg.add_argument(
        "--assume-known-after",
        type=assumed_delay,
        metavar="3h",
        help="赛季回填必须显式指定；增量只用真实采集时刻",
    )
    fdorg.add_argument("--finished-only", action="store_true", help="只请求指定赛季的 FINISHED")
    fdorg.add_argument("--db", help="目标 SQLite 文件")
    fdorg.add_argument("--aliases-file", help="按联赛和本源数字球队 ID 验证的别名 JSON")
    fdorg.add_argument(
        "--raw-file", help="离线重放一份匹配赛季或分窗、带 HTTP/SHA256 sidecar 的原件"
    )
    fdorg.add_argument("--raw-root", default="data/raw", help="原始快照根目录")
    fdorg.add_argument("--audit-dir", default="data", help="拒绝和疑似重复行的 CSV 目录")

    results = feeds.add_parser("results", help="回填已开奖场次的常规时间及半场比分")
    span = results.add_mutually_exclusive_group()
    span.add_argument("--days", type=int, help="最近N个北京时间自然日，含今天（默认30）")
    span.add_argument("--from", dest="start", type=date.fromisoformat, help="开始日期，含当日")
    results.add_argument("--to", dest="end", type=date.fromisoformat, help="结束日期，含当日")
    results.add_argument(
        "--source",
        choices=("jc", "c500"),
        default="jc",
        help="jc官方历史赛果；c500仅根页近期备用，日期超出覆盖范围报错",
    )
    results.add_argument(
        "--assume-known-after", type=assumed_delay, metavar="3h", help="显式假设历史比分可知时间"
    )

    train = sub.add_parser("train", help="训练常规时间比分模型，并按周回测")
    train.add_argument("--model", choices=["dixon-coles"], default="dixon-coles")
    train.add_argument("--db", help="SQLite 文件，默认读取 ZUCAI_DB 或 data/zucai.db")
    train.add_argument("--league", help="联赛，库内有多个联赛时指定")
    train.add_argument("--source", help="数据来源或逗号分隔集合，例如jc,fd；仍须单一联赛")
    train.add_argument("--since", help="回测起始日（含），之前的数据仍用于训练")
    train.add_argument("--as-of", help="数据可知截点，日期按 UTC 零时解释，默认当前时刻")
    train.add_argument(
        "--half-life-days", type=float, default=90, help="时间衰减半衰期，默认 90 天"
    )
    train.add_argument("--output", default="models/dixon-coles.pkl", help="模型输出文件")
    availability = train.add_mutually_exclusive_group()
    availability.add_argument(
        "--strict-availability",
        action="store_true",
        help="排除 assumed 赛果训练，并同时报告默认与严格回测",
    )
    availability.add_argument(
        "--replay-known-after",
        type=assumed_delay,
        metavar="3h",
        help="仅在内存中重放迟到赛果的可知时间（开发阶段历史回放）",
    )

    predict = sub.add_parser("predict", help="运行纯模型批量预测并冻结报告，P2实验")
    predict.add_argument("--model", choices=["dixon-coles"], default="dixon-coles")
    predict.add_argument("--db", help="数据库路径，默认 ZUCAI_DB 或 data/zucai.db")
    predict.add_argument("--model-file", action="append", help="可信模型文件，可按联赛重复指定")
    selection = predict.add_mutually_exclusive_group()
    selection.add_argument("--date", type=date.fromisoformat, help="北京时间开球自然日")
    selection.add_argument("--match-id", action="append", help="已核实比赛ID，可重复指定")
    selection.add_argument("--matches", help="单列 match_id 的CSV比赛清单")
    predict.add_argument("--as-of", help="仅历史回放使用，必须是带时区的ISO时间")
    predict.add_argument("--replay", action="store_true", help="显式标记历史回放")
    predict.add_argument(
        "--market-weight", type=float, default=0, help="独立实验混合权重，默认关闭"
    )
    predict.add_argument("--out", help="新的报告目录，不覆盖现有批次")
    report = sub.add_parser("report", help="从冻结JSON校验并重新导出报告")
    report.add_argument("report_kind", nargs="?", choices=["sfc"], help="14场胜负彩期次分析")
    report.add_argument("--batch", required=True, help="冻结 forecast.json 路径")
    report.add_argument("--out", required=True, help="新的导出目录")
    report.add_argument("--db", help="sfc：期次数据库")
    report.add_argument("--issue", help="sfc：期号，多期并存时必须明确指定")
    report.add_argument("--max-lines", type=int, default=64, help="sfc：复式总注数上限，默认64")

    features = sub.add_parser("features", help="提取严格赛前球队特征，市场列独立")
    feature_commands = features.add_subparsers(dest="feature_command", required=True)
    feature_build = feature_commands.add_parser("build", help="只读数据库并冻结Parquet特征表")
    feature_build.add_argument("--db", help="已有数据库路径，默认 ZUCAI_DB 或 data/zucai.db")
    feature_build.add_argument(
        "--match-id", action="append", help="目标比赛ID，可重复指定；默认全库"
    )
    feature_build.add_argument("--as-of", help="带时区的ISO可知截点，默认提取时刻")
    feature_build.add_argument(
        "--strict-availability", action="store_true", help="排除assumed可知时间"
    )
    feature_build.add_argument(
        "--out", default="data/features.parquet", help="新Parquet文件，拒绝覆盖"
    )

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
    if args.command == "features":
        from sqlalchemy.exc import SQLAlchemyError

        try:
            from zucai.features.command import run_features

            run_features(args)
        except ImportError:
            logging.getLogger(__name__).error(
                '请安装Parquet依赖：python -m pip install -e ".[features]"'
            )
            return 2
        except (ValueError, OSError, TypeError, SQLAlchemyError) as exc:
            logging.getLogger(__name__).error("特征提取或导出未完成：%s", exc)
            return 2
        return 0
    if args.command in {"predict", "report"}:
        from pickle import UnpicklingError

        from sqlalchemy.exc import SQLAlchemyError

        try:
            from zucai.report.command import run_predict, run_report

            if args.command == "predict":
                run_predict(args)
            elif args.report_kind == "sfc":
                from zucai.report.sfc import run_sfc_report

                run_sfc_report(args)
            else:
                run_report(args)
        except ModuleNotFoundError as exc:
            if exc.name != "penaltyblog":
                raise
            logging.getLogger(__name__).error('请安装模型依赖 python -m pip install -e ".[model]"')
            return 2
        except (ValueError, OSError, SQLAlchemyError, TypeError, EOFError, UnpicklingError) as exc:
            logging.getLogger(__name__).error("预测或导出未完成：%s", exc)
            return 2
        return 0
    if args.command == "fetch":
        from requests import RequestException
        from sqlalchemy.exc import SQLAlchemyError

        from zucai.sources.results import fetch_results
        from zucai.sources.sporttery import BEIJING, fetch_schedule

        try:
            if args.feed == "jc":
                fetch_schedule(args.date)
            elif args.feed == "sfc":
                from zucai.sources.sfc import fetch_sfc

                fetch_sfc(args.issue, db=args.db, matches_out=args.matches_out)
            elif args.feed == "fdorg":
                from zucai.sources.fdorg_command import fetch_fdorg

                fetch_fdorg(
                    competition=args.competition,
                    seasons=args.season,
                    days=args.days,
                    assume=args.assume_known_after,
                    finished_only=args.finished_only,
                    aliases_path=args.aliases_file,
                    db=args.db,
                    raw_file=args.raw_file,
                    raw_root=args.raw_root,
                    audit_dir=args.audit_dir,
                )
            elif args.feed == "fd":
                from zucai.sources.fd_ingest import fetch_fd
                from zucai.sources.football_data import ALIASES_PATH, LEAGUES_PATH

                fetch_fd(
                    div=args.div,
                    seasons=args.season,
                    country=args.country,
                    from_season=args.from_season,
                    to_season=args.to_season,
                    assume=args.assume_known_after,
                    db=args.db,
                    leagues_path=args.leagues_file or LEAGUES_PATH,
                    aliases_path=args.aliases_file or ALIASES_PATH,
                    raw_file=args.raw_file,
                    raw_root=args.raw_root,
                    audit_dir=args.audit_dir,
                )
            else:
                today = datetime.now(BEIJING).date()
                if args.start is not None:
                    if args.end is None:
                        raise ValueError("--from 与 --to 须同时给出")
                    start, end = args.start, args.end
                else:
                    days = 30 if args.days is None else args.days
                    if days < 1 or args.end is not None:
                        raise ValueError("请使用 --days N 或 --from 日期 --to 日期")
                    start, end = today - timedelta(days=days - 1), today
                fetch_results(start, end, args.source, args.assume_known_after)
        except (RequestException, ValueError, KeyError, TypeError, OSError, SQLAlchemyError) as exc:
            logging.getLogger(__name__).error("采集未完成：%s", exc)
            return 1
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
                strict_availability=args.strict_availability,
                replay_known_after=args.replay_known_after,
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


# T10 additive registration; original T09 functions remain unchanged.
from zucai.apif_cli import extend_cli  # noqa: E402

build_parser, main = extend_cli(build_parser, main)

if __name__ == "__main__":
    sys.exit(main())
