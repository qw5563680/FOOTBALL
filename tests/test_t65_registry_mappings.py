"""Unreviewed catalogue proposals are evidence, never import permission."""

import copy
import json
from pathlib import Path

import pytest

from zucai.league_registry import load_registry, provider_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/fixtures/t61/registry_policy.json"


def proposal_registry():
    doc = json.loads(FIXTURE.read_bytes())
    row = doc["competitions"][0]
    evidence = [{"kind": "fixture", "claim": "Synthetic validation case"}]
    row["official_catalogue"] = {
        "status": "candidate",
        "proposed_status": "verified",
        "review_status": "pending",
        "searched_names": ["Synthetic"],
        "reason": "Manual identity proposal, not reviewed",
        "owner_decision_required": False,
        "entries": [
            {
                "league_id": 123,
                "league_abbr": "Synthetic",
                "league_name": "Synthetic league",
                "observed_match_count": 1,
                "first_match_date": "2026-01-01",
                "last_match_date": "2026-01-01",
                "evidence": evidence,
            }
        ],
        "master_context": {
            "source_names": row["source_names"],
            "jc_match_count": 1,
            "jc_first_sale_day": "2026-01-01",
            "jc_last_sale_day": "2026-01-01",
            "sfc_match_count": 0,
            "sfc_first_issue": None,
            "sfc_last_issue": None,
        },
        "observation_window": {
            "start_beijing": "2025-11-01",
            "end_beijing": "2026-10-31",
            "count_basis": "official result date; master sale date",
        },
        "evidence": evidence,
    }
    row["api_catalogue_proposal"] = {
        "status": "candidate",
        "review_status": "pending",
        "searched_names": ["Synthetic"],
        "reason": "Catalogue listing does not grant access",
        "entries": [
            {
                "code": "1002",
                "name": "Synthetic",
                "country": "Fixture country",
                "type": "League",
                "catalogue_seasons": [2024],
                "evidence": evidence,
            }
        ],
    }
    return doc


def save(tmp_path, doc):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def test_proposal_fields_do_not_grant_permission(tmp_path):
    doc = proposal_registry()
    path = save(tmp_path, doc)
    assert load_registry(path) == doc
    assert provider_config("apif", path=path) == provider_config("apif", path=FIXTURE)


def test_reviewed_proposal_requires_an_approved_mapping_for_every_entry(tmp_path):
    doc = proposal_registry()
    proposal = doc["competitions"][0]["api_catalogue_proposal"]
    proposal.update(status="verified", review_status="verified")
    with pytest.raises(ValueError, match="proposal identity is not approved"):
        load_registry(save(tmp_path, doc))
    proposal["entries"][0]["code"] = "1001"
    assert load_registry(save(tmp_path, doc)) == doc


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["official_catalogue"].update(status="verified"),
        lambda r: r["official_catalogue"].update(review_status="verified"),
        lambda r: r["official_catalogue"].update(owner_decision_required="false"),
        lambda r: r["official_catalogue"].update(entries=[]),
        lambda r: r["official_catalogue"].update(unexpected=True),
        lambda r: r["official_catalogue"]["entries"][0].update(league_id=True),
        lambda r: r["official_catalogue"]["entries"][0].update(observed_match_count=-1),
        lambda r: r["official_catalogue"]["entries"][0].update(first_match_date="2026-99-99"),
        lambda r: r["official_catalogue"]["entries"][0].update(last_match_date="2025-01-01"),
        lambda r: r["official_catalogue"]["entries"].append(
            copy.deepcopy(r["official_catalogue"]["entries"][0])
        ),
        lambda r: r["api_catalogue_proposal"].update(status="verified"),
        lambda r: r["api_catalogue_proposal"].update(review_status="existing_verified"),
        lambda r: r["api_catalogue_proposal"]["entries"][0].update(code="9999"),
        lambda r: r["api_catalogue_proposal"]["entries"][0].update(type="Unknown"),
        lambda r: r["api_catalogue_proposal"]["entries"][0].update(catalogue_seasons=[2024, 2024]),
    ],
)
def test_invalid_proposals_rejected(tmp_path, change):
    doc = proposal_registry()
    change(doc["competitions"][0])
    with pytest.raises(ValueError):
        load_registry(save(tmp_path, doc))
