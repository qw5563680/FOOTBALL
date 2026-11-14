"""Real duplicate years retain evidence but cannot become downloadable seasons."""

import copy
import hashlib
import json

import pytest
import responses

from test_t65_apif_catalogue import FIXTURES, _responses
from test_t65_apif_catalogue import actions as actions
from zucai.cloud_catalogue import (
    CatalogueError,
    collect_catalogue,
    parse_apif_catalogue,
    write_summary,
)
from zucai.cloud_catalogue_backfill import catalogue_scopes

REAL = FIXTURES / "t65/apif-catalogue-duplicate-years-real.json"
CONFLICTS = {"397": 2018, "276": 2018, "379": 2020, "402": 2019, "542": 2021}
NORMAL_YEARS = {
    "397": [2012, 2013, 2014, 2015, 2016, 2017, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026],
    "276": [2012, 2013, 2014, 2015, 2016, 2017, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026],
    "379": [2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2024, 2025],
    "402": [2016, 2017, 2018, 2020, 2021, 2022, 2025, 2026],
    "542": [2020, 2022, 2023, 2024, 2025, 2026],
}


def registry_for_fixture():
    """Planner-only authorization; these five leagues stay unapproved in the real registry."""
    return {
        "download_policy": {"scope": "related"},
        "providers": {
            "apif": {
                str(row["league"]["id"]): {
                    "status": "verified",
                    "mode": "import",
                    "backfill": True,
                    "relevant": True,
                    "config": {"api_name": row["league"]["name"]},
                }
                for row in json.loads(REAL.read_bytes())["response"]
            }
        },
    }


def test_all_five_real_conflicts_preserve_each_league_and_normal_year():
    raw = REAL.read_bytes()
    source = json.loads(raw)
    parsed = parse_apif_catalogue(raw)
    assert {row["id"] for row in parsed["leagues"]} == set(CONFLICTS)
    assert parsed["raw_season_count"] == 62
    assert parsed["conflicted_season_count"] == 5
    assert sum(len(row["seasons"]) for row in parsed["leagues"]) == 57
    assert len(parsed["warnings"]) == 5
    for row in parsed["leagues"]:
        normal = [season for season in row["seasons"] if season.get("status") != "conflict"]
        assert [season["year"] for season in normal] == NORMAL_YEARS[row["id"]]
        assert all(season["access"] == "unverified" for season in normal)
        conflict = next(s for s in row["seasons"] if s["year"] == CONFLICTS[row["id"]])
        assert conflict["status"] == "conflict" and conflict["access"] == "conflict"
        assert all(
            conflict[key] is None for key in ["start_date", "end_date", "fixtures", "results"]
        )
        warning = conflict["warnings"][0]
        source_index, original = next(
            (i, r) for i, r in enumerate(source["response"]) if str(r["league"]["id"]) == row["id"]
        )
        records = [
            (i, s) for i, s in enumerate(original["seasons"]) if s["year"] == conflict["year"]
        ]
        assert warning["code"] == "duplicate_season_year"
        assert warning["league_id"] == row["id"]
        assert warning["season_year"] == CONFLICTS[row["id"]]
        assert warning["original_records"] == [s for _, s in records]
        assert warning["raw_sha256"] == hashlib.sha256(raw).hexdigest()
        assert warning["record_pointers"] == [
            f"/response/{source_index}/seasons/{i}" for i, _ in records
        ]
        assert warning in parsed["warnings"]


@pytest.mark.parametrize(
    "mutation",
    ["missing-start", "missing-year", "date-type", "coverage", "season-type", "duplicate-id"],
)
def test_conflict_quarantine_does_not_hide_structural_errors(mutation):
    doc = json.loads(REAL.read_bytes())
    row = doc["response"][0]
    index = next(i for i, s in enumerate(row["seasons"]) if s["year"] == 2018) + 1
    if mutation == "missing-start":
        row["seasons"][index].pop("start")
    elif mutation == "missing-year":
        row["seasons"][index].pop("year")
    elif mutation == "date-type":
        row["seasons"][index]["end"] = True
    elif mutation == "coverage":
        row["seasons"][index]["coverage"]["fixtures"]["events"] = "yes"
    elif mutation == "season-type":
        row["seasons"][index] = None
    else:
        doc["response"][1]["league"]["id"] = 397
    with pytest.raises(CatalogueError):
        parse_apif_catalogue(json.dumps(doc).encode())


def test_identical_repeats_are_conflicts_and_all_three_originals_survive():
    doc = json.loads(REAL.read_bytes())
    row = doc["response"][0]
    original = next(s for s in row["seasons"] if s["year"] == 2018)
    row["seasons"] = [copy.deepcopy(original) for _ in range(3)]
    doc.update(response=[row], results=1)
    parsed = parse_apif_catalogue(json.dumps(doc).encode())
    assert len(parsed["leagues"][0]["seasons"]) == 1
    assert parsed["leagues"][0]["seasons"][0]["access"] == "conflict"
    assert parsed["warnings"][0]["original_records"] == [original, original, original]


def test_conflicting_year_still_reports_invalid_dates_in_its_original_records():
    doc = json.loads(REAL.read_bytes())
    original = [s for s in doc["response"][0]["seasons"] if s["year"] == 2018]
    original[1].update(start="2286-11-20", end="1970-01-01")
    doc["response"][0]["seasons"] = original
    doc.update(response=doc["response"][:1], results=1)
    parsed = parse_apif_catalogue(json.dumps(doc).encode())
    conflict = parsed["leagues"][0]["seasons"][0]
    assert conflict["status"] == "conflict" and conflict["access"] == "conflict"
    assert {warning["code"] for warning in conflict["warnings"]} == {
        "duplicate_season_year",
        "invalid_season_dates",
    }
    assert conflict["warnings"][0]["original_records"] == original
    assert parsed["warnings"] == conflict["warnings"]


def test_conflicts_are_archived_with_raw_pointer_and_never_planned(tmp_path, actions, monkeypatch):
    database = tmp_path / "sentinel.db"
    database.write_bytes(b"untouched")
    summary_path = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    with responses.RequestsMock() as http:
        _responses(http, apif=REAL.read_bytes())
        report = collect_catalogue(database, "real-conflicts")
    assert report["status"] == "complete" and report["errors"] == []
    observed = report["providers"]["apif"]
    assert observed["league_count"] == 5 and observed["season_count"] == 57
    assert observed["raw_season_count"] == 62 and observed["conflicted_season_count"] == 5
    assert len(observed["warnings"]) == 5
    folder = tmp_path / "state/catalogue/real-conflicts"
    archived = json.loads((folder / "apif.json").read_bytes())
    assert archived["warnings"] == observed["warnings"]
    assert report["warnings"] == [
        dict(warning, provider="apif") for warning in observed["warnings"]
    ]
    assert all(
        w["raw_path"] == "state/catalogue/real-conflicts/apif-raw.json" for w in report["warnings"]
    )
    assert (folder / "apif-raw.json").read_bytes() == REAL.read_bytes()
    assert json.loads((folder / "summary.json").read_bytes()) == report
    latest = json.loads((folder.parent / "latest.json").read_bytes())
    pointer = latest["providers"]["apif"]
    assert pointer["sha256"] == hashlib.sha256((folder / "apif.json").read_bytes()).hexdigest()
    assert pointer["raw_sha256"] == hashlib.sha256(REAL.read_bytes()).hexdigest()
    write_summary(report)
    summary = summary_path.read_text()
    for code, year in CONFLICTS.items():
        assert f"league {code}, season {year}" in summary
    assert "apif-raw.json" in summary and "2 original records" in summary
    plan = catalogue_scopes(tmp_path, "apif", registry=registry_for_fixture())
    assert len(plan["scopes"]) == 52 and plan["missing"] == []
    assert plan["access_is_verified"] is False
    assert {(e["code"], e["season"]) for e in plan["excluded"]} == set(CONFLICTS.items())
    for code, years in NORMAL_YEARS.items():
        assert [
            scope["season"] for candidate, scope in plan["scopes"] if candidate == code
        ] == years
        with pytest.raises(ValueError, match="赛季"):
            catalogue_scopes(
                tmp_path,
                "apif",
                code,
                CONFLICTS[code],
                CONFLICTS[code],
                registry=registry_for_fixture(),
            )
    # A later transport failure must retain the successful conflict-aware pointer.
    with responses.RequestsMock() as http:
        _responses(http, apif=b'{"errors":{"server":"offline"}}', apif_status=503)
        failed = collect_catalogue(database, "after-conflicts")
    assert failed["status"] == "partial"
    assert json.loads((folder.parent / "latest.json").read_bytes())["providers"]["apif"] == pointer
    assert database.read_bytes() == b"untouched"


@pytest.mark.parametrize("marker", ["status", "access"])
def test_backfill_rejects_conflicts_even_with_positive_coverage(tmp_path, marker):
    folder = tmp_path / "state/catalogue/marked"
    folder.mkdir(parents=True)
    # Independently authored normalized metadata tests the planner without the parser.
    doc = {
        "version": 1,
        "provider": "apif",
        "leagues": [
            {
                "id": "397",
                "name": "Girabola",
                "seasons": [
                    {"year": 2017, "fixtures": True, "results": True, "access": "unverified"},
                    {"year": 2018, "fixtures": True, "results": True, marker: "conflict"},
                    {"year": 2019, "fixtures": True, "results": True, "access": "unverified"},
                ],
            }
        ],
    }
    payload = json.dumps(doc).encode()
    (folder / "apif.json").write_bytes(payload)
    (folder.parent / "latest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {
                    "apif": {
                        "path": "state/catalogue/marked/apif.json",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                },
            }
        )
    )
    plan = catalogue_scopes(tmp_path, "apif", "397", registry=registry_for_fixture())
    assert [scope["season"] for _, scope in plan["scopes"]] == [2017, 2019]
    assert [(e["code"], e["season"]) for e in plan["excluded"]] == [("397", 2018)]
    # Repeating a normalized year cannot override an earlier conflict marker.
    doc["leagues"][0]["seasons"].append({"year": 2018, "access": "unverified"})
    payload = json.dumps(doc).encode()
    (folder / "apif.json").write_bytes(payload)
    latest = json.loads((folder.parent / "latest.json").read_bytes())
    latest["providers"]["apif"]["sha256"] = hashlib.sha256(payload).hexdigest()
    (folder.parent / "latest.json").write_text(json.dumps(latest))
    with pytest.raises(ValueError, match="重复"):
        catalogue_scopes(tmp_path, "apif", "397", registry=registry_for_fixture())
