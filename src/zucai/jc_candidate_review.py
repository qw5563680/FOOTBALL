"""Offline, explicit JC candidate review; never merge identities or alter matches.

The operator supplies exact names and a conclusion. Each new plan decision pins
its candidate, teams and matches. Older plans still pin the complete inventory.
"""

import argparse
import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from zucai.scoreboard.score import instant
from zucai.sources.base import _atomic_write
from zucai.sources.teams import normalize_team

QUEUE = "state/alias_candidates/jc.json"
LOG = logging.getLogger(__name__)


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _snapshot(database, connection):
    connection.row_factory = sqlite3.Row
    teams = [
        dict(row) for row in connection.execute("SELECT id,name,aliases FROM teams ORDER BY id")
    ]
    for team in teams:
        team["aliases"] = json.loads(team["aliases"] or "[]")
    matches = [
        dict(row)
        for row in connection.execute(
            "SELECT match_id,source,league,home_team_id,away_team_id,kickoff_utc,counterpart_id "
            "FROM matches ORDER BY match_id"
        )
    ]
    queue_bytes = (database.parent / QUEUE).read_bytes()
    candidates = json.loads(queue_bytes)
    ids = {
        observation["match_id"]
        for names in candidates.values()
        for entry in names.values()
        for observation in entry.get("evidence", [])
    }
    info = dict(
        version=1,
        inventory_sha256=_sha(_encoded(dict(teams=teams, matches=matches))),
        queue_sha256=_sha(queue_bytes),
        counts=dict(teams=len(teams), matches=len(matches)),
        teams=teams,
        candidate_matches=[row for row in matches if row["match_id"] in ids],
        candidates=candidates,
    )
    return info, matches


def inspect_candidates(database):
    """Read all identities, observation rows and snapshot hashes; change nothing."""
    database = Path(database).resolve(strict=True)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        return _snapshot(database, connection)[0]


def _db_instant(value):
    # SQLite ORM UTC timestamps are stored without an offset.
    stamp = datetime.fromisoformat(value)
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else instant(stamp)


def _pending(candidates):
    return sum(
        entry.get("status") == "pending"
        for names in candidates.values()
        for entry in names.values()
    )


def _decision(row, info, matches, now):
    league, name = row["league"], row["raw_name"]
    entry = info["candidates"].get(league, {}).get(name)
    team_id = row["team_id"]
    if (
        not isinstance(entry, dict)
        or entry.get("status") != "pending"
        or entry.get("source") != "jc"
        or entry["team_id"] != team_id
    ):
        raise ValueError("核验计划须对应当前 pending 竞彩候选身份")
    teams = {team["id"]: team for team in info["teams"]}
    if team_id not in teams or teams[team_id]["name"] != name:
        raise ValueError("候选名称与现有球队身份不一致")
    names = row.get("checked_names")
    if (
        not isinstance(names, list)
        or not names
        or name not in names
        or any(not isinstance(n, str) or not n.strip() for n in names)
    ):
        raise ValueError("须显式列出已核对的精确队名（包括原始名）")
    if not isinstance(row.get("reason"), str) or not row["reason"].strip():
        raise ValueError("每个核验结论必须附原因")
    conclusion = row.get("conclusion")
    if conclusion not in {"distinct", "duplicate", "unresolved"}:
        raise ValueError("核验结论只能为 distinct / duplicate / unresolved")
    keys = {normalize_team(n).casefold() for n in names}
    owners = {
        team["id"]
        for team in teams.values()
        if any(normalize_team(n).casefold() in keys for n in [team["name"], *team["aliases"]])
    }
    lookup = {match["match_id"]: match for match in matches}
    observations = entry.get("evidence", [])
    if not observations:
        raise ValueError("候选缺少原始比赛证据")
    same_games = []
    for observation in observations:
        game = lookup.get(observation.get("match_id"))
        side = observation.get("side")
        if (
            game is None
            or side not in {"home", "away"}
            or game["source"] != "jc"
            or game["league"] != league
            or game[side + "_team_id"] != team_id
            or _db_instant(game["kickoff_utc"]) != instant(observation["kickoff_utc"])
            or instant(observation["captured_utc"]) > now
        ):
            raise ValueError("候选原始比赛、方向、时间或球队证据不符")
        opposite = "away" if side == "home" else "home"
        for other in matches:
            if (
                other["league"] == league
                and _db_instant(other["kickoff_utc"]) == _db_instant(game["kickoff_utc"])
                and other[opposite + "_team_id"] == game[opposite + "_team_id"]
                and other[side + "_team_id"] != team_id
            ):
                owners.add(other[side + "_team_id"])
                same_games.append(other)
    others = sorted(owners - {team_id})
    if conclusion == "distinct" and others:
        raise ValueError(f"{league}/{name} 有重复或可疑身份，不能清除待核：{others}")
    if conclusion == "duplicate" and not others:
        raise ValueError("重复结论须指明既有身份的精确核对证据")
    return dict(
        **row,
        observations=observations,
        observation_matches=[lookup[e["match_id"]] for e in observations],
        exact_name_owners=[teams[i] for i in sorted(owners)],
        other_team_ids=others,
        same_game_conflicts=same_games,
        next_action=(
            "cleared_in_pinned_inventory"
            if conclusion == "distinct"
            else "T54_reconcile_required"
            if conclusion == "duplicate"
            else "keep_pending"
        ),
    )


def _plan_rows(plan):
    if (
        not isinstance(plan, dict)
        or not isinstance(plan.get("reviewer"), str)
        or not plan["reviewer"].strip()
        or not isinstance(plan.get("decisions"), list)
        or not plan["decisions"]
    ):
        raise ValueError("须提供有审核者、版本及逐条决定的显式计划")
    rows = plan["decisions"]
    if any(
        not isinstance(row, dict)
        or any(
            not isinstance(row.get(key), str) or not row[key].strip()
            for key in ("league", "raw_name")
        )
        or type(row.get("team_id")) is not int
        or row["team_id"] <= 0
        for row in rows
    ):
        raise ValueError("计划决定必须指定赛事、候选名和正整数球队编号")
    if len({(row["league"], row["raw_name"]) for row in rows}) != len(rows):
        raise ValueError("同一候选不能有多个核验决定")
    return rows


def _scoped_evidence(connection, decision, info):
    match_ids = {
        row["match_id"]
        for row in (*decision["observation_matches"], *decision["same_game_conflicts"])
    }
    # Include full evidence rows only in v2. The v1 global inventory digest keeps
    # the original identity projection for already approved plans.
    matches = [
        dict(
            connection.execute(
                "SELECT match_id,source,league,home_team_id,away_team_id,kickoff_utc,"
                "counterpart_id,jc_num,status,result_checked_utc,result_provider "
                "FROM matches WHERE match_id=?",
                (mid,),
            ).fetchone()
        )
        for mid in sorted(match_ids)
    ]
    ids = {team["id"] for team in decision["exact_name_owners"]}
    for match in matches:
        ids.update((match["home_team_id"], match["away_team_id"]))
        result = connection.execute(
            "SELECT match_id,home_goals,away_goals,ht_home_goals,ht_away_goals,"
            "available_utc,available_source FROM results WHERE match_id=?",
            (match["match_id"],),
        ).fetchone()
        match["result"] = dict(result) if result is not None else None
    teams = [row for row in info["teams"] if row["id"] in ids]
    if len(teams) != len(ids):
        raise ValueError("证据比赛引用的球队身份缺失")
    return dict(
        candidate=dict(info["candidates"][decision["league"]][decision["raw_name"]]),
        teams=teams,
        matches=matches,
    )


def plan_candidates(database, draft):
    """Seal the operator's explicit decisions against a single read-only snapshot."""
    rows = _plan_rows(draft)
    if any("scope_sha256" in row for row in rows):
        raise ValueError("请用复核后的决定草稿生成计划")
    database = Path(database).resolve(strict=True)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        info, matches = _snapshot(database, connection)
        now = datetime.now(UTC)
        decisions = [
            dict(
                row,
                scope_sha256=_sha(
                    _encoded(_scoped_evidence(connection, _decision(row, info, matches, now), info))
                ),
            )
            for row in rows
        ]
    return dict(version=2, reviewer=draft["reviewer"], decisions=decisions)


def review_candidates(database, plan_path, audit_dir):
    """Apply an explicit plan to a copy's queue and save review evidence.

    DB identities and matches are read only. BEGIN IMMEDIATE serializes the queue
    update with normal JC ingestion; no SQL update is issued. All decisions are
    validated before any output. Confirmed duplicates stay pending until merged.
    """
    database = Path(database).resolve(strict=True)
    plan_bytes = Path(plan_path).read_bytes()
    plan = json.loads(plan_bytes)
    audit_dir = Path(audit_dir).resolve()
    if audit_dir.exists():
        raise FileExistsError(f"核验证据目录已存在：{audit_dir}")
    queue_path = database.parent / QUEUE
    if queue_path.is_symlink():
        raise ValueError("候选队列不能为符号链接")
    rows = _plan_rows(plan)
    version = plan.get("version")
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("须提供有审核者、版本及逐条决定的显式计划")
    with sqlite3.connect(database.as_uri() + "?mode=rw", uri=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        info, matches = _snapshot(database, connection)
        if version == 1 and any(
            plan.get(key) != info[key] for key in ("inventory_sha256", "queue_sha256")
        ):
            raise ValueError("身份清单或候选队列已变化，须重新核验后制定计划")
        now = datetime.now(UTC)
        decisions = [_decision(row, info, matches, now) for row in rows]
        if version == 2:
            for decision in decisions:
                evidence = _scoped_evidence(connection, decision, info)
                if decision.get("scope_sha256") != _sha(_encoded(evidence)):
                    raise ValueError("计划所涉及的候选、球队或比赛事实已变化，须重新核验")
                decision["scoped_evidence"] = evidence
        pending_before = _pending(info["candidates"])
        report = dict(
            version=1,
            scope="仅核验固定库既有身份，不是全球别名核准或模型适用性声明",
            reviewer=plan["reviewer"],
            plan_sha256=_sha(plan_bytes),
            recorded_utc=now.isoformat(),
            recorded_beijing=now.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
            inventory_sha256=info["inventory_sha256"],
            queue_before_sha256=info["queue_sha256"],
            counts=info["counts"],
            pending_before=pending_before,
            pending_after=pending_before - sum(d["conclusion"] == "distinct" for d in decisions),
            decisions=decisions,
        )
        if version == 2:
            report["plan_version"] = version
        audit_bytes = _encoded(report)
        for decision in decisions:
            entry = info["candidates"][decision["league"]][decision["raw_name"]]
            entry["status"] = "cleared" if decision["conclusion"] == "distinct" else "pending"
            entry["review"] = dict(
                reviewer=plan["reviewer"],
                recorded_utc=report["recorded_utc"],
                recorded_beijing=report["recorded_beijing"],
                inventory_sha256=info["inventory_sha256"],
                plan_sha256=report["plan_sha256"],
                audit_sha256=_sha(audit_bytes),
                conclusion=decision["conclusion"],
                reason=decision["reason"],
                checked_names=decision["checked_names"],
                other_team_ids=decision["other_team_ids"],
                next_action=decision["next_action"],
            )
            if version == 2:
                entry["review"]["scope_sha256"] = decision["scope_sha256"]
        audit_dir.mkdir(parents=True)
        (audit_dir / "plan.json").write_bytes(plan_bytes)
        (audit_dir / "identity_inventory.json").write_bytes(_encoded(info["teams"]))
        (audit_dir / "review.json").write_bytes(audit_bytes)
        # Atomic replacement is last: a failed evidence write leaves the queue untouched.
        _atomic_write(queue_path, _encoded(info["candidates"]))
        # There are no DB writes to commit; release the ingestion lock.
        connection.rollback()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="只读导出固定身份清单与候选证据")
    inspect.add_argument("--db", type=Path, required=True)
    inspect.add_argument("--out", type=Path, required=True)
    plan = commands.add_parser("plan", help="用显式决定草稿生成逐条绑定证据的计划")
    plan.add_argument("--db", type=Path, required=True)
    plan.add_argument("--decisions", type=Path, required=True)
    plan.add_argument("--out", type=Path, required=True)
    review = commands.add_parser("apply", help="将显式核验计划应用到恢复副本的候选队列")
    review.add_argument("--db", type=Path, required=True)
    review.add_argument("--plan", type=Path, required=True)
    review.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.command in {"inspect", "plan"}:
        result = (
            inspect_candidates(args.db)
            if args.command == "inspect"
            else plan_candidates(args.db, json.loads(args.decisions.read_bytes()))
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as stream:
            stream.write(_encoded(result))
        LOG.info("只读核验产物已保存：%s", args.out)
    else:
        result = review_candidates(args.db, args.plan, args.audit)
        LOG.info(
            "待核队名：%s → %s；未改比赛/球队/预测/账本",
            result["pending_before"],
            result["pending_after"],
        )


if __name__ == "__main__":
    main()
