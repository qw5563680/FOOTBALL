"""Public lottery announcements for the traditional 14-match game."""

import hashlib
import re
from datetime import UTC, date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from lxml import etree, html

from zucai.sources.base import RateLimiter, RawStore, Source

# The national site publishes amendments with the same appendix layout as the weekly notice.
# Municipal/provincial lottery centres also publish the issuing centre's notice on these lists.
INDEXES = (
    "https://www.sporttery.cn/ctzc/zcgg/index.html",
    "https://www.wztc.cn/zczq/index.shtml",
    "https://www.gstc.org.cn/newsList/110300",
)
_DETAIL_PATH = {
    "www.sporttery.cn": r"/ctzc/(?:zcgg|czgg)/\d{8}/\d+\.html",
    "www.wztc.cn": r"/system/\d{4}/\d{2}/\d{2}/\d+\.shtml",
    "www.gstc.org.cn": r"/newsDetail/110300/\d+",
}
_USER_AGENT = "Mozilla/5.0 (compatible; ZucaiResearch/0.1)"
_BEIJING = timezone(timedelta(hours=8))
_ISSUE = re.compile(r"足球彩票胜负游戏[（(]14场(?:和|、|及)任选9场[）)]第(\d{5})期")
_CLOCK = r"(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)"
_NOTICE = re.compile(r"^(?=.*足球彩票)(?=.*(?:调整|变更|下发))(?=.*(?:竞猜场次|停售时间|销售时间))")
_MAX_BYTES = 3 * 1024 * 1024
_MAX_ARTICLES = 12


class _UnresolvedNotice(ValueError):
    """A listed bulletin may supersede an older schedule."""


class UnreadableAmendments(_UnresolvedNotice):
    """Index evidence for unreadable articles, alongside the bodies successfully read."""

    def __init__(self, pending, notices, weekly_error=None):
        super().__init__("官方调整公告无法核对：" + "；".join(p["reason"] for p in pending))
        self.pending = pending
        self.notices = notices
        self.weekly_error = weekly_error


def _page(raw):
    # Wenzhou's pages declare GB2312. Recent place names can require its GB18030 superset.
    encoding = (
        "gb18030" if re.search(rb"charset\s*=\s*[\"']?(gb|gbk)", raw[:4096], re.I) else "utf-8-sig"
    )
    try:
        doc = html.fromstring(raw.decode(encoding))
    except (UnicodeError, etree.ParserError) as exc:
        raise ValueError("官方页面编码或 HTML 不可读") from exc
    for node in doc.xpath("//script|//style"):
        node.drop_tree()
    return doc


def _text(node):
    return " ".join(node.text_content().split())


def _utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=_BEIJING).astimezone(UTC).isoformat()


def _publication(doc):
    # Only the page's dateline (before any appendix), never a fixture date or sale deadline.
    candidates = doc.xpath('//*[@id="pubtime_baidu"]|//time|//meta[@name="PubDate"]/@content')
    preamble = doc.xpath("//h1/following::text()[count(preceding::table)=0]")
    for value in [*candidates, *preamble[:35]]:
        s = _text(value) if isinstance(value, etree._Element) else str(value)
        if dt := re.fullmatch(r"(?:时间[：:]\s*)?" + _CLOCK + r"(?:\s+竞彩网)?", s.strip()):
            return _utc(dt[1])
        if day := re.fullmatch(r"时间[：:]\s*(\d{4}-\d{2}-\d{2})", s.strip()):
            # A date-only dateline cannot order two notices on the same day.
            return _utc(day[1] + " 00:00:00")
    raise ValueError("官方公告缺少可核实的发布时间")


def _times(text):
    def time_for(label):
        values = re.findall(label + r"\s*[：:]\s*" + _CLOCK, text)
        return _utc(values[0]) if len(values) == 1 else None

    return time_for("开售时间"), time_for("停售时间")


def _schedule(table, issue):
    """The first row binds the game/issue. The four rightmost cells are never rowspans."""
    rows = [
        [_text(cell) for cell in row.xpath("./th|./td")]
        for row in table.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")
    ]
    if not rows or rows[0] != ["期号", "赛制", "序号", "主队", "客队", "比赛日期"]:
        return None
    if len(rows) < 2 or rows[1][:1] != [f"第{issue}期"]:
        return None
    matches = []
    for row in rows[1:]:
        if len(row) == 1 and (not row[0] or "开售时间" in row[0]):
            continue
        if len(row) < 4:
            return None
        seq, home, away, _ = row[-4:]
        if seq != str(len(matches) + 1) or not home or not away:
            return None
        matches.append({"seq": int(seq), "home": home, "away": away})
    if len(matches) != 14:
        return None
    times = _text(table)
    after = table.getnext()
    if after is not None and after.tag == "p" and _text(after).startswith("开售时间"):
        times += " " + _text(after)
    start, end = _times(times)
    if not start or not end or start >= end:
        return None
    return dict(fixtures=matches, sale_start_utc=start, sale_end_utc=end)


def _parse(raw, *, include_other_games=False):
    doc = _page(raw)
    title = "".join("".join(_text(node).split()) for node in doc.xpath("//h1"))
    if not _NOTICE.search(title):
        raise ValueError("官方正文标题不是足球彩票竞猜通知")
    content = doc.xpath('//*[@id="artibody" or @class="content-box"]') or [doc]
    body = content[0]
    full = _text(body)
    if "国家体育总局体育彩票管理中心" not in full:
        raise ValueError("缺少国家体育总局体育彩票管理中心落款")
    numbers = sorted(set(_ISSUE.findall(re.sub(r"\s+", "", full))))
    other_game = not numbers and ("6场半全场" in full or "4场进球" in full) and "14场" not in full
    if other_game and not include_other_games:
        return []
    if not numbers and not other_game:
        raise ValueError("无法定位14场胜负游戏及期号")
    published = _publication(doc)
    signed = re.search(r"国家体育总局体育彩票管理中心\s*(\d{4})年(\d{1,2})月(\d{1,2})日", full)
    if not signed:
        raise ValueError("无法确定体彩中心公告签发日期")
    notice_date = date(*map(int, signed.groups())).isoformat()
    if other_game:
        return [dict(game="other", title=title, published_at=published, notice_date=notice_date)]
    records = {
        n: dict(
            issue_no=n,
            title=title,
            published_at=published,
            kind="amendment" if re.search("调整|变更", title) else "schedule",
            notice_date=notice_date,
            fixtures=[],
            sale_start_utc=None,
            sale_end_utc=None,
        )
        for n in numbers
    }
    seen = set()
    for table in body.xpath(".//table"):
        # An issue number alone is ambiguous across lottery games. Bind this table to its
        # own adjacent game heading and require the same number inside the table.
        heading = table.xpath("preceding-sibling::*[normalize-space()][1]")
        if not heading:
            continue
        game = _ISSUE.search(re.sub(r"\s+", "", _text(heading[0])))
        if not game or game[1] not in records:
            continue
        n = game[1]
        if n in seen:
            raise ValueError(f"同一公告重复列出胜负游戏第{n}期场次表")
        seen.add(n)
        schedule = _schedule(table, n)
        if schedule:
            records[n].update(schedule)
    return list(records.values())


def parse_notice(
    raw: bytes, *, source_url: str, observed_utc: str, include_other_games=False
) -> list[dict]:
    """Offline parser; provenance refers to the bytes the caller actually supplies."""
    digest = hashlib.sha256(raw).hexdigest()
    return [
        {**row, "source_url": source_url, "raw_sha256": digest, "observed_utc": observed_utc}
        for row in _parse(raw, include_other_games=include_other_games)
    ]


def _links(raw, index):
    host = urlsplit(index).netloc
    found = {}
    for node in _page(raw).xpath("//a[@href]"):
        title = node.get("title") or _text(node)
        if not _NOTICE.search(title):
            continue
        url = urlsplit(urljoin(index, node.get("href")))
        if url.netloc != host or url.scheme not in {"http", "https"}:
            raise _UnresolvedNotice("官方目录包含未核准的公告地址")
        if not re.fullmatch(_DETAIL_PATH[host], url.path) or url.query or url.fragment:
            raise _UnresolvedNotice("官方目录的公告路径不符合预期")
        link = f"https://{host}{url.path}"
        # Some provincial lists repeat a link in a feature card and in the news feed.
        found[link] = re.sub(r"\s", "", title.split("通知")[0] + "通知")
    links = []
    schedules = 0
    for link in sorted(
        found, key=lambda url: int(re.findall(r"\d+", urlsplit(url).path)[-1]), reverse=True
    ):
        title = found[link]
        if "下发" in title:
            schedules += 1
            if schedules > 2:
                break
        if len(links) >= _MAX_ARTICLES:
            raise _UnresolvedNotice("两期周公告间的调整超过单次采集上限")
        links.append((link, title, "下发" in title and schedules > 1))
    if not links:
        raise ValueError("官方目录缺少近期场次安排")
    return links


class OfficialNotices(Source[dict]):
    name = "sfc_official"

    def __init__(self, session, store):
        self.session = session
        self.store = store
        self.limiter = RateLimiter(1)
        self.policies = {}

    def _get(self, url, *, robots=False):
        self.limiter.wait()
        with self.session.get(url, stream=True, timeout=(5, 10), allow_redirects=False) as response:
            if robots and response.status_code in {404, 410}:
                return b""
            if response.status_code != 200:
                raise ValueError(f"官方来源 HTTP {response.status_code} ({url})")
            pieces = bytearray()
            for chunk in response.iter_content(65536):
                pieces.extend(chunk)
                if len(pieces) > _MAX_BYTES:
                    raise ValueError("官方页面超过读取上限")
            return bytes(pieces)

    def fetch_raw(self, *, url):
        parsed = urlsplit(url)
        host = parsed.netloc
        if (
            parsed.scheme != "https"
            or host not in _DETAIL_PATH
            or (url not in INDEXES and not re.fullmatch(_DETAIL_PATH[host], parsed.path))
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("请求地址不属于已核准的官方目录或公告")
        if host not in self.policies:
            robot_url = f"https://{host}/robots.txt"
            policy = RobotFileParser(robot_url)
            raw = self._get(robot_url, robots=True)
            if raw.lstrip().startswith(b"<"):
                raise ValueError("robots.txt 返回非规则页面")
            policy.parse(raw.decode("utf-8-sig").splitlines())
            self.policies[host] = policy
        policy = self.policies[host]
        if not all(policy.can_fetch(agent, url) for agent in ("ZucaiResearch", _USER_AGENT)):
            raise ValueError(f"robots.txt 禁止抓取 {url}")
        delay = policy.crawl_delay("ZucaiResearch")
        if delay and delay > 10:
            raise ValueError(f"官方来源要求的抓取间隔超出本次预算 ({url})")
        if delay and delay > self.limiter.min_interval_s:
            self.limiter.min_interval_s = delay
        raw = self._get(url)
        return self.store.save(self.name, "notice", raw, "html", url=url, status_code=200)

    def parse(self, raw):
        return _parse(raw)


def fetch_official_notices(previous_pending=()) -> list[dict]:
    """Read recent schedules and revisions from each available official publisher.

    Return every version. A shared as-of selector reconciles mirrors by the issuing
    authority's date instead of letting an old schedule repost hide a newer amendment.
    """
    failures, rows, pending = [], [], []
    missing_weekly = {}
    read_titles = set()
    retry_urls = {p["source_url"] for p in previous_pending}
    with requests.Session() as session:
        session.headers["User-Agent"] = _USER_AGENT
        source = OfficialNotices(session, RawStore())
        for index in INDEXES:
            try:
                index_path = source.fetch_raw(url=index)
                links = _links(source.store.load(index_path), index)
                index_meta = source.store.load_metadata(index_path)
            except _UnresolvedNotice:
                raise
            except (requests.RequestException, ValueError) as exc:
                failures.append(f"{index}: {exc}")
                continue
            listed = {url for url, _, _ in links}
            retries = [
                (p["source_url"], p["title"], False)
                for p in previous_pending
                if p.get("index_url") == index
                and p["source_url"] not in listed
                and urlsplit(p["source_url"]).netloc == urlsplit(index).netloc
            ]
            for url, title, older_week in (links + retries)[:_MAX_ARTICLES]:
                # Do not hide a known but unreadable revision with an older mirror.
                try:
                    path = source.fetch_raw(url=url)
                    observed = source.store.load_metadata(path)["fetched_at_utc"]
                    parsed = parse_notice(
                        source.store.load(path),
                        source_url=url,
                        observed_utc=observed,
                        include_other_games=url in retry_urls,
                    )
                    if any(row["title"] != title for row in parsed):
                        raise ValueError("官方目录标题与正文不一致")
                    rows.extend(parsed)
                    read_titles.add(title)
                except (requests.RequestException, ValueError) as exc:
                    if url not in listed:
                        # Its original directory receipt is already pending. Today's
                        # directory did not list this URL and cannot attest discovery.
                        continue
                    # An older weekly appendix need not erase an accessible newer one.
                    # A missing initial notice may also be available on another channel.
                    if "下发" in title:
                        if not older_week:
                            missing_weekly[title] = f"{url}: {exc}"
                        continue
                    # The index proves discovery, not the contents or signing date of the
                    # missing body. Date-only or multi-issue titles have no reliable scope.
                    hint = re.search(r"第(\d{5})期", title)
                    pending.append(
                        dict(
                            source_url=url,
                            title=title,
                            issue_hint=hint[1]
                            if hint and len(re.findall(r"\d{5}", title)) == 1
                            else None,
                            first_observed_utc=index_meta["fetched_at_utc"],
                            failed_at_utc=datetime.now(UTC).isoformat(),
                            index_url=index,
                            index_sha256=index_meta["sha256"],
                            reason=str(exc),
                        )
                    )
    unresolved = set(missing_weekly) - read_titles
    weekly_error = None
    if unresolved:
        weekly_error = "官方周公告无法核对：" + "；".join(
            missing_weekly[t] for t in sorted(unresolved)
        )
    if pending:
        raise UnreadableAmendments(pending, [] if weekly_error else rows, weekly_error)
    if weekly_error:
        raise _UnresolvedNotice(weekly_error)
    if not rows:
        raise requests.RequestException("官方目录不可用：" + "；".join(failures))
    return rows
