"""500 根页的近期备用赛果。只取已结算精确选项，不支持历史查询。"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from lxml import html
from lxml.etree import ParserError

from zucai.sources.base import RateLimiter, RawStore, Source, make_session
from zucai.sources.results import VOID, GameResult, score
from zucai.sources.sporttery import BEIJING, beijing_kickoff

URL = "https://trade.500.com/jczq/"
LOGGER = logging.getLogger(__name__)


class C500JcHistory(Source[GameResult]):
    name = "c500-results"

    def __init__(self, store=None, session=None):
        self.rejected = []
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else make_session()
        self.limiter = RateLimiter(1)

    def fetch_raw(self) -> Path:
        self.limiter.wait()
        r = self.session.get(URL, allow_redirects=False)
        path = self.store.save_response(self.name, "current", r, "html")
        r.raise_for_status()
        if 300 <= r.status_code < 400:
            raise ValueError("500根页发生重定向，拒绝跟随未核实的路径")
        return path

    def parse(self, raw: bytes) -> list[GameResult]:
        doc = html.fromstring(raw, parser=html.HTMLParser(encoding="gbk"))
        rows = doc.xpath("//tr[@data-fixtureid and @data-matchdate]")
        if not rows and not doc.xpath("//*[@id='betTbThead']"):
            raise ValueError("500页面没有竞彩比赛表")
        out = []
        for row in rows:
            score_text = row.xpath("string(.//a[@class='score'])").strip()
            void = score_text in VOID
            sister = row.getnext()
            winners = (
                []
                if sister is None or sister.get("data-fixtureid")
                else sister.xpath(
                    ".//*[@data-type='bf' and "
                    "contains(concat(' ',normalize-space(@class),' '),' sbetbtn-ok ')]/@data-value"
                )
            )
            full = score(winners[0]) if len(winners) == 1 else None
            if full is None and not void:
                if winners:
                    LOGGER.warning(
                        "500 %s 没有精确的已结算比分，保留未知", row.get("data-fixtureid")
                    )
                continue
            when = beijing_kickoff(f"{row.attrib['data-matchdate']} {row.attrib['data-matchtime']}")
            sid = row.get("data-id", "")
            out.append(
                GameResult(
                    f"c500:{int(row.attrib['data-fixtureid'])}",
                    row.attrib["data-simpleleague"],
                    row.attrib["data-matchnum"],
                    row.attrib["data-homesxname"],
                    row.attrib["data-awaysxname"],
                    when.astimezone(BEIJING).date(),
                    "cancelled" if void else "finished",
                    None if void else full,
                    None,
                    kickoff_utc=when,
                    official_id=f"jc:{sid}" if sid.isdecimal() else None,
                )
            )
        return out

    def range_results(self, start: date, end: date) -> list[tuple[GameResult, datetime]]:
        """Validate the entire kickoff-date request before returning any results."""
        path = self.fetch_raw()
        raw = self.store.load(path)
        try:
            doc = html.fromstring(raw, parser=html.HTMLParser(encoding="gbk"))
        except ParserError as exc:
            raise ValueError("500近期备用根页为空或无法解析，无法确定日期覆盖范围") from exc
        rows = doc.xpath("//tr[@data-fixtureid]")
        if not doc.xpath("//*[@id='betTbThead']") or not rows:
            raise ValueError(
                "500近期备用根页没有比赛，无法确定日期覆盖范围；历史回填请用 --source jc"
            )
        try:
            covered = {date.fromisoformat(row.attrib["data-matchdate"]) for row in rows}
            number_days = {date.fromisoformat(row.attrib["data-processdate"]) for row in rows}
        except (KeyError, ValueError) as exc:
            raise ValueError("500近期备用根页比赛日期缺失或无效，无法确定覆盖范围") from exc
        requested = {start + timedelta(days=i) for i in range((end - start).days + 1)}
        if start > end or not requested <= covered:
            raise ValueError(
                f"500仅为近期备用，根页开球自然日覆盖 {', '.join(map(str, sorted(covered)))}；"
                f"请求 {start} 至 {end} 超出覆盖范围，历史回填请用 --source jc"
            )
        captured = datetime.fromisoformat(self.store.load_metadata(path)["fetched_at_utc"])
        games = [g for g in self.parse(raw) if start <= g.play_date <= end]
        LOGGER.info(
            "500近期备用根页：编号日 %s，开球自然日 %s，%d 场，范围内已结算精确赛果/无效 %d 场",
            ", ".join(map(str, sorted(number_days))),
            ", ".join(map(str, sorted(covered))),
            len(rows),
            len(games),
        )
        return [(g, captured) for g in games]


@dataclass(frozen=True)
class SfcFixture:
    seq: int
    fixture_id: str
    league: str
    home: str
    away: str
    kickoff_utc: datetime
    ended: bool
    home_id: str | None = None
    away_id: str | None = None


@dataclass(frozen=True)
class SfcSnapshot:
    issue_no: str
    sale_end_utc: datetime | None
    fixtures: list[SfcFixture]
    date_basis: str


def issue_number(value: str) -> str:
    if not re.fullmatch(r"[0-9]{5}", value) or value.endswith("000"):
        raise ValueError("期号须为5位YYNNN，例如26156")
    return value


def _cell(row, class_name):
    nodes = row.xpath(
        ".//*[contains(concat(' ',normalize-space(@class),' '), $c)]", c=f" {class_name} "
    )
    if len(nodes) != 1:
        raise ValueError(f"14场页面字段缺失或重复：{class_name}")
    return nodes[0].text_content().strip()


class C500Sfc(Source[SfcSnapshot]):
    """Read fixture identity only; never interpret page odds or ranks as a model."""

    name = "c500-sfc"
    url = "https://trade.500.com/sfc/"

    def __init__(self, store=None, session=None):
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else make_session()
        self.limiter = RateLimiter(1)

    def fetch_raw(self, *, issue: str | None = None, current_only=False) -> Path:
        if issue is not None:
            issue_number(issue)
        self.limiter.wait()
        if current_only:
            if issue is not None:
                raise ValueError("current-only fetch cannot select an issue")
            from requests.adapters import HTTPAdapter

            self.session.mount("https://", HTTPAdapter(max_retries=0))
            response = self.session.get(self.url, allow_redirects=False)
            if 300 <= response.status_code < 400:
                raise ValueError("SFC redirect rejected")
        else:
            response = self.session.get(self.url, params={"expect": issue} if issue else {})
        path = self.store.save_response(self.name, f"issue-{issue or 'current'}", response, "html")
        response.raise_for_status()
        return path

    def parse(self, raw: bytes) -> list[SfcSnapshot]:
        doc = html.fromstring(raw.decode("gb18030"))
        numbers = doc.xpath("//input[@id='expect']/@value")
        ends = doc.xpath("//input[@id='buyendtime']/@value")
        if len(numbers) != 1 or len(ends) != 1:
            raise ValueError("不是完整的14场期次页面")
        number = issue_number(numbers[0])
        year = 2000 + int(number[:2])
        end = beijing_kickoff(ends[0]) if ends[0] else None
        if end and abs(end.astimezone(BEIJING).year - year) > 1:
            raise ValueError("截止年份与期号冲突")
        rows = doc.xpath("//table[@id='vsTable']//tr[@data-cid]")
        if len(rows) != 14 or sorted(int(r.get("data-cid")) for r in rows) != list(range(1, 15)):
            raise ValueError("期次必须包含不重复的1至14场")
        fixtures = []
        for row in rows:
            links = row.xpath(".//a[contains(@href,'/fenxi/shuju-')]/@href")
            ids = [re.search(r"/fenxi/shuju-([0-9]+)\.shtml$", x) for x in links]
            if len(ids) != 1 or ids[0] is None:
                raise ValueError("比赛分析ID缺失或不唯一")
            names = [row.xpath(f".//a[@class='team-{side}']/text()") for side in ("l", "r")]
            if any(len(n) != 1 or not n[0].strip() for n in names):
                raise ValueError("主客队字段不完整")
            team_ids = []
            for side in ("l", "r"):
                team_links = row.xpath(f".//a[@class='team-{side}']/@href")
                provider_id = (
                    re.search(r"/team/([0-9]+)/?$", team_links[0]) if len(team_links) == 1 else None
                )
                if provider_id is None:
                    raise ValueError("主客球队ID缺失")
                team_ids.append(provider_id[1])
            short = _cell(row, "td-endtime")
            if not re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}", short):
                raise ValueError("比赛时间未知或格式发生变化")
            if end:
                # The full source deadline anchors short dates across December/January.
                anchor = end.astimezone(BEIJING).year
                dates = []
                for y in (anchor - 1, anchor, anchor + 1):
                    try:
                        dates.append(beijing_kickoff(f"{y}-{short}"))
                    except ValueError:
                        continue
                candidates = [
                    d for d in dates if end - timedelta(days=7) < d < end + timedelta(days=30)
                ]
                if len(candidates) != 1:
                    raise ValueError("无法根据截止时间确定比赛年份")
                kickoff = candidates[0]
            else:
                # No full date anchors the year. Boundary-month historical pages
                # may contain only the next/previous year, so span checks alone
                # cannot detect an off-by-one year. Require better evidence.
                if short[:2] in {"01", "12"}:
                    raise ValueError("历史年末/年初比赛年份未核实，需完整来源日期")
                kickoff = beijing_kickoff(f"{year}-{short}")
            fixtures.append(
                SfcFixture(
                    int(row.get("data-cid")),
                    ids[0][1],
                    _cell(row, "td-evt"),
                    names[0][0].strip(),
                    names[1][0].strip(),
                    kickoff.astimezone(UTC),
                    row.get("data-isend") == "1",
                    *team_ids,
                )
            )
        if len({f.fixture_id for f in fixtures}) != 14:
            raise ValueError("期次包含重复的比赛ID")
        if not end and max(f.kickoff_utc for f in fixtures) - min(
            f.kickoff_utc for f in fixtures
        ) > timedelta(days=30):
            raise ValueError("历史跨年日期存在歧义，需完整来源日期")
        return [
            SfcSnapshot(
                number,
                end,
                sorted(fixtures, key=lambda f: f.seq),
                "deadline_anchored" if end else "issue_year_assumed",
            )
        ]


class C500FixtureBridge(Source[dict]):
    """The public current JC page links provider fixture/team IDs to sporttery IDs."""

    name = "c500-sfc-bridge"

    def __init__(self, store=None, session=None):
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else make_session()
        self.limiter = RateLimiter(1)

    def fetch_raw(self) -> Path:
        self.limiter.wait()
        response = self.session.get(URL)  # Root only; robots excludes historical query strings.
        path = self.store.save_response(self.name, "current", response, "html")
        response.raise_for_status()
        return path

    def parse(self, raw: bytes) -> list[dict]:
        # Ads can contain non-GB bytes. Reject replacement characters in consumed
        # identity fields, while ignoring unrelated ad content.
        doc = html.fromstring(raw.decode("gb18030", errors="replace"))
        if not doc.xpath('//*[@id="betTbThead"]'):
            raise ValueError("500比赛ID对照页不完整")
        out = []
        for row in doc.xpath("//tr[@data-fixtureid and @data-id]"):
            values = {
                key: row.get(attr, "")
                for key, attr in {
                    "fixture_id": "data-fixtureid",
                    "jc_id": "data-id",
                    "home_id": "data-homeid",
                    "away_id": "data-awayid",
                    "home": "data-homesxname",
                    "away": "data-awaysxname",
                    "league": "data-simpleleague",
                    "jc_num": "data-matchnum",
                }.items()
            }
            if not values["jc_id"].isdigit() or int(values["jc_id"]) <= 0:
                continue
            if any(
                not values[k].isdigit() or int(values[k]) <= 0
                for k in ("fixture_id", "home_id", "away_id")
            ):
                raise ValueError("比赛ID对照缺少球队ID")
            if any(not values[k] or "\ufffd" in values[k] for k in ("home", "away", "league")):
                raise ValueError("比赛ID对照身份缺失或解码失败")
            values["kickoff_utc"] = beijing_kickoff(
                f"{row.get('data-matchdate', '')} {row.get('data-matchtime', '')}"
            ).isoformat()
            values["jc_id"] = "jc:" + values["jc_id"]
            out.append(values)
        if len({x["fixture_id"] for x in out}) != len(out) or len({x["jc_id"] for x in out}) != len(
            out
        ):
            raise ValueError("比赛ID对照重复，不能自动匹配")
        return out
