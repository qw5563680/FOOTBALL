"""An explicit proposal may explain a short name but cannot approve a download route."""

from collections import Counter

import pytest

from test_t65_registry_mappings import proposal_registry, save
from zucai.catalogue_mappings import crosscheck_official


def dictionary():
    return [{"league_id": 123, "league_name": "Synthetic league", "league_abbr": "Synthetic"}]


def test_explicit_identity_and_counts_with_review_pending(tmp_path):
    path = save(tmp_path, proposal_registry())
    rows, decisions = crosscheck_official(
        Counter({(123, "Synthetic league", "Synthetic"): 7}),
        dictionary(),
        registry_path=path,
        observed_ranges=[
            {"league_id": 123, "first_match_day": "2026-01-01", "last_match_day": "2026-03-01"}
        ],
    )
    assert not decisions
    assert rows[0]["status"] == "official_identity_proposed_observed"
    assert rows[0]["review_status"] == "pending"
    assert not rows[0]["grants_import_permission"]
    assert rows[0]["observations"][0]["match_count"] == 7
    assert rows[0]["observations"][0]["last_match_day"] == "2026-03-01"
    assert rows[0]["master_context"]["jc_match_count"] == 1


def test_no_observed_matches_is_not_an_owner_decision(tmp_path):
    rows, decisions = crosscheck_official(
        Counter(), dictionary(), registry_path=save(tmp_path, proposal_registry())
    )
    assert not decisions
    assert rows[0]["status"] == "official_identity_proposed_not_observed"
    assert rows[0]["observations"][0]["first_match_day"] is None


def test_dictionary_rename_is_not_automatically_accepted(tmp_path):
    renamed = [{"league_id": 123, "league_name": "Another competition", "league_abbr": "Synthetic"}]
    rows, decisions = crosscheck_official(
        Counter(), renamed, registry_path=save(tmp_path, proposal_registry())
    )
    assert rows[0]["status"] == "catalogue_changed_review_required"
    assert decisions[0]["discrepancies"][0]["reason"] == "official_identity_changed"


def test_ambiguous_proposal_stays_in_decision_list(tmp_path):
    doc = proposal_registry()
    doc["competitions"][0]["official_catalogue"].update(
        proposed_status="candidate", owner_decision_required=True
    )
    rows, decisions = crosscheck_official(
        Counter(), dictionary(), registry_path=save(tmp_path, doc)
    )
    assert rows[0]["status"] == "owner_decision"
    assert len(decisions) == 1


def test_observed_identity_disagrees_with_dictionary(tmp_path):
    with pytest.raises(ValueError, match="contradicts"):
        crosscheck_official(
            Counter({(123, "Wrong", "Synthetic"): 1}),
            dictionary(),
            registry_path=save(tmp_path, proposal_registry()),
        )
