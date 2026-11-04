"""Conservative, read-only identity matching; only the attached index is written."""

import hashlib
import json
from collections import Counter
from datetime import timedelta

from sqlalchemy import select

from zucai.models import ApifFixtureIndex, Match
from zucai.predict.seasons import season_for
from zucai.sources.api_football import APIF_IDENTITY_LEAGUE_IDS, LEAGUES
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.apif_ingest import _Teams
from zucai.sources.apif_reconcile import _formal_entry
from zucai.sources.apif_stats import PARSER_VERSION, envelope, fixture_identity, instant
from zucai.sources.fd_ingest import normalize_team
from zucai.sources.football_data import read_json

INDEX_REQUEST_LEAGUES = APIF_IDENTITY_LEAGUE_IDS | {2}


def link_fixtures(session, raw, *, league_id, season, aliases, metadata, config=None):
    config = read_json(LEAGUES) if config is None else config
    if str(league_id) not in config:
        raise ValueError("索引联赛未配置")
    league = config[str(league_id)]["league"]
    rows = envelope(raw, "fixtures", {"league": league_id, "season": season})
    validate_approvals(aliases, require_metadata=True)
    lookup = _Teams(session, aliases)
    aliases_sha = hashlib.sha256(
        json.dumps(aliases, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    matches = list(session.scalars(select(Match).where(Match.league == league)))
    previous = {row.provider_fixture_id: row for row in session.scalars(select(ApifFixtureIndex))}
    claimed = {row.match_id: row.provider_fixture_id for row in previous.values()}
    planned, audit = [], []

    def reject(fixture_id, reason, detail="", candidates=()):
        audit.append(
            dict(
                provider_fixture_id=str(fixture_id),
                league=league,
                season=season,
                reason=reason,
                detail=detail,
                candidates=sorted(candidates),
                **metadata,
            )
        )

    ids = Counter(str(row.get("fixture", {}).get("id")) for row in rows)
    for row in rows:
        fixture_id = row.get("fixture", {}).get("id", "")
        try:
            identity = fixture_identity(row)
            fixture_id = identity["provider_fixture_id"]
            if identity["provider_league_id"] != league_id or identity["season"] != season:
                raise ValueError("fixture 联赛季与请求不一致")
            if league_id in INDEX_REQUEST_LEAGUES | {3, 848}:
                kickoff = identity["kickoff_utc"]
                if kickoff.year - (kickoff.month < 7) != season:
                    raise ValueError("fixture 开球不在声明的欧战/桥接联赛赛季")
        except ValueError as exc:
            reject(fixture_id, "候选不全", str(exc))
            continue
        if ids[fixture_id] != 1:
            reject(fixture_id, "多对一", "同一响应含重复 fixture ID")
            continue
        sides, evidence = {}, {}
        try:
            for side in ("home", "away"):
                pid = str(identity[f"{side}_provider_id"])
                formal, origins = _formal_entry(session, aliases, league, pid)
                team = lookup.one(normalize_team(formal["canonical"])) if formal else None
                if team is None or team.name.startswith(("apif:", "fdorg:", "fd:")):
                    raise ValueError("正式别名未映射到既有规范球队")
                binding = lookup.one(f"apif:{league}:team:{pid}")
                if binding is not None and binding.id != team.id:
                    raise ValueError("已有来源 ID 绑定与正式别名矛盾")
                sides[side] = team.id
                evidence[side] = dict(
                    source_leagues=origins,
                    status=formal["status"],
                    canonical=formal["canonical"],
                    kind="same_provider_id",
                )
            if sides["home"] == sides["away"]:
                raise ValueError("正式别名的主客身份相同")
        except ValueError as exc:
            reject(fixture_id, "身份临时", str(exc))
            continue
        kickoff = identity["kickoff_utc"]
        window = [
            m
            for m in matches
            if abs(instant(m.kickoff_utc) - kickoff) <= timedelta(hours=3)
            and season_for(m.kickoff_utc, league) == season_for(kickoff, league)
        ]
        direct = [
            m for m in window if (m.home_team_id, m.away_team_id) == (sides["home"], sides["away"])
        ]
        reverse = [
            m for m in window if (m.away_team_id, m.home_team_id) == (sides["home"], sides["away"])
        ]
        conflict = [
            m
            for m in window
            if m not in direct
            and m not in reverse
            and {m.home_team_id, m.away_team_id} & set(sides.values())
        ]
        reason = (
            "主客反向"
            if reverse
            else "同窗冲突"
            if conflict
            else "多对一"
            if len(direct) > 1
            else "候选不全"
            if not direct
            else None
        )
        if reason:
            reject(fixture_id, reason, candidates=[m.match_id for m in direct + reverse + conflict])
            continue
        match = direct[0]
        old = previous.get(fixture_id)
        if old and (
            old.match_id,
            old.home_provider_id,
            old.away_provider_id,
            old.season,
            old.provider_league_id,
        ) != (
            match.match_id,
            identity["home_provider_id"],
            identity["away_provider_id"],
            season,
            league_id,
        ):
            reject(fixture_id, "同窗冲突", "既有索引禁止重指向", [old.match_id, match.match_id])
            continue
        if match.match_id in claimed and claimed[match.match_id] != fixture_id:
            reject(fixture_id, "多对一", "目标已被其他 fixture 占用", [match.match_id])
            continue
        planned.append((identity, match, sides, evidence, old))
    counts = Counter(match.match_id for _, match, _, _, _ in planned)
    linked, unchanged = 0, 0
    for identity, match, sides, evidence, old in planned:
        if counts[match.match_id] > 1:
            reject(
                identity["provider_fixture_id"],
                "多对一",
                "整批多个 fixture 对应同一比赛",
                [match.match_id],
            )
        elif old:
            unchanged += 1
        else:
            fields = {key: value for key, value in identity.items() if key != "status"}
            session.add(
                ApifFixtureIndex(
                    **fields,
                    match_id=match.match_id,
                    league=league,
                    home_team_id=sides["home"],
                    away_team_id=sides["away"],
                    **metadata,
                    parser_version=PARSER_VERSION,
                    evidence={**evidence, "aliases_sha256": aliases_sha},
                )
            )
            linked += 1
    session.flush()
    return dict(linked=linked, unchanged=unchanged, rejected=len(audit), audit=audit)
