from pathlib import Path

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
