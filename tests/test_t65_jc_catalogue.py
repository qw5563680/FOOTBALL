"""Real archived JC zero-page shape, official dictionary, and offline reprocessing."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
import responses

from zucai import cloud_jc_catalogue as catalogue
from zucai.sources.sporttery import API

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t65"
EMPTY = FIXTURES / "jc-empty-real.json"
RESULT = FIXTURES / "jc-one-result-real.json"
PROVENANCE = json.loads((FIXTURES / "provenance-jc.json").read_bytes())
STARTED = "2026-11-14T01:18:15.740191+00:00"
CAPTURED = "2026-11-14T01:25:02.878048+00:00"
REPLAYED = datetime(2026, 12, 3, 8, tzinfo=UTC)


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


@pytest.mark.parametrize("empty_day", PROVENANCE["empty_days"])
def test_all_twenty_archived_empty_days_are_valid_zero_result_days(empty_day):
    raw = EMPTY.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == empty_day["sha256"]
    assert catalogue.parse_day_pages(date.fromisoformat(empty_day["day"]), [raw]) == []


@pytest.mark.parametrize(
    "change",
    [
        "missing_result_count",
        "boolean_result_count",
        "nonzero_result_count",
        "missing_rows",
        "missing_page_size",
        "nonzero_pages",
        "nonzero_page_number",
        "wrong_page_size",
        "nonempty_rows",
        "missing_dictionary",
        "empty_dictionary",
        "duplicate_dictionary_id",
        "boolean_dictionary_id",
        "missing_dictionary_name",
    ],
)
def test_zero_page_exception_does_not_accept_malformed_neighbouring_shapes(change):
    body = json.loads(EMPTY.read_bytes())
    value = body["value"]
    if change == "missing_result_count":
        value.pop("resultCount")
    elif change == "boolean_result_count":
        value["resultCount"] = False
    elif change == "nonzero_result_count":
        value["resultCount"] = 1
    elif change == "missing_rows":
        value.pop("matchResult")
    elif change == "missing_page_size":
        value.pop("pageSize")
    elif change == "nonzero_pages":
        value["pages"] = 1
    elif change == "nonzero_page_number":
        value["pageNo"] = 1
    elif change == "wrong_page_size":
        value["pageSize"] = 1
    elif change == "nonempty_rows":
        value["matchResult"] = json.loads(RESULT.read_bytes())["value"]["matchResult"]
    elif change == "missing_dictionary":
        value.pop("leagueList")
    elif change == "empty_dictionary":
        value["leagueList"] = []
    elif change == "duplicate_dictionary_id":
        value["leagueList"].append(value["leagueList"][0])
    elif change == "boolean_dictionary_id":
        value["leagueList"][0]["leagueId"] = True
    else:
        value["leagueList"][0].pop("leagueAllName")
    with pytest.raises(ValueError):
        catalogue.parse_day_pages(date(2025, 11, 11), [encoded(body)])


def test_real_single_match_retains_only_identity_and_dictionary_has_official_names():
    assert catalogue.parse_day_pages(date(2025, 12, 17), [RESULT.read_bytes()]) == [
        {
            "match_id": 2036099,
            "league_id": 24,
            "league_name": "英格兰联赛杯",
            "league_abbr": "英联赛杯",
        }
    ]
    parser = getattr(catalogue, "parse_official_leagues", None)
    assert callable(parser), "official leagueList extraction is not implemented"
    leagues = parser(EMPTY.read_bytes())
    assert len(leagues) == 113
    assert next(row for row in leagues if row["league_id"] == 25) == {
        "league_id": 25,
        "league_abbr": "英超",
        "league_name": "英格兰超级联赛",
    }


def archive_at(root):
    """Synthetic transport archive; the two response bodies are untouched real fixtures."""
    folder = root / "state/catalogue/source-run"
    day = date(2025, 11, 1)
    while day <= date(2026, 10, 31):
        raw = RESULT.read_bytes() if day == date(2025, 12, 17) else EMPTY.read_bytes()
        path = folder / "jc" / day.strftime("%Y-%m") / f"{day}-p1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        query = dict(
            matchBeginDate=day.isoformat(),
            matchEndDate=day.isoformat(),
            leagueId="",
            pageSize=30,
            pageNo=1,
            isFix=0,
            matchPage=1,
            pcOrWap=1,
        )
        path.with_suffix(".json.meta.json").write_bytes(
            encoded(
                dict(
                    url=API + "getUniformMatchResultV1.qry?" + urlencode(query),
                    fetched_at_utc="2026-11-14T01:20:00Z",
                    status_code=200,
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
            )
        )
        day += timedelta(days=1)
    summary = dict(
        collection="catalogue",
        provider="jc",
        status="partial",
        run_id="source-run",
        started_utc=STARTED,
        captured_utc=CAPTURED,
        requests=365,
        window=dict(
            start_beijing="2025-11-01",
            end_beijing="2026-10-31",
            basis="previous_12_complete_beijing_calendar_months",
        ),
    )
    for name in ("jc.json", "summary.json"):
        (folder / name).write_bytes(encoded(summary))
    return folder


def replay(source, root, run_id="replayed"):
    runner = getattr(catalogue, "replay_jc_catalogue", None)
    assert callable(runner), "validated offline JC catalogue reprocessing is not implemented"
    return runner(source, root, run_id, now=REPLAYED)


def files_sha256(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@responses.activate
def test_replay_produces_complete_year_and_dictionary_without_touching_source_or_latest(
    tmp_path,
):
    source = archive_at(tmp_path / "source")
    output = tmp_path / "output"
    latest = output / "state/catalogue/latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_bytes(b"existing latest pointer must remain byte-identical")
    database = output / "zucai.db"
    database.write_bytes(b"existing database must remain byte-identical")
    before = files_sha256(tmp_path)
    report = replay(source, output)
    assert report["status"] == "complete"
    assert report["requests"] == 0 and report["replayed_pages"] == 365
    assert report["captured_utc"] == CAPTURED
    assert report["started_utc"] == STARTED
    assert report["replayed_utc"] == REPLAYED.isoformat()
    assert report["source_run_id"] == "source-run"
    assert (
        report["source_report_sha256"]
        == hashlib.sha256((source / "jc.json").read_bytes()).hexdigest()
    )
    assert sum(month["complete_days"] for month in report["months"]) == 365
    assert sum(month["match_count"] for month in report["months"]) == 1
    assert sum(len(month["empty_days"]) for month in report["months"]) == 364
    assert "2025-12-17" not in report["months"][1]["empty_days"]
    assert report["errors"] == [] and len(report["master_crosscheck"]) == 90
    assert report["observations"] == [
        {
            "league_id": 24,
            "league_name": "英格兰联赛杯",
            "league_abbr": "英联赛杯",
            "match_count": 1,
            "first_match_day": "2025-12-17",
            "last_match_day": "2025-12-17",
        }
    ]
    folder = output / "state/catalogue/replayed"
    dictionary_raw = (folder / "jc-leagues.json").read_bytes()
    dictionary = json.loads(dictionary_raw)
    assert len(dictionary["leagues"]) == 113
    league = next(row for row in dictionary["leagues"] if row["league_id"] == 25)
    assert league["league_abbr"] == "英超" and league["league_name"] == "英格兰超级联赛"
    assert league["first_observed_run_id"] == "source-run"
    assert league["first_observed_day"] == "2025-11-01"
    assert league["first_observed_captured_utc"] == "2026-11-14T01:20:00Z"
    assert league["raw_path"] == "jc/2025-11/2025-11-01-p1.json"
    assert league["raw_sha256"] == hashlib.sha256(EMPTY.read_bytes()).hexdigest()
    assert (
        report["official_league_dictionary"]["sha256"] == hashlib.sha256(dictionary_raw).hexdigest()
    )
    for relative, sha256 in before.items():
        assert hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest() == sha256
    for path in (source / "jc").rglob("*.json"):
        assert (folder / path.relative_to(source)).read_bytes() == path.read_bytes()
    assert json.loads((folder / "summary.json").read_bytes()) == report
    assert not responses.calls


def rewrite_sidecar(path, change):
    sidecar = path.with_suffix(".json.meta.json")
    value = json.loads(sidecar.read_bytes())
    change(value)
    sidecar.write_bytes(encoded(value))


@pytest.mark.parametrize(
    "change",
    [
        "body_hash",
        "missing_sidecar",
        "wrong_source",
        "wrong_request_day",
        "wrong_request_page",
        "selected_league",
        "duplicate_query",
        "status_500",
        "naive_capture",
        "capture_outside_run",
        "missing_day",
        "missing_page",
        "duplicate_page",
        "outside_window",
        "wrong_month_folder",
        "wrong_run_id",
        "wrong_window",
        "different_summaries",
        "wrong_page_count",
        "dictionary_conflict",
        "repeated_match_id",
        "symlink",
    ],
)
@responses.activate
def test_replay_rejects_corrupt_or_incomplete_archive_without_publishing(tmp_path, change):
    source = archive_at(tmp_path / "source")
    path = source / "jc/2025-11/2025-11-01-p1.json"
    if change == "body_hash":
        path.write_bytes(path.read_bytes() + b" ")
    elif change == "missing_sidecar":
        path.with_suffix(".json.meta.json").unlink()
    elif change in {
        "wrong_source",
        "wrong_request_day",
        "wrong_request_page",
        "selected_league",
        "duplicate_query",
        "status_500",
        "naive_capture",
        "capture_outside_run",
    }:

        def mutate(meta):
            if change == "wrong_source":
                meta["url"] = meta["url"].replace("webapi.sporttery.cn", "example.invalid")
            elif change == "wrong_request_day":
                meta["url"] = meta["url"].replace("2025-11-01", "2025-11-02")
            elif change == "wrong_request_page":
                meta["url"] = meta["url"].replace("pageNo=1", "pageNo=2")
            elif change == "selected_league":
                meta["url"] = meta["url"].replace("leagueId=", "leagueId=25")
            elif change == "duplicate_query":
                meta["url"] += "&pageNo=1"
            elif change == "status_500":
                meta["status_code"] = 500
            elif change == "naive_capture":
                meta["fetched_at_utc"] = "2026-11-14T01:20:00"
            else:
                meta["fetched_at_utc"] = "2026-11-15T01:20:00Z"

        rewrite_sidecar(path, mutate)
    elif change in {"missing_day", "missing_page"}:
        if change == "missing_page":
            body = json.loads(RESULT.read_bytes())
            body["value"].update(total=31, pages=2)
            first = body["value"]["matchResult"][0]
            body["value"]["matchResult"] = [
                dict(first, matchId=2036099 + offset) for offset in range(30)
            ]
            path = source / "jc/2025-12/2025-12-17-p1.json"
            path.write_bytes(encoded(body))
            rewrite_sidecar(
                path, lambda meta: meta.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            )
        else:
            path.unlink()
            path.with_suffix(".json.meta.json").unlink()
    elif change in {"duplicate_page", "outside_window", "wrong_month_folder", "symlink"}:
        relative = {
            "duplicate_page": "jc/2025-11/2025-11-01-p01.json",
            "outside_window": "jc/2025-10/2025-10-31-p1.json",
            "wrong_month_folder": "jc/2025-12/2025-11-01-p1.json",
            "symlink": "jc/2025-11/2025-11-02-p1.json",
        }[change]
        other = source / relative
        other.parent.mkdir(parents=True, exist_ok=True)
        if change == "symlink":
            other.unlink()
            other.symlink_to(path)
        else:
            other.write_bytes(path.read_bytes())
            other.with_suffix(".json.meta.json").write_bytes(
                path.with_suffix(".json.meta.json").read_bytes()
            )
    elif change in {"wrong_run_id", "wrong_window", "different_summaries", "wrong_page_count"}:
        summary = json.loads((source / "jc.json").read_bytes())
        if change == "wrong_run_id":
            summary["run_id"] = "another-run"
        elif change == "wrong_window":
            summary["window"]["end_beijing"] = "2026-10-30"
        elif change == "wrong_page_count":
            summary["requests"] = 366
        else:
            summary["captured_utc"] = "2026-11-14T01:26:00Z"
        (source / "jc.json").write_bytes(encoded(summary))
        if change != "different_summaries":
            (source / "summary.json").write_bytes(encoded(summary))
    elif change == "dictionary_conflict":
        path = source / "jc/2026-10/2026-10-31-p1.json"
        body = json.loads(path.read_bytes())
        body["value"]["leagueList"][0]["leagueAllName"] = "不同的官方赛事"
        path.write_bytes(encoded(body))
        rewrite_sidecar(
            path, lambda meta: meta.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        )
    else:
        path = source / "jc/2025-12/2025-12-18-p1.json"
        body = json.loads(RESULT.read_bytes())
        body["value"]["matchResult"][0]["matchDate"] = "2025-12-18"
        path.write_bytes(encoded(body))
        rewrite_sidecar(
            path, lambda meta: meta.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        )
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        replay(source, output)
    assert not output.exists()


def test_replay_refuses_existing_run_and_source_tree_output(tmp_path):
    source = archive_at(tmp_path / "source")
    with pytest.raises(ValueError):
        replay(source, source)
    with pytest.raises((FileExistsError, ValueError)):
        replay(source, tmp_path / "source", run_id="source-run")


def test_dictionary_conflict_between_valid_pages_of_one_day_is_rejected():
    first = json.loads(RESULT.read_bytes())
    first["value"].update(total=31, pages=2)
    row = first["value"]["matchResult"][0]
    first["value"]["matchResult"] = [dict(row, matchId=2036099 + n) for n in range(30)]
    second = json.loads(RESULT.read_bytes())
    second["value"].update(total=31, pages=2, pageNo=2)
    second["value"]["matchResult"][0]["matchId"] = 2036129
    second["value"]["leagueList"][0]["leagueAllName"] = "另一个赛事"
    with pytest.raises(ValueError, match="字典冲突"):
        catalogue.parse_day_pages(date(2025, 12, 17), [encoded(first), encoded(second)])


@pytest.mark.parametrize("conflict", [False, True])
@responses.activate
def test_collector_uses_real_empty_shape_and_never_silently_overwrites_dictionary(
    tmp_path, monkeypatch, conflict
):
    for key, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "RUNNER_ENVIRONMENT": "self-hosted",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)

    def response(request):
        query = parse_qs(urlparse(request.url).query)
        day = query["matchBeginDate"][0]
        body = json.loads(RESULT.read_bytes() if day == "2025-12-17" else EMPTY.read_bytes())
        if conflict and day == "2026-10-31":
            body["value"]["leagueList"][0]["leagueAllName"] = "不同的官方赛事"
        return 200, {}, encoded(body)

    responses.add_callback(responses.GET, API + "getUniformMatchResultV1.qry", callback=response)
    database = tmp_path / "zucai.db"
    report = catalogue.collect_jc_catalogue(
        database, "real-shape", now=datetime(2026, 11, 14, tzinfo=UTC)
    )
    assert report["status"] == ("partial" if conflict else "complete")
    assert sum(month["complete_days"] for month in report["months"]) == (364 if conflict else 365)
    assert sum(month["match_count"] for month in report["months"]) == 1
    dictionary = json.loads((tmp_path / "state/catalogue/real-shape/jc-leagues.json").read_bytes())
    assert len(dictionary["leagues"]) == 113
    assert dictionary["leagues"][0]["league_name"] == "亚洲冠军精英联赛"
    assert not database.exists()
