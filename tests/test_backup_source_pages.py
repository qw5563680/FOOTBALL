from pathlib import Path

import pytest
from lxml import html

from zucai.backup_source_pages import discover_pages, inspect_page

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures/okooo_offline"


def test_recorded_okooo_schedule_is_cropped_to_allowlisted_fixture_evidence():
    raw = (FIXTURES / "26170-sfc.html").read_bytes()
    result = inspect_page(raw, "sfc")
    assert result["status"] == "readable"
    assert result["row_count"] == 14
    assert len(result["sample"]) == 3
    assert result["sample"][0] == {
        "period": "26170",
        "seq": 1,
        "league": "亚冠精英",
        "home": "大阪",
        "away": "曼谷港",
        "kickoff": "2026-11-25T10:00:00+00:00",
        "sale_end": "2026-11-25T09:30:00+00:00",
        "fixture_id": "1346894",
    }
    assert set(result["fields"]) == set(result["sample"][0])
    assert result["complete_fields"] == result["fields"]
    assert result["sfc_order_complete"]
    assert result["evidence"]["row_source"] == "okooo_sfc_markup"
    assert inspect_page(raw, "jc")["status"] == "unconfirmed"


def test_matchups_inside_script_do_not_count_as_static_html():
    raw = (FIXTURES / "26170-sfc.html").read_bytes()
    doc = html.document_fromstring(raw.decode("utf-8"))
    table = doc.xpath("//table")[0]
    script = html.Element("script")
    script.text = html.tostring(table, encoding="unicode")
    table.getparent().replace(table, script)
    result = inspect_page(html.tostring(doc, encoding="utf-8"), "sfc")
    assert result["status"] == "requires_js"
    assert result["evidence"]["js_signal"] == "fixtures_in_embedded_markup"
    assert result["sample"] == result["fields"] == []


def test_ordinary_analytics_script_is_not_evidence_that_javascript_is_required():
    raw = """<title>AI数据-传足</title><script src='/analytics.js'></script>
    <main>近期没有赛程</main>""".encode()
    result = inspect_page(raw, "sfc")
    assert result["status"] == "unconfirmed"
    assert result["evidence"]["purpose_labels"] == ["sfc"]
    assert result["evidence"]["js_signal"] is None
    shell = raw + b"<noscript>Please enable JavaScript to see the schedule</noscript>"
    assert inspect_page(shell, "sfc")["evidence"]["js_signal"] == "enable_javascript_notice"


def test_existing_access_challenge_has_no_sample():
    raw = (FIXTURES / "26170-sfc-405.html").read_bytes()
    result = inspect_page(raw, "sfc")
    assert result["status"] == "blocked"
    assert result["sample"] == []


def test_labelled_static_table_extracts_teams_and_time_without_market_cells():
    # Parser unit case for an annotated HTML table, not a captured provider response.
    raw = """<title>竞彩足球赛程</title><table>
    <tr><th>场次</th><th>主队</th><th>客队</th><th>比赛时间</th><th>赔率</th></tr>
    <tr><td>周一001</td><td>布鲁日</td><td>利物浦</td>
    <td>2026-11-25 04:00</td><td>12.34</td></tr>
    <tr hidden><td>周一002</td><td>曼谷港</td><td>大阪</td>
    <td>2026-11-25 04:50</td><td>99.99</td></tr>
    <tr><td>周一003</td><td>大阪 1.9</td><td>曼谷港</td>
    <td>2026-11-25 05:00</td><td>6.3</td></tr></table>""".encode()
    result = inspect_page(raw, "jc")
    assert result["status"] == "readable"
    assert result["row_count"] == 1
    assert result["evidence"]["headers"] == ["seq", "home", "away", "kickoff"]
    assert result["sample"] == [
        dict(seq="周一001", home="布鲁日", away="利物浦", kickoff="2026-11-25 04:00")
    ]
    score_listing = raw.replace("竞彩足球赛程".encode(), "足球比分直播".encode())
    assert inspect_page(score_listing, "jc")["status"] == "unconfirmed"
    table_markup = raw[raw.index(b"<table>") :].replace(
        b"<table>", "<table><caption>全球足球比分</caption>".encode(), 1
    )
    other_table = "<title>竞彩足球赛程</title>".encode() + table_markup
    assert inspect_page(other_table, "jc")["status"] == "unconfirmed"


def test_labelled_team_fields_are_reported_if_the_visible_time_is_incomplete():
    raw = """<title>竞彩足球赛程</title><table>
    <tr><th>主队</th><th>客队</th><th>开球时间</th></tr>
    <tr><td>布鲁日</td><td>利物浦</td><td>未知</td></tr></table>""".encode()
    result = inspect_page(raw, "jc")
    assert result["status"] == "unconfirmed"
    assert result["fields"] == ["home", "away"]
    assert result["sample"] == [dict(home="布鲁日", away="利物浦")]
    raw = raw.replace(
        b"</table>", "<tr><td>曼谷港</td><td>大阪</td><td>11-25 18:00</td></tr></table>".encode()
    )
    mixed = inspect_page(raw, "jc")
    assert mixed["fields"] == ["home", "away", "kickoff"]
    assert mixed["complete_fields"] == ["home", "away"]
    assert mixed["evidence"]["pair_rows"] == 2


def test_sfc_duplicate_order_is_not_counted_as_a_complete_sheet():
    raw = (
        (FIXTURES / "26170-sfc.html")
        .read_bytes()
        .replace(b'<td class="xh">14</td>', b'<td class="xh">13</td>')
    )
    result = inspect_page(raw, "sfc")
    assert result["row_count"] == 14 and not result["sfc_order_complete"]


def test_schedule_discovery_requires_same_origin_clean_public_schedule_links():
    raw = """<nav>
    <a href='/jingcai/football/'>竞彩足球</a>
    <a href='/jingcai/'>竞彩</a>
    <a href='/misc/'>竞彩</a>
    <a href='/zucai/index.html'>传统足彩</a>
    <a href='/zucai/'>14场胜负彩</a>
    <a href='https://www.okooo.com/zucai/'>胜负彩</a>
    <a href='/jc?token=42'>竞彩足球</a>
    <a href='/odds/'>竞彩足球</a>
    <a href='/football/oddscomparison/'>竞彩足球</a>
    <a href='/api/fixtures'>竞彩赛程</a>
    <a href='/look/up'>竞彩足球赔率</a>
    <a href='/basketball/'>竞彩篮球</a>
    <a href='https://user@www.okooo.com/jc/'>竞彩赛程</a>
    <a href='https://www.okooo.com:9999/jc/'>竞彩赛程</a>
    <a href='https://www.okooo.com.other.test/jc/'>竞彩赛程</a>
    <a href='/jc/%2e%2e/api'>竞彩赛程</a>
    <a href='/jc/#odds'>竞彩赛程</a></nav>""".encode()
    assert discover_pages(raw, "https://www.okooo.com") == {
        "sfc": ["https://www.okooo.com/zucai/index.html", "https://www.okooo.com/zucai/"],
        "jc": ["https://www.okooo.com/jingcai/football/", "https://www.okooo.com/jingcai/"],
    }


def test_zgzcw_public_links_can_target_only_the_configured_site_and_its_subdomains():
    raw = """<nav>
    <a href="https://cp.zgzcw.com/football/index.jsp">14 场胜负彩</a>
    <a href="//zgzcw.com/jingcai/">竞彩足球</a>
    <a href="/misc/">竞彩</a>
    <a href="https://zc.zgzcw.com">传统足彩</a>
    <a href="/">14 场胜负彩</a>
    <a href="/sfc/overview.html">胜负彩介绍</a>
    <a href="https://jc.zgzcw.com/rules.html">竞彩足球玩法规则</a>
    <a href="/odds/sfc/">足彩赔率 ¥12345</a>
    <a href="https://zgzcw.com.external.test/jc/">竞彩足球赛程</a>
    <a href="https://zgzcw.com@outside.test/jc/">竞彩足球赛程</a>
    <a href="https://cp.zgzcw.com:443/jc/">竞彩足球赛程</a>
    <a href="https://cp.zgzcw.com/jc/%2e%2e/football/">竞彩足球赛程</a>
    <a href="https://cp.zgzcw.com/jc/odds/">竞彩足球赛程</a>
    <a href="https://cp.zgzcw.com/jc/?anything=1">竞彩足球赛程</a>
    <a href="http://cp.zgzcw.com/jc/">竞彩足球赛程</a>
    <a href="/hidden/" hidden>隐藏竞彩足球</a></nav>""".encode()
    labels = []
    assert discover_pages(
        raw, "https://www.zgzcw.com", site_domain="zgzcw.com", link_labels=labels
    ) == {
        "sfc": ["https://cp.zgzcw.com/football/index.jsp", "https://zc.zgzcw.com/"],
        "jc": ["https://zgzcw.com/jingcai/"],
    }
    assert labels[:3] == ["14 场胜负彩", "竞彩足球", "竞彩"]
    assert "竞彩足球赛程" in labels
    assert "胜负彩介绍" in labels and "竞彩足球玩法规则" in labels
    assert all("12345" not in label and "隐藏" not in label for label in labels)


@pytest.mark.parametrize(
    ("markup", "classification", "status"),
    [
        ("<title>足彩开奖公告</title><p>第 26172 期</p>", "not_fixture_page", "unconfirmed"),
        (
            "<title>竞彩足球对阵</title><div id='app'></div><script src='/app.js'></script>",
            "requires_js",
            "requires_js",
        ),
        (
            "<title>竞彩足球赛程</title><script src='/stats.js'></script><table>"
            "<tr><th>联赛</th><th>主方</th><th>客方</th><th>开赛时刻</th><th>奖金</th></tr>"
            "<tr><td>英冠</td><td>德比郡</td><td>朴次茅斯</td>"
            "<td>2026-11-26 03:45</td><td>837462</td></tr></table>",
            "unrecognized_static",
            "unconfirmed",
        ),
    ],
)
def test_no_fixture_reasons_are_separately_recorded(markup, classification, status):
    raw = markup.encode()
    result = inspect_page(raw, "jc")
    assert result["row_count"] == 0
    assert result["status"] == status
    evidence = result["evidence"]
    assert evidence["no_pair_reason"] == classification
    assert evidence["body_bytes"] == len(raw)
    if classification == "unrecognized_static":
        assert evidence["script_count"] == evidence["table_count"] == 1
        assert evidence["table_snippets"] == [
            "联赛 | 主方 | 客方 | 开赛时刻",
            "英冠 | 德比郡 | 朴次茅斯 | 2026-11-26 03:45",
        ]


def test_explanatory_evidence_restricts_table_snippets_to_safe_columns_and_bounds():
    rows = (
        "<tr><td>26172</td><td>1</td><td>英冠</td><td>德比郡</td><td>朴次茅斯</td>"
        "<td>2026-11-26 03:45</td><td>87123486</td><td>98.73</td><td>9837625</td></tr>"
    )
    raw = (
        "<title>竞彩足球赛程 14 场 赔率 91.37 奖金 ￥928172</title>"
        "<script>let hidden = '竞彩 对阵';</script><p>胜负彩 14场 第 26172 期 对阵</p>"
        "<table><caption>竞彩足球赛程</caption><tr><th>期号</th><th>场序</th><th>联赛</th>"
        "<th>主队</th><th>客队</th><th>比赛时间</th><th>比赛ID</th><th>赔率</th>"
        "<th>奖金</th></tr>" + rows * 12 + "</table>"
        "<table hidden><tr><td>应排除的表格</td></tr></table>"
    ).encode()
    evidence = inspect_page(raw, "jc")["evidence"]
    assert "竞彩足球赛程" in evidence["title"]
    assert evidence["body_bytes"] == len(raw)
    assert evidence["table_count"] == evidence["script_count"] == 1
    assert evidence["keyword_counts"]["胜负彩"] == 1
    assert evidence["keyword_counts"]["14 场"] == 2
    assert evidence["keyword_counts"]["对阵"] == 1
    assert len(evidence["table_snippets"]) == 5
    assert all(len(row) <= 200 for row in evidence["table_snippets"])
    assert evidence["table_snippets"][0] == "期号 | 场序 | 联赛 | 主队 | 客队 | 比赛时间"
    assert (
        evidence["table_snippets"][1] == "26172 | 1 | 英冠 | 德比郡 | 朴次茅斯 | 2026-11-26 03:45"
    )
    assert all(
        number not in str(evidence)
        for number in ("91.37", "928172", "87123486", "98.73", "9837625")
    )


def test_home_and_unparseable_responses_still_have_bounded_evidence():
    home = inspect_page("<title>中国足彩网</title><p>竞彩足球</p>".encode(), "home")["evidence"]
    assert home["title"] == "中国足彩网" and home["keyword_counts"]["竞彩"] == 1
    assert home["no_pair_reason"] is None
    bad = inspect_page(b"\xff", "jc")["evidence"]
    assert bad["body_bytes"] == 1
    assert bad["title"] is None and bad["table_snippets"] == [] and bad["no_pair_reason"] is None
