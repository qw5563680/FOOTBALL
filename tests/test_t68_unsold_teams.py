"""T68 开启检查：竞彩从不销售的预备队经所有者逐队批准后可不绑定（合成样本，只测规则）。"""

import importlib
import json
from hashlib import sha256
from pathlib import Path

import pytest

from zucai import identity_unsold_teams as unsold

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t68/daily_gate/facts.json"
CUTOFF = "2026-11-14T12:00:00Z"
FIRST = "club/men/senior/first"


def _registry(tmp_path, **changes):
    folder = tmp_path / "identity_reviews"
    (folder / "evidence").mkdir(parents=True, exist_ok=True)
    note = folder / "evidence/reserve.json"
    note.write_text('{"claim": "synthetic"}')
    entry = dict(
        competition_id="307",
        provider_team_id="99",
        provider_name="Jong Test",
        category="club/men/senior/reserve",
        status="approved",
        evidence=[
            dict(
                path="evidence/reserve.json",
                sha256=sha256(note.read_bytes()).hexdigest(),
                observed_utc="2026-11-10T00:00:00Z",
            )
        ],
        approved_by="qw5563680",
        approved_utc="2026-11-12T00:00:00Z",
        approval_pr="https://github.com/qw5563680/FOOTBALL/pull/1",
        review_url="https://github.com/qw5563680/FOOTBALL/pull/1",
    )
    entry.update(changes)
    path = folder / "t68-unsold-teams.json"
    path.write_text(json.dumps(dict(version=1, teams=[entry]), ensure_ascii=False))
    return path


def _facts(label="Jong Test"):
    """The gate fixture plus one current-season fixture against an unbound reserve side."""
    facts = json.loads(FIXTURE.read_bytes())
    for row in facts["apif"]:
        row.update(home_name="Home", away_name="Away", home_category=FIRST, away_category=FIRST)
    facts["apif"].append(
        dict(
            facts["apif"][0],
            match_id="apif:99",
            kickoff_utc="2026-10-27T12:00:00Z",
            away="99",
            away_name=label,
        )
    )
    return facts


def _gate(facts):
    module = importlib.import_module("zucai.identity_daily_gate")
    return module.assess_league(facts, "307", CUTOFF)


def test_unregistered_unbound_roster_team_still_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(unsold, "REGISTRY", tmp_path / "missing.json")
    result = _gate(_facts())
    assert "current_roster_unbound" in result["blockers"]
    assert result["evidence"]["current_roster_unsold"]["refused"] == [
        dict(provider_team_id="99", labels=["Jong Test"], reason="not_registered")
    ]


def test_owner_approved_reserve_side_may_stay_unbound(tmp_path, monkeypatch):
    monkeypatch.setattr(unsold, "REGISTRY", _registry(tmp_path))
    result = _gate(_facts())
    assert "current_roster_unbound" not in result["blockers"]
    (accepted,) = result["evidence"]["current_roster_unsold"]["accepted"]
    assert accepted["provider_team_id"] == "99" and accepted["labels"] == ["Jong Test"]
    assert accepted["category"] == "club/men/senior/reserve" and accepted["fixtures"] == 1
    assert result["evidence"]["current_roster_unsold"]["refused"] == []
    # It lifts only this one requirement: nothing else about the check changes.
    assert result["status"] == "insufficient_evidence"
    assert result["teams"]["total"] == result["teams"]["bound"] == 2


@pytest.mark.parametrize(
    ("change", "label", "reason"),
    [
        (dict(status="proposed"), "Jong Test", "not_approved"),
        (dict(approved_utc="2026-11-14T13:00:00Z"), "Jong Test", "approved_after_cutoff"),
        (dict(provider_team_id="98"), "Jong Test", "not_registered"),
        (dict(competition_id="308"), "Jong Test", "not_registered"),
        ({}, "Test Town", "label_without_reserve_marker"),
        (dict(provider_team_id="10"), "Jong Test", "not_registered"),
    ],
)
def test_reserve_exception_is_reverified_not_declared(tmp_path, monkeypatch, change, label, reason):
    monkeypatch.setattr(unsold, "REGISTRY", _registry(tmp_path, **change))
    result = _gate(_facts(label))
    assert "current_roster_unbound" in result["blockers"]
    (refused,) = result["evidence"]["current_roster_unsold"]["refused"]
    assert refused["provider_team_id"] == "99" and refused["reason"] == reason


def test_entry_for_a_bound_provider_id_is_never_used(tmp_path):
    """An approved entry cannot unbind a team that has an approved binding."""
    registry = unsold.load_unsold(_registry(tmp_path, provider_team_id="10"))
    facts = _facts()
    accepted, refused = unsold.unsold_roster(facts, "307", {"10"}, CUTOFF, registry=registry)
    assert accepted == {} and refused["10"]["reason"] == "provider_id_is_bound"


def test_every_observed_label_must_carry_the_reserve_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(unsold, "REGISTRY", _registry(tmp_path))
    facts = _facts()
    facts["apif"].append(dict(facts["apif"][-1], match_id="apif:100", away_name="Test Town"))
    result = _gate(facts)
    (refused,) = result["evidence"]["current_roster_unsold"]["refused"]
    assert refused["reason"] == "label_without_reserve_marker"


@pytest.mark.parametrize(
    "change",
    [
        dict(category="club/men/senior/first"),
        dict(category="reserve"),
        dict(evidence=[]),
        dict(approved_by="someone"),
        dict(review_url=""),
        dict(approved_utc="2026-11-01T00:00:00Z"),
        dict(provider_team_id="0"),
    ],
)
def test_registry_rejects_malformed_or_unapproved_entries(tmp_path, change):
    with pytest.raises(ValueError):
        unsold.load_unsold(_registry(tmp_path, **change))


def test_registry_rejects_tampered_evidence_and_duplicates(tmp_path):
    path = _registry(tmp_path)
    (path.parent / "evidence/reserve.json").write_text("changed")
    with pytest.raises(ValueError):
        unsold.load_unsold(path)
    path = _registry(tmp_path)
    document = json.loads(path.read_text())
    document["teams"] *= 2
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        unsold.load_unsold(path)
    assert unsold.load_unsold(tmp_path / "absent.json") == {}
