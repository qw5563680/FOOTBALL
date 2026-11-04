"""Merge only owner-approved fd name pairs, retaining all identity prechecks."""

import hashlib
import json
from pathlib import Path

from sqlalchemy import or_, select

from zucai.models import ApifFixtureIndex, MatchEvent, MatchLineup, MatchStat, Team
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.apif_ingest import _Teams
from zucai.sources.apif_reconcile import _references, write_audit
from zucai.sources.fd_ingest import WINDOW, normalize_team
from zucai.sources.fd_verify import _utc
from zucai.sources.football_data import ALIASES_PATH, read_json
from zucai.stats_storage import transaction

TECHNICAL_MODELS = (MatchStat, MatchEvent, MatchLineup)
AUDIT_NAME = "fd_identity_merges.csv"


def approved_fd_merges():
    """Read the formal file only; inferred/proposed aliases cannot authorize a merge."""
    aliases = read_json(ALIASES_PATH)
    validate_approvals(aliases, require_metadata=True)
    approved = {}
    for league, entries in aliases.items():
        for source, entry in entries.items():
            if not isinstance(entry, dict) or entry.get("status") != "approved":
                continue
            canonical = entry.get("canonical")
            if (
                entry.get("approved_by") != "qw5563680"
                or not isinstance(canonical, str)
                or not canonical.strip()
                or canonical == source
            ):
                raise ValueError("fd 合并需要所有者的正式批准记录及不同的规范名称")
            approved[league, source] = entry
    return approved


def _plan(session, old, target, league, source, canonical):
    lookup = _Teams(session, {})
    if target is None or lookup.one(canonical) is not target:
        raise ValueError("规范球队缺失或存在全局名称别名冲突")
    owners = dict(lookup.owners.get(normalize_team(source).casefold(), {}))
    # The explicitly approved target may already own this exact English alias.
    # A third identity is still a conflict; do not change the importer's lookup rules.
    owners.pop(target.id, None)
    if owners != {old.id: old}:
        raise ValueError("fd 原名称存在批准合并对以外的全局名称别名冲突")
    if old.id == target.id:
        raise ValueError("原身份与规范身份相同")
    if old.aliases:
        raise ValueError("fd 原身份 aliases 非空")
    games = _references(session, old.id)
    if any(game.source != "fd" or game.league != league for game in games):
        raise ValueError("原身份被非 fd 或批准联赛以外的比赛引用")
    if any(target.id in (game.home_team_id, game.away_team_id) for game in games):
        raise ValueError("合并会导致主客身份相同")
    existing = _references(session, target.id, league)
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


def reconcile_fd_aliases(database, run_id):
    """Apply the formal owner-approved fd merge list; never infer names or merge matches."""
    approved = approved_fd_merges()
    aliases_sha = hashlib.sha256(Path(ALIASES_PATH).read_bytes()).hexdigest()
    report = dict(
        operation="reconcile_fd_aliases",
        status="complete",
        requests=0,
        merged_teams=0,
        rekeyed_matches=0,
        deleted_teams=0,
        rekeyed_stats=0,
        rekeyed_indices=0,
        skipped_conflicts=0,
        identity_redirects=[],
    )
    with transaction(database, AUDIT_NAME) as (session, audit):
        # Require actual existing names. An alias by itself cannot select a team to merge.
        for (league, source), entry in sorted(approved.items()):
            canonical = entry["canonical"]
            teams = list(session.scalars(select(Team)))
            old = next((team for team in teams if team.name == source), None)
            target = next((team for team in teams if team.name == canonical), None)
            if old is None:
                continue
            row = dict(
                run_id=run_id,
                league=league,
                team_id=source,
                provisional_team_row_id=old.id,
                canonical=canonical,
                matches_rekeyed=0,
                alias_status=entry["status"],
                alias_source=json.dumps(
                    dict(
                        kind="owner_approved_fd_name",
                        source_entry=entry,
                        formal_aliases_sha256=aliases_sha,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                reason="",
            )
            try:
                games, technical, indices = _plan(session, old, target, league, source, canonical)
            except ValueError as exc:
                row["reason"] = str(exc)
                report["status"] = "partial"
                report["skipped_conflicts"] += 1
            else:
                # All prechecks precede mutation. Match IDs, payloads and versions stay intact.
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
                    raise ValueError("fd 合并后仍有比赛引用")
                report["identity_redirects"].append(
                    dict(
                        league=league,
                        source_name=source,
                        canonical=canonical,
                        old_team_id=old.id,
                        new_team_id=target.id,
                        approval_url=entry["approval_pr"],
                    )
                )
                # Foreign keys guard references from any other attached tables.
                session.delete(old)
                if source not in target.aliases:
                    target.aliases.append(source)
                session.flush()
                row.update(matches_rekeyed=len(games), reason="merged")
                for key, count in dict(
                    merged_teams=1,
                    rekeyed_matches=len(games),
                    deleted_teams=1,
                    rekeyed_stats=len(technical),
                    rekeyed_indices=len(indices),
                ).items():
                    report[key] += count
            write_audit(audit, [row])
    return report
