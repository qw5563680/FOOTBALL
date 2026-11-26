"""Conservative, offline inspection of visible schedule markup and public links."""

import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from zucai.okooo_probe import inspect_page as inspect_okooo_sfc

_COLUMNS = {
    "period": ("期号", "期次"),
    "seq": ("场", "场次", "场序", "序号", "竞彩编号", "场次编号"),
    "league": ("赛事", "赛事名称", "比赛赛事", "联赛", "联赛名称"),
    "home": ("主队", "主场球队", "主队名称"),
    "away": ("客队", "客场球队", "客队名称"),
    "kickoff": ("开球时间", "比赛时间", "开赛时间", "赛事时间"),
    "sale_end": ("停售时间", "销售截止时间", "投注截止时间"),
    "fixture_id": ("比赛ID", "比赛编号"),
}
_HEADERS = {label: field for field, labels in _COLUMNS.items() for label in labels}
_PURPOSE = {
    "sfc": re.compile(r"胜负彩|胜负游戏|传统足彩|传足|(?:14|十四)\s*场"),
    "jc": re.compile(r"竞彩足球|足球竞彩|竞足|竞彩(?:足球)?\s*(?:赛程|对阵)"),
}
_SKIP_LINK = re.compile(r"赔率|指数|预测|推荐|开奖|分析|资讯|比分|专家|购买|登录|注册|篮球")
_SKIP_PATH = re.compile(
    r"/(?:api|ajax|json|data|graphql|login|signin|register|passport|auth|pay|checkout|buy|bet|"
    r"vip|expert|analysis|news|basketball|nba)[./_-]|/(?:odds|peilv|pankou|price|chart)",
    re.I,
)
_JC_PATH = re.compile(r"/(?:jingcai|jczq|zuqiu|soccer|football|schedule|jc)(?:[./_-]|$)", re.I)
_TIMESTAMP = re.compile(
    r"(?:(\d{4})(?:年|[-/.]))?(\d{1,2})(?:月|[-/.])(\d{1,2})(?:日)?"
    r"[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*(?:北京时间|Z|[+-]\d{2}:\d{2}))?"
)
_ACCESS = re.compile(
    r"access denied|\b405\b|forbidden|访问受限|访问验证|人机验证|安全验证|验证码", re.I
)
_LOGIN = re.compile(r"^(?:用户登录|登录|注册|login|sign\s+in)$|请(?:先)?登录", re.I)
_JS_NOTICE = re.compile(
    r"(?:enable|requires?|disabled).*javascript|(?:启用|开启).*javascript", re.I
)
_SAMPLE_SIZE = 3


def _document(raw):
    try:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            text = raw.decode("gb18030")
        return html.document_fromstring(text)
    except (UnicodeError, etree.ParserError, ValueError):
        return None


def _visible(document):
    hidden = document.xpath(
        "//script|//noscript|//style|//template|//iframe|//svg|//*[@hidden or @aria-hidden='true']"
        "|//*[contains(concat(' ', normalize-space(@class), ' '), ' d-none ')"
        " or contains(concat(' ', normalize-space(@class), ' '), ' hidden ')]"
    )
    hidden.extend(
        node
        for node in document.xpath("//*[@style]")
        if re.search(
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", node.get("style", ""), re.I
        )
    )
    for node in hidden:
        if node.getparent() is not None:
            node.drop_tree()


def _text(node):
    return " ".join(node.text_content().split())


def _label(value):
    # Team and competition fields carry short text labels, never standalone prices.
    if (
        len(value) <= 48
        and any(char.isalpha() for char in value)
        and re.fullmatch(r"[\w ()（）.&'’·+\-–]+", value)
        and not re.search(r"\d+\.\d+", value)
    ):
        return value


def _time(value):
    match = _TIMESTAMP.fullmatch(value)
    if match:
        year, month, day, hour, minute, second = match.groups()
        try:
            datetime(
                int(year or 2000), int(month), int(day), int(hour), int(minute), int(second or 0)
            )
        except ValueError:
            return None
        # Keep the printed evidence if the page gives no year or timezone.
        return value


def _field(field, cell):
    value = _text(cell)
    if field in ("home", "away", "league"):
        return _label(value)
    if field in ("kickoff", "sale_end"):
        return _time(value)
    if field == "period":
        match = re.fullmatch(r"(?:第\s*)?(\d{5}|\d{7})(?:\s*期)?", value)
        return match[1] if match else None
    if field == "seq":
        return value if re.fullmatch(r"(?:周[一二三四五六日]\s*)?\d{1,3}", value) else None
    if field == "fixture_id":
        return value if re.fullmatch(r"\d{1,15}", value) else None


def _rows(table, headers):
    width = 0
    columns = {}
    for tr in table.xpath("./tr|./thead/tr|./tbody/tr"):
        cells = tr.xpath("./th|./td")
        if any(
            cell.get("rowspan", "1") != "1" or cell.get("colspan", "1") != "1" for cell in cells
        ):
            columns.clear()
            continue
        if not columns or any(cell.tag == "th" for cell in cells):
            recognized = [
                _HEADERS.get(re.sub(r"[（(]北京时间[)）]$", "", _text(cell)).replace(" ", ""))
                for cell in cells
            ]
            fields = [field for field in recognized if field]
            if {"home", "away", "kickoff"} <= set(fields):
                headers.update(fields)
            else:
                continue
            columns = (
                {field: index for index, field in enumerate(recognized) if field}
                if len(fields) == len(set(fields))
                else {}
            )
            width = len(cells)
        elif columns and len(cells) == width and all(cell.tag == "td" for cell in cells):
            row = {}
            for field, index in columns.items():
                if (value := _field(field, cells[index])) is not None:
                    row[field] = value
            if row.get("home") and row.get("away") and row["home"] != row["away"]:
                yield row


def _purposes(label):
    return [purpose for purpose, pattern in _PURPOSE.items() if pattern.search(label)]


def _table_matches(table, purpose, page_labels):
    local = " ".join(
        _text(node)
        for node in table.xpath("./caption|preceding-sibling::*[1][self::h2 or self::h3]")
    )
    local_labels = _purposes(local)
    if local_labels:
        return local_labels == [purpose]
    return page_labels == [purpose] and not re.search(r"比分|战绩|积分|篮球|结果", local)


def _js_signal(document):
    if any(_JS_NOTICE.search(_text(node)) for node in document.xpath("//noscript")):
        return "enable_javascript_notice"
    for node in document.xpath("//script[not(@src)]|//template"):
        markup = html.tostring(node, encoding="unicode")
        if "<tr" in markup and (
            "homenameobj" in markup
            and "awaynameobj" in markup
            or "主队" in markup
            and "客队" in markup
        ):
            return "fixtures_in_embedded_markup"
    if document.xpath("//script[@src]") and any(
        not _text(node) and not len(node)
        for node in document.xpath("//*[@id='app' or @id='root' or @id='__next']")
    ):
        return "empty_application_root"


def _known_okooo_rows(document):
    # Only the existing explicit Okooo structure runs this parser. Drop markets links first.
    if not document.xpath(
        '//table[@id="table4"]//td[contains(@class,"homenameobj")]'
        '|//table[@id="table4"]//span[contains(@class,"homenameobj")]'
    ):
        return []
    for link in document.xpath('//a[contains(@href,"/odds/")]'):
        link.drop_tree()
    page = inspect_okooo_sfc(
        html.tostring(document, encoding="utf-8"), None, "sfc", "1970-01-01T00:00:00+00:00"
    )
    rows = []
    for source in page["rows"]:
        home, away = _label(source["home"]), _label(source["away"])
        kickoff = source["kickoff_beijing"]
        if home and away and home != away and kickoff:
            row = dict(
                period=page["issue_no"],
                seq=source["seq"],
                league=_label(source["league"]),
                home=home,
                away=away,
                kickoff=datetime.fromisoformat(kickoff).astimezone(UTC).isoformat(),
                sale_end=page["sale_end_utc"],
                fixture_id=source["okooo_fixture_id"],
            )
            rows.append({key: value for key, value in row.items() if value is not None})
    return rows


def inspect_page(raw: bytes, purpose: str) -> dict:
    """Return classified evidence, cropped to explicitly identified schedule fields."""
    evidence = dict(purpose_labels=[], headers=[], pair_rows=0, row_source=None, js_signal=None)
    result = dict(
        status="unconfirmed",
        reason="HTML 无法解析",
        fields=[],
        complete_fields=[],
        sample=[],
        row_count=0,
        sfc_order_complete=False,
        evidence=evidence,
    )
    document = _document(raw)
    if document is None:
        return result
    evidence["js_signal"] = _js_signal(document)
    _visible(document)
    titles = " ".join(_text(node) for node in document.xpath("//title|//h1"))
    body = " ".join(_text(node) for node in document.xpath("//body"))[:300]
    if _ACCESS.search(titles):
        return dict(result, status="blocked", reason="静态页面为访问拦截或验证页")
    page_labels = _purposes(titles)
    evidence["purpose_labels"] = page_labels
    rows = _known_okooo_rows(document) if purpose == "sfc" and page_labels == [purpose] else []
    if rows:
        evidence["row_source"] = "okooo_sfc_markup"
    else:
        headers = set()
        rows = [
            row
            for table in document.xpath("//table")
            if _table_matches(table, purpose, page_labels)
            for row in _rows(table, headers)
        ]
        evidence["headers"] = [field for field in _COLUMNS if field in headers]
        if rows:
            evidence["row_source"] = "labelled_static_table"
    if rows:
        with_time = [row for row in rows if row.get("kickoff")]
        evidence["pair_rows"] = len(rows)
        result.update(
            fields=[field for field in _COLUMNS if any(field in row for row in rows)],
            complete_fields=[field for field in _COLUMNS if all(field in row for row in rows)],
            sample=(with_time or rows)[:_SAMPLE_SIZE],
            row_count=len(with_time),
        )
        if purpose == "sfc":
            result["sfc_order_complete"] = (
                len(rows) == len(with_time) == 14
                and None not in {row.get("period") for row in rows}
                and len({row["period"] for row in rows}) == 1
                and [str(row.get("seq")) for row in rows] == [str(seq) for seq in range(1, 15)]
            )
        if with_time:
            return dict(result, status="readable", reason="静态 HTML 有明确的主客队及开球时间")
    if _ACCESS.search(body) or _LOGIN.search(titles.strip()) or _LOGIN.search(body):
        return dict(
            result,
            status="blocked",
            reason="静态页面要求登录或验证",
            fields=[],
            complete_fields=[],
            sample=[],
        )
    if purpose in page_labels and evidence["js_signal"]:
        return dict(
            result,
            status="requires_js",
            reason="静态正文没有可核对的对阵和开球时间；页面有脚本或嵌入内容",
        )
    return dict(result, reason="未核实所需玩法，或静态正文没有可核对的对阵和开球时间")


def _public_path(href, origin):
    try:
        parsed = urlsplit(urljoin(origin + "/", href))
        if (
            parsed.scheme == "https"
            and parsed.hostname == urlsplit(origin).hostname
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"/[\w/-]+(?:\.(?:html?|aspx))?", parsed.path, re.ASCII)
            and not _SKIP_PATH.search(parsed.path.rstrip("/") + "/")
            and not re.search(r"(?:^|/)\.\.?(/|$)|[?#\\]", href)
            and "//" not in parsed.path
        ):
            return origin + parsed.path
    except ValueError:
        return None


def discover_pages(raw: bytes, origin: str) -> dict[str, list[str]]:
    """Find explicitly labeled, same-origin schedule links in the public homepage."""
    result = {purpose: [] for purpose in _PURPOSE}
    document = _document(raw)
    if document is None:
        return result
    _visible(document)
    for link in document.xpath("//a[@href]"):
        label = _text(link)
        if _SKIP_LINK.search(label):
            continue
        url = _public_path(link.get("href"), origin.rstrip("/"))
        if url is None:
            continue
        purposes = _purposes(label)
        if label == "竞彩" and _JC_PATH.search(urlsplit(url).path):
            purposes = ["jc"]
        if len(purposes) == 1:
            links = result[purposes[0]]
            if url not in links:
                links.append(url)
    return result
