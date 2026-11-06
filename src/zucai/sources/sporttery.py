"""中国竞彩网在售足球赛程和固定奖金。所有时间均以原始响应的采集时刻为准。"""

import csv
import io
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
from zucai.sources.base import RateLimiter, RawStore, Source, _atomic_write, make_session
from zucai.sources.teams import upsert_team

LOGGER = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
API = "https://webapi.sporttery.cn/gateway/uniform/football/"
POOLS = ("had", "hhad", "crs", "ttg", "hafu")
MARKET_SIZE = {"had": 3, "hhad": 3, "crs": 31, "ttg": 8, "hafu": 9}
OTHER_SCORES = {"s1sh": "5+", "s1sd": "draw_other", "s1sa": "away_other"}
CATALOG_FIELDS = (
    "business_date",
    "jc_num",
    "match_id",
    "matchId",
    "home",
    "away",
    "league",
    "matchStatus",
    "sellStatus",
    "has_fixed_odds",
    "retained",
    "captured_utc",
    "kickoff_utc",
    "poolList",
    "lastUpdateTime",
    "sale_end_fields",
)
PARLAY_CATALOG_FIELDS = ("poolList", "lastUpdateTime", "sale_end_fields")
LEGACY_AUDIT_MONTH = "2026-11"


def utc_stamp(instant: datetime) -> str:
    if instant.utcoffset() is None:
        raise ValueError("竞彩采集时刻必须包含时区")
    return instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sale_end_fields(row):
    """Preserve supplied sale-deadline-like fields verbatim, without interpreting clocks."""
    found = {}
    scopes = [("", row)] + [
        (f"poolList[{i}].", pool)
        for i, pool in enumerate(row.get("poolList") or [])
        if isinstance(pool, dict)
    ]
    for prefix, values in scopes:
        for key, value in values.items():
            lower = key.lower()
            if any(s in lower for s in ("sell", "sale", "bet")) and any(
                s in lower for s in ("end", "stop", "close", "deadline")
            ):
                found[prefix + key] = value
    return json.dumps(found, ensure_ascii=False, sort_keys=True) if found else ""


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
        LOGGER.warning("%s 让球线缺失或无效，跳过让球玩法", row["matchId"])
        return []
    line = int(values["goalLine"]) if pool == "hhad" else 0
    quotes = []
    for key, raw_price in values.items():
        opt = selection(pool, key)
        if opt is None or raw_price in (None, "", "--", "-"):
            continue
        try:
            price = float(raw_price)
        except (ValueError, TypeError):
            continue
        if math.isfinite(price) and price > 0:
            quotes.append(OddsQuote(pool, line, opt, price))
    if len(quotes) != MARKET_SIZE[pool]:
        LOGGER.warning("%s %s 报价不全，跳过此玩法", row["matchId"], pool)
        return []
    return quotes


class SportteryCalculator(Source[JcMatch]):
    name = "sporttery-calculator"

    def __init__(self, store=None, session=None):
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else make_session()
        self.limiter = RateLimiter(1)
        self.catalog: list[dict[str, str]] = []
        self.skipped: list[dict[str, str]] = []
        self.captured_utc = ""

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

    def parse(self, raw: bytes, *, captured: datetime | None = None) -> list[JcMatch]:
        """Expose every raw row in catalog; return the existing sale-eligible matches only."""
        self.catalog = []
        self.skipped = []
        captured_utc = utc_stamp(captured) if captured is not None else ""
        self.captured_utc = captured_utc
        value = official_value(raw)
        if "matchInfoList" not in value:
            if value == {}:
                return []
            raise ValueError("体彩响应缺少 matchInfoList")
        matches = []
        for group in value["matchInfoList"]:
            for row in group["subMatchList"]:
                # Excluded rows may omit pools or clocks; audit available source values
                # without requiring fields that the original sale filter did not read.
                pools = row.get("poolList")
                has_fixed_odds = any(
                    isinstance(pool, dict)
                    and str(pool.get("poolCode", "")).lower() in POOLS
                    and pool.get("poolOddsType") == "F"
                    for pool in (pools if isinstance(pools, list) else [])
                )
                quotes = []
                if row["matchStatus"] != "Selling":
                    reason = "match_status_not_selling"
                elif str(row["sellStatus"]) != "1":
                    reason = "sell_status_not_1"
                else:
                    enabled = {
                        p["poolCode"].lower()
                        for p in row["poolList"]
                        if p["poolStatus"] == "Selling"
                        and p["poolOddsType"] == "F"
                        and (str(p["single"]) == "1" or str(p["allUp"]) == "1")
                    }
                    quotes = [
                        q for pool in POOLS if pool in enabled for q in market_quotes(row, pool)
                    ]
                    reason = (
                        "no_fixed_odds"
                        if not has_fixed_odds
                        else "no_selling_fixed_pool"
                        if not enabled.intersection(POOLS)
                        else "no_valid_fixed_quotes"
                    )
                try:
                    kickoff = utc_stamp(beijing_kickoff(f"{row['matchDate']} {row['matchTime']}"))
                except (KeyError, TypeError, ValueError):
                    kickoff = ""
                raw_id = str(row.get("matchId", ""))
                catalog = dict(
                    business_date=group["businessDate"],
                    jc_num=str(row.get("matchNumStr", "")),
                    match_id=f"jc:{int(raw_id)}" if re.fullmatch(r"[0-9]+", raw_id) else "",
                    matchId=raw_id,
                    home=str(row.get("homeTeamAbbName", "")),
                    away=str(row.get("awayTeamAbbName", "")),
                    league=str(row.get("leagueAbbName", "")),
                    matchStatus=str(row["matchStatus"]),
                    sellStatus=str(row.get("sellStatus", "")),
                    has_fixed_odds=str(int(has_fixed_odds)),
                    retained=str(int(bool(quotes))),
                    captured_utc=captured_utc,
                    kickoff_utc=kickoff,
                    poolList=json.dumps(pools, ensure_ascii=False) if pools is not None else "",
                    lastUpdateTime=str(row.get("lastUpdateTime", "")),
                    sale_end_fields=sale_end_fields(row),
                )
                self.catalog.append(catalog)
                if not quotes:
                    self.skipped.append(dict(catalog, reason=reason))
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


def _existing_audit(path: Path, fields) -> list[dict[str, str]]:
    existing = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            legacy_fields = [field for field in fields if field not in PARLAY_CATALOG_FIELDS]
            if reader.fieldnames not in (list(fields), legacy_fields):
                raise ValueError(f"竞彩审计字段无效：{path.name}")
            existing = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in existing):
            raise ValueError(f"竞彩审计行无效：{path.name}")
    return existing


def _audit_content(path: Path, rows: list[dict[str, str]], fields, keys, *, legacy=None) -> bytes:
    existing = _existing_audit(path, fields)
    historical = _existing_audit(legacy, fields) if legacy is not None else []
    seen = {}
    historical_keys = {tuple(row[field] for field in keys) for row in historical}
    for row in [*historical, *existing]:
        key = tuple(row[field] for field in keys)
        if key in seen and any(seen[key][k] != row[k] for k in seen[key].keys() & row.keys()):
            raise ValueError(f"竞彩同一编号与采集时刻的审计冲突：{path.name}")
        seen[key] = row
    for row in rows:
        key = tuple(row[field] for field in keys)
        if key in seen:
            # Expanded old rows have blank new fields: absence is not contrary evidence.
            if any(
                value != row.get(k)
                for k, value in seen[key].items()
                if k not in PARLAY_CATALOG_FIELDS or value != ""
            ):
                raise ValueError(f"竞彩同一编号与采集时刻的审计冲突：{path.name}")
            if key not in historical_keys:
                seen[key].update(row)
            continue
        existing.append(row)
        seen[key] = row
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(existing)
    return output.getvalue().encode("utf-8")


def save_schedule_audit(directory: Path, feed: SportteryCalculator) -> None:
    """Keep complete raw-page evidence, including rows outside a requested number date."""
    month = utc_stamp(datetime.fromisoformat(feed.captured_utc))[:7]
    catalog = directory / "jc_catalog" / f"{month}.csv"
    skipped = directory / "jc_skipped" / f"{month}.csv"
    contents = {
        catalog: _audit_content(
            catalog,
            feed.catalog,
            CATALOG_FIELDS,
            ("business_date", "jc_num", "captured_utc"),
            legacy=directory / "jc_catalog.csv" if month == LEGACY_AUDIT_MONTH else None,
        ),
        skipped: _audit_content(
            skipped,
            feed.skipped,
            (*CATALOG_FIELDS, "reason"),
            ("business_date", "jc_num", "matchId", "captured_utc", "reason"),
            legacy=directory / "jc_skipped.csv" if month == LEGACY_AUDIT_MONTH else None,
        ),
    }
    previous = {path: path.read_bytes() if path.exists() else None for path in contents}
    try:
        for path, content in contents.items():
            if content != previous[path]:
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(path, content)
    except Exception:
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, content)
        raise


def append_market(
    session: Session, match_id: str, quotes: list[OddsQuote], captured: datetime
) -> int:
    """Save each changed market in full so later comparisons use one observation time."""
    first = quotes[0]
    where = (Odds.match_id == match_id, Odds.pool == first.pool)
    previous = session.scalar(
        select(func.max(Odds.snapshot_utc)).where(*where, Odds.snapshot_utc <= captured)
    )
    if previous is not None and captured - datetime.fromisoformat(previous) < timedelta(minutes=10):
        known = set(
            session.execute(
                select(Odds.line, Odds.selection, Odds.price).where(
                    *where, Odds.snapshot_utc == previous
                )
            ).all()
        )
        if known == {(q.line, q.selection, q.price) for q in quotes}:
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
        rescheduled = bool(
            current
            and current.status == "cancelled"
            and current.result_checked_utc
            and captured > datetime.fromisoformat(current.result_checked_utc)
            and item.kickoff_utc > datetime.fromisoformat(current.kickoff_utc)
        )
        if current and current.status != "scheduled" and not rescheduled:
            continue
        newest = session.scalar(
            select(func.max(Odds.snapshot_utc)).where(Odds.match_id == item.match_id)
        )
        if newest is None or datetime.fromisoformat(newest) <= captured:
            home = upsert_team(session, item.home, "jc")
            away = upsert_team(session, item.away, "jc")
            if current and (home.id, away.id) != (current.home_team_id, current.away_team_id):
                raise ValueError(f"{item.match_id} 在售场次与原有主客队身份不符")
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
                **({"result_checked_utc": captured} if rescheduled else {}),
            )
        for _, group in groupby(item.odds, key=lambda q: (q.pool, q.line)):
            quotes += append_market(session, item.match_id, list(group), captured)
        matches += 1
    return matches, quotes


def fetch_schedule(
    number_date: date | None = None, *, metadata: dict | None = None
) -> tuple[int, int]:
    feed = SportteryCalculator()
    try:
        path = feed.fetch_raw()
        captured = datetime.fromisoformat(feed.store.load_metadata(path)["fetched_at_utc"])
        games = feed.parse(feed.store.load(path), captured=captured)
    finally:
        feed.session.close()
    if number_date is not None:
        games = [g for g in games if g.business_date == number_date]
    engine = get_engine()
    try:
        save_schedule_audit(Path(engine.url.database).parent / "state/audit", feed)
        init_db(engine)
        with session_scope(engine) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            matches, quotes = save_schedule(session, games, captured)
    finally:
        engine.dispose()
    if metadata is not None:
        metadata["captured_utc"] = utc_stamp(captured)
    LOGGER.info("赛程 %d 场，新增报价 %d 项，快照 %s", matches, quotes, path)
    return matches, quotes
