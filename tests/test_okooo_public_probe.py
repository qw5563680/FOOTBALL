from pathlib import Path

import pytest

from zucai import okooo_probe as probe

FIX = Path(__file__).resolve().parents[1] / "data/fixtures/okooo"
NOW = "2026-11-23T16:35:00+00:00"
URL = "https://www.okooo.com/zucai/"


def test_recorded_14_rows_both_issues_and_source_scoped_ids():
    for number, deadline in [("26169", "11-24(周二) 22:00"), ("26170", "11-25(周三) 17:30")]:
        page = probe.inspect_page((FIX / f"{number}.html").read_bytes(), number, "sfc", NOW)
        assert page["issue_no"] == number and page["deadline_label"] == deadline
        assert [r["seq"] for r in page["rows"]] == list(range(1, 15))
        assert len({r["okooo_fixture_id"] for r in page["rows"]}) == 14
        assert all(r["home_team_id"] is None and r["away_team_id"] is None for r in page["rows"])
        assert page["read_status"] == "complete"
        assert not any(k in page["rows"][0] for k in ("odds", "prices", "probabilities"))


@pytest.mark.parametrize(
    "change", ["missing", "duplicate_id", "wrong_issue", "wrong_game", "swapped"]
)
def test_uncertain_or_conflicting_evidence_is_preserved(change):
    from lxml import html

    from zucai.sources.sfc_official import parse_notice

    raw = (FIX / "26169.html").read_bytes()
    doc = html.fromstring(raw.decode())
    rows = doc.xpath('//table[@id="table4"]//tr')
    if change == "missing":
        rows[-1].drop_tree()
    elif change == "duplicate_id":
        for link in rows[1].xpath('.//a[contains(@href,"/soccer/match/")]'):
            parts = link.get("href").split("/")
            parts[3] = "1347499"
            link.set("href", "/".join(parts))
    elif change == "swapped":
        home, away = rows[2].xpath('.//span[contains(@class,"nameobj")]')
        home.text, away.text = away.text, home.text
    check = probe.inspect_page(
        html.tostring(doc, encoding="utf-8"),
        "26170" if change == "wrong_issue" else "26169",
        "ren9" if change == "wrong_game" else "sfc",
        NOW,
    )
    if change == "swapped":
        notice = parse_notice(
            (FIX.parent / "sfc_official/wenzhou_20261118.html").read_bytes(),
            source_url="https://www.wztc.cn/system/2026/11/18/105874264.shtml",
            observed_utc=NOW,
        )
        comparison = probe.compare(check, notice, NOW)
        assert comparison["provider"] == "okooo.com"
        assert comparison["notice_input"] == "archived_notice_replay"
        assert any(r["seq"] == 3 and r["status"] == "conflict" for r in comparison["differences"])
    else:
        assert check["read_status"] == "pending" and check["reasons"]


def test_405_and_repeated_samples_are_not_predictions(tmp_path):
    import requests
    from responses import RequestsMock

    with RequestsMock() as http:
        http.get(URL, status=405)
        http.get(URL, body=(FIX / "26169.html").read_bytes())
        http.get(URL, body=(FIX / "26169.html").read_bytes())
        with requests.Session() as client:
            a, b, c = [probe.observe(client, URL, "26169", "sfc", tmp_path) for _ in range(3)]
    assert a["http_status"] == 405 and a["read_status"] == "pending" and not a["rows"]
    assert a["observed_utc"] <= b["observed_utc"] <= c["observed_utc"]
    assert b["rows"] == c["rows"] and b["sha256"] == c["sha256"]
    assert len(list((tmp_path / "raw").glob("*.html"))) == 2


def test_two_readable_games_disagree_on_match_id_even_if_both_have_14_rows():
    from copy import deepcopy

    first = probe.inspect_page((FIX / "26169.html").read_bytes(), "26169", "sfc", NOW)
    first["url"] = URL
    other = deepcopy(first)
    other.update(game="ren9", url=URL + "ren9/")
    other["rows"][0]["okooo_fixture_id"] = "99999"
    check = probe.compare_sheets([first, other], "26169")
    assert check["status"] == "conflict" and check["differences"][0]["field"] == "okooo_fixture_id"


@pytest.mark.parametrize("ref", ["main", "football-ingest-run-test", "football-ingest-verify-test"])
def test_probe_on_actual_collection_runner_has_no_other_jobs_or_secrets(ref):
    from test_stats_workflow import jobs
    from test_t61_workflow import enabled_jobs

    assert enabled_jobs("okooo-sfc", **{"github.ref_name": ref}) == {"okooo-public-probe"}
    job = jobs()["okooo-public-probe"]
    assert "runs-on: [self-hosted, football-local]" in job and "contents: read" in job
    assert "secrets." not in job and "publish-football-feeds.sh" not in job
