"""Reconcile the single owner-approved fd Den Haag identity without fuzzy matching."""

import hashlib
import json
from pathlib import Path

from sqlalchemy import or_, select

from zucai.models import ApifFixtureIndex, MatchEvent, MatchLineup, MatchStat, Team
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.apif_ingest import _Teams
from zucai.sources.apif_reconcile import _references, write_audit
from zucai.sources.fd_ingest import WINDOW
from zucai.sources.fd_verify import _utc
from zucai.sources.football_data import ALIASES_PATH, read_json
from zucai.stats_storage import transaction

LEAGUE, SOURCE, TARGET = "荷甲", "Den Haag", "海牙"
APPROVAL = {
    "canonical": TARGET,
    "status": "approved",
    "approved_by": "qw5563680",
    "approved_utc": "2026-11-04T09:10:00Z",
    "approval_pr": "https://github.com/qw5563680/FOOTBALL/pull/49#issuecomment-6627072639",
}
TECHNICAL_MODELS = (MatchStat, MatchEvent, MatchLineup)
AUDIT_NAME = "fd_identity_merges.csv"


def _plan(session, old, target):
    lookup = _Teams(session, {})
    if lookup.one(SOURCE) is not old or target is None or lookup.one(TARGET) is not target:
        raise ValueError("Den Haag 或海牙存在全局名称别名冲突或规范球队缺失")
    if old.id == target.id:
        raise ValueError("原身份与规范身份相同")
    if old.aliases:
        raise ValueError("Den Haag 原身份 aliases 非空")
    games = _references(session, old.id)
    if any(game.source != "fd" or game.league != LEAGUE for game in games):
        raise ValueError("Den Haag 被非 fd 荷甲比赛引用")
    if any(target.id in (game.home_team_id, game.away_team_id) for game in games):
        raise ValueError("合并会导致主客身份相同")
    existing = _references(session, target.id, LEAGUE)
    if any(
        abs(_utc(game.kickoff_utc) - _utc(other.kickoff_utc)) <= WINDOW
        for game in games
        for other in existing
    ):
        raise ValueError("同联赛三小时内规范身份已有比赛")
    by_id = {game.match_id: game for game in games}
    technical = []
    for model in TECHNICAL_MODELS:
        for row in session.scalars(
            select(model).where(or_(model.team_id == old.id, model.match_id.in_(by_id)))
        ):
            game = by_id.get(row.match_id)
            if game is None or row.team_id != getattr(game, row.side + "_team_id", None):
                raise ValueError("技术数据球队与 fd 比赛身份不一致")
            if row.team_id == old.id:
                technical.append(row)
    indices = list(
        session.scalars(
            select(ApifFixtureIndex).where(
                or_(
                    ApifFixtureIndex.home_team_id == old.id,
                    ApifFixtureIndex.away_team_id == old.id,
                    ApifFixtureIndex.match_id.in_(by_id),
                )
            )
        )
    )
    for row in indices:
        game = by_id.get(row.match_id)
        if game is None or (row.league, row.home_team_id, row.away_team_id) != (
            game.league,
            game.home_team_id,
            game.away_team_id,
        ):
            raise ValueError("技术数据索引与 fd 比赛身份不一致")
    return games, technical, indices


def reconcile_den_haag(database, run_id):
    """Apply only the approved 荷甲 Den Haag → 海牙 mapping; never merge matches."""
    aliases = read_json(ALIASES_PATH)
    entry = aliases.get(LEAGUE, {}).get(SOURCE)
    if not isinstance(entry, dict) or any(
        entry.get(key) != value for key, value in APPROVAL.items()
    ):
        raise ValueError("Den Haag 合并需要所有者指定的正式批准记录")
    validate_approvals({LEAGUE: {SOURCE: entry}}, require_metadata=True)
    report = dict(
        operation="reconcile_den_haag",
        status="complete",
        requests=0,
        merged_teams=0,
        rekeyed_matches=0,
        deleted_teams=0,
        rekeyed_stats=0,
        rekeyed_indices=0,
        skipped_conflicts=0,
    )
    with transaction(database, AUDIT_NAME) as (session, audit):
        # Require actual existing names. An alias by itself cannot select a team to merge.
        teams = list(session.scalars(select(Team)))
        old = next((team for team in teams if team.name == SOURCE), None)
        target = next((team for team in teams if team.name == TARGET), None)
        if old is None:
            return report
        row = dict(
            run_id=run_id,
            league=LEAGUE,
            team_id=SOURCE,
            provisional_team_row_id=old.id,
            canonical=TARGET,
            matches_rekeyed=0,
            alias_status=entry["status"],
            alias_source=json.dumps(
                dict(
                    kind="owner_approved_fd_name",
                    source_entry=entry,
                    formal_aliases_sha256=hashlib.sha256(
                        Path(ALIASES_PATH).read_bytes()
                    ).hexdigest(),
                ),
                ensure_ascii=False,
                sort_keys=True,
            ),
            reason="",
        )
        try:
            games, technical, indices = _plan(session, old, target)
        except ValueError as exc:
            row["reason"] = str(exc)
            report.update(status="partial", skipped_conflicts=1)
        else:
            # Every identity check precedes the first mutation. Match IDs and payloads stay intact.
            for game in games:
                if game.home_team_id == old.id:
                    game.home_team_id = target.id
                if game.away_team_id == old.id:
                    game.away_team_id = target.id
            for record in technical:
                record.team_id = target.id
            for index in indices:
                if index.home_team_id == old.id:
                    index.home_team_id = target.id
                if index.away_team_id == old.id:
                    index.away_team_id = target.id
            session.flush()
            if _references(session, old.id):
                raise ValueError("Den Haag 合并后仍有比赛引用")
            # Foreign keys also guard against any references from unknown attached tables.
            session.delete(old)
            if SOURCE not in target.aliases:
                target.aliases.append(SOURCE)
            session.flush()
            row.update(matches_rekeyed=len(games), reason="merged")
            report.update(
                merged_teams=1,
                rekeyed_matches=len(games),
                deleted_teams=1,
                rekeyed_stats=len(technical),
                rekeyed_indices=len(indices),
            )
        write_audit(audit, [row])
    return report
