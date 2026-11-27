"""Replay the production outputs with T70 disabled, using only offline fixtures."""

import argparse
import csv
import json
import logging
import os
import re
import shutil
import socket
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import patch

import requests

from zucai import cloud_predict
from zucai.cloud_feeds import digest, export_feeds
from zucai.daily_digest import generate
from zucai.db import get_engine, init_db, session_scope
from zucai.forecast_publication import candidates
from zucai.models import Match, Odds, Result, Team
from zucai.scoreboard import command as scoreboard

LOG = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "data/fixtures"
TRAIN_AT = datetime(2026, 11, 2, 18, tzinfo=UTC)
SCORE_AT = datetime(2026, 11, 3, 20, tzinfo=UTC)
RUN_ID = "offline-t70-parity"
RESULTS = {
    "jc:t17-today": (2, 0, "2026-11-03T16:10:00+00:00"),
    "jc:t17-tomorrow": (1, 1, "2026-11-03T18:40:00+00:00"),
}


def _export(database, archive, through, run_id, at):
    export_feeds(database, archive, through.date(), run_id)
    path = archive / "feed_manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["created_utc"] = (at - timedelta(minutes=1)).isoformat()
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def seed(root):
    workspace = root / "seed"
    workspace.mkdir()
    params = json.loads((REPO / "data/model/params.json").read_bytes())
    params["leagues"] = {k: v for k, v in params["leagues"].items() if k in {"英超", "芬超"}}
    (workspace / "params.json").write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    with (FIXTURES / "t16/train_history.csv").open(encoding="utf-8", newline="") as source:
        history = list(csv.DictReader(source))
    upcoming = json.loads((FIXTURES / "t17/upcoming.json").read_bytes())

    database = workspace / "seed.db"
    engine = get_engine(database)
    try:
        init_db(engine)
        with session_scope(engine) as session:
            teams = {name: Team(name=name) for name in "ABCD"}
            session.add_all(teams.values())
            session.flush()
            for row in history + upcoming:
                match_id = row["match_id"]
                finished = "home_goals" in row
                session.add(
                    Match(
                        match_id=match_id,
                        source=row["source"],
                        league=row["league"],
                        home_team_id=teams[row["home"]].id,
                        away_team_id=teams[row["away"]].id,
                        kickoff_utc=row["kickoff_utc"],
                        status="finished" if finished else "scheduled",
                    )
                )
                session.flush()
                if finished:
                    session.add(
                        Result(
                            match_id=match_id,
                            home_goals=int(row["home_goals"]),
                            away_goals=int(row["away_goals"]),
                            available_utc=row["available_utc"],
                            available_source=row["available_source"],
                        )
                    )
                session.add_all(
                    Odds(
                        match_id=match_id,
                        pool="had",
                        line=0,
                        selection=selection,
                        price=price,
                        snapshot_utc=TRAIN_AT.isoformat(),
                    )
                    for selection, price in zip("hda", row.get("had", ()), strict=False)
                )
        _export(database, workspace / "archive", TRAIN_AT, "seed", TRAIN_AT)
        with session_scope(engine) as session:
            for match_id, (home, away, observed) in RESULTS.items():
                match = session.get(Match, match_id)
                match.status = "finished"
                match.result_checked_utc = observed
                match.result_provider = "jc"
                session.add(
                    Result(
                        match_id=match_id,
                        home_goals=home,
                        away_goals=away,
                        available_utc=observed,
                        available_source="captured",
                    )
                )
        _export(database, root / "settled-feed", SCORE_AT, "parity-settled", SCORE_AT)
    finally:
        engine.dispose()


def _confirm_synthetic_publication(archive):
    path = archive / "forecast_manifest.json"
    manifest = json.loads(path.read_bytes())
    stamp = (TRAIN_AT + timedelta(minutes=1)).isoformat()
    for entry, name, prefix in candidates(manifest):
        entry[prefix + "published_utc"] = stamp
        entry[prefix + "publication_evidence"] = dict(
            method="post_push_confirmation",
            candidate_path=name,
            candidate_sha256=manifest["files"][name],
            published_utc=stamp,
            commit="0" * 40,
            ref="refs/remotes/offline-fixture/data/football-feeds",
        )
    path.write_text(json.dumps(manifest), encoding="utf-8")


@contextmanager
def _fixed_stage(root, *, prefix):
    # Model provenance includes the absolute path of the candidate's parameters.
    stage = root / "temporary-forecast"
    stage.mkdir()
    try:
        yield str(stage)
    finally:
        shutil.rmtree(stage)


class _ScoreClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return SCORE_AT.replace(tzinfo=None) if tz is None else SCORE_AT.astimezone(tz)


def replay(root, name):
    out = root / name
    out.mkdir()
    archive = out / "archive"
    shutil.copytree(root / "seed/archive", archive)
    os.environ.update(
        GITHUB_STEP_SUMMARY=str(out / "actions.md"),
        GITHUB_EVENT_NAME="schedule",
        ZUCAI_OPERATION="daily",
    )
    with (
        patch.object(cloud_predict, "_now", return_value=TRAIN_AT),
        patch.object(cloud_predict, "TemporaryDirectory", partial(_fixed_stage, root)),
    ):
        trained = cloud_predict.run(
            archive, root / "seed/params.json", RUN_ID, sporttery_status="skipped"
        )
    _confirm_synthetic_publication(archive)
    shutil.copytree(root / "settled-feed", archive, dirs_exist_ok=True)
    with patch.object(scoreboard, "datetime", _ScoreClock):
        board = scoreboard.run(archive, as_of=SCORE_AT)
    daily = generate(archive, archive / "daily", now=SCORE_AT, run_id=RUN_ID)
    rows = trained["leagues"]["英超"]["training_rows"]
    predicted = sum(entry["predicted"] for entry in trained["forecasts"].values())
    scored = board["probability"]["total"]["n"]
    if (rows, predicted, scored) != (84, 2, 2):
        raise ValueError(f"回放样本数量与预期不符：{rows} / {predicted} / {scored}")
    if "联网研究线" in daily.read_text(encoding="utf-8") or any(
        path.relative_to(archive)
        .as_posix()
        .startswith(("scoreboard/web_research", "scoreboard/web-research"))
        or "web_research" in path.relative_to(archive).parts
        for path in archive.rglob("*")
    ):
        raise ValueError("关闭研究线的回放产生了研究产物")

    evidence = {
        p.relative_to(archive).as_posix(): digest(p)
        for p in sorted(archive.rglob("*"))
        if p.is_file()
    }
    (out / "hashes.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    LOG.info("代码入口 %s", cloud_predict.__file__)
    LOG.info("训练/预测 UTC %s，评分/日报 UTC %s", TRAIN_AT, SCORE_AT)
    LOG.info(
        "英超训练 %s 行，预测 %s 场，计分 %s 场，共 %s 个文件",
        rows,
        predicted,
        scored,
        len(evidence),
    )


def _block_network(*args, **kwargs):
    raise AssertionError("离线回放禁止联网")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="已存在的独立临时目录")
    tasks = parser.add_subparsers(dest="task", required=True)
    tasks.add_parser("seed", help="准备两次回放共用的赛前和赛后归档")
    replay_parser = tasks.add_parser("replay", help="回放当前 PYTHONPATH 指向的源码")
    replay_parser.add_argument("--name", required=True, help="根目录下新建的回放目录名")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir() or root.is_relative_to(REPO):
        parser.error("临时目录必须已存在且位于代码仓库外")
    if args.task == "seed" and any((root / name).exists() for name in ("seed", "settled-feed")):
        parser.error("回放目录已有输入归档，请用新目录准备样本")
    if args.task == "replay" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.name):
        parser.error("回放目录名只能包含字母、数字、点、下划线或连字符")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    LOG.setLevel(logging.INFO)
    with (
        patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}),
        patch.object(requests.sessions.Session, "request", _block_network),
        patch.object(socket, "create_connection", _block_network),
        patch.object(socket, "getaddrinfo", _block_network),
        patch.object(socket.socket, "connect", _block_network),
        patch.object(socket.socket, "connect_ex", _block_network),
    ):
        for variable in ("DEEPSEEK_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_REPOSITORY"):
            os.environ.pop(variable, None)
        if args.task == "seed":
            seed(root)
        else:
            replay(root, args.name)


if __name__ == "__main__":
    main()
