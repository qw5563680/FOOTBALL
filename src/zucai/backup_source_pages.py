"""Conservative, offline inspection of visible schedule markup and public links."""

import re
from datetime import UTC, datetime
from itertools import islice
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
_RELATED_LINK = re.compile(r"足彩|胜负彩|竞彩|传足|竞足|(?:14|十四)\s*场|对阵")
_SKIP_LINK = re.compile(
    r"赔率|指数|预测|推荐|开奖|分析|资讯|比分|专家|购买|登录|注册|篮球|介绍|玩法规则|使用帮助"
)
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
_KEYWORDS = {
    "期": re.compile(r"期"),
    "胜负彩": re.compile(r"胜负彩"),
    "14 场": re.compile(r"(?:14|十四)\s*场"),
    "竞彩": re.compile(r"竞彩"),
    "对阵": re.compile(r"对阵"),
}
_OTHER_PAGE = re.compile(r"开奖|赛果|比分|战绩|积分|篮球|购买|预测|资讯|新闻|规则|公告|赔率|指数")
_SCHEDULE_TEXT = re.compile(r"赛程|对阵|场次")
_PRICE_TEXT = re.compile(
    r"(?:赔率|奖金|返奖|派奖|赔付|派彩|投注额)\s*(?:为|[:：=])?\s*"
    r"(?:[$￥¥€£]\s*)?\d+(?:[.,]\d+)*(?:\s*(?:万元|美元|元|块))?"
    r"|[$￥¥€£]\s*\d+(?:[.,]\d+)*"
    r"|\d+(?:[.,]\d+)*\s*(?:万元|美元|元|块)|\d+\.\d+"
)
_AMOUNT_CONTEXT = re.compile(r"赔率|奖金|返奖|派奖|赔付|派彩|投注额")
_SNIPPET_HEADERS = {
    **{label: field for label, field in _HEADERS.items() if field != "fixture_id"},
    "主方": "home",
    "客方": "away",
    "开赛时刻": "kickoff",
}
_SAMPLE_SIZE = 3
_SNIPPET_ROWS = 5
_SNIPPET_CHARS = 200
_LINK_LABEL_LIMIT = 20


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


def _short_public_text(value, limit):
    value = _PRICE_TEXT.sub("", value)
    if _AMOUNT_CONTEXT.search(value):
        value = re.sub(r"\d+", "", value)
    value = re.sub(r"[^\w\s()（）.,、/:\-—–·]", "", value)
    return " ".join(value.split())[:limit]


def _label(value):
    # Team and competition fields carry short text labels, never standalone prices.
    if (
        len(value) <= 48
        and any(char.isalpha() for char in value)
        and re.fullmatch(r"[\w ()（）.&'’·+\-–]+", value)
        and not re.search(r"\d+\.\d+", value)
        and not _PRICE_TEXT.search(value)
        and not _AMOUNT_CONTEXT.search(value)
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


def _column_name(cell):
    return re.sub(r"[（(]北京时间[)）]$", "", _text(cell)).replace(" ", "")


def _spanned(cells):
    return any(cell.get("rowspan", "1") != "1" or cell.get("colspan", "1") != "1" for cell in cells)


def _rows(table, headers):
    width = 0
    columns = {}
    for tr in table.xpath("./tr|./thead/tr|./tbody/tr"):
        cells = tr.xpath("./th|./td")
        if _spanned(cells):
            columns.clear()
            continue
        if not columns or any(cell.tag == "th" for cell in cells):
            recognized = [_HEADERS.get(_column_name(cell)) for cell in cells]
            fields = [field for field in recognized if field]
            if {"home", "away"} <= set(fields):
                headers.update(fields)
            else:
                columns.clear()
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


def _table_snippets(tables):
    for table in tables:
        columns, width = {}, 0
        for tr in table.xpath("./tr|./thead/tr|./tbody/tr"):
            cells = tr.xpath("./th|./td")
            if _spanned(cells):
                columns.clear()
                continue
            if not columns or any(cell.tag == "th" for cell in cells):
                names = [_column_name(cell) for cell in cells]
                columns = {
                    i: _SNIPPET_HEADERS[name]
                    for i, name in enumerate(names)
                    if name in _SNIPPET_HEADERS
                }
                if not columns:
                    continue
                width = len(cells)
                snippet = [names[i] for i in columns]
            elif len(cells) == width:
                snippet = [
                    value
                    for i, field in columns.items()
                    if (value := _field(field, cells[i])) is not None
                ]
            else:
                continue
            if snippet:
                yield " | ".join(snippet)[:_SNIPPET_CHARS]


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
    evidence = dict(
        purpose_labels=[],
        headers=[],
        pair_rows=0,
        row_source=None,
        js_signal=None,
        title=None,
        body_bytes=len(raw),
        table_count=0,
        script_count=0,
        keyword_counts=dict.fromkeys(_KEYWORDS, 0),
        table_snippets=[],
        no_pair_reason=None,
    )
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
    evidence["script_count"] = len(document.xpath("//script"))
    _visible(document)
    tables = document.xpath("//table")
    static_text = " ".join(document.itertext())
    evidence["title"] = _short_public_text(document.xpath("string((//title)[1])"), 160) or None
    evidence["table_count"] = len(tables)
    evidence["keyword_counts"] = {
        label: len(pattern.findall(static_text)) for label, pattern in _KEYWORDS.items()
    }
    snippets = _table_snippets(tables)
    evidence["table_snippets"] = list(islice(snippets, _SNIPPET_ROWS))
    titles = " ".join(_text(node) for node in document.xpath("//title|//h1"))
    body = " ".join(_text(node) for node in document.xpath("//body"))[:300]
    if _ACCESS.search(titles):
        return dict(result, status="blocked", reason="静态页面为访问拦截或验证页")
    page_labels = _purposes(titles)
    evidence["purpose_labels"] = page_labels
    if purpose == "home":
        return dict(result, status="discovery", reason=None)
    purpose_tables = [table for table in tables if _table_matches(table, purpose, page_labels)]
    rows = _known_okooo_rows(document) if purpose == "sfc" and page_labels == [purpose] else []
    if rows:
        evidence["row_source"] = "okooo_sfc_markup"
    else:
        headers = set()
        rows = [row for table in purpose_tables for row in _rows(table, headers)]
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
    if rows:
        return dict(result, reason="静态 HTML 读到主客对阵，开球时间未核实")
    off_topic = _OTHER_PAGE.search(titles) and not _SCHEDULE_TEXT.search(titles)
    if (page_labels and purpose not in page_labels) or off_topic:
        evidence["no_pair_reason"] = "not_fixture_page"
        return dict(result, reason="不是对阵页，静态标题指向其他玩法或内容")
    if (purpose in page_labels or purpose_tables) and evidence["js_signal"]:
        evidence["no_pair_reason"] = "requires_js"
        return dict(
            result,
            status="requires_js",
            reason="需要执行脚本，静态正文未见可核对对阵，页面有赛程脚本或应用占位证据",
        )
    if purpose_tables or (purpose in page_labels and (tables or _SCHEDULE_TEXT.search(titles))):
        evidence["no_pair_reason"] = "unrecognized_static"
        return dict(result, reason="是普通网页但结构未识别，静态正文没有可核对的对阵及开球时间")
    return dict(result, reason="未确认页面用途或静态对阵结构")


def _public_path(href, origin, site_domain):
    try:
        parsed = urlsplit(urljoin(origin + "/", href))
        hostname, home_host = parsed.hostname, urlsplit(origin).hostname
        allowed = (
            hostname == site_domain or hostname.endswith("." + site_domain)
            if hostname and site_domain
            else hostname == home_host
        )
        path = parsed.path or "/"
        # A purpose-labeled subdomain root can itself be the relevant landing page.
        new_root = bool(site_domain and hostname != home_host and path == "/")
        if (
            parsed.scheme == "https"
            and hostname
            and allowed
            and parsed.netloc.lower() == hostname
            and not parsed.query
            and not parsed.fragment
            and (
                new_root or re.fullmatch(r"/[\w/-]+(?:\.(?:html?|shtml|aspx|jsp))?", path, re.ASCII)
            )
            and not _SKIP_PATH.search(path.rstrip("/") + "/")
            and not re.search(r"(?:^|/)\.\.?(/|$)|[?#\\]", href)
            and "//" not in path
        ):
            return f"https://{hostname}{path}"
    except ValueError:
        return None


def discover_pages(
    raw: bytes, origin: str, *, site_domain=None, link_labels=None
) -> dict[str, list[str]]:
    """Find explicitly labeled public schedule links in the requested site's scope."""
    result = {purpose: [] for purpose in _PURPOSE}
    document = _document(raw)
    if document is None:
        return result
    _visible(document)
    for link in document.xpath("//a[@href]"):
        label = _text(link)
        if link_labels is not None and len(link_labels) < _LINK_LABEL_LIMIT:
            if _RELATED_LINK.search(label):
                short = _short_public_text(label, 80)
                if short and short not in link_labels:
                    link_labels.append(short)
        if _SKIP_LINK.search(label):
            continue
        url = _public_path(link.get("href"), origin.rstrip("/"), site_domain)
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
