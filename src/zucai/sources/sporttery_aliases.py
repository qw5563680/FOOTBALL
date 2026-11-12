"""Reviewed exact JC names with explicit competition or national-team scope."""

import json
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path

from sqlalchemy import select

from zucai.db import session_scope
from zucai.models import Team
from zucai.sources.base import _atomic_write
from zucai.sources.football_data import CONFIG_ROOT, read_json
from zucai.sources.teams import normalize_team, upsert_team

ALIASES_PATH = CONFIG_ROOT / "team_aliases/sporttery.json"
NATIONAL_TEAM_COMPETITIONS = frozenset({"欧国联", "友谊赛", "世预赛", "欧洲杯", "世界杯"})


def load_aliases(path=None):
    aliases = read_json(path if path is not None else ALIASES_PATH)
    if not isinstance(aliases, dict) or any(
        not isinstance(league, str)
        or not isinstance(entries, dict)
        or any(
            not isinstance(name, str) or not isinstance(row, dict) for name, row in entries.items()
        )
        for league, entries in aliases.items()
    ):
        raise ValueError("竞彩别名配置须按联赛和精确简称组织")
    return aliases


def approved_canonical(aliases, league, raw_name):
    """Use exact approved keys; team scope applies only in listed national competitions."""
    canonical_names = set()
    for configured_league, names in aliases.items():
        entry = names.get(raw_name)
        if entry is None or entry.get("status") != "approved":
            continue
        scope = entry.get("scope", "competition")
        if configured_league != league and not (
            scope == "national_team" and league in NATIONAL_TEAM_COMPETITIONS
        ):
            continue
        if scope not in {"competition", "national_team"} or (
            scope == "national_team" and configured_league not in NATIONAL_TEAM_COMPETITIONS
        ):
            raise ValueError(f"竞彩核准别名的适用范围无效：{configured_league}/{raw_name}")
        canonical = entry.get("canonical")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError(f"竞彩核准别名缺少规范名：{configured_league}/{raw_name}")
        if normalize_team(canonical) != canonical:
            raise ValueError(f"竞彩核准别名的规范名未规范化：{configured_league}/{raw_name}")
        canonical_names.add(canonical)
    if len(canonical_names) > 1:
        raise ValueError(f"竞彩核准别名的适用范围冲突：{league}/{raw_name}")
    return next(iter(canonical_names), None)


def resolve_teams(session, game, captured, *, aliases=None, candidates=None):
    """Reuse approved canonical names without adding a globally active short alias."""
    aliases = load_aliases() if aliases is None else aliases
    known = list(session.scalars(select(Team)))
    existing = {team.id for team in known}
    teams = []
    for side, raw_name in (("home", game.home), ("away", game.away)):
        canonical = approved_canonical(aliases, game.league, raw_name)
        if canonical is not None:
            owners = {
                team.id
                for team in known
                if any(
                    normalize_team(name).casefold() == canonical.casefold()
                    for name in (team.name, *(team.aliases or []))
                )
            }
            if len(owners) > 1:
                raise ValueError(f"竞彩规范队名存在多个既有身份：{canonical}")
        team = upsert_team(session, canonical if canonical is not None else raw_name, "jc")
        if canonical is None and candidates is not None and team.id not in existing:
            candidates.append(
                dict(
                    league=game.league,
                    raw_name=raw_name,
                    team_id=team.id,
                    evidence=dict(
                        match_id=game.match_id,
                        side=side,
                        kickoff_utc=game.kickoff_utc.astimezone(UTC)
                        .isoformat(timespec="microseconds")
                        .replace("+00:00", "Z"),
                        captured_utc=captured.astimezone(UTC)
                        .isoformat(timespec="microseconds")
                        .replace("+00:00", "Z"),
                    ),
                )
            )
            existing.add(team.id)
        teams.append(team)
    return tuple(teams)


def save_candidates(database, pending):
    """Write review-only records; the caller coordinates rollback with the database."""
    if not pending:
        return
    target = Path(database).parent / "state/alias_candidates/jc.json"
    candidates = read_json(target) if target.exists() else {}
    for row in pending:
        entry = candidates.setdefault(row["league"], {}).setdefault(
            row["raw_name"],
            dict(
                source="jc", canonical=None, status="pending", team_id=row["team_id"], evidence=[]
            ),
        )
        if entry["team_id"] != row["team_id"]:
            raise ValueError("竞彩简称候选对应多个球队身份，须人工核验")
        if row["evidence"] not in entry["evidence"]:
            entry["evidence"].append(row["evidence"])
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        target,
        (json.dumps(candidates, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )


@contextmanager
def candidate_transaction(engine):
    """Roll back new identities on queue failure, and restore the queue on commit failure."""
    target = Path(engine.url.database).parent / "state/alias_candidates/jc.json"
    previous = None
    attempted = False
    try:
        with session_scope(engine) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            previous = target.read_bytes() if target.exists() else None
            candidates = []
            yield session, candidates
            if candidates:
                attempted = True
                save_candidates(engine.url.database, candidates)
    except BaseException:
        if attempted and (target.read_bytes() if target.exists() else None) != previous:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_write(target, previous)
        raise
