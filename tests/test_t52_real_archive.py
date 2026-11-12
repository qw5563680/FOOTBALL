"""26163 regressions use immutable, cropped production archive rows, never the network."""

import copy
import csv
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from zucai.cloud_feeds import TABLES
from zucai.cloud_predict import open_national_fixtures
from zucai.db import get_engine, init_db
from zucai.models import Match, SfcIssue, Team
from zucai.report.batch import predict_batch
from zucai.report.sfc import build_sfc_report
from zucai.sources.sfc import refresh_snapshot

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t52"
EXPECTED_SIX = {
    "apif:1528958",
    "apif:1528959",
    "apif:1528961",
    "apif:1528962",
    "apif:1528963",
    "apif:1545613",
}
CUTOFF = datetime.fromisoformat("2026-11-11T17:43:58.323712+00:00")


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def real_issue(tmp_path):
    engine = get_engine(tmp_path / "t52.db")
    init_db(engine)
    with Session(engine) as session:
        for table, cls in (("teams", Team), ("matches", Match)):
            for line in (FIXTURE / f"{table}.jsonl").read_text().splitlines():
                row = dict(zip(TABLES[table], json.loads(line), strict=True))
                if table == "teams":
                    row["aliases"] = json.loads(row["aliases"])
                session.add(cls(**row))
            session.flush()
        issue = SfcIssue(**json.loads((FIXTURE / "26163.json").read_text())["issue"])
        session.add(issue)
        session.flush()
        yield session, issue
    engine.dispose()


def test_actual_26163_maps_all_without_mutating_original(real_issue):
    session, issue = real_issue
    original = json_bytes(issue.snapshot)
    rows = refresh_snapshot(session, issue)
    assert len(rows) == 14
    assert all(row["mapping"] == "matched" for row in rows)
    assert {row["match_id"] for row in rows[:6]} == EXPECTED_SIX
    for old, row in zip(issue.snapshot[:6], rows[:6], strict=True):
        evidence = row["mapping_evidence"]
        assert evidence["basis"] == "national_team_ids_and_exact_kickoff"
        assert evidence["bridge_fallback_reason"] == "target_match_missing"
        assert evidence["bridge"] == old["mapping_evidence"]
        assert session.get(Match, evidence["bridge"]["jc_id"]) is None
    assert [row["match_id"] for row in rows[6:]] == [row["match_id"] for row in issue.snapshot[6:]]
    assert json_bytes(issue.snapshot) == original


def test_actual_26163_enters_nov13_batch_input(real_issue):
    session, _ = real_issue
    dates = open_national_fixtures(session, CUTOFF)
    keys = sorted(dates.get(date(2026, 11, 13), set()))
    # Exercise the actual batch selector without embedding a binary model in a fixture.
    # scripts/verify-t52-archive.py also loads the SHA-verified archived trained model.
    batch = predict_batch(
        session,
        model_files=[],
        as_of=CUTOFF,
        generated=CUTOFF,
        match_ids=keys,
        persist=False,
        allow_empty=True,
    )
    assert set(batch["input"]["match_ids"]) == EXPECTED_SIX


def test_actual_eight_match_ids_and_frozen_probabilities_are_byte_equal(real_issue):
    session, issue = real_issue
    batch = json.loads((FIXTURE / "forecast-eight.json").read_text())
    original_batch = json_bytes(batch)
    report = build_sfc_report(
        session,
        issue,
        batch,
        forecast_sha256=hashlib.sha256(original_batch).hexdigest(),
        generated=datetime.fromisoformat(batch["generated_utc"]),
        allow_missing=True,
    )
    old = {row["match_id"]: row["model_probabilities"] for row in batch["predictions"]}
    before = [
        {"match_id": row["match_id"], "probabilities": old[row["match_id"]]}
        for row in issue.snapshot[6:]
    ]
    after = [
        {"match_id": row["match_id"], "probabilities": row["probabilities"]}
        for row in report["rows"][6:]
    ]
    assert json_bytes(before) == json_bytes(after)
    assert json_bytes(batch) == original_batch


def test_wrong_bridge_to_another_actual_archived_match_still_conflicts(real_issue):
    session, issue = real_issue
    changed = copy.deepcopy(issue.snapshot)
    wrong = changed[6]["match_id"]
    assert session.get(Match, wrong) is not None
    changed[0]["mapping_evidence"]["jc_id"] = wrong
    issue.snapshot = changed
    row = refresh_snapshot(session, issue)[0]
    assert row["mapping"] == "ambiguous"
    assert row["match_id"] is None
    assert row["reason"] == "conflicting_identity"
    assert wrong in row["candidate_ids"]


def test_archive_fixture_hashes_and_real_sale_filter_evidence():
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    for name, meta in provenance["files"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == meta["sha256"]
    for name in ("jc_catalog", "jc_skipped"):
        with (FIXTURE / f"{name}.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        assert {row["match_id"] for row in rows} == {f"jc:{i}" for i in range(2042692, 2042698)}
        assert all(row["sellStatus"] == "2" and row["retained"] == "0" for row in rows)
        if name == "jc_skipped":
            assert all(row["reason"] == "sell_status_not_1" for row in rows)
