"""Rekey verified APIF provisional identities without altering importer policy."""

import csv
import hashlib
import io
import json
import logging
import re
from copy import deepcopy
from pathlib import Path

from sqlalchemy import or_, select, text

from zucai.db import get_engine, session_scope
from zucai.models import Match, Team
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.apif_command import ALIASES
from zucai.sources.apif_ingest import _Teams
from zucai.sources.base import _atomic_write
from zucai.sources.cup_identity import CUP_LEAGUES, _bridge
from zucai.sources.fd_ingest import WINDOW, _canonical
from zucai.sources.fd_verify import _utc
from zucai.sources.football_data import read_json

LOG = logging.getLogger(__name__)
FIELDS = (
    "run_id",
    "league",
    "team_id",
    "provisional_team_row_id",
    "canonical",
    "matches_rekeyed",
    "alias_status",
    "alias_source",
    "reason",
)


def write_audit(path, rows):
    previous = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(FIELDS):
                raise ValueError("身份合并审计字段无效")
            previous = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in previous):
            raise ValueError("身份合并审计行无效")
    records = {}
    for row in [*previous, *rows]:
        row = {k: str(row[k]) for k in FIELDS}
        key = tuple(row[k] for k in ("league", "team_id", "canonical"))
        old = records.get(key)
        if old and all(old[k] == row[k] for k in FIELDS if k != "run_id"):
            continue
        records[key] = row
    if list(records.values()) == previous:
        return
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS)
    writer.writeheader()
    writer.writerows(records.values())
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, output.getvalue().encode("utf-8"))


def _references(session, team_id, league=None):
    statement = select(Match).where(
        or_(Match.home_team_id == team_id, Match.away_team_id == team_id)
    )
    if league is not None:
        statement = statement.where(Match.league == league)
    return list(session.scalars(statement.order_by(Match.match_id)))


def reconcile(database, run_id):
    """Only formal active aliases and the existing cup bridge can authorize a move."""
    database = Path(database)
    if not database.is_file():
        raise ValueError("待合并数据库不存在")
    aliases = read_json(ALIASES)
    validate_approvals(aliases, require_metadata=True)
    alias_sha = hashlib.sha256(Path(ALIASES).read_bytes()).hexdigest()
    engine = get_engine(database)
    report = dict(
        status="complete",
        operation="reconcile",
        requests=0,
        merged_teams=0,
        rekeyed_matches=0,
        deleted_teams=0,
        skipped_conflicts=0,
        unresolved_pairs=0,
    )
    merged, rekeyed, audit = set(), set(), []
    audit_path = database.parent / "state/audit/apif_identity_merges.csv"
    previous_audit, audit_attempted = None, False
    try:
        with session_scope(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            provisional = [
                t
                for t in session.scalars(select(Team).order_by(Team.id))
                if re.fullmatch(r"apif:team:[1-9][0-9]*", t.name)
            ]
            for temporary in provisional:
                key = temporary.name.removeprefix("apif:team:")
                leagues = sorted({m.league for m in _references(session, temporary.id)})
                for league in leagues:
                    row = dict(
                        run_id=run_id,
                        league=league,
                        team_id=key,
                        provisional_team_row_id=temporary.id,
                        canonical="",
                        matches_rekeyed=0,
                        alias_status="",
                        alias_source="",
                        reason="",
                    )
                    try:
                        entries = aliases.get(league, {})
                        if league in CUP_LEAGUES["apif"]:
                            entries = _bridge(
                                "apif", database, league, {key: temporary.name}, aliases
                            )
                        canonical = _canonical({league: entries}, league, key)
                        if canonical is None:
                            report["unresolved_pairs"] += 1
                            continue
                        entry = entries[key]
                        row.update(
                            canonical=canonical,
                            alias_status=entry["status"],
                            alias_source=json.dumps(
                                dict(
                                    formal_aliases_sha256=alias_sha,
                                    evidence=entry.get("evidence", []),
                                    league=league,
                                    team_id=key,
                                ),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        )
                        if canonical.casefold().startswith("apif:"):
                            raise ValueError("规范队名使用了来源 ID 保留前缀")
                        resolved = deepcopy(aliases)
                        resolved[league] = entries
                        teams = _Teams(session, resolved)
                        if (league, key) in teams.collisions:
                            raise ValueError("同一规范球队对应多个生效的来源球队 ID")
                        target = teams.one(canonical)
                        if target is None:
                            raise ValueError("规范球队在库中缺失")
                        if target.id == temporary.id:
                            raise ValueError("规范身份仍为临时球队")
                        if temporary.aliases:
                            raise ValueError("临时身份 aliases 非空")
                        prefix = f"apif:{league}:team:"
                        binding = prefix + key
                        owner = teams.one(binding)
                        if owner and owner.id != target.id:
                            raise ValueError("来源 ID 已绑定其他球队")
                        if any(a.startswith(prefix) and a != binding for a in target.aliases):
                            raise ValueError("规范球队已有不同的本联赛 APIF ID")
                        games = _references(session, temporary.id, league)
                        if any(m.source != "apif" for m in games):
                            raise ValueError("临时身份被非 APIF 比赛引用")
                        existing = _references(session, target.id, league)
                        if any(
                            abs(_utc(g.kickoff_utc) - _utc(m.kickoff_utc)) <= WINDOW
                            for g in games
                            for m in existing
                        ):
                            raise ValueError("同联赛三小时内规范身份已有比赛")
                        # All checks precede the first mutation for this league/ID.
                        for game in games:
                            if game.home_team_id == temporary.id:
                                game.home_team_id = target.id
                            if game.away_team_id == temporary.id:
                                game.away_team_id = target.id
                            rekeyed.add(game.match_id)
                        if binding not in target.aliases:
                            target.aliases.append(binding)
                        session.flush()
                        merged.add(temporary.id)
                        row.update(matches_rekeyed=len(games), reason="merged")
                    except ValueError as exc:
                        row["reason"] = str(exc)
                        report["skipped_conflicts"] += 1
                        report["status"] = "partial"
                    audit.append(row)
                if temporary.id in merged and not _references(session, temporary.id):
                    session.delete(temporary)
                    report["deleted_teams"] += 1
                    session.flush()
            previous_audit = audit_path.read_bytes() if audit_path.exists() else None
            audit_attempted = True
            write_audit(audit_path, audit)
    except Exception:
        # session_scope has rolled back; don't leave a successful audit of a failed commit.
        if audit_attempted:
            try:
                if previous_audit is None:
                    audit_path.unlink(missing_ok=True)
                elif not audit_path.exists() or audit_path.read_bytes() != previous_audit:
                    _atomic_write(audit_path, previous_audit)
            except OSError:
                LOG.exception("数据库已回滚，但恢复身份合并审计失败；本轮不得发布")
        raise
    finally:
        engine.dispose()
    report.update(merged_teams=len(merged), rekeyed_matches=len(rekeyed))
    LOG.info(
        "APIF 身份合并 %d 队，重定向 %d 场，冲突跳过 %d",
        len(merged),
        len(rekeyed),
        report["skipped_conflicts"],
    )
    return report


def write_summary(report):
    import os

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(
                f"### APIF 存量身份合并：{report['status']}\n\n"
                f"合并临时球队 {report['merged_teams']}；删除空临时行 {report['deleted_teams']}；"
                f"重定向不同比赛 {report['rekeyed_matches']}；"
                f"冲突跳过 {report['skipped_conflicts']}；"
                f"尚无生效别名 {report['unresolved_pairs']} 个联赛/ID。\n\n"
                "不请求 API，不改 match_id、赛果或冻结预测。"
                "审计：state/audit/apif_identity_merges.csv。\n"
            )
