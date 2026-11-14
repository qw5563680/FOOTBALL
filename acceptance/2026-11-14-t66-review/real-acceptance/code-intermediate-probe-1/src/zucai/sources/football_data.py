"""Football-data.co.uk CSV adapter; identity and write policy live in fd_ingest."""

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from requests.adapters import HTTPAdapter

from zucai.league_registry import provider_config
from zucai.sources.base import RateLimiter, RawStore, Source, make_session

CONFIG_ROOT = Path(__file__).resolve().parents[3] / "data"
LEAGUES_PATH = CONFIG_ROOT / "football_data_co_uk/leagues.json"
ALIASES_PATH = CONFIG_ROOT / "team_aliases/football_data_co_uk.json"
ROOT_URL = "https://www.football-data.co.uk"


@dataclass(frozen=True)
class FdGame:
    div_code: str
    season: str
    kickoff_utc: datetime
    home_raw: str
    away_raw: str
    full: tuple[int, int] | None
    half: tuple[int, int] | None
    closing: dict[str, tuple[float, float, float]]
    row_number: int = 0
    raw_sha256: str = ""


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def verified_league(leagues, code):
    entry = leagues.get(code, {})
    if (
        entry.get("div_code") != code
        or entry.get("status") != "verified"
        or type(entry.get("verified_by")) is not int
        or entry["verified_by"] < 1
        or not entry.get("league")
        or not entry.get("verified_utc")
    ):
        raise ValueError(f"{code} 时区尚未通过jc比赛核验")
    verified = datetime.fromisoformat(entry["verified_utc"])
    if verified.utcoffset() is None:
        raise ValueError("时区核验时间须含UTC偏移")
    ZoneInfo(entry["time_basis"])
    return entry


def local_kickoff(day, clock, time_basis):
    if not re.fullmatch(r"\d{2}:\d{2}", clock):
        raise ValueError("Time缺失或无效")
    fmt = "%d/%m/%Y" if len(day.split("/")[-1]) == 4 else "%d/%m/%y"
    naive = datetime.strptime(day + " " + clock, fmt + " %H:%M")
    zone = ZoneInfo(time_basis)
    candidates = set()
    for fold in (0, 1):
        instant = naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == naive:
            candidates.add(instant)
    if len(candidates) != 1:
        raise ValueError("Time处于夏令时歧义或不存在的本地时刻")
    return candidates.pop()


def season_start(value):
    if not re.fullmatch(r"\d{4}(?:/\d{4})?", str(value)):
        raise ValueError("合并文件Season格式无效")
    parts = str(value).split("/")
    start = int(parts[0])
    if not 1900 <= start <= 2099 or (len(parts) == 2 and int(parts[1]) != start + 1):
        raise ValueError("Season年份无效")
    return start


def _goals(home, away, *, optional=False):
    if optional and home == away == "":
        return None
    if not all(re.fullmatch(r"\d{1,2}", value) for value in (home, away)):
        raise ValueError("比分缺失或不是非负整数")
    return int(home), int(away)


def _outcome(pair):
    return "H" if pair[0] > pair[1] else "A" if pair[0] < pair[1] else "D"


def csv_url(*, div=None, season=None, country=None):
    if bool(div) == bool(country):
        raise ValueError("指定一种来源：--div或--country")
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,3}", div or country):
        raise ValueError("无效联赛代码")
    if div:
        if not isinstance(season, str) or not re.fullmatch(r"\d{4}", season):
            raise ValueError("按季文件须指定四位season，例如2526")
        if (int(season[:2]) + 1) % 100 != int(season[2:]):
            raise ValueError("season起止年份必须连续")
        return f"{ROOT_URL}/mmz4281/{season}/{div}.csv"
    if season is not None or not re.fullmatch(r"[A-Z]{3}", country):
        raise ValueError("合并文件使用三字母country，不使用season参数")
    return f"{ROOT_URL}/new/{country}.csv"


class FootballDataCoUk(Source[FdGame]):
    name = "football-data-co-uk"
    limiter = RateLimiter(2)

    def __init__(self, store=None, session=None, leagues=None, div_code=None, season=None):
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else make_session()
        if session is None:
            # Retries inside requests could violate the source's two-second spacing.
            self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.leagues = provider_config("fd") if leagues is None else leagues
        self.div_code, self.season = div_code, season
        self.rejected = []

    def fetch_raw(self, *, div=None, season=None, country=None):
        url = csv_url(div=div, season=season, country=country)
        code = div or country
        verified_league(self.leagues, code)
        self.div_code, self.season = code, season
        allowed = {url, url.replace("//www.", "//")}
        visited = set()
        for _ in range(3):
            if url not in allowed or url in visited:
                raise ValueError("重定向改变来源/文件身份或出现循环")
            visited.add(url)
            self.limiter.wait()
            response = self.session.get(url, allow_redirects=False)
            path = self.store.save_response(self.name, f"{code}-{season or 'all'}", response, "csv")
            response.raise_for_status()
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(response.url, response.headers.get("Location", ""))
                continue
            if response.status_code != 200:
                raise ValueError(f"CSV响应须为HTTP200，实际{response.status_code}")
            return path
        raise ValueError("重定向超过允许次数")

    def parse_statistics(self, raw):
        from zucai.sources.fd_stats import parse_fd_statistics

        games, self.rejected = parse_fd_statistics(raw, self.div_code, self.season, self.leagues)
        return games

    def parse(self, raw):
        self.rejected = []
        entry = verified_league(self.leagues, self.div_code)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        header = reader.fieldnames or []
        named = [c for c in header if c]
        main = "Div" in header
        required = (
            {"Div", "Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
            if main
            else {"Country", "League", "Season", "Date", "Home", "Away", "HG", "AG", "Res"}
        )
        if not required.issubset(header) or len(named) != len(set(named)):
            raise ValueError("CSV列头缺少身份/赛果字段或重复，未导入此文件")
        if main:
            csv_url(div=self.div_code, season=self.season)
        digest = hashlib.sha256(raw).hexdigest()
        games, seen, invalid = {}, {}, set()
        try:
            for row_number, raw_row in enumerate(reader, start=2):
                row = {k: (v or "").strip() for k, v in raw_row.items() if k is not None}
                if not any(row.values()):
                    continue
                if main and row["Div"] != self.div_code:
                    raise ValueError("CSV Div与请求联赛不符，未导入此文件")
                if not main and (
                    entry.get("publisher_country", row["Country"]) != row["Country"]
                    or entry.get("publisher_league", row["League"]) != row["League"]
                ):
                    raise ValueError("CSV Country/League与配置不符，未导入此文件")
                audit = dict(
                    div_code=self.div_code,
                    row_number=row_number,
                    raw_sha256=digest,
                    date=row["Date"],
                    season=self.season if main else row["Season"],
                    home=row.get("HomeTeam", row.get("Home", "")),
                    away=row.get("AwayTeam", row.get("Away", "")),
                )
                try:
                    if raw_row.get(None):
                        raise ValueError("CSV行列数溢出")
                    game = self._row(row, row_number, digest, entry, main)
                    key = (game.season, game.kickoff_utc, game.home_raw, game.away_raw)
                    if key in seen:
                        if key not in invalid:
                            self.rejected.append(dict(seen[key], reason="文件内重复比赛"))
                            games.pop(key, None)
                        invalid.add(key)
                        raise ValueError("文件内重复比赛")
                    seen[key] = audit
                    games[key] = game
                except (ValueError, KeyError, OverflowError) as exc:
                    self.rejected.append(dict(audit, reason=str(exc)))
        except csv.Error as exc:
            raise ValueError("CSV结构无法完整读取，未导入此文件") from exc
        return list(games.values())

    def _row(self, row, row_number, digest, entry, main):
        home, away = (row["HomeTeam"], row["AwayTeam"]) if main else (row["Home"], row["Away"])
        if not home or not away or home.casefold() == away.casefold():
            raise ValueError("主客队缺失或相同")
        when = local_kickoff(row["Date"], row.get("Time", ""), entry["time_basis"])
        for equivalent in entry.get("equivalent_time_bases_in_observed_sample", []):
            if local_kickoff(row["Date"], row.get("Time", ""), equivalent) != when:
                raise ValueError("样本中等价的时区在本行不再一致，须重新核验")
        season = self.season if main else row["Season"]
        if not main:
            season_start(season)
        full = _goals(row["FTHG"], row["FTAG"]) if main else _goals(row["HG"], row["AG"])
        half = _goals(row.get("HTHG", ""), row.get("HTAG", ""), optional=True)
        if row["FTR" if main else "Res"] != _outcome(full):
            raise ValueError("胜平负代码与比分冲突")
        if half and (half[0] > full[0] or half[1] > full[1]):
            raise ValueError("半场比分大于全场比分")
        if half and row.get("HTR") and row["HTR"] != _outcome(half):
            raise ValueError("半场结果代码与比分冲突")
        closing = {}
        for book in ("PS", "B365", "Avg", "Max"):
            try:
                values = tuple(float(row.get(f"{book}C{k}", "")) for k in "HDA")
            except ValueError:
                continue
            if all(math.isfinite(v) and v > 1 for v in values):
                closing[book] = values
        return FdGame(
            self.div_code, season, when, home, away, full, half, closing, row_number, digest
        )
