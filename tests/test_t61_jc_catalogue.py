"""Read-only daily JC pages produce monthly league evidence without score imports."""

import hashlib
import importlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from test_t61_catalogue_archive import database_at
from zucai.sources.sporttery import API

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t61"


def module():
    # A missing new collector is the intended initial failing contract.
    spec = importlib.util.find_spec("zucai.cloud_jc_catalogue")
    assert spec is not None, "read-only JC catalogue collector is not implemented"
    return importlib.import_module("zucai.cloud_jc_catalogue")


def fixture(name):
    return json.loads((FIXTURES / name).read_bytes())


def actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")


@pytest.mark.parametrize(
    "instant,start,end",
    [
        ("2026-11-14T00:00:00+00:00", "2025-11-01", "2026-10-31"),
        ("2026-10-31T15:59:59+00:00", "2025-10-01", "2026-09-30"),
        ("2026-10-31T16:00:00+00:00", "2025-11-01", "2026-10-31"),
        ("2026-12-31T16:00:00+00:00", "2026-01-01", "2026-12-31"),
    ],
)
def test_window_is_twelve_complete_beijing_calendar_months(instant, start, end):
    assert module().complete_month_window(datetime.fromisoformat(instant)) == (
        date.fromisoformat(start),
        date.fromisoformat(end),
    )


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "schedule",
            "RUNNER_ENVIRONMENT": "self-hosted",
        },
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "RUNNER_ENVIRONMENT": "github-hosted",
        },
        {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"},
    ],
)
def test_jc_catalogue_rejects_nonmanual_or_nonselfhosted_before_any_output(
    tmp_path, monkeypatch, environment
):
    for key in ("GITHUB_ACTIONS", "GITHUB_EVENT_NAME", "RUNNER_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="self-hosted|workflow_dispatch"):
        module().collect_jc_catalogue(tmp_path / "data/zucai.db", "guard")
    assert not list(tmp_path.iterdir())


@responses.activate
def test_monthly_catalogue_preserves_originals_and_db_and_crosschecks_all_master_rows(
    tmp_path, monkeypatch
):
    catalogue = module()
    actions(monkeypatch)
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    database = database_at(tmp_path / "data/zucai.db")
    before = database.read_bytes()
    requests = []

    def day_page(request):
        query = parse_qs(urlparse(request.url).query)
        day, page = query["matchBeginDate"][0], int(query["pageNo"][0])
        assert query["matchEndDate"] == [day]
        assert query["pageSize"] == ["30"]
        requests.append((day, page))
        name = f"jc-pages-p{page}.json" if day == "2026-10-31" else "jc-empty.json"
        return 200, {"Content-Type": "application/json"}, (FIXTURES / name).read_bytes()

    responses.add_callback(responses.GET, API + "getUniformMatchResultV1.qry", callback=day_page)
    report = catalogue.collect_jc_catalogue(
        database, "jc-offline", now=datetime(2026, 11, 14, tzinfo=UTC)
    )
    assert database.read_bytes() == before
    assert report["collection"] == "catalogue" and report["provider"] == "jc"
    assert report["status"] == "complete"
    assert report["window"] == {
        "start_beijing": "2025-11-01",
        "end_beijing": "2026-10-31",
        "basis": "previous_12_complete_beijing_calendar_months",
    }
    assert report["requests"] == 366
    assert len(set(day for day, page in requests)) == 365
    assert requests[0] == ("2025-11-01", 1) and requests[-1] == ("2026-10-31", 2)
    assert len(report["months"]) == 12
    month = next(row for row in report["months"] if row["month"] == "2026-10")
    assert month["complete_days"] == 31 and month["failed_days"] == []
    assert month["match_count"] == 31
    assert [(row["league_id"], row["match_count"]) for row in month["leagues"]] == [
        (25, 1),
        (62, 29),
        (9999, 1),
    ]
    crosscheck = report["master_crosscheck"]
    assert len(crosscheck) == 90
    assert (
        next(row for row in crosscheck if row["master_name"] == "西甲")["status"]
        == "observed_exact"
    )
    assert (
        next(row for row in crosscheck if row["master_name"] == "日职（J1）")["status"]
        == "owner_decision"
    )
    assert (
        next(row for row in crosscheck if row["master_name"] == "日本J1百年构想联赛")["status"]
        == "owner_decision"
    )
    assert (
        next(row for row in crosscheck if row["master_name"] == "亚运女足")["status"]
        == "not_observed_in_window"
    )
    reasons = {row["reason"] for row in report["owner_decisions"]}
    assert {
        "ambiguous_source_name",
        "observed_name_not_in_master",
        "master_not_observed_in_window",
    } <= reasons
    folder = database.parent / "state/catalogue/jc-offline"
    raw = folder / "jc/2026-10/2026-10-31-p2.json"
    assert raw.read_bytes() == (FIXTURES / "jc-pages-p2.json").read_bytes()
    metadata = json.loads(raw.with_suffix(".json.meta.json").read_bytes())
    assert metadata["sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert metadata["status_code"] == 200
    assert datetime.fromisoformat(metadata["fetched_at_utc"]).utcoffset().total_seconds() == 0
    assert json.loads((folder / "jc.json").read_bytes()) == report
    assert json.loads((folder / "summary.json").read_bytes()) == report
    assert "getMatchHead" not in json.dumps([call.request.url for call in responses.calls])
    assert not (database.parent / "unmatched_teams.csv").exists()


@pytest.mark.parametrize(
    "change",
    [
        "wrong_page",
        "changed_total",
        "changed_pages",
        "repeated_id",
        "wrong_day",
        "missing_row",
        "missing_identity",
        "boolean_total",
    ],
)
def test_incomplete_or_inconsistent_day_fails_closed(change):
    catalogue = module()
    first = fixture("jc-pages-p1.json")
    second = fixture("jc-pages-p2.json")
    if change == "wrong_page":
        second["value"]["pageNo"] = 1
    elif change == "changed_total":
        second["value"]["total"] = 32
    elif change == "changed_pages":
        second["value"]["pages"] = 3
    elif change == "repeated_id":
        second["value"]["matchResult"][0]["matchId"] = 9000001
    elif change == "wrong_day":
        second["value"]["matchResult"][0]["matchDate"] = "2026-10-30"
    elif change == "missing_row":
        second["value"]["matchResult"] = []
    elif change == "missing_identity":
        second["value"]["matchResult"][0].pop("leagueName")
    else:
        second["value"]["total"] = True
    with pytest.raises(ValueError):
        catalogue.parse_day_pages(
            date(2026, 10, 31), [json.dumps(first).encode(), json.dumps(second).encode()]
        )


def test_empty_page_requires_explicit_pagination_and_duplicate_ids_in_one_page_fail():
    catalogue = module()
    assert (
        catalogue.parse_day_pages(date(2026, 10, 31), [(FIXTURES / "jc-empty.json").read_bytes()])
        == []
    )
    with pytest.raises(ValueError):
        catalogue.parse_day_pages(
            date(2026, 10, 31), [b'{"success":true,"errorCode":"0","value":{}}']
        )
    first = fixture("jc-pages-p1.json")
    first["value"]["matchResult"][1]["matchId"] = 9000001
    with pytest.raises(ValueError):
        catalogue.parse_day_pages(
            date(2026, 10, 31),
            [json.dumps(first).encode(), (FIXTURES / "jc-pages-p2.json").read_bytes()],
        )


@responses.activate
def test_failed_day_is_reported_without_counting_partial_rows_or_losing_raw_evidence(
    tmp_path, monkeypatch
):
    catalogue = module()
    actions(monkeypatch)
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    database = database_at(tmp_path / "data/zucai.db")

    def day_page(request):
        query = parse_qs(urlparse(request.url).query)
        day, page = query["matchBeginDate"][0], int(query["pageNo"][0])
        if day == "2026-10-31":
            body = fixture(f"jc-pages-p{page}.json")
            if page == 2:
                body["value"]["matchResult"][0]["matchDate"] = "2026-10-30"
        else:
            body = fixture("jc-empty.json")
        return 200, {}, json.dumps(body)

    responses.add_callback(responses.GET, API + "getUniformMatchResultV1.qry", callback=day_page)
    report = catalogue.collect_jc_catalogue(
        database, "partial", now=datetime(2026, 11, 14, tzinfo=UTC)
    )
    assert report["status"] == "partial"
    october = report["months"][-1]
    assert october["complete_days"] == 30
    assert october["failed_days"] == ["2026-10-31"]
    assert october["match_count"] == 0 and october["leagues"] == []
    assert (database.parent / "state/catalogue/partial/jc/2026-10/2026-10-31-p2.json").is_file()


@responses.activate
def test_jc_latest_pointer_preserves_other_provider_evidence(tmp_path, monkeypatch):
    catalogue = module()
    actions(monkeypatch)
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    database = database_at(tmp_path / "data/zucai.db")
    folder = database.parent / "state/catalogue/previous"
    folder.mkdir(parents=True)
    (folder / "apif.json").write_bytes(b'{"provider":"apif"}')
    pointer = dict(
        path="state/catalogue/previous/apif.json",
        run_id="previous",
        sha256=hashlib.sha256((folder / "apif.json").read_bytes()).hexdigest(),
        raw_sha256="a" * 64,
    )
    latest = folder.parent / "latest.json"
    latest.write_text(json.dumps(dict(version=1, providers=dict(apif=pointer))))
    responses.add(
        responses.GET,
        API + "getUniformMatchResultV1.qry",
        body=(FIXTURES / "jc-empty.json").read_bytes(),
    )
    catalogue.collect_jc_catalogue(database, "jc-index", now=datetime(2026, 11, 14, tzinfo=UTC))
    index = json.loads(latest.read_bytes())
    assert index["providers"]["apif"] == pointer
    jc = index["providers"]["jc"]
    assert jc["path"] == "state/catalogue/jc-index/jc.json" and jc["run_id"] == "jc-index"
    assert jc["sha256"] == hashlib.sha256((database.parent / jc["path"]).read_bytes()).hexdigest()
    assert len(jc["raw_sha256s"]) == 365
