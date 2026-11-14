"""Strict stored-ID links for API fixtures that arrived before their official JC row."""

import csv
import io
import json
import logging
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from sqlalchemy import or_, select

from zucai.db import get_engine, session_scope
from zucai.models import Match, Team
from zucai.sources.base import _atomic_write
from zucai.sources.fd_counterpart import WINDOW, CounterpartConflict

LOG = logging.getLogger(__name__)
PROVIDERS = ("apif", "fdorg")
FIELDS = (
    "api_id",
    "jc_id",
    "league",
    "api_kickoff_utc",
    "jc_kickoff_utc",
    "outcome",
    "reason",
    "candidates",
)


def _utc(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _neighbors(session, match, sources, *, team_ids=None):
    """A shared team can veto a link; only the exact oriented pair can create one."""
    when = _utc(match.kickoff_utc)
    teams = team_ids if team_ids is not None else (match.home_team_id, match.away_team_id)
    return list(
        session.scalars(
            select(Match)
            .where(
                Match.source.in_(sources),
                Match.match_id != match.match_id,
                Match.league == match.league,
                Match.kickoff_utc.between(when - WINDOW, when + WINDOW),
                or_(Match.home_team_id.in_(teams), Match.away_team_id.in_(teams)),
            )
            .order_by(Match.match_id)
        )
    )


def late_jc_candidate(session, existing, *, team_ids=None):
    """Return a unique JC target (including an already verified link), without writing.

    Both endpoints must be free of any other claims, even outside this league/window.
    Reverse lookup prevents iteration order choosing between competing API rows.
    Reconcile may supply its approved projected pair without mutating the stored row.
    """
    pair = team_ids if team_ids is not None else (existing.home_team_id, existing.away_team_id)
    candidates = _neighbors(session, existing, ("jc",), team_ids=pair)
    ids = [m.match_id for m in candidates]
    claims = list(
        session.scalars(
            select(Match.match_id)
            .where(
                Match.counterpart_id == existing.match_id,
            )
            .order_by(Match.match_id)
        )
    )
    if not candidates:
        if claims:
            raise CounterpartConflict("API 行已被其他比赛声明，当前身份或开球无法复核", claims)
        return None
    if existing.source not in PROVIDERS:
        raise CounterpartConflict("既有行不是 API 来源自己的比赛", ids)
    if any(session.get(Team, t).name.startswith(("apif:team:", "fdorg:team:")) for t in pair):
        raise CounterpartConflict("API 球队仍为临时身份", ids)
    if any((m.home_team_id, m.away_team_id) != pair for m in candidates):
        raise CounterpartConflict("jc 候选主客相反或同队冲突", ids)
    if len(candidates) != 1:
        raise CounterpartConflict("多个 jc 候选比赛，不猜测链接", ids)
    target = candidates[0]
    if existing.counterpart_id is not None:
        raise CounterpartConflict("API 行已有 counterpart", ids + [existing.counterpart_id])
    if claims and not (claims == [target.match_id] and target.counterpart_id == existing.match_id):
        raise CounterpartConflict("API 行已被其他比赛声明", claims)
    if target.counterpart_id not in (None, existing.match_id):
        raise CounterpartConflict("jc 行已有不同 counterpart", ids + [target.counterpart_id])
    target_claims = list(
        session.scalars(
            select(Match.match_id).where(
                Match.counterpart_id == target.match_id,
            )
        )
    )
    if target_claims:
        raise CounterpartConflict("jc 行已被其他比赛声明", target_claims)
    others = [
        m.match_id
        for m in _neighbors(session, target, PROVIDERS)
        if m.match_id != existing.match_id
    ]
    if others:
        raise CounterpartConflict("jc 候选对应多个 API 比赛，不猜测链接", ids + others)
    return target


def check_late_observation(session, existing, game, pair, target):
    """Check both the saved fixture and this capture; never repair identity or kickoff here."""
    if (
        existing.source != game.provider
        or existing.league != game.league
        or (existing.home_team_id, existing.away_team_id) != pair
        or abs(_utc(existing.kickoff_utc) - game.kickoff_utc) > WINDOW
    ):
        raise CounterpartConflict(
            "既有 API 身份或开球与当前观察冲突", [existing.match_id, target.match_id]
        )
    candidate = late_jc_candidate(session, existing)
    if candidate is None or candidate.match_id != target.match_id:
        raise CounterpartConflict(
            "既有 API 与 jc 开球窗口无法唯一复核", [existing.match_id, target.match_id]
        )


def require_relink_actions():
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
    ):
        raise ValueError("--relink-jc 只能由审查者通过 Actions workflow_dispatch 触发")


def _write_audit(path, rows):
    previous = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(FIELDS):
                raise ValueError("jc 补链审计字段无效")
            previous = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in previous):
            raise ValueError("jc 补链审计行无效")
    seen = {tuple(row[field] for field in FIELDS) for row in previous}
    added = []
    for record in rows:
        row = {field: str(record[field]) for field in FIELDS}
        row["candidates"] = json.dumps(
            record["candidates"], ensure_ascii=False, separators=(",", ":")
        )
        key = tuple(row[field] for field in FIELDS)
        if key not in seen:
            added.append(row)
            seen.add(key)
    if not added:
        return
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS)
    writer.writeheader()
    writer.writerows([*previous, *added])
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, output.getvalue().encode("utf-8"))


def relink_jc(database):
    """Offline cleanup core: only JC counterpart_id changes; no evidence rows are rewritten."""
    database = Path(database)
    if not database.is_file():
        raise ValueError("待补链数据库不存在")
    report = dict(
        status="complete",
        operation="relink-jc",
        requests=0,
        linked=0,
        already_linked=0,
        skipped=0,
        reasons={},
        pairs=[],
    )
    audit = database.parent / "state/audit/jc_relink.csv"
    previous_audit = audit.read_bytes() if audit.exists() else None
    reasons = Counter()
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            for existing in session.scalars(
                select(Match)
                .where(
                    Match.source.in_(PROVIDERS),
                )
                .order_by(Match.match_id)
            ):
                candidates = _neighbors(session, existing, ("jc",))
                if not candidates:
                    continue  # No pair in the stored-ID window: outside this cleanup's scope.
                target = candidates[0] if len(candidates) == 1 else None
                row = dict(
                    api_id=existing.match_id,
                    jc_id=target.match_id if target else "",
                    league=existing.league,
                    api_kickoff_utc=existing.kickoff_utc,
                    jc_kickoff_utc=target.kickoff_utc if target else "",
                    outcome="skipped",
                    reason="",
                    candidates=[m.match_id for m in candidates],
                )
                try:
                    target = late_jc_candidate(session, existing)
                    if target.counterpart_id == existing.match_id:
                        row["outcome"] = "already_linked"
                    else:
                        target.counterpart_id = existing.match_id
                        row["outcome"] = "linked"
                except CounterpartConflict as exc:
                    row["reason"] = str(exc)
                    row["candidates"] = sorted(set(row["candidates"] + exc.candidates))
                    reasons[str(exc)] += 1
                report[row["outcome"]] += 1
                report["pairs"].append(row)
                LOG.info("API/jc 补链：%s", row)
            session.flush()
            _write_audit(audit, report["pairs"])
    except Exception:
        # Do not leave a success receipt after a failed transaction or failed audit write.
        if previous_audit is None:
            audit.unlink(missing_ok=True)
        elif not audit.exists() or audit.read_bytes() != previous_audit:
            _atomic_write(audit, previous_audit)
        raise
    finally:
        engine.dispose()
    report["reasons"] = dict(sorted(reasons.items()))
    return report


def write_summary(report):
    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(
            f"### API/jc 存量补链\n\n补链 {report['linked']}；"
            f"已链接 {report['already_linked']}；跳过 {report['skipped']}。\n\n"
        )
        stream.write("| 跳过原因 | 数量 |\n| --- | ---: |\n")
        for reason, count in report["reasons"].items():
            stream.write(f"| {reason} | {count} |\n")
        stream.write(
            "\n| API ID | jc ID | 联赛 | API 开球 UTC | jc 开球 UTC | 结果 | 原因 | 候选 |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        )
        for row in report["pairs"]:
            stream.write("| " + " | ".join(str(row[k]) for k in FIELDS) + " |\n")
