"""中国竞彩网在售足球赛程和固定奖金。所有时间均以原始响应的采集时刻为准。"""

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from zucai.db import get_engine, init_db, session_scope, upsert_match
from zucai.models import Match, Odds
from zucai.sources.base import RateLimiter, RawStore, Source, make_session
from zucai.sources.teams import upsert_team

LOGGER = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
API = "https://webapi.sporttery.cn/gateway/uniform/football/"
POOLS = ("had", "hhad", "crs", "ttg", "hafu")
OTHER_SCORES = {"s1sh": "5+", "s1sd": "draw_other", "s1sa": "away_other"}


def official_value(raw: bytes) -> dict:
    body = json.loads(raw)
    if body.get("success") is not True or str(body.get("errorCode")) != "0":
        raise ValueError(f"体彩接口失败：{body.get('errorMessage', '响应异常')}")
    if not isinstance(body.get("value"), dict):
        raise ValueError("体彩响应缺少 value")
    return body["value"]


def beijing_kickoff(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=BEIJING).astimezone(UTC)


@dataclass(frozen=True)
class OddsQuote:
    pool: str
    line: int
    selection: str
    price: float


@dataclass(frozen=True)
class JcMatch:
    match_id: str
    business_date: date
    jc_num: str
    league: str
    home: str
    away: str
    kickoff_utc: datetime
    odds: list[OddsQuote]


def selection(pool: str, key: str) -> str | None:
    if pool in {"had", "hhad"} and key in ("h", "d", "a"):
        return key
    if pool == "hafu" and re.fullmatch("[hda]{2}", key):
        return key
    if pool == "ttg" and re.fullmatch("s[0-7]", key):
        return "7+" if key == "s7" else key[1]
    if pool == "crs":
        score = re.fullmatch(r"s(\d\d)s(\d\d)", key)
        if score:
            return f"{int(score[1])}:{int(score[2])}"
        return OTHER_SCORES.get(key)
    return None


def market_quotes(row: dict, pool: str) -> list[OddsQuote]:
    values = row.get(pool) or {}
    if not values:
        return []
    if pool == "hhad" and not re.fullmatch(r"[+-]?\d+", str(values.get("goalLine", ""))):
        raise ValueError(f"{row['matchId']} 让球线缺失或无效")
    line = int(values["goalLine"]) if pool == "hhad" else 0
    quotes = []
    for key, raw_price in values.items():
        opt = selection(pool, key)
        if opt is None or raw_price in (None, "", "--", "-"):
            continue
        price = float(raw_price)
        if math.isfinite(price) and price > 0:
            quotes.append(OddsQuote(pool, line, opt, price))
    if pool in {"had", "hhad"} and len(quotes) != 3:
        LOGGER.warning("%s %s 三项报价不全，跳过此玩法", row["matchId"], pool)
        return []
    return quotes


class SportteryCalculator(Source[JcMatch]):
    name = "sporttery-calculator"

    def __init__(self, store=None, session=None):
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else make_session()
        self.limiter = RateLimiter(1)

    def fetch_raw(self) -> Path:
        self.limiter.wait()
        response = self.session.get(
            API + "getMatchCalculatorV1.qry",
            params={"poolCode": ",".join(POOLS), "channel": "c"},
            headers={"Referer": "https://www.sporttery.cn/"},
        )
        path = self.store.save_response(self.name, "calculator", response, "json")
        response.raise_for_status()
        return path

    def parse(self, raw: bytes) -> list[JcMatch]:
        value = official_value(raw)
        if "matchInfoList" not in value:
            if value == {}:
                return []
            raise ValueError("体彩响应缺少 matchInfoList")
        matches = []
        for group in value["matchInfoList"]:
            for row in group["subMatchList"]:
                if row["matchStatus"] != "Selling" or str(row["sellStatus"]) != "1":
                    continue
                enabled = {
                    p["poolCode"].lower()
                    for p in row["poolList"]
                    if p["poolStatus"] == "Selling"
                    and p["poolOddsType"] == "F"
                    and (str(p["single"]) == "1" or str(p["allUp"]) == "1")
                }
                quotes = [q for pool in POOLS if pool in enabled for q in market_quotes(row, pool)]
                if quotes:
                    matches.append(
                        JcMatch(
                            f"jc:{int(row['matchId'])}",
                            date.fromisoformat(group["businessDate"]),
                            row["matchNumStr"],
                            row["leagueAbbName"],
                            row["homeTeamAbbName"],
                            row["awayTeamAbbName"],
                            beijing_kickoff(f"{row['matchDate']} {row['matchTime']}"),
                            quotes,
                        )
                    )
        return matches


def append_market(
    session: Session, match_id: str, quotes: list[OddsQuote], captured: datetime
) -> int:
    """Save each changed market in full so later comparisons use one observation time."""
    first = quotes[0]
    where = (Odds.match_id == match_id, Odds.pool == first.pool, Odds.line == first.line)
    previous = session.scalar(
        select(func.max(Odds.snapshot_utc)).where(*where, Odds.snapshot_utc <= captured)
    )
    if previous is not None and captured - datetime.fromisoformat(previous) < timedelta(minutes=10):
        known = dict(
            session.execute(
                select(Odds.selection, Odds.price).where(*where, Odds.snapshot_utc == previous)
            ).all()
        )
        if known == {q.selection: q.price for q in quotes}:
            return 0
        if datetime.fromisoformat(previous) == captured:
            raise ValueError(f"{match_id} 同一时刻出现互相矛盾的报价")
    rows = [
        dict(
            match_id=match_id,
            pool=q.pool,
            line=q.line,
            selection=q.selection,
            price=q.price,
            snapshot_utc=captured,
        )
        for q in quotes
    ]
    return session.execute(insert(Odds.__table__).values(rows)).rowcount


def save_schedule(session: Session, fixtures: list[JcMatch], captured: datetime) -> tuple[int, int]:
    matches = quotes = 0
    for item in fixtures:
        if item.kickoff_utc <= captured:
            continue
        current = session.get(Match, item.match_id)
        if current and current.status != "scheduled":
            continue
        newest = session.scalar(
            select(func.max(Odds.snapshot_utc)).where(Odds.match_id == item.match_id)
        )
        if newest is None or datetime.fromisoformat(newest) <= captured:
            home = upsert_team(session, item.home, "jc")
            away = upsert_team(session, item.away, "jc")
            upsert_match(
                session,
                match_id=item.match_id,
                source="jc",
                league=item.league,
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=item.kickoff_utc,
                jc_num=item.jc_num,
                status="scheduled",
            )
        for _, group in groupby(item.odds, key=lambda q: (q.pool, q.line)):
            quotes += append_market(session, item.match_id, list(group), captured)
        matches += 1
    return matches, quotes


def fetch_schedule(number_date: date | None = None) -> tuple[int, int]:
    feed = SportteryCalculator()
    try:
        path = feed.fetch_raw()
        games = feed.parse(feed.store.load(path))
        captured = datetime.fromisoformat(feed.store.load_metadata(path)["fetched_at_utc"])
    finally:
        feed.session.close()
    if number_date is not None:
        games = [g for g in games if g.business_date == number_date]
    engine = get_engine()
    try:
        init_db(engine)
        with session_scope(engine) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            matches, quotes = save_schedule(session, games, captured)
    finally:
        engine.dispose()
    LOGGER.info("赛程 %d 场，新增报价 %d 项，快照 %s", matches, quotes, path)
    return matches, quotes
