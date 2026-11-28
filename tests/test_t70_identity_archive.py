"""Replay a frozen research response and the identity evidence available before kickoff."""

import hashlib
import json
from pathlib import Path

import pytest

from zucai.web_research_validation import parse

FIXTURE = Path(__file__).resolve().parents[1] / "data/fixtures/t70/identity-run-46125497755.json"


@pytest.fixture
def trial():
    return json.loads(FIXTURE.read_bytes())


def test_archived_trial_keeps_its_structure_failure(trial):
    source = trial["provenance"]
    (report_sha,) = [
        value for key, value in source["run_files_sha256"].items() if key.endswith(".md")
    ]
    assert hashlib.sha256(trial["report_text"].encode()).hexdigest() == report_sha
    assert (
        hashlib.sha256(trial["structure_text"].encode()).hexdigest()
        == source["structure_text_sha256"]
    )
    original = json.loads(trial["structure_text"])
    assert "basis" not in original and "counterevidence" not in original
    for side in ("home", "away"):
        assert all(isinstance(item, str) for item in original["injuries_lineups"][side])

    status, row, checks = parse(
        trial["structure_text"],
        trial["material"],
        trial["citations"],
        report_text=trial["report_text"],
    )

    assert status == trial["archived_status"] == "unparsed" and row is None
    assert checks["reason"] == trial["archived_checks"]["reason"] == "invalid_structure"
    assert checks["identity_evidence"]["result"] == "unverified"


def test_archived_trial_resolves_only_the_team_with_approved_prematch_evidence(trial):
    example = json.loads(trial["structure_text"])
    example.update(basis=[], counterevidence=[], injuries_lineups={"home": [], "away": []})

    status, row, checks = parse(
        json.dumps(example),
        trial["material"],
        trial["citations"],
        report_text=trial["report_text"],
    )

    assert status == "unparsed" and row is None
    evidence = checks["identity_evidence"]
    assert evidence["result"] == "unverified"
    assert [(i["field"], i["reason"]) for i in evidence["issues"]] == [("home", "unapproved_name")]
    (match,) = [o for o in evidence["observations"] if o["home"] == "中央海岸水手"]
    assert match["away"] == "奥克兰FC"
    assert match["league"] == "澳超"
    assert match["kickoff_utc"] == "2026-11-28T08:40:00+00:00"
    assert match["resolved"] == {"home": None, "away": 455}
    assert match["linked_matches"] == []
