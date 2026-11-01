"""按日归档官方开奖页，验证分页，再把常规时间比分与赛前场次对应。"""

import csv
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zucai.db import get_engine, init_db, session_scope, upsert_match
from zucai.models import Match, Odds, Prediction, Result, SfcIssueMatch
from zucai.sources.base import RateLimiter, RawStore, Source, make_session
from zucai.sources.sporttery import API, BEIJING, beijing_kickoff, official_value
from zucai.sources.teams import upsert_team

LOGGER = logging.getLogger(__name__)
Score = tuple[int, int]
VOID = {"无效场次", "取消", "延期", "推迟", "-1:-1"}


class ResultRowsRejected(ValueError):
    """All requested pages were read; valid rows committed, rejected rows audited."""


def score(value: str) -> Score | None:
    match = re.fullmatch(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", str(value).strip())
    return (int(match[1]), int(match[2])) if match else None


@dataclass(frozen=True)
class GameResult:
    match_id: str
    league: str
    jc_num: str
    home: str
    away: str
    play_date: date
    status: str
    full: Score | None
    half: Score | None
    kickoff_utc: datetime | None = None
    official_id: str | None = None
    team_ids: tuple[int, int] | None = None
    published_utc: datetime | None = None


class SportteryResults(Source[GameResult]):
    name = "sporttery-results"

    def __init__(self, store=None, session=None):
        self.rejected = []
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else make_session()
        self.session.headers["Referer"] = "https://www.sporttery.cn/"
        self.pages_limit = RateLimiter(1)
        self.heads_limit = RateLimiter(1)

    def fetch_raw(self, *, day: date, page: int = 1) -> Path:
        self.pages_limit.wait()
        r = self.session.get(
            API + "getUniformMatchResultV1.qry",
            params={
                "matchBeginDate": day.isoformat(),
                "matchEndDate": day.isoformat(),
                "leagueId": "",
                "pageSize": 30,
                "pageNo": page,
                "isFix": 0,
                "matchPage": 1,
                "pcOrWap": 1,
            },
        )
        path = self.store.save_response(self.name, f"results-{day}-p{page}", r, "json")
        r.raise_for_status()
        return path

    def parse(self, raw: bytes) -> list[GameResult]:
        value = official_value(raw)
        if not value:
            return []
        return [g for item in value["matchResult"] if (g := self._row(item)) is not None]

    @staticmethod
    def _row(item: dict) -> GameResult | None:
        ft_text = item.get("sectionsNo999", "")
        void = ft_text in VOID or item.get("poolStatus") == "Refund"
        if not void and str(item["matchResultStatus"]) != "2":
            return None
        ft = None if void else score(ft_text)
        if not void and ft is None:
            raise ValueError("已结算但没有明确常规时间比分")
        half = None if void else score(item.get("sectionsNo1", ""))
        if ft and half and (half[0] > ft[0] or half[1] > ft[1]):
            raise ValueError("半场比分大于全场比分")
        return GameResult(
            f"jc:{int(item['matchId'])}",
            item["leagueNameAbbr"],
            item["matchNumStr"],
            item["homeTeam"],
            item["awayTeam"],
            date.fromisoformat(item["matchDate"]),
            "cancelled" if void else "finished",
            ft,
            half,
            team_ids=(int(item["homeTeamId"]), int(item["awayTeamId"])),
        )

    def day_results(self, day: date) -> list[tuple[GameResult, datetime]]:
        observations = []
        ids = set()
        page = 1
        first_total = None
        while True:
            path = self.fetch_raw(day=day, page=page)
            raw = self.store.load(path)
            value = official_value(raw)
            if not value and page == 1:
                return []
            if (
                page == 1
                and value.get("matchResult") == []
                and int(value["total"]) == 0
                and int(value["pages"]) == 0
                and int(value["pageNo"]) in (0, 1)
            ):
                return []
            if int(value["pageNo"]) != page:
                raise ValueError(f"{day} 分页号不符，请重试")
            total = int(value["total"])
            if first_total is not None and total != first_total:
                raise ValueError(f"{day} 采集期间分页总数变动，请重试")
            first_total = total
            rows = value["matchResult"]
            new_ids = {int(m["matchId"]) for m in rows}
            if ids & new_ids or len(new_ids) != len(rows) or (not rows and len(ids) < total):
                raise ValueError(f"{day} 分页重复或缺行")
            if any(date.fromisoformat(m["matchDate"]) != day for m in rows):
                raise ValueError(f"{day} 接口返回了其他日期")
            ids.update(new_ids)
            observed = datetime.fromisoformat(self.store.load_metadata(path)["fetched_at_utc"])
            for row in rows:
                try:
                    game = self._row(row)
                except (ValueError, KeyError, TypeError) as exc:
                    self.rejected.append((day, f"jc:{row['matchId']}", observed, str(exc)))
                    continue
                if game is not None:
                    observations.append((game, observed))
            if page >= int(value["pages"]):
                if len(ids) != total:
                    raise ValueError(f"{day} 共{total}场，仅取得{len(ids)}场")
                return observations
            page += 1

    def kickoff(self, game: GameResult) -> datetime:
        """Resolve the exact kickoff from the official lottery match ID."""
        sid = game.match_id.split(":")[1]
        folder = self.store.root / "sporttery-head"
        # Only completed-match headers, with HTTP provenance and exact team IDs, may be reused.
        for cached in sorted(folder.glob(f"*/match-{sid}_*Z_*.json"), reverse=True):
            if cached.name.endswith(".meta.json"):
                continue
            metadata = self.store.load_metadata(cached)
            if metadata["status_code"] == 200:
                try:
                    when = self._head_time(self.store.load(cached), game)
                except (ValueError, KeyError):
                    continue
                if datetime.fromisoformat(metadata["fetched_at_utc"]) >= when:
                    return when
        self.heads_limit.wait()
        response = self.session.get(
            API + "getMatchHeadV1.qry", params={"source": "web", "sportteryMatchId": sid}
        )
        path = self.store.save_response("sporttery-head", f"match-{sid}", response, "json")
        response.raise_for_status()
        return self._head_time(self.store.load(path), game)

    @staticmethod
    def _head_time(raw: bytes, game: GameResult) -> datetime:
        head = official_value(raw)
        if (
            f"jc:{head.get('sportteryMatchId')}" != game.match_id
            or (head.get("sportteryHomeTeamId"), head.get("sportteryAwayTeamId")) != game.team_ids
        ):
            raise ValueError(f"{game.match_id} 比赛详情与开奖场次或主客队不符")
        when = beijing_kickoff(head["matchDateTime"])
        if when.astimezone(BEIJING).date() != game.play_date:
            raise ValueError(f"{game.match_id} 开奖日期与比赛详情不符")
        return when


def availability(
    game: GameResult,
    seen: datetime,
    assume: timedelta | None,
    old: Result | None,
    changed: bool,
    last_seen: datetime | None,
) -> tuple[datetime, str]:
    published = game.published_utc
    if old and not changed and old.available_utc:
        basis = old.available_source or "captured"
        if basis != "assumed" or published is None:
            return datetime.fromisoformat(old.available_utc), basis
    if published is not None:
        if not game.kickoff_utc < published <= seen:
            raise ValueError("赛果发布时间早于开球或晚于采集")
        if not changed or last_seen is None or published > last_seen:
            return published, "published"
    if assume is not None and not changed and game.kickoff_utc + assume <= seen:
        return game.kickoff_utc + assume, "assumed"
    return seen, "captured"


def reconcile_backup(session: Session, game: GameResult, home_id: int, away_id: int) -> None:
    """Promote one corroborated backup ID; keep historical quote/forecast values and row IDs."""
    old = session.scalars(
        select(Match).where(
            Match.source == "c500",
            Match.counterpart_id == game.match_id,
            Match.home_team_id == home_id,
            Match.away_team_id == away_id,
            Match.jc_num == game.jc_num,
            Match.kickoff_utc == game.kickoff_utc,
        )
    ).one_or_none()
    if old is None:
        return
    canonical = session.get(Match, game.match_id)
    if canonical is None:
        canonical = upsert_match(
            session,
            match_id=game.match_id,
            source="jc",
            league=game.league,
            jc_num=game.jc_num,
            kickoff_utc=game.kickoff_utc,
            home_team_id=home_id,
            away_team_id=away_id,
            status=old.status,
            result_provider=old.result_provider,
            result_checked_utc=old.result_checked_utc,
        )
    if canonical.counterpart_id not in (None, old.match_id):
        raise ValueError(f"{game.match_id} 同时对应多个备用ID，请人工核对")
    canonical.counterpart_id = old.match_id
    session.flush()
    for dep in (Odds, Prediction, SfcIssueMatch, Result):
        table = dep.__table__
        if dep is Result and session.get(Result, game.match_id) is not None:
            session.execute(delete(table).where(table.c.match_id == old.match_id))
        else:
            session.execute(
                update(table).where(table.c.match_id == old.match_id).values(match_id=game.match_id)
            )
    session.delete(old)
    session.flush()
    session.expire_all()


def apply_result(
    session: Session, game: GameResult, seen: datetime, assume: timedelta | None = None
) -> str:
    if game.kickoff_utc is None:
        raise ValueError(f"{game.match_id} 缺少已核实开球时间")
    if game.status == "finished" and seen <= game.kickoff_utc:
        raise ValueError(f"{game.match_id} 完赛比分的采集不晚于开球")
    provider = game.match_id.split(":")[0]
    home = upsert_team(session, game.home, provider)
    away = upsert_team(session, game.away, provider)
    sid = game.match_id
    if provider == "jc":
        reconcile_backup(session, game, home.id, away.id)
    if provider == "c500" and game.official_id:
        linked = session.get(Match, game.official_id)
        if (
            linked
            and (linked.home_team_id, linked.away_team_id, linked.jc_num)
            == (home.id, away.id, game.jc_num)
            and datetime.fromisoformat(linked.kickoff_utc) == game.kickoff_utc
        ):
            sid = linked.match_id
    match = session.get(Match, sid)
    if (
        match
        and match.status == "scheduled"
        and match.result_checked_utc
        and game.status == "cancelled"
        and game.kickoff_utc < datetime.fromisoformat(match.kickoff_utc)
    ):
        return "unchanged"  # the old date's void notice does not void a verified later re-listing
    last_seen = (
        datetime.fromisoformat(match.result_checked_utc)
        if match and match.result_checked_utc
        else None
    )
    if match and (
        (provider == "c500" and match.result_provider == "jc") or (last_seen and seen < last_seen)
    ):
        return "unchanged"
    if match and (match.home_team_id, match.away_team_id) != (home.id, away.id):
        raise ValueError(f"{sid} 原有场次与新赛果的球队身份或主客不一致")
    old = session.get(Result, sid)
    if match and match.source == "jc" and provider == "c500" and old and not match.result_provider:
        return "unchanged"  # a legacy official result has no weaker-source override
    old_status = match.status if match else None
    if last_seen == seen:
        if match.status != game.status or (
            old
            and (
                (old.home_goals, old.away_goals) != game.full
                or (game.half is not None and (old.ht_home_goals, old.ht_away_goals) != game.half)
            )
        ):
            raise ValueError(f"{sid} 同一采集时间出现冲突赛果")
        return "unchanged"
    upsert_match(
        session,
        match_id=sid,
        source=sid.split(":")[0],
        league=match.league if match else game.league,
        jc_num=game.jc_num,
        kickoff_utc=game.kickoff_utc,
        home_team_id=home.id,
        away_team_id=away.id,
        status=game.status,
        result_provider=provider,
        result_checked_utc=seen,
        counterpart_id=(
            (game.official_id if sid == game.match_id else game.match_id)
            if provider == "c500"
            else (match.counterpart_id if match else None)
        ),
    )
    if game.status == "cancelled":
        if old:
            session.delete(old)
        return "unchanged" if old_status == "cancelled" and not old else "cancelled"
    if game.full is None:
        raise ValueError("完赛场次缺少常规时间比分")
    half = game.half or (None, None)
    old_pair = (old.home_goals, old.away_goals) if old else None
    if old and old_pair == game.full and game.half is None:
        half = (old.ht_home_goals, old.ht_away_goals)
    changed = old_status == "cancelled" or bool(
        old and (old_pair != game.full or (old.ht_home_goals, old.ht_away_goals) != half)
    )
    when, basis = availability(game, seen, assume, old, changed, last_seen)
    if (
        old
        and not changed
        and old.available_utc
        and datetime.fromisoformat(old.available_utc) == when
        and (old.available_source or "captured") == basis
    ):
        return "unchanged"
    result = old if old else Result(match_id=sid)
    result.home_goals, result.away_goals = game.full
    result.ht_home_goals, result.ht_away_goals = half
    result.available_utc, result.available_source = when, basis
    session.add(result)
    return basis


def fetch_results(
    start: date, end: date, source: str = "jc", assume: timedelta | None = None
) -> dict[str, int]:
    from zucai.sources.c500 import C500JcHistory

    if start > end or (assume is not None and assume <= timedelta(0)):
        raise ValueError("日期范围或假设延时无效")
    feed = SportteryResults() if source == "jc" else C500JcHistory()
    engine = get_engine()
    counts = {"finished": 0, "cancelled": 0, "assumed": 0, "unchanged": 0}
    try:
        init_db(engine)
        # 500 groups by the sales day, which can contain next-calendar-day kickoffs.
        day = start if source == "jc" else start - timedelta(days=1)
        while day <= end:
            ready = []
            for game, seen in feed.day_results(day):
                if not start <= game.play_date <= end:
                    continue
                if game.kickoff_utc is None:
                    try:
                        game = replace(game, kickoff_utc=feed.kickoff(game))
                    except (ValueError, KeyError, TypeError) as exc:
                        feed.rejected.append((day, game.match_id, seen, str(exc)))
                        continue
                ready.append((game, seen))
            with session_scope(engine) as s:
                s.connection().exec_driver_sql("BEGIN IMMEDIATE")
                for game, seen in ready:
                    try:
                        with s.begin_nested():
                            state = apply_result(s, game, seen, assume)
                    except (ValueError, IntegrityError) as exc:
                        feed.rejected.append((day, game.match_id, seen, str(exc)))
                        continue
                    if state in {"unchanged", "cancelled"}:
                        counts[state] += 1
                    else:
                        counts["finished"] += 1
                        counts["assumed"] += int(state == "assumed")
            day += timedelta(days=1)
    finally:
        feed.session.close()
        engine.dispose()
        if feed.rejected:
            audit = Path("data/rejected_results.csv")
            audit.parent.mkdir(parents=True, exist_ok=True)
            with audit.open("a", encoding="utf-8", newline="") as out:
                writer = csv.writer(out)
                if out.tell() == 0:
                    writer.writerow(["date", "match_id", "observed_utc", "reason"])
                writer.writerows(feed.rejected)
    LOGGER.info(
        "回填 %s 至 %s，来源 %s，完赛 %d，取消/无效 %d，assumed %d，跳过 %d",
        start,
        end,
        source,
        counts["finished"],
        counts["cancelled"],
        counts["assumed"],
        counts["unchanged"],
    )
    if feed.rejected:
        raise ResultRowsRejected(f"{len(feed.rejected)} 场未通过核验，见 data/rejected_results.csv")
    return counts


def assumed_delay(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([hm])", value)
    if match is None:
        raise ValueError("延时示例：3h、90m")
    minutes = float(match[1]) * (60 if match[2] == "h" else 1)
    if not 0 < minutes <= 60 * 24 * 14:
        raise ValueError("仅支持正延时，最多14天")
    return timedelta(minutes=minutes)
