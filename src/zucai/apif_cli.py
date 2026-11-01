"""Additive CLI registration, leaving the T09 parser and dispatcher functions intact."""

import argparse
import logging
import sys
from functools import wraps


def _subparsers(parser):
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def extend_cli(original_parser, original_main):
    @wraps(original_parser)
    def build_parser():
        from zucai.sources.results import assumed_delay

        parser = original_parser()
        feeds = _subparsers(_subparsers(parser).choices["fetch"])
        apif = feeds.add_parser("apif", help="API-Football 历史联赛赛程与常规时间赛果")
        apif.add_argument("--league", type=int, dest="league_id")
        span = apif.add_mutually_exclusive_group(required=True)
        span.add_argument("--season", type=int, action="append", dest="seasons")
        span.add_argument("--date", help="UTC 日期 YYYY-MM-DD，全部已配置联赛")
        apif.add_argument("--assume-known-after", type=assumed_delay, dest="assume")
        apif.add_argument("--reserve", type=int, default=10)
        apif.add_argument("--quota-file", dest="quota_path", default="data/api_football_quota.json")
        apif.add_argument("--db")
        apif.add_argument("--aliases-file", dest="aliases_path")
        apif.add_argument("--leagues-file", dest="leagues_path")
        apif.add_argument("--raw-file")
        apif.add_argument("--raw-root", default="data/raw")
        apif.add_argument("--audit-dir", default="data")
        return parser

    @wraps(original_main)
    def main(argv=None):
        from sqlalchemy.exc import SQLAlchemyError

        values = sys.argv[1:] if argv is None else argv
        args = build_parser().parse_args(values)
        if args.command != "fetch" or args.feed != "apif":
            return original_main(values)
        # requests/urllib3 debug logs contain raw headers. Never enable their wire logging.
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        try:
            from zucai.sources.apif_command import fetch_apif

            fetch_apif(
                **{k: v for k, v in vars(args).items() if k not in {"command", "feed", "verbose"}}
            )
        except (ValueError, OSError, TypeError, KeyError, SQLAlchemyError) as exc:
            logging.getLogger(__name__).error("APIF 采集未完成：%s", exc)
            return 1
        return 0

    return build_parser, main
