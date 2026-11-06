"""T31 command boundary: offline replays, or one explicitly requested Actions index page."""

import logging
import os
from pathlib import Path

LOG = logging.getLogger(__name__)


def add_stats_parser(sub):
    parser = sub.add_parser("stats", help="技术数据解析、存储与身份索引，不生成模型特征")
    commands = parser.add_subparsers(dest="stats_command", required=True)
    restore = commands.add_parser("restore-rows", help="从 Release 分片恢复全部技术数据历史行")
    restore.add_argument(
        "--db", type=Path, default=Path(os.environ.get("ZUCAI_DB", "data/zucai.db"))
    )
    restore.add_argument("--archive", type=Path, help="feeds 归档目录；默认读取数据库旁持久清单")
    restore.add_argument("--release", help="仅恢复指定 stats-raw-YYYYMM 月份；默认恢复全部")
    restore.add_argument("--repository", help="GitHub owner/repo；默认 GITHUB_REPOSITORY")
    replay = commands.add_parser("replay-fd", help="按哈希清单离线重放已有 fd CSV，不请求网络")
    replay.add_argument("--db", type=Path, required=True)
    replay.add_argument(
        "--plan", type=Path, required=True, help="原件/sidecar哈希和真实捕获时间清单"
    )
    replay.add_argument("--leagues-file", type=Path)
    replay.add_argument("--aliases-file", type=Path)
    for command in ("migrate", "ingest-fd", "link", "ingest-apif", "ingest-injuries"):
        item = commands.add_parser(command)
        item.add_argument(
            "--db", type=Path, default=Path(os.environ.get("ZUCAI_DB", "data/zucai.db"))
        )
        if command == "migrate":
            continue
        item.add_argument("--raw-location", help="长期原件归档位置；不改变或上传原件")
        if command == "link":
            source = item.add_mutually_exclusive_group(required=True)
            source.add_argument("--raw-file", type=Path, help="已归档的 fixtures 原件（离线）")
            source.add_argument(
                "--fetch", action="store_true", help="仅 Actions 手动：请求一页索引原件"
            )
            item.add_argument("--league", type=int, required=True)
            item.add_argument("--season", type=int, required=True)
            item.add_argument("--aliases-file", type=Path)
        else:
            item.add_argument("--raw-file", type=Path, required=True)
            if command == "ingest-fd":
                item.add_argument("--div", required=True)
                item.add_argument("--season", required=True)
                item.add_argument("--leagues-file", type=Path)
                item.add_argument("--aliases-file", type=Path)
            elif command == "ingest-injuries":
                item.add_argument("--league", type=int, required=True)
                item.add_argument("--season", type=int, required=True)
                item.add_argument(
                    "--daily", action="store_true", help="已归档的日常原件；历史季页不能使用"
                )
            else:
                item.add_argument(
                    "--endpoint",
                    choices=("statistics", "events", "lineups", "players"),
                    required=True,
                )
                item.add_argument(
                    "--fixture-file",
                    type=Path,
                    required=True,
                    help="含 fixture 状态/主客 ID 的已归档 fixtures 页",
                )


def fetch_index(database, *, league_id, season):
    from zucai.cloud_probe import require_probe_actions
    from zucai.sources.api_football import ApiFootball
    from zucai.sources.base import RawStore

    require_probe_actions()
    ApiFootball.index_scope(league_id=league_id, season=season)
    if not Path(database).is_file():
        raise ValueError("索引需要已有数据库")
    client = ApiFootball(
        store=RawStore(Path(database).parent / "raw"),
        quota_path=Path(database).parent / "state/api_football_quota.json",
    )
    try:
        return client.fetch_index_raw(league_id=league_id, season=season)
    finally:
        client.session.close()


def run_stats(args):
    from zucai.db import get_engine
    from zucai.sources.football_data import CONFIG_ROOT
    from zucai.stats_schema import migrate_stats
    from zucai.stats_storage import ingest_apif, ingest_fd, link_archive

    if args.stats_command == "replay-fd":
        from zucai.fd_stats_replay import replay_fd

        result = replay_fd(
            args.db, args.plan, leagues_path=args.leagues_file, aliases_path=args.aliases_file
        )
    elif args.stats_command == "restore-rows":
        from zucai.stats_release import ReleaseStore
        from zucai.stats_rows import restore_rows

        result = restore_rows(
            args.db, args.archive, release=ReleaseStore(args.repository), tag=args.release
        )
    elif args.stats_command == "migrate":
        if not args.db.is_file():
            raise ValueError("技术数据迁移需要已有数据库")
        engine = get_engine(args.db)
        try:
            migrate_stats(engine)
        finally:
            engine.dispose()
        result = {"tables": 5}
    elif args.stats_command == "ingest-injuries":
        from zucai.injuries_storage import ingest_injuries

        result = ingest_injuries(
            args.db,
            args.raw_file,
            league_id=args.league,
            season=args.season,
            raw_location=args.raw_location,
            daily=args.daily,
        )
    elif args.stats_command == "ingest-fd":
        result = ingest_fd(
            args.db,
            args.raw_file,
            div=args.div,
            season=args.season,
            leagues_path=args.leagues_file,
            aliases_path=args.aliases_file,
            raw_location=args.raw_location,
        )
    elif args.stats_command == "link":
        path = (
            fetch_index(args.db, league_id=args.league, season=args.season)
            if args.fetch
            else args.raw_file
        )
        result = link_archive(
            args.db,
            path,
            league_id=args.league,
            season=args.season,
            aliases_path=args.aliases_file or CONFIG_ROOT / "team_aliases/api_football.json",
            raw_location=args.raw_location,
        )
    else:
        result = ingest_apif(
            args.db,
            args.raw_file,
            fixture_file=args.fixture_file,
            endpoint=args.endpoint,
            raw_location=args.raw_location,
        )
    LOG.info("技术数据 %s：%s", args.stats_command, result)
    return result
