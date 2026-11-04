"""Synthetic endpoint audits; no values in this file claim live coverage."""

import csv
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "data/fixtures/t30"


def fixture(name):
    return json.loads((ROOT / f"{name}.json").read_bytes())


def test_sampler_covers_status_groups_before_filling_and_prefers_existing_ids():
    from zucai.stats_probe import sample_fixtures

    rows = fixture("fixtures")["response"]
    known = fixture("cases")["known_ids"]
    assert [r["fixture"]["id"] for r in sample_fixtures(rows, 3, known)] == [102, 202, 302]
    assert [r["fixture"]["id"] for r in sample_fixtures(rows, 2, known)] == [102, 202]
    assert [r["fixture"]["id"] for r in sample_fixtures(rows, 1, known)] == [102]
    assert len(sample_fixtures(rows, 20, known)) == 7  # NS is never sampled.
    assert sample_fixtures([], 2, known) == []


@pytest.mark.parametrize("name", ["statistics", "events", "lineups", "players", "injuries"])
def test_endpoint_audit_reads_fixture_fields_and_preserves_nulls(name):
    from zucai.stats_probe import parse_response

    doc = fixture(name)
    result = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
    assert result["outcome"] == "ok"
    assert result["results"] == len(doc["response"])
    assert result["null_fields"] > 0 and json.loads(result["field_paths"])
    if name == "statistics":
        assert result["null_fields"] == 1
        assert result["expected_goals_present"] is True
        assert result["expected_goals_non_null"] is True
        assert json.loads(result["statistic_types"]) == [
            "Ball Possession",
            "Goalkeeper Saves",
            "Shots on Goal",
            "expected_goals",
        ]
    else:
        assert result["expected_goals_present"] is False


@pytest.mark.parametrize("shape", ["missing", "null", "zero", "string"])
def test_expected_goals_missing_null_and_zero_are_distinct(shape):
    from zucai.stats_probe import parse_response

    doc = fixture("statistics")
    doc["response"] = doc["response"][:1]
    fields = doc["response"][0]["statistics"]
    fields[:] = [row for row in fields if row["type"] != "expected_goals"]
    if shape != "missing":
        fields.append(
            {"type": "expected_goals", "value": {"null": None, "zero": 0, "string": "1.89"}[shape]}
        )
    doc["results"] = 1
    result = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
    assert result["expected_goals_present"] is (shape != "missing")
    assert result["expected_goals_non_null"] is (shape in {"zero", "string"})


@pytest.mark.parametrize("name", ["statistics", "events", "lineups", "players", "injuries"])
def test_empty_and_api_errors_cannot_count_as_valid_data(name):
    from zucai.stats_probe import parse_response

    original = fixture(name)
    doc = deepcopy(original)
    doc.update(response=[], results=0)
    row = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
    assert row["outcome"] == "empty" and row["reason"]
    for errors in fixture("cases")["errors"].values():
        doc["errors"] = errors
        row = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
        assert row["outcome"] == "error" and "errors" in row["reason"]


@pytest.mark.parametrize("bad", ["get", "parameters", "results", "response", "paging"])
def test_mismatched_envelope_is_audited_as_structure_error(bad):
    from zucai.stats_probe import parse_response

    original = fixture("statistics")
    doc = deepcopy(original)
    doc[bad] = {
        "get": "predictions",
        "parameters": {"fixture": "999"},
        "results": 99,
        "response": {},
        "paging": {"current": 2, "total": 2},
    }[bad]
    row = parse_response(json.dumps(doc).encode(), original["get"], original["parameters"])
    assert row["outcome"] == "error" and "结构" in row["reason"]


def test_audit_deduplicates_replayed_observation_but_keeps_new_run_and_changed_raw(tmp_path):
    from zucai.stats_probe import FIELDS, append_audit

    path = tmp_path / "audit.csv"
    row = dict.fromkeys(FIELDS, "")
    row.update(
        run_id="one",
        league=3,
        season=2024,
        fixture_id=102,
        endpoint="fixtures/statistics",
        parameters='{"fixture":"102"}',
        raw_sha256="a" * 64,
        outcome="ok",
        observed_utc="2026-11-04T00:00:00Z",
    )
    append_audit(path, row)
    before = path.read_bytes()
    append_audit(path, {**row, "observed_utc": "2026-11-04T00:00:01Z", "elapsed_ms": 9})
    assert path.read_bytes() == before
    append_audit(path, {**row, "run_id": "two"})
    append_audit(path, {**row, "raw_sha256": "b" * 64})
    assert len(list(csv.DictReader(path.open()))) == 3


@pytest.mark.parametrize("endpoint", ["leagues", "events", "lineups", "players", "injuries"])
def test_nonempty_but_unstructured_payload_is_not_valid(endpoint):
    from zucai.stats_probe import parse_response

    doc = fixture(endpoint)
    doc["response"] = [{"league": None}] if endpoint == "leagues" else [{}]
    doc["results"] = 1
    row = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
    assert row["outcome"] == "error" and "结构" in row["reason"]


def test_all_null_statistics_are_distinct_from_zero_statistics():
    from zucai.stats_probe import parse_response

    doc = fixture("statistics")
    for team in doc["response"]:
        for stat in team["statistics"]:
            stat["value"] = None
    row = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
    assert row["outcome"] == "empty" and row["null_fields"] == 6
    doc["response"][0]["statistics"][0]["value"] = 0
    row = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
    assert row["outcome"] == "ok" and row["null_fields"] == 5


def test_malformed_fixture_status_is_audited_instead_of_crashing_sampler():
    from zucai.stats_probe import parse_response

    doc = fixture("fixtures")
    doc["response"][0]["fixture"]["status"]["short"] = []
    row = parse_response(json.dumps(doc).encode(), "fixtures", doc["parameters"])
    assert row["outcome"] == "error"


@pytest.mark.parametrize("endpoint", ["events", "lineups", "players", "injuries"])
@pytest.mark.parametrize("value", [None, "unrelated"])
def test_unknown_payload_fields_do_not_count_as_effective_technical_data(endpoint, value):
    from zucai.stats_probe import parse_response

    doc = fixture(endpoint)
    doc.update(response=[{"unexpected": value}], results=1)
    row = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
    assert row["outcome"] == "error"


@pytest.mark.parametrize("endpoint", ["lineups", "players"])
def test_team_metadata_without_players_is_empty_technical_data(endpoint):
    from zucai.stats_probe import parse_response

    doc = fixture(endpoint)
    for team in doc["response"]:
        if endpoint == "lineups":
            team.update(startXI=[], substitutes=[])
        else:
            team["players"] = []
    row = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
    assert row["outcome"] == "empty" and row["reason"]


def test_injuries_body_must_match_requested_league_and_season():
    from zucai.stats_probe import parse_response

    doc = fixture("injuries")
    doc["response"][0]["league"].update(id=999, season=1999)
    row = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
    assert row["outcome"] == "error"


def test_api_error_reason_names_observed_category_without_exposing_message():
    from zucai.stats_probe import parse_response

    doc = fixture("events")
    for errors in fixture("cases")["errors"].values():
        doc["errors"] = errors
        row = parse_response(json.dumps(doc).encode(), doc["get"], doc["parameters"])
        assert row["outcome"] == "error"
        assert all(key in row["reason"] for key in errors)
        assert all(value not in row["reason"] for value in errors.values())
