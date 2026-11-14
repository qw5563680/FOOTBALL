"""Real archived APIF dates degrade to warnings without losing catalogue seasons."""

import hashlib
import json
from pathlib import Path

import pytest
import responses

from zucai.cloud_catalogue import (
    CatalogueError,
    collect_catalogue,
    parse_apif_catalogue,
    write_summary,
)

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures"
REAL = FIXTURES / "t65/apif-catalogue-real.json"
APIF_URL = "https://v3.football.api-sports.io/leagues"
FDORG_URL = "https://api.football-data.org/v4/competitions"


def _single_season(start="2026-07-24", end="2027-05-23"):
    """Mutations of a real row below are synthetic edge cases, not source evidence."""
    doc = json.loads(REAL.read_bytes())
    row = next(row for row in doc["response"] if row["league"]["id"] == 271)
    season = next(season for season in row["seasons"] if season["year"] == 2026)
    season.update(start=start, end=end)
    row["seasons"] = [season]
    doc.update(response=[row], results=1)
    return doc


def _parse(document):
    return parse_apif_catalogue(json.dumps(document).encode())


def test_real_bad_dates_preserve_all_leagues_years_and_unverified_access():
    parsed = parse_apif_catalogue(REAL.read_bytes())
    leagues = {league["id"]: league for league in parsed["leagues"]}
    assert set(leagues) == {"2", "39", "271"}
    assert sum(len(row["seasons"]) for row in leagues.values()) == 49
    assert [s["year"] for s in leagues["39"]["seasons"]] == list(range(2010, 2027))
    assert [s["year"] for s in leagues["271"]["seasons"]] == list(range(2011, 2027))
    assert [s["year"] for s in leagues["2"]["seasons"]] == list(range(2011, 2027))
    assert leagues["271"]["name"] == "NB I"
    assert leagues["271"]["country"] == "Hungary"
    bad = leagues["271"]["seasons"][-1]
    assert bad["start_date"] is None and bad["end_date"] is None
    assert len(bad["warnings"]) == 1
    warning = bad["warnings"][0]
    assert warning["code"] == "invalid_season_dates"
    assert warning["league_id"] == "271" and warning["season_year"] == 2026
    assert warning["original_start"] == "2286-11-20"
    assert warning["original_end"] == "1970-01-01"
    assert "1900..2100" in warning["reason"] and "order" in warning["reason"]
    assert parsed["warnings"] == [warning]
    good = leagues["39"]["seasons"][-1]
    assert good["start_date"] == "2026-08-21" and good["end_date"] == "2027-05-30"
    assert not good.get("warnings")
    assert all(
        season["access"] == "unverified"
        and season["fixtures"] is None
        and season["results"] is None
        for row in leagues.values()
        for season in row["seasons"]
    )


@pytest.mark.parametrize(
    "start,end,reason",
    [
        ("2026-11-30", "2026-01-01", "order"),
        ("not-a-date", "2027-05-23", "invalid"),
        ("2026-02-30", "2027-05-23", "invalid"),
        ("20260724", "2027-05-23", "YYYY-MM-DD"),
        ("2286-11-20", "2287-01-01", "1900..2100"),
        ("1800-01-01", "1801-01-01", "1900..2100"),
        ("2026-07-24", "9999-12-31", "1900..2100"),
    ],
)
def test_apif_invalid_date_values_warn_and_clear_both_dates(start, end, reason):
    parsed = _parse(_single_season(start, end))
    season = parsed["leagues"][0]["seasons"][0]
    assert season["year"] == 2026
    assert season["start_date"] is None and season["end_date"] is None
    assert season["access"] == "unverified"
    warning = season["warnings"][0]
    assert warning["original_start"] == start and warning["original_end"] == end
    assert reason in warning["reason"]
    assert parsed["warnings"] == [warning]


@pytest.mark.parametrize(
    "start,end",
    [(None, None), (None, "2027-05-23"), ("2026-07-24", None)],
)
def test_explicit_null_dates_remain_unknown_without_fabricated_dates(start, end):
    parsed = _parse(_single_season(start, end))
    season = parsed["leagues"][0]["seasons"][0]
    assert season["start_date"] == start and season["end_date"] == end
    assert not season.get("warnings") and not parsed["warnings"]


@pytest.mark.parametrize("field", ["year", "start", "end"])
def test_missing_required_season_fields_still_reject_entire_catalogue(field):
    doc = _single_season()
    doc["response"][0]["seasons"][0].pop(field)
    with pytest.raises(CatalogueError):
        _parse(doc)


@pytest.mark.parametrize("field", ["start", "end"])
@pytest.mark.parametrize("value", [True, 2026, [], {}])
def test_wrong_date_field_type_is_structural_and_rejects_entire_catalogue(field, value):
    doc = _single_season()
    doc["response"][0]["seasons"][0][field] = value
    with pytest.raises(CatalogueError):
        _parse(doc)


@pytest.mark.parametrize("mutation", ["league-id", "duplicate-id", "duplicate-year", "coverage"])
def test_date_warning_does_not_mask_structural_errors(mutation):
    doc = json.loads(REAL.read_bytes())
    row = next(row for row in doc["response"] if row["league"]["id"] == 271)
    if mutation == "league-id":
        row["league"].pop("id")
    elif mutation == "duplicate-id":
        doc["response"][2]["league"]["id"] = 271
    elif mutation == "duplicate-year":
        row["seasons"][-2]["year"] = 2026
    else:
        row["seasons"][-1]["coverage"]["results"] = "yes"
    with pytest.raises(CatalogueError):
        _parse(doc)


@pytest.mark.parametrize("raw", [b"not-json", b'{"get":"leagues","get":"leagues"}'])
def test_non_json_or_duplicate_keys_still_reject_entire_catalogue(raw):
    with pytest.raises(CatalogueError):
        parse_apif_catalogue(raw)


@pytest.mark.parametrize("league_id", [397, 276, 379, 402, 542])
def test_real_duplicate_season_years_remain_structural_errors(league_id):
    doc = json.loads((FIXTURES / "t65/apif-catalogue-duplicate-years-real.json").read_bytes())
    row = next(row for row in doc["response"] if row["league"]["id"] == league_id)
    doc.update(response=[row], results=1)
    with pytest.raises(CatalogueError, match="duplicate season years"):
        _parse(doc)


def test_date_warning_preserves_explicit_unavailable_coverage():
    doc = _single_season("2286-11-20", "1970-01-01")
    doc["response"][0]["seasons"][0]["coverage"] = {"fixtures": False, "results": False}
    season = _parse(doc)["leagues"][0]["seasons"][0]
    assert season["warnings"]
    assert season["fixtures"] is False and season["results"] is False
    assert season["access"] == "unverified"


@pytest.fixture
def actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("API_FOOTBALL_KEY", "synthetic-apif-key")
    monkeypatch.setenv("FOOTBALL_DATA_ORG_TOKEN", "synthetic-fdorg-token")
    monkeypatch.delenv("ZUCAI_APIF_DAILY_CAP", raising=False)
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)


def _responses(http, *, apif=None, apif_status=200):
    http.get(
        APIF_URL,
        body=REAL.read_bytes() if apif is None else apif,
        status=apif_status,
        headers={"x-ratelimit-requests-limit": "7500", "x-ratelimit-requests-remaining": "7498"},
    )
    http.get(
        FDORG_URL,
        body=(FIXTURES / "t61/catalogue-fdorg-synthetic.json").read_bytes(),
    )


def test_real_date_warning_is_archived_in_summary_and_publishes_apif_pointer(
    tmp_path, actions, monkeypatch
):
    database = tmp_path / "data/zucai.db"
    database.parent.mkdir()
    database.write_bytes(b"Database must remain untouched.")
    step_summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    with responses.RequestsMock() as http:
        _responses(http)
        report = collect_catalogue(database, "real-date-warning")
    assert report["status"] == "complete" and report["errors"] == []
    assert report["requests"] == 2 and report["status_requests"] == 0
    assert database.read_bytes() == b"Database must remain untouched."
    observed = report["providers"]["apif"]
    assert observed["status"] == "complete" and observed["errors"] == []
    assert observed["league_count"] == 3 and observed["season_count"] == 49
    assert len(observed["warnings"]) == 1
    assert report["warnings"] == [dict(observed["warnings"][0], provider="apif")]
    folder = database.parent / "state/catalogue/real-date-warning"
    assert json.loads((folder / "summary.json").read_bytes()) == report
    archived = json.loads((folder / "apif.json").read_bytes())
    assert archived["warnings"] == observed["warnings"]
    assert (folder / "apif-raw.json").read_bytes() == REAL.read_bytes()
    latest = json.loads((folder.parent / "latest.json").read_bytes())
    pointer = latest["providers"]["apif"]
    assert pointer["path"] == "state/catalogue/real-date-warning/apif.json"
    assert pointer["raw_sha256"] == hashlib.sha256(REAL.read_bytes()).hexdigest()
    assert pointer["sha256"] == hashlib.sha256((folder / "apif.json").read_bytes()).hexdigest()
    write_summary(report)
    text = step_summary.read_text()
    assert all(value in text for value in ("271", "2026", "2286-11-20", "1970-01-01"))
    with responses.RequestsMock() as http:
        _responses(http, apif=b'{"errors":{"server":"offline synthetic 503"}}', apif_status=503)
        failed = collect_catalogue(database, "after-date-warning")
    assert failed["status"] == "partial"
    latest = json.loads((folder.parent / "latest.json").read_bytes())
    assert latest["providers"]["apif"] == pointer
