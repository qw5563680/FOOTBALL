"""Import football-data.org observations with ID-based teams and official-first linking."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from zucai.db import upsert_match
from zucai.models import Match, Result, Team
from zucai.sources.fd_counterpart import CounterpartConflict, fdorg_candidate
from zucai.sources.fd_ingest import WINDOW, _canonical
from zucai.sources.results import GameResult, apply_result
from zucai.sources.teams import normalize_team

COUNT_KEYS = (
    "inserted",
    "updated",
    "corrected",
    "availability_corrected",
    "availability_quarantined",
    "unchanged",
    "cancelled",
    "skipped",
    "linked_existing",
    "linked_fd",
    "rejected_rows",
    "suspected_duplicates",
    "assumed",
)

SCHEDULED = {"SCHEDULED", "TIMED"}
CANCELLED = {"POSTPONED", "CANCELLED"}
LIVE = {"IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT", "SUSPENDED"}
STORED_STATUS = {
    "SCHEDULED": "scheduled",
    "TIMED": "scheduled",
    "FINISHED": "finished",
    "POSTPONED": "cancelled",
    "CANCELLED": "cancelled",
}


class _Suspected(ValueError):
    def __init__(self, reason, candidates):
        super().__init__(reason)
        self.candidates = sorted(set(candidates))


@dataclass(frozen=True)
class _Side:
    name: str
    team: Team | None
    binding: str | None  # Only active, league-scoped provider IDs can link external fixtures.


class _Teams:
    """Look up canonical and source-qualified identities without fuzzy name matching."""

    def __init__(self, session, aliases):
        self.owners = defaultdict(dict)
        for team in session.scalars(select(Team)):
            self.remember(team)
        self.aliases = aliases
        self.collisions = set()
        if not isinstance(aliases, dict):
            raise ValueError("别名配置必须按联赛和数字ID组织")
        for league, entries in aliases.items():
            if not isinstance(entries, dict):
                raise ValueError(f"联赛别名配置无效：{league}")
            reverse = defaultdict(list)
            for key in entries:
                if (
                    not isinstance(key, str)
                    or not key.isascii()
                    or not key.isdecimal()
                    or key.startswith("0")
                ):
                    continue
                try:
                    name = _canonical(aliases, league, key)
                    if name:
                        owner = self.one(name)
                        reverse[owner.id if owner else name.casefold()].append(key)
                except ValueError:
                    continue  # Resolve rejects just the rows that reference this malformed entry.
            for keys in reverse.values():
                if len(keys) > 1:
                    self.collisions.update((league, key) for key in keys)

    def remember(self, team):
        for name in (team.name, *(team.aliases or [])):
            self.owners[normalize_team(name).casefold()][team.id] = team

    def one(self, label):
        found = self.owners.get(label.casefold(), {})
        if len(found) > 1:
            raise ValueError(f"球队名称或来源ID有多个既有身份：{label}")
        return next(iter(found.values()), None)

    def resolve(self, session, league, provider_id):
        if (league, str(provider_id)) in self.collisions:
            raise ValueError("同一规范球队对应多个生效的来源球队ID")
        canonical = _canonical(self.aliases, league, str(provider_id))
        provisional = f"fdorg:team:{provider_id}"
        prefix = f"fdorg:{league}:team:"
        binding = f"{prefix}{provider_id}"
        previous = self.one(binding)
        independent = self.one(provisional)
        if independent and independent.name != provisional:
            raise ValueError(f"来源保留队名已被用作别名：{provisional}")
        if canonical is None:
            if previous:
                raise ValueError(f"已有生效的球队ID关联，新别名未获核准：{binding}")
            return _Side(provisional, independent, None)
        if canonical.casefold().startswith("fdorg:"):
            raise ValueError(f"规范队名使用了来源ID保留前缀：{canonical}")
        team = self.one(canonical)
        if previous and team != previous:
            raise ValueError(f"来源球队ID的规范身份发生变化：{binding}")
        if team and any(a.startswith(prefix) and a != binding for a in team.aliases):
            raise ValueError(f"规范队名已有不同的来源球队ID：{canonical}")
        if independent and session.scalar(
            select(Match.match_id)
            .where(
                Match.league == league,
                or_(Match.home_team_id == independent.id, Match.away_team_id == independent.id),
            )
            .limit(1)
        ):
            raise ValueError(f"{binding} 在本联赛已有独立球队身份，须先核对存量比赛")
        return _Side(canonical, team, binding)

    def sides(self, session, game, unknown):
        sides = []
        for provider_id, raw in (
            (game.home_provider_id, game.home_raw),
            (game.away_provider_id, game.away_raw),
        ):
            side = self.resolve(session, game.league, provider_id)
            if side.binding is None:
                unknown[game.league, provider_id] = raw
            sides.append(side)
        home, away = sides
        if home.name.casefold() == away.name.casefold() or (home.team and home.team == away.team):
            raise ValueError("两个来源球队ID指向同一球队")
        return sides


def _materialize(session, side):
    team = side.team
    if team is None:
        team = Team(name=side.name, aliases=[])
        session.add(team)
    if side.binding and side.binding not in team.aliases:
        team.aliases.append(side.binding)
    return team


def _validate(game, seen):
    if game.provider != "fdorg" or any(
        type(value) is not int or value <= 0
        for value in (game.provider_match_id, game.home_provider_id, game.away_provider_id)
    ):
        raise ValueError("来源或比赛/球队数字ID无效")
    if game.home_provider_id == game.away_provider_id:
        raise ValueError("主客球队ID相同")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (game.league, game.home_raw, game.away_raw)
    ):
        raise ValueError("缺少联赛或球队名称")
    if not isinstance(game.kickoff_utc, datetime) or game.kickoff_utc.utcoffset() is None:
        raise ValueError("开球时刻须含UTC偏移")
    if game.status == "AWARDED":
        raise ValueError("AWARDED 判罚比分不作为常规时间赛果")
    if game.status in LIVE and game.kickoff_utc > seen:
        raise ValueError("比赛进行中的观察早于来源开球时间，须核对赛程")
    if game.status in SCHEDULED | CANCELLED | LIVE:
        return
    if game.status != "FINISHED":
        raise ValueError(f"未知状态：{game.status}")
    if seen <= game.kickoff_utc or game.regular_time_basis not in {"regular", "derived"}:
        raise ValueError("完赛观察须晚于开球且有明确常规时间比分依据")
    for score in (game.full,) if game.half is None else (game.full, game.half):
        if (
            not isinstance(score, tuple)
            or len(score) != 2
            or any(type(value) is not int or not 0 <= value <= 99 for value in score)
        ):
            raise ValueError("无效的常规时间或半场比分")
    if game.half and any(a > b for a, b in zip(game.half, game.full, strict=True)):
        raise ValueError("半场比分大于全场比分")


def _check_capture(games, seen):
    """Index valid rows by source team so the first processed row cannot claim an ambiguous link."""
    rows, per_team = [], defaultdict(list)
    for game in games:
        try:
            _validate(game, seen)
        except (TypeError, ValueError, OverflowError) as exc:
            rows.append((game, str(exc)))
            continue
        rows.append((game, None))
        if game.status not in CANCELLED:
            for team_id in (game.home_provider_id, game.away_provider_id):
                per_team[game.league, team_id].append((game.kickoff_utc, game.provider_match_id))
    return rows, per_team


def _check_claims(game, per_team, target=None):
    when = datetime.fromisoformat(target.kickoff_utc) if target else game.kickoff_utc
    others = set()
    for team_id in (game.home_provider_id, game.away_provider_id):
        for kickoff, other_id in per_team[game.league, team_id]:
            if other_id != game.provider_match_id and abs(kickoff - when) <= WINDOW:
                others.add(f"fdorg:{other_id}")
    if others:
        if target:
            others.add(target.match_id)
        raise _Suspected("本批三小时内同队有多个来源比赛ID", others)


def _link_target(session, key, game, sides, existing, per_team):
    _check_claims(game, per_team)
    pair = tuple(side.team.id if side.team else None for side in sides)
    known = all(side.binding is not None for side in sides)
    window = session.scalars(
        select(Match).where(
            Match.source.in_(("jc", "fd", "fdorg")),
            Match.match_id != key,
            Match.league == game.league,
            Match.kickoff_utc.between(game.kickoff_utc - WINDOW, game.kickoff_utc + WINDOW),
        )
    )
    exact, conflicts = defaultdict(list), defaultdict(list)
    raw_names = {normalize_team(raw).casefold() for raw in (game.home_raw, game.away_raw)}
    for candidate in window:
        oriented = (candidate.home_team_id, candidate.away_team_id)
        source = candidate.source
        if source != "fdorg" and known and pair == oriented:
            exact[source].append(candidate)
        elif (
            (source != "fdorg" and not known)
            or any(t in pair for t in oriented)
            # T08 may retain provisional English teams. Their names can veto a duplicate,
            # but only two verified numeric identities can ever establish a link.
            or source == "fd"
            and any(
                normalize_team(session.get(Team, t).name).casefold() in raw_names for t in oriented
            )
        ):
            conflicts[source].append(candidate.match_id)
    if conflicts["fdorg"]:
        raise _Suspected("同联赛三小时内已有另一 fdorg 比赛ID", conflicts["fdorg"])
    target = None
    for source in ("jc", "fd"):
        matches = exact[source]
        candidates = conflicts[source] + [m.match_id for m in matches]
        if conflicts[source] or len(matches) > 1 or (existing and matches and source == "jc"):
            raise _Suspected(f"{source} 候选身份不全、主客冲突或存在重复比赛", candidates)
        if matches:
            target = matches[0]
            _check_claims(game, per_team, target)
            if source == "fd" and existing:
                if (
                    existing.source != "fdorg"
                    or existing.league != game.league
                    or (existing.home_team_id, existing.away_team_id) != pair
                ):
                    raise _Suspected("既有 fdorg 身份与当前观察冲突", [key])
                try:
                    candidate = fdorg_candidate(session, target)
                except CounterpartConflict as exc:
                    raise _Suspected(str(exc), exc.candidates) from exc
                if candidate is None or candidate.match_id != key:
                    raise _Suspected("既有 fdorg 与 fd 开球窗口无法复核", [key, target.match_id])
            break
    if (
        existing
        and (existing.counterpart_id or "").startswith("fd:")
        and (target is None or target.match_id != existing.counterpart_id)
    ):
        raise _Suspected("既有 fd 链接与当前身份或开球窗口不符", [existing.counterpart_id])
    claimed = set(session.scalars(select(Match.match_id).where(Match.counterpart_id == key)))
    if claimed and (target is None or claimed != {target.match_id}):
        raise _Suspected("来源比赛ID已有另一关联，当前身份或开球无法复核", claimed)
    if (
        target
        and (target.counterpart_id or "").startswith("fdorg:")
        and target.counterpart_id != key
    ):
        raise _Suspected("候选比赛已有其他 fdorg 比赛ID", [target.match_id])
    return target


def _reconcile_unsettled(match, score, seen):
    """Reconcile an independently identified, post-kickoff non-final observation with a score."""
    last = datetime.fromisoformat(match.result_checked_utc) if match.result_checked_utc else None
    if score.available_utc is not None:
        if score.available_source != "assumed" or seen < datetime.fromisoformat(
            score.available_utc
        ):
            return None
        if last is not None and match.result_provider == "fdorg":
            if last < seen:
                return (
                    None  # A final score was already observed before this later status regression.
                )
            if last > seen:
                score.available_utc = match.result_checked_utc
                score.available_source = "captured"
                return "availability_corrected"
        score.available_utc = score.available_source = None
    elif last is not None and seen <= last:
        return None  # Already excluded from training; retain the newer observation.
    if last is None or seen > last:
        match.result_checked_utc = seen
    return "availability_quarantined"


def _persist(session, key, game, seen, assume, existing, sides):
    old = session.get(Result, key, populate_existing=True)
    if existing and (
        existing.source != "fdorg"
        or existing.league != game.league
        or (existing.home_team_id, existing.away_team_id)
        != tuple(side.team.id if side.team else None for side in sides)
        or existing.result_provider not in (None, "fdorg")
    ):
        raise ValueError("既有比赛的来源、联赛或主客身份冲突，须人工复核")
    live = game.status in LIVE
    # The schema uses scheduled for an unsettled fixture. A LIVE row can only have a past kickoff.
    status = "scheduled" if live else STORED_STATUS[game.status]
    previous = existing.status if existing else None
    kickoff = datetime.fromisoformat(existing.kickoff_utc) if existing else None
    checked = max(
        (
            datetime.fromisoformat(t)
            for t in (
                existing.result_checked_utc if existing else None,
                old.available_utc if old else None,
            )
            if t
        ),
        default=None,
    )
    if (
        status == "scheduled"
        and old
        and previous == "finished"
        and game.kickoff_utc == kickoff <= seen
    ):
        resolution = _reconcile_unsettled(existing, old, seen)
        if resolution:
            return resolution, False, ()
    if live and (old or previous in {"finished", "cancelled"}):
        return "skipped", False, ()
    changed = bool(
        old
        and (
            (old.home_goals, old.away_goals) != game.full
            or (game.half is not None and (old.ht_home_goals, old.ht_away_goals) != game.half)
        )
    )
    if checked and seen <= checked:
        if seen == checked and (
            status != previous or kickoff != game.kickoff_utc or (old and changed)
        ):
            raise ValueError("同一采集时刻出现冲突的赛程或比分")
        return "unchanged", False, ()
    if (
        status == "scheduled"
        and kickoff
        and game.kickoff_utc <= seen
        and game.kickoff_utc != kickoff
    ):
        raise ValueError("未结算观察已到开球时间，但与原有赛程的开球时刻不符")
    if status == "scheduled" and (
        old or previous == "finished" or (previous == "cancelled" and game.kickoff_utc <= seen)
    ):
        return "unchanged", False, ()
    teams = [_materialize(session, side) for side in sides]
    session.flush()
    if status == "scheduled":
        upsert_match(
            session,
            match_id=key,
            source="fdorg",
            league=game.league,
            home_team_id=teams[0].id,
            away_team_id=teams[1].id,
            kickoff_utc=game.kickoff_utc,
            status="scheduled",
            result_checked_utc=seen,
            result_provider="fdorg",
        )
        if previous is None:
            return "inserted", False, teams
        outcome = (
            "updated" if live or previous != status or kickoff != game.kickoff_utc else "unchanged"
        )
        return outcome, False, teams
    observed = GameResult(
        key,
        game.league,
        None,
        teams[0].name,
        teams[1].name,
        game.kickoff_utc.date(),
        status,
        game.full if status == "finished" else None,
        game.half if status == "finished" else None,
        kickoff_utc=game.kickoff_utc,
    )
    if old and old.available_utc and status == "finished" and not changed:
        if datetime.fromisoformat(old.available_utc) <= game.kickoff_utc:
            raise ValueError("开球更改后原比分可知时刻不再有效，须人工复核")
    # A later known non-final observation rules out an earlier assumed availability.
    if old and (old.available_utc is None or old.available_source in {"published", "captured"}):
        assume = None
    elif checked and assume and not old and game.kickoff_utc + assume <= checked:
        assume = None
    state = apply_result(session, observed, seen, assume)
    if state in {"unchanged", "cancelled"}:
        return state, False, teams
    if old:
        session.flush()
        session.refresh(old)  # Expose the persisted UTC strings and any DB correction trigger.
    outcome = "inserted" if previous is None else "updated"
    if changed or previous == "cancelled":
        outcome = "corrected"
    return outcome, state == "assumed", teams


def import_fdorg_games(session, games, *, seen, assume, aliases):
    """Import one capture in the caller's transaction, with a savepoint per fixture."""
    if not isinstance(seen, datetime) or seen.utcoffset() is None:
        raise ValueError("采集时刻须含UTC偏移")
    if assume is not None and (
        not isinstance(assume, timedelta) or not timedelta(0) < assume <= timedelta(days=14)
    ):
        raise ValueError("--assume-known-after 须为正延时且不超过14天")
    conn = session.connection()
    if conn.dialect.name == "sqlite" and not conn.connection.driver_connection.in_transaction:
        # SQLite can otherwise commit a top-level SAVEPOINT independently of the caller.
        conn.exec_driver_sql("BEGIN IMMEDIATE")
    index = _Teams(session, aliases)
    rows, per_team = _check_capture(games, seen)
    counts = dict.fromkeys(COUNT_KEYS, 0)
    rejected, suspected, links, outcomes = [], [], [], []
    unmatched = {}
    for game, invalid in rows:
        audit = dict(
            competition_code=game.competition_code,
            league=game.league,
            provider_match_id=game.provider_match_id,
            home_provider_id=game.home_provider_id,
            away_provider_id=game.away_provider_id,
            home=game.home_raw,
            away=game.away_raw,
            status=game.status,
            kickoff_utc=game.kickoff_utc.isoformat()
            if isinstance(game.kickoff_utc, datetime)
            else str(game.kickoff_utc),
        )
        unknown = {}
        teams = []
        assumed = False
        key = f"fdorg:{game.provider_match_id}"
        match_id = key
        try:
            with session.begin_nested():
                if invalid:
                    raise ValueError(invalid)
                existing = session.get(Match, key, populate_existing=True)
                if game.status in CANCELLED and existing is None:
                    outcome = "skipped"
                else:
                    sides = index.sides(session, game, unknown)
                    target = _link_target(session, key, game, sides, existing, per_team)
                    if target:
                        if target.source == "fd" and existing:
                            existing.counterpart_id = target.match_id
                        match_id = target.match_id
                        outcome = "linked_existing" if target.source == "jc" else "linked_fd"
                        if target.source == "jc" and target.counterpart_id is None:
                            target.counterpart_id = key
                    else:
                        outcome, assumed, teams = _persist(
                            session, key, game, seen, assume, existing, sides
                        )
                session.flush()
        except _Suspected as exc:
            counts["suspected_duplicates"] += 1
            suspected.append(dict(audit, candidates=exc.candidates, reason=str(exc)))
            unmatched.update(unknown)
            continue
        except (ValueError, TypeError, KeyError, IntegrityError, OverflowError) as exc:
            counts["rejected_rows"] += 1
            rejected.append(dict(audit, reason=str(exc)))
            continue
        for team in teams:
            index.remember(team)
        unmatched.update(unknown)
        counts[outcome] += 1
        counts["assumed"] += int(assumed)
        record = dict(audit, fdorg_id=key, match_id=match_id, outcome=outcome)
        if outcome in {"availability_corrected", "availability_quarantined"}:
            result = session.get(Result, key)
            record.update(
                available_utc=result.available_utc, available_source=result.available_source
            )
        if outcome == "availability_quarantined":
            counts["rejected_rows"] += 1
            rejected.append(
                dict(audit, reason="已知该时仍未结算，缺可靠的更晚终场采集时刻，已隔离比分可知时间")
            )
        outcomes.append(record)
        if outcome in {"linked_existing", "linked_fd"}:
            field = "jc_id" if outcome == "linked_existing" else "fd_id"
            links.append({**record, field: match_id})
    return dict(
        counts,
        rejected=rejected,
        suspected=suspected,
        links=links,
        outcomes=outcomes,
        unmatched_names=[
            {"league": league, "id": i, "name": unmatched[league, i]}
            for league, i in sorted(unmatched)
        ],
    )
