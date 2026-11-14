"""Only hand-authored data/fixtures/t63/source-audit.json is used; no real collection."""

import csv
import hashlib
import importlib
import json
import sqlite3
from pathlib import Path

import pytest

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "data/fixtures/t63/source-audit.json").read_bytes()
)
DECISION = "keep_existing_source_pending_owner_review"


def database(tmp_path, *cases):
    path = tmp_path / "synthetic.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT, aliases TEXT);"
            "CREATE TABLE matches (match_id TEXT PRIMARY KEY, source TEXT, league TEXT, "
            "home_team_id INTEGER, away_team_id INTEGER, kickoff_utc TEXT, counterpart_id TEXT);"
            "CREATE TABLE results (match_id TEXT PRIMARY KEY, home_goals INTEGER, "
            "away_goals INTEGER, available_utc TEXT, available_source TEXT);"
        )
        connection.executemany(
            "INSERT INTO teams VALUES (?, ?, ?)",
            [
                (team_id, name, json.dumps(FIXTURE["team_aliases"].get(str(team_id), [])))
                for team_id, name in FIXTURE["teams"]
            ],
        )
        for case in cases:
            for values in FIXTURE["cases"][case]:
                record = dict(zip(FIXTURE["columns"], values, strict=True))
                connection.execute("INSERT INTO matches VALUES (?, ?, ?, ?, ?, ?, ?)", values[:7])
                if record["home_goals"] is not None:
                    connection.execute(
                        "INSERT INTO results VALUES (?, ?, ?, ?, ?)",
                        [record["match_id"], *values[7:]],
                    )
    return path


def audit(path, destination):
    # The initial red test fails on the missing public module, after fixture setup succeeds.
    module = importlib.import_module("zucai.source_results_audit")
    summary = module.audit_source_results(path, destination)
    with destination.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        assert reader.fieldnames
    assert all(row["decision"] == DECISION for row in rows)
    return summary, rows


def test_audit_is_read_only_deterministic_and_preserves_both_evidence_versions(tmp_path):
    path = database(tmp_path, "consistent", "kickoff_only")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    destination = tmp_path / "audit" / "source_results.csv"
    summary, rows = audit(path, destination)
    assert summary["matched"] == 2
    assert summary["conflicts"] == 1
    assert summary["unmatched"] == summary["unverified"] == 0
    row = next(row for row in rows if row["apif_match_id"] == "apif:kickoff")
    assert row["classification"] == "kickoff_only_conflict"
    assert row["link_basis"] == "exact_team_ids_unique"
    assert row["kickoff_delta_seconds"] == "3600"
    assert row["apif_score"] == row["comparison_score"] == "2:1"
    assert row["apif_home_team_id"] == row["comparison_home_team_id"] == "1"
    assert row["apif_available_utc"] == "2026-09-02T18:00:00Z"
    assert row["comparison_available_utc"] == "2026-09-03T14:00:00Z"
    assert row["comparison_available_source"] == "published"
    assert summary["rows"] == len(rows) == 1
    assert summary["comparison_rows"] == 2
    assert summary["classifications"]["consistent"] == 1
    assert all(row["classification"] != "consistent" for row in rows)
    original_csv = destination.read_bytes()
    assert audit(path, destination)[0] == summary
    assert destination.read_bytes() == original_csv
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    ("case", "classification"),
    [
        ("disciplinary", "disciplinary_vs_on_field"),
        ("other_german_conflict", "score_conflict"),
        ("other_berlin_date", "score_conflict"),
        ("score_and_kickoff", "score_and_kickoff_conflict"),
    ],
)
def test_conflicts_are_classified_without_choosing_a_score(tmp_path, case, classification):
    summary, rows = audit(database(tmp_path, case), tmp_path / "audit.csv")
    assert summary["matched"] == summary["conflicts"] == 1
    assert len(rows) == 1
    assert rows[0]["classification"] == classification


@pytest.mark.parametrize(
    ("case", "reason", "count"),
    [
        ("reverse", "reversed_home_away", 1),
        ("temporary_identity", "temporary_team_identity", 1),
        ("unknown_team_prefix", "temporary_team_identity", 1),
        ("multiple_comparison", "multiple_comparison_candidates", 2),
        ("multiple_apif", "multiple_apif_candidates", 2),
        ("multiple_apif_without_results", "multiple_apif_candidates", 2),
        ("multiple_comparisons_without_apif_result", "multiple_comparison_candidates", 2),
        ("outside_window", "kickoff_outside_window", 1),
        ("unknown_counterpart", "unknown_source_prefix", 1),
        ("dangling_counterpart", "counterpart_missing", 1),
        ("wrong_identity", "team_id_mismatch", 1),
        ("invalid_kickoff", "invalid_kickoff", 1),
        ("league_mismatch", "league_mismatch", 1),
        ("unknown_match_prefix", "unknown_source_prefix", 1),
    ],
)
def test_unverified_links_are_excluded_with_evidence(tmp_path, case, reason, count):
    summary, rows = audit(database(tmp_path, case), tmp_path / "audit.csv")
    assert summary["matched"] == summary["conflicts"] == 0
    assert summary["unverified"] == count
    assert summary["unmatched"] == 0
    assert len(rows) == count
    assert {row["classification"] for row in rows} == {"unverified"}
    assert {row["reason"] for row in rows} == {reason}
    assert all(row["link_basis"] != "exact_team_ids_unique" for row in rows)
    if case == "outside_window":
        assert rows[0]["comparison_match_id"] == "fd:late-link"
        assert rows[0]["kickoff_delta_seconds"] == "10801"
        assert rows[0]["apif_score"] == rows[0]["comparison_score"] == "2:1"


def test_three_hour_boundary_and_reverse_counterpart_are_verifiable(tmp_path):
    summary, rows = audit(database(tmp_path, "boundary_window"), tmp_path / "audit.csv")
    assert summary["matched"] == summary["conflicts"] == 1
    assert rows[0]["link_basis"] == "counterpart"
    assert rows[0]["kickoff_delta_seconds"] == "10800"


def test_names_do_not_link_different_stored_team_ids(tmp_path):
    summary, rows = audit(database(tmp_path, "same_names_different_ids"), tmp_path / "audit.csv")
    assert summary["matched"] == summary["unverified"] == 0
    assert summary["unmatched"] == 2
    assert summary["reasons"] == {"no_comparison_candidate": 1, "no_apif_candidate": 1}
    assert summary["comparison_rows"] == 2
    assert summary["rows"] == 0
    assert rows == []


def test_missing_result_is_not_an_equal_or_conflicting_score(tmp_path):
    summary, rows = audit(database(tmp_path, "comparison_result_missing"), tmp_path / "audit.csv")
    assert summary["matched"] == summary["conflicts"] == summary["unverified"] == 0
    assert summary["unmatched"] == 1
    assert summary["reasons"] == {"comparison_result_missing": 1}
    assert summary["comparison_rows"] == 1
    assert rows == []


def test_each_comparison_source_is_a_separate_pair(tmp_path):
    summary, rows = audit(database(tmp_path, "all_comparison_sources"), tmp_path / "audit.csv")
    assert summary["matched"] == 3
    assert summary["conflicts"] == 1
    assert summary["unverified"] == summary["unmatched"] == 0
    assert len(rows) == 1
    assert summary["comparison_rows"] == 3
    assert summary["classifications"]["consistent"] == 2


@pytest.mark.parametrize("case", FIXTURE["binding_cases"])
def test_api_identity_requires_unique_explicit_binding_and_global_owner(tmp_path, case):
    scenario = FIXTURE["binding_cases"][case]
    path = database(tmp_path, scenario["base_case"])
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO teams VALUES (?, ?, ?)",
            [(team_id, name, json.dumps(aliases)) for team_id, name, aliases in scenario["teams"]],
        )
    before = path.read_bytes()
    summary, rows = audit(path, tmp_path / "audit.csv")
    row = next(row for row in rows if row["comparison_match_id"] == scenario["comparison_match_id"])
    assert row["classification"] == "unverified"
    assert row["reason"].startswith(scenario["reason_prefix"])
    assert summary["unverified"] == 1
    assert summary["matched"] == scenario["matched"]
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='result_source_identities'"
        ).fetchall()


def test_legacy_missing_alias_column_cannot_verify_api_identity(tmp_path):
    path = database(tmp_path, "consistent")
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE teams DROP COLUMN aliases")
    summary, rows = audit(path, tmp_path / "audit.csv")
    assert summary["matched"] == 0
    assert summary["unverified"] == 1
    assert rows[0]["reason"].startswith("provider_binding_invalid:apif:home:")


def test_old_database_without_apif_results_still_reports_existing_result(tmp_path):
    path = database(tmp_path, "no_apif_result")
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE matches DROP COLUMN counterpart_id")
        connection.execute("ALTER TABLE results DROP COLUMN available_utc")
        connection.execute("ALTER TABLE results DROP COLUMN available_source")
    summary, rows = audit(path, tmp_path / "audit.csv")
    assert summary["status"] == "no_apif_results"
    assert summary["matched"] == 0
    assert summary["unmatched"] == 1
    assert summary["comparison_rows"] == 1
    assert summary["reasons"] == {"apif_result_missing": 1}
    assert rows == []


@pytest.mark.parametrize("has_schema", [False, True])
def test_empty_database_writes_header_and_zero_counts(tmp_path, has_schema):
    if has_schema:
        path = database(tmp_path)
    else:
        path = tmp_path / "empty.sqlite"
        sqlite3.connect(path).close()
    summary, rows = audit(path, tmp_path / "audit.csv")
    assert rows == []
    assert all(summary[key] == 0 for key in ("matched", "conflicts", "unmatched", "unverified"))


def test_scope_excludes_cups_and_the_preexisting_t60_leagues(tmp_path):
    summary, rows = audit(database(tmp_path, "out_of_scope"), tmp_path / "audit.csv")
    assert summary["rows"] == 0
    assert rows == []


def test_destination_cannot_replace_database_or_its_symlink(tmp_path):
    path = database(tmp_path, "consistent")
    before = path.read_bytes()
    alias = tmp_path / "danger.csv"
    alias.symlink_to(path)
    module = importlib.import_module("zucai.source_results_audit")
    for destination in (path, alias):
        with pytest.raises(ValueError, match="数据库"):
            module.audit_source_results(path, destination)
    assert path.read_bytes() == before


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite"
    module = importlib.import_module("zucai.source_results_audit")
    with pytest.raises(ValueError, match="数据库"):
        module.audit_source_results(path, tmp_path / "audit.csv")
    assert not path.exists()
