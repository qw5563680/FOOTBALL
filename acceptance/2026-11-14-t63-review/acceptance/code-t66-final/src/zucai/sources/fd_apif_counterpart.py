"""Guarded APIF-to-FD fixture links; never change provider teams or result evidence."""

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select

from zucai.models import Match, Team
from zucai.sources.fd_counterpart import WINDOW, CounterpartConflict
from zucai.sources.teams import normalize_team

_RESERVED = ("apif:", "fdorg:", "fd:", "jc:")
_COLUMNS = (
    "match_id",
    "source",
    "league",
    "home_team_id",
    "away_team_id",
    "kickoff_utc",
    "counterpart_id",
    "result_provider",
)


@dataclass(frozen=True)
class FixtureIdentity:
    match_id: str
    source: str
    league: str
    home_team_id: int | str | None
    away_team_id: int | str | None
    kickoff_utc: datetime | str
    counterpart_id: str | None = None
    result_provider: str | None = None


def _utc(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _pair(match):
    return match.home_team_id, match.away_team_id


def _window(left, right):
    return (
        left.league == right.league
        and abs(_utc(left.kickoff_utc) - _utc(right.kickoff_utc)) <= WINDOW
    )


def _neighbors(match, rows, source):
    teams = set(_pair(match)) - {None}
    return [
        row
        for row in rows
        if row.source == source
        and row.match_id != match.match_id
        and _window(match, row)
        and teams.intersection(_pair(row))
    ]


def _canonical(name):
    return (
        isinstance(name, str) and bool(name.strip()) and not name.casefold().startswith(_RESERVED)
    )


def same_fixture_identity(fd, api, names):
    if not _window(fd, api):
        return False
    api_names = [names.get(team) for team in _pair(api)]
    fd_names = [names.get(team) for team in _pair(fd)]
    if not all(_canonical(name) for name in fd_names):
        return False
    unknown = [bool(re.fullmatch(r"apif:team:[1-9][0-9]*", name or "")) for name in api_names]
    if not any(unknown):
        return all(_canonical(name) for name in api_names) and _pair(fd) == _pair(api)
    if sum(unknown) != 1:
        return False
    known = 1 if unknown[0] else 0
    return _canonical(api_names[known]) and _pair(fd)[known] == _pair(api)[known]


def find_apif_candidate(fd, rows, names, *, incoming=(), fd_bound=None, aliases=None):
    """Check both directions and every claim, with at most one row per source.

    Shared teams only veto alternatives. A link needs the exact oriented pair,
    or one opaque APIF side and the other exact canonical ID in the same role.
    Incoming FD identities participate before any row can win by import order.
    """
    candidates = _neighbors(fd, rows, "apif")
    claims = [row for row in rows if row.source == "apif" and row.counterpart_id == fd.match_id]
    if not candidates:
        if claims:
            raise CounterpartConflict(
                "既有 apif→fd 链接身份或开球窗口不符", [r.match_id for r in claims]
            )
        return None
    if (
        fd_bound is not None
        and not claims
        and all(
            api.counterpart_id is None
            and all(_canonical(names.get(team)) for team in _pair(api))
            and any(
                fd_bound[side]
                and not fd_bound[1 - side]
                and _pair(fd)[side] == _pair(api)[side]
                and _pair(fd)[1 - side] != _pair(api)[1 - side]
                for side in (0, 1)
            )
            for api in candidates
        )
    ):
        # FD has always retained unbound raw-name teams. Do not turn an identity
        # gap on that side into either a guessed link or a new admission veto.
        return None
    ids = [row.match_id for row in candidates]
    if len(candidates) != 1 or not same_fixture_identity(fd, candidates[0], names):
        raise CounterpartConflict("apif 候选不唯一、主客冲突或已知球队不符", ids)
    api = candidates[0]
    if getattr(api, "result_provider", None) not in (None, "apif"):
        raise CounterpartConflict("apif 候选的赛果来源与自身来源不符", [api.match_id])
    if aliases is not None:
        owners = defaultdict(set)
        for team, name in names.items():
            for value in (name, *aliases.get(team, ())):
                owners[normalize_team(value).casefold()].add(team)
        pattern = rf"apif:{re.escape(api.league)}:team:[1-9][0-9]*"
        for side, team in enumerate(_pair(api)):
            name = names[team]
            if _canonical(name):
                bindings = [
                    value for value in aliases.get(team, ()) if re.fullmatch(pattern, value)
                ]
                if len(bindings) != 1 or owners[bindings[0].casefold()] != {team}:
                    raise CounterpartConflict(
                        "apif 已知球队缺少唯一的本联赛来源绑定", [api.match_id]
                    )
            else:
                binding = f"apif:{api.league}:team:{name.rsplit(':', 1)[1]}".casefold()
                if owners[name.casefold()] != {team} or (
                    owners[binding] and owners[binding] != {_pair(fd)[side]}
                ):
                    raise CounterpartConflict("apif 临时球队已有相反或多个来源绑定", [api.match_id])
    others = {row.match_id for row in _neighbors(api, [*rows, *incoming], "fd")}
    if others != {fd.match_id}:
        raise CounterpartConflict("apif 对应多个或反向 fd 候选", sorted(others | {fd.match_id}))
    if api.counterpart_id not in (None, fd.match_id):
        raise CounterpartConflict(
            "apif 已有不同 counterpart 链接", [api.match_id, api.counterpart_id]
        )

    # An independently verified FDORG sibling may coexist with APIF on the FD anchor.
    siblings = [
        row
        for row in rows
        if row.source == "fdorg"
        and (row.counterpart_id == fd.match_id or fd.counterpart_id == row.match_id)
    ]
    if len(siblings) > 1:
        raise CounterpartConflict("fd 已有多个 fdorg 声明", [row.match_id for row in siblings])
    allowed = {fd.match_id: {api.match_id}, api.match_id: {fd.match_id}}
    if siblings:
        sibling = siblings[0]
        if (
            not _window(fd, sibling)
            or _pair(fd) != _pair(sibling)
            or sibling.counterpart_id not in (None, fd.match_id)
            or {row.match_id for row in _neighbors(sibling, rows, "fd")} != {fd.match_id}
            or {row.match_id for row in _neighbors(fd, rows, "fdorg")} != {sibling.match_id}
        ):
            raise CounterpartConflict("apif→fd 的 fdorg 同场声明无法唯一复核", [sibling.match_id])
        allowed[fd.match_id].add(sibling.match_id)
        allowed[sibling.match_id] = {fd.match_id}
    for row in rows:
        if row.match_id in allowed and row.counterpart_id not in (None, *allowed[row.match_id]):
            raise CounterpartConflict(
                "apif→fd 端点已有不同 counterpart", [row.match_id, row.counterpart_id]
            )
        if row.counterpart_id in allowed and row.match_id not in allowed[row.counterpart_id]:
            raise CounterpartConflict("apif→fd 端点已被其他比赛声明", [row.match_id])
    return api


def apif_candidate(session, fd, *, incoming=(), fd_bound=None):
    """Read the neighborhood plus out-of-window claims without writing evidence."""
    when = _utc(fd.kickoff_utc)
    rows = list(
        session.scalars(
            select(Match).where(
                or_(
                    (Match.league == fd.league)
                    & Match.kickoff_utc.between(when - 2 * WINDOW, when + 2 * WINDOW),
                    Match.counterpart_id == fd.match_id,
                )
            )
        )
    )
    ids = {row.match_id for row in rows} | {fd.match_id}
    pointers = {row.counterpart_id for row in rows if row.counterpart_id}
    related = list(
        session.scalars(
            select(Match).where(or_(Match.counterpart_id.in_(ids), Match.match_id.in_(pointers)))
        )
    )
    indexed = {row.match_id: row for row in [*rows, *related, fd]}
    # An isolated Team can still own a conflicting provider binding. Only
    # actual APIF candidates need this full owner inventory.
    teams = (
        list(session.scalars(select(Team)))
        if _neighbors(fd, list(indexed.values()), "apif")
        else []
    )
    names = {team.id: team.name for team in teams}
    candidate = find_apif_candidate(
        fd,
        list(indexed.values()),
        names,
        incoming=incoming,
        fd_bound=fd_bound,
        aliases={team.id: team.aliases for team in teams},
    )
    return indexed[candidate.match_id] if candidate else None


def check_linked_observation(session, existing, game, pair):
    """Only a stored FD pointer permits the narrow partial-identity reimport path."""
    fd = session.get(Match, existing.counterpart_id)
    if (
        existing.source != "apif"
        or existing.league != game.league
        or _pair(existing) != pair
        or abs(_utc(existing.kickoff_utc) - game.kickoff_utc) > WINDOW
        or fd is None
        or fd.source != "fd"
        or fd.league != game.league
        or abs(_utc(fd.kickoff_utc) - game.kickoff_utc) > WINDOW
    ):
        raise CounterpartConflict("既有 apif→fd 链接与当前观察不符", [existing.match_id])
    candidate = apif_candidate(session, fd)
    if candidate is None or candidate.match_id != existing.match_id:
        raise CounterpartConflict("既有 apif→fd 链接无法唯一复核", [existing.match_id])
    return fd


def verified_training_links(session, match_ids):
    """Validate relevant explicit links before a finished/source filter hides anchors."""
    present = set(match_ids)
    pairs = set()
    for api in session.scalars(
        select(Match).where(Match.source == "apif", Match.counterpart_id.like("fd:%"))
    ):
        if api.match_id not in present:
            continue
        fd = session.get(Match, api.counterpart_id)
        if fd is None or fd.source != "fd":
            raise CounterpartConflict("apif→fd 链接目标不存在或来源错误", [api.match_id])
        candidate = apif_candidate(session, fd)
        if candidate is None or candidate.match_id != api.match_id:
            raise CounterpartConflict("apif→fd 链接无法唯一复核", [api.match_id])
        pairs.add((api.match_id, fd.match_id))
    return sorted(pairs)


def validate_apif_fd_links(db):
    """Export/restore must recheck explicit links against the complete stored graph."""
    if not db.execute(
        "SELECT 1 FROM matches WHERE source='apif' AND counterpart_id LIKE 'fd:%' LIMIT 1"
    ).fetchone():
        return
    rows = [
        FixtureIdentity(*row) for row in db.execute(f"SELECT {','.join(_COLUMNS)} FROM matches")
    ]
    indexed = {row.match_id: row for row in rows}
    teams = list(db.execute("SELECT id,name,aliases FROM teams"))
    names = {row[0]: row[1] for row in teams}
    aliases = {row[0]: json.loads(row[2]) for row in teams}
    for api in rows:
        if api.source != "apif" or not (api.counterpart_id or "").startswith("fd:"):
            continue
        fd = indexed.get(api.counterpart_id)
        if fd is None or fd.source != "fd":
            raise CounterpartConflict("apif→fd 链接目标不存在或来源错误", [api.match_id])
        candidate = find_apif_candidate(fd, rows, names, aliases=aliases)
        if candidate is None or candidate.match_id != api.match_id:
            raise CounterpartConflict("apif→fd 链接无法唯一复核", [api.match_id])
