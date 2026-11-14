"""Conservative, read-only identity matching; only the attached index is written."""

import hashlib
import json
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import select

from zucai.models import ApifFixtureIndex, Match
from zucai.sources.api_football import APIF_INDEX_LEAGUE_IDS, LEAGUES
from zucai.sources.apif_aliases import binding_scope_allows, validate_approvals
from zucai.sources.apif_ingest import _Teams
from zucai.sources.apif_reconcile import _formal_entry
from zucai.sources.apif_stats import PARSER_VERSION, envelope, fixture_identity, instant
from zucai.sources.fd_ingest import normalize_team
from zucai.sources.football_data import read_json
from zucai.stats_match_identity import matches_provider_fixture, verified_match_groups

INDEX_REQUEST_LEAGUES = APIF_INDEX_LEAGUE_IDS
CALENDAR_LEAGUES = {292, 113, 103, 244, 253, 169}
SOURCE_PRIORITY = {"jc": 0, "fd": 1, "fdorg": 2, "apif": 3}


def provider_season(kickoff, league_id):
    if league_id in CALENDAR_LEAGUES:
        return kickoff.year
    if league_id == 98:
        # APIF uses the ending year after J1's 2026 transition (current_season=2027).
        if (kickoff.year, kickoff.month) >= (2026, 8):
            return kickoff.year + (kickoff.month >= 8)
        return kickoff.year
    if league_id in INDEX_REQUEST_LEAGUES | {3, 848, 119, 188}:
        return kickoff.year - (kickoff.month < 7)
    raise ValueError("联赛的供应商赛季规则未配置")


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
    groups = verified_match_groups(session)

    def group(match_id):
        return groups.get(match_id, frozenset({match_id}))

    previous = {row.provider_fixture_id: row for row in session.scalars(select(ApifFixtureIndex))}
    claimed = defaultdict(set)
    for row in previous.values():
        claimed[group(row.match_id)].add(row.provider_fixture_id)
    planned, audit, preferred, coalesced = [], [], {}, {}

    def record_audit(fixture_id, reason, detail="", candidates=(), *, outcome="rejected"):
        audit.append(
            dict(
                provider_fixture_id=str(fixture_id),
                league=league,
                season=season,
                reason=reason,
                outcome=outcome,
                detail=detail,
                candidates=sorted(candidates),
                **metadata,
            )
        )

    prepared, contexts = [], {}
    for row in rows:
        fixture_id = row["fixture"].get("id", "") if isinstance(row.get("fixture"), dict) else ""
        try:
            identity = fixture_identity(row)
            fixture_id = identity["provider_fixture_id"]
            if identity["provider_league_id"] != league_id or identity["season"] != season:
                raise ValueError("fixture 联赛季与请求不一致")
            if provider_season(identity["kickoff_utc"], league_id) != season:
                raise ValueError("fixture 开球不在声明的供应商联赛赛季")
        except ValueError as exc:
            record_audit(fixture_id, "候选不全", str(exc))
            continue
        prepared.append(identity)
        contexts[fixture_id] = {key: row[key] for key in ("fixture", "league", "teams")}
    ids = Counter(
        str(row["fixture"].get("id")) for row in rows if isinstance(row.get("fixture"), dict)
    )
    for identity in prepared:
        fixture_id = identity["provider_fixture_id"]
        if ids[fixture_id] != 1:
            record_audit(fixture_id, "多对一", "同一响应含重复 fixture ID")
            continue
        pair = (identity["home_provider_id"], identity["away_provider_id"])
        incoming = []
        for other in prepared:
            other_pair = (other["home_provider_id"], other["away_provider_id"])
            if other["provider_fixture_id"] == fixture_id or abs(
                other["kickoff_utc"] - identity["kickoff_utc"]
            ) > timedelta(hours=3):
                continue
            if set(pair) & set(other_pair):
                incoming.append(
                    "主客反向"
                    if pair == other_pair[::-1]
                    else "多对一"
                    if pair == other_pair
                    else "同窗冲突"
                )
        if incoming:
            reason = next(r for r in ("主客反向", "同窗冲突", "多对一") if r in incoming)
            record_audit(fixture_id, reason, "整批供应商 fixture 存在同窗身份冲突")
            continue
        sides, evidence = {}, {}
        try:
            for side in ("home", "away"):
                pid = str(identity[f"{side}_provider_id"])
                formal, origins = _formal_entry(session, aliases, league, pid)
                if formal and not binding_scope_allows(
                    formal, league, kickoff_utc=identity["kickoff_utc"].isoformat()
                ):
                    raise ValueError("binding_scope does not authorize this fixture time/league")
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
            record_audit(fixture_id, "身份临时", str(exc))
            continue
        kickoff = identity["kickoff_utc"]
        window = [
            m
            for m in matches
            if abs(instant(m.kickoff_utc) - kickoff) <= timedelta(hours=3)
            and provider_season(instant(m.kickoff_utc), league_id) == season
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
        preferred_candidates = []
        old = previous.get(fixture_id)
        if (
            len(direct) > 1
            and not reverse
            and not conflict
            and len({m.source for m in direct}) == len(direct)
            and len({group(m.match_id) for m in direct}) == 1
        ):
            coalesced[fixture_id] = [m.match_id for m in direct]
            # Previously attached technical rows keep their immutable target.
            # Only a new index selects the usual source representative.
            pinned = next((m for m in direct if old and m.match_id == old.match_id), None)
            direct = [
                pinned or min(direct, key=lambda m: (SOURCE_PRIORITY.get(m.source, 99), m.match_id))
            ]
        if (
            len(direct) == 2
            and not reverse
            and not conflict
            and {m.source for m in direct} == {"fd", "fdorg"}
            and instant(direct[0].kickoff_utc) == instant(direct[1].kickoff_utc)
        ):
            preferred_candidates = [m.match_id for m in direct]
            direct = [m for m in direct if m.source == "fd"]
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
            record_audit(
                fixture_id, reason, candidates=[m.match_id for m in direct + reverse + conflict]
            )
            continue
        match = direct[0]
        if not matches_provider_fixture(groups.get(match.match_id, ()), fixture_id):
            record_audit(
                fixture_id,
                "同窗冲突",
                "同场证明中的 APIF fixture ID 与原件不符",
                group(match.match_id),
            )
            continue
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
            record_audit(
                fixture_id, "同窗冲突", "既有索引禁止重指向", [old.match_id, match.match_id]
            )
            continue
        if claimed[group(match.match_id)] - {fixture_id}:
            record_audit(
                fixture_id, "多对一", "同场组件已被其他 fixture 占用", group(match.match_id)
            )
            continue
        if preferred_candidates:
            preferred[fixture_id] = preferred_candidates
        planned.append((identity, match, sides, evidence, old))
    counts = Counter(group(match.match_id) for _, match, _, _, _ in planned)
    linked, unchanged = 0, 0
    for identity, match, sides, evidence, old in planned:
        if counts[group(match.match_id)] > 1:
            record_audit(
                identity["provider_fixture_id"],
                "多对一",
                "整批多个 fixture 对应同一比赛",
                group(match.match_id),
            )
            continue
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
                    evidence={
                        **evidence,
                        "aliases_sha256": aliases_sha,
                        "fixture": contexts[identity["provider_fixture_id"]],
                    },
                )
            )
            linked += 1
        if identity["provider_fixture_id"] in preferred:
            record_audit(
                identity["provider_fixture_id"],
                "preferred_fd",
                f"同场 fd/fdorg 双行优先关联 {match.match_id}",
                preferred[identity["provider_fixture_id"]],
                outcome="linked",
            )
        if identity["provider_fixture_id"] in coalesced:
            record_audit(
                identity["provider_fixture_id"],
                "verified_same_match",
                f"有效同场证明保留索引目标 {match.match_id}",
                coalesced[identity["provider_fixture_id"]],
                outcome="linked",
            )
    session.flush()
    return dict(
        linked=linked,
        unchanged=unchanged,
        rejected=sum(row["outcome"] == "rejected" for row in audit),
        audit=audit,
    )
