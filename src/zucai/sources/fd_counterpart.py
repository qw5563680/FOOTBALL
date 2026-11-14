"""Link uniquely identified fdorg rows to higher-priority fd rows without changing evidence."""

import logging
import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select

from zucai.db import get_engine, session_scope
from zucai.models import Match

LOG = logging.getLogger(__name__)
WINDOW = timedelta(hours=3)


class CounterpartConflict(ValueError):
    def __init__(self, reason, candidates):
        super().__init__(reason)
        self.candidates = sorted(set(candidates))


def _neighbors(session, match, source):
    when = match.kickoff_utc
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    home, away = match.home_team_id, match.away_team_id
    return list(
        session.scalars(
            select(Match)
            .where(
                Match.source == source,
                Match.league == match.league,
                Match.kickoff_utc.between(when - WINDOW, when + WINDOW),
                or_(
                    and_(Match.home_team_id == home, Match.away_team_id == away),
                    and_(Match.home_team_id == away, Match.away_team_id == home),
                ),
            )
            .order_by(Match.match_id)
        )
    )


def fdorg_candidate(session, fd):
    """Exact stored team IDs only; both sides must have a unique, nonconflicting partner."""
    candidates = _neighbors(session, fd, "fdorg")
    if not candidates:
        claimed = list(
            session.scalars(
                select(Match.match_id).where(
                    Match.source == "fdorg",
                    Match.counterpart_id == fd.match_id,
                )
            )
        )
        if (fd.counterpart_id or "").startswith("fdorg:"):
            claimed.append(fd.counterpart_id)
        if claimed:
            raise CounterpartConflict("既有 fdorg 链接与当前身份或开球窗口不符", claimed)
        return None
    ids = [row.match_id for row in candidates]
    if len(candidates) != 1:
        raise CounterpartConflict("多个 fdorg 候选比赛，不猜测链接", ids)
    candidate = candidates[0]
    if (candidate.home_team_id, candidate.away_team_id) != (fd.home_team_id, fd.away_team_id):
        raise CounterpartConflict("fdorg 候选主客相反", ids)
    others = [
        row.match_id for row in _neighbors(session, candidate, "fd") if row.match_id != fd.match_id
    ]
    if others:
        raise CounterpartConflict("fdorg 对应多个或反向 fd 候选", others + [fd.match_id])
    if candidate.counterpart_id not in (None, fd.match_id) or fd.counterpart_id not in (
        None,
        candidate.match_id,
    ):
        raise CounterpartConflict("fd/fdorg 已有不同 counterpart 链接", ids + [fd.match_id])
    claims = list(
        session.scalars(
            select(Match.match_id).where(
                or_(
                    and_(Match.counterpart_id == fd.match_id, Match.match_id != candidate.match_id),
                    and_(Match.counterpart_id == candidate.match_id, Match.match_id != fd.match_id),
                )
            )
        )
    )
    if claims:
        from zucai.sources.fd_apif_counterpart import apif_candidate

        # Another provider may describe this same FD anchor, but it must pass
        # the complete bidirectional identity and claim checks independently.
        api = apif_candidate(session, fd)
        allowed = {api.match_id} if api and api.counterpart_id == fd.match_id else set()
        if set(claims) - allowed:
            raise CounterpartConflict("fd/fdorg 已被其他比赛链接", claims)
    return candidate


def require_relink_actions():
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
    ):
        raise ValueError("--relink-fdorg 只能由审查者通过 Actions workflow_dispatch 触发")


def relink_fdorg(database):
    """Offline core for the guarded manual command; modifies counterpart_id only."""
    database = Path(database)
    if not database.is_file():
        raise ValueError("待补链数据库不存在")
    report = dict(
        status="complete",
        operation="relink-fdorg",
        requests=0,
        linked=0,
        skipped=0,
        reasons={},
        pairs=[],
    )
    reasons = Counter()
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            for existing in session.scalars(
                select(Match).where(Match.source == "fdorg").order_by(Match.match_id)
            ):
                candidates = _neighbors(session, existing, "fd")
                # No possible pair is outside the cleanup scope, not a skipped pair.
                if not candidates:
                    continue
                row = dict(fdorg_id=existing.match_id, fd_id="", league=existing.league, reason="")
                try:
                    if len(candidates) != 1:
                        raise CounterpartConflict(
                            "多个 fd 候选比赛，不猜测链接", [m.match_id for m in candidates]
                        )
                    target = candidates[0]
                    row["fd_id"] = target.match_id
                    if _neighbors(session, existing, "jc") or _neighbors(session, target, "jc"):
                        raise CounterpartConflict("存在 jc 候选，保持官方规则", [])
                    candidate = fdorg_candidate(session, target)
                    if candidate is None or candidate.match_id != existing.match_id:
                        raise CounterpartConflict("fdorg 候选无法唯一复核", [])
                    if existing.counterpart_id == target.match_id:
                        row["reason"] = "already_linked"
                    else:
                        existing.counterpart_id = target.match_id
                        row["reason"] = "linked"
                        report["linked"] += 1
                except CounterpartConflict as exc:
                    row["reason"] = str(exc)
                    row["candidates"] = exc.candidates
                if row["reason"] != "linked":
                    report["skipped"] += 1
                    reasons[row["reason"]] += 1
                report["pairs"].append(row)
                LOG.info("fd/fdorg 补链：%s", row)
    finally:
        engine.dispose()
    report["reasons"] = dict(sorted(reasons.items()))
    LOG.info(
        "fd/fdorg 补链 %d，跳过 %d：%s", report["linked"], report["skipped"], report["reasons"]
    )
    return report


def write_summary(report):
    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(
            f"### fd/fdorg 存量补链\n\n补链 {report['linked']}；跳过 {report['skipped']}。\n\n"
        )
        stream.write("| 原因 | 跳过数 |\n| --- | ---: |\n")
        for reason, count in report["reasons"].items():
            stream.write(f"| {reason} | {count} |\n")
        stream.write("\n| fdorg | fd | 结果 / 原因 |\n| --- | --- | --- |\n")
        for row in report["pairs"]:
            stream.write(f"| {row['fdorg_id']} | {row['fd_id']} | {row['reason']} |\n")
