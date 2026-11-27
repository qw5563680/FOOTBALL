"""Read-only identity checks for a 500-root JC fixture with no official row."""

import re
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import or_, select

from zucai.models import Match
from zucai.nations import LEAGUE as NATIONS_LEAGUE
from zucai.nations import sfc_team
from zucai.sfc_club_names import _table as _approved_club_entries
from zucai.sfc_club_names import club_name
from zucai.sources.sfc import _find_team, _league_key
from zucai.sources.sporttery import BEIJING
from zucai.sources.teams import normalize_team

_SIDE_LABELS = {"home": "主队", "away": "客队"}
_PROVIDER_ID = re.compile(r"[1-9][0-9]*")
_TEMPORARY_TEAM = re.compile(r"[a-z][a-z0-9_-]*:team:[0-9]+", re.I)


@dataclass(frozen=True)
class FixtureIdentity:
    league: str
    home_team_id: int
    away_team_id: int
    evidence: dict


class FixtureRejection(ValueError):
    def __init__(self, code, reason, *, details=None):
        super().__init__(reason)
        self.code = code
        self.details = {} if details is None else details


def _reject_teams(gaps):
    summary = [
        f"{_SIDE_LABELS[row['side']]}「{row['label'] or '缺失'}」"
        f"（500 网球队编号 {row['provider_team_id'] or '缺失'}）{row['reason']}"
        for row in gaps
    ]
    raise FixtureRejection("team_unverified", "；".join(summary), details={"teams": gaps})


def _team(session, league, provider_id, label):
    if not _PROVIDER_ID.fullmatch(provider_id) or not label or "\ufffd" in label:
        raise ValueError("缺少有效球队编号或简称")
    if league == NATIONS_LEAGUE:
        candidate, basis = sfc_team(provider_id, label), "approved_national_team_id"
    else:
        entry = _approved_club_entries().get((league, provider_id))
        approved = club_name(league, provider_id, label)
        if (
            entry is not None
            and approved is None
            and normalize_team(label).casefold() != entry["canonical"].casefold()
        ):
            raise ValueError("与该编号已获批的球队冲突")
        candidate = approved if approved is not None else label
        basis = "approved_club_short_name" if approved is not None else "exact_team_name"
    team = _find_team(session, candidate)
    if team is None or _TEMPORARY_TEAM.fullmatch(team.name):
        raise ValueError("没有已核实的库内球队")
    if normalize_team(candidate).casefold() != team.name.casefold():
        # General aliases alone do not prove a new fixture. The SFC route
        # also uses them when ordered teams, league and kickoff match a stored fixture.
        basis = "verified_same_fixture_alias"
    return team, basis


def _batch_conflicts(row, batch_rows):
    if any(
        sum(getattr(other, field) == getattr(row, field) for other in batch_rows) > 1
        for field in ("match_id", "provider_fixture_id")
    ):
        raise FixtureRejection("fixture_conflict", "500 网同批比赛编号重复")
    provider_ids = {row.home_id, row.away_id} - {""}
    names = {row.home, row.away} - {""}
    conflicts = [
        other.match_id
        for other in batch_rows
        if other.match_id != row.match_id
        and other.kickoff_utc == row.kickoff_utc
        and (
            provider_ids.intersection((other.home_id, other.away_id))
            or (
                _league_key(other.league) == _league_key(row.league)
                and names.intersection((other.home, other.away))
            )
        )
    ]
    if conflicts:
        raise FixtureRejection(
            "kickoff_conflict",
            "500 网同批相同球队的开球时间冲突",
            details={"conflicting_matches": sorted(set(conflicts))},
        )


def _has_sides(match, league, ordered_teams):
    return (_league_key(match.league), match.home_team_id, match.away_team_id) == (
        league,
        *ordered_teams,
    )


def _fixture_witnesses(session, regular, row, league, ordered_teams):
    own_id = f"c500:{row.provider_fixture_id}"
    own = session.get(Match, own_id)
    nearby = list(
        session.scalars(
            select(Match).where(
                *regular,
                or_(
                    Match.counterpart_id == own_id,
                    Match.match_id == (own.counterpart_id if own else None),
                    (Match.kickoff_utc == row.kickoff_utc)
                    & or_(
                        Match.home_team_id.in_(ordered_teams),
                        Match.away_team_id.in_(ordered_teams),
                    ),
                ),
            )
        )
    )
    other_official = {match.match_id for match in nearby if match.source == "jc"}
    if own and own.counterpart_id and own.counterpart_id.startswith("jc:"):
        other_official.add(own.counterpart_id)
    other_official.discard(row.match_id)
    if other_official:
        raise FixtureRejection(
            "fixture_conflict",
            "库内已有另一场体彩比赛，须核对官方比赛编号",
            details={"conflicting_matches": sorted(other_official)},
        )
    if own is not None:
        nearby.append(own)
    conflict = sorted(
        match.match_id
        for match in nearby
        if not _has_sides(match, league, ordered_teams)
        or datetime.fromisoformat(match.kickoff_utc) != row.kickoff_utc
    )
    if conflict:
        raise FixtureRejection(
            "kickoff_conflict",
            "与库内比赛的开球时间、主客或联赛冲突",
            details={"conflicting_matches": conflict},
        )
    return sorted(match.match_id for match in nearby if match.source != "c500")


def resolve_fixture(session, row, batch_rows, *, backup_ids=()) -> FixtureIdentity:
    """Resolve existing identities using the SFC evidence rules without any writes."""
    if row.problem or not _PROVIDER_ID.fullmatch(row.provider_fixture_id):
        raise FixtureRejection("fixture_unverified", row.problem or "500 网比赛编号缺失或无效")
    if row.kickoff_utc is None or row.kickoff_utc.utcoffset() is None:
        raise FixtureRejection("kickoff_unverified", "500 网开球时间缺失或无法核实")
    _batch_conflicts(row, list(batch_rows))
    league = _league_key(row.league)
    regular = (
        Match.source != "c500",
        Match.status != "cancelled",
        Match.match_id.not_in(tuple(backup_ids)),
    )
    if not league or not session.scalar(
        select(Match.match_id).where(*regular, Match.league == league).limit(1)
    ):
        raise FixtureRejection(
            "league_unverified",
            f"500 网赛事「{row.league or '缺失'}」没有已核实的联赛对应",
            details={"source_league": row.league},
        )

    teams, side_evidence, gaps = {}, {}, []
    for side in _SIDE_LABELS:
        label, provider_id = getattr(row, side), getattr(row, f"{side}_id")
        evidence = dict(side=side, label=label, provider_team_id=provider_id)
        try:
            team, basis = _team(session, league, provider_id, label)
        except ValueError as error:
            gaps.append(dict(evidence, reason=str(error)))
        else:
            teams[side] = team.id
            side_evidence[side] = dict(evidence, basis=basis, canonical=team.name)
    if gaps:
        _reject_teams(gaps)
    ordered_teams = (teams["home"], teams["away"])
    if len(set(ordered_teams)) != 2 or row.home_id == row.away_id:
        _reject_teams(
            [
                dict(evidence, reason="与另一边的球队编号或身份重复")
                for evidence in side_evidence.values()
            ]
        )

    witnesses = _fixture_witnesses(session, regular, row, league, ordered_teams)
    if row.kickoff_utc.astimezone(BEIJING).time() == time.min and not witnesses:
        raise FixtureRejection(
            "kickoff_placeholder", "北京时间 00:00 可能是占位时刻，库内无同场证据"
        )
    for side, team_id in teams.items():
        evidence = side_evidence[side]
        witness = (
            witnesses[0]
            if witnesses
            else session.scalar(
                select(Match.match_id)
                .where(
                    *regular,
                    Match.league == league,
                    or_(Match.home_team_id == team_id, Match.away_team_id == team_id),
                )
                .order_by(Match.kickoff_utc.desc(), Match.match_id)
                .limit(1)
            )
        )
        if witness is None:
            gaps.append(dict(evidence, reason="没有该赛事的既有球队记录"))
        elif evidence["basis"] == "verified_same_fixture_alias" and not witnesses:
            gaps.append(dict(evidence, reason="简称缺少核准对照及同场比赛证据"))
        else:
            evidence["match_witness"] = witness
    if gaps:
        _reject_teams(gaps)
    return FixtureIdentity(
        league,
        *ordered_teams,
        {"source_league": row.league, "teams": side_evidence, "fixture_witnesses": witnesses},
    )
