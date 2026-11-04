import json
from pathlib import Path

import pytest

from zucai.sources.fd_ingest import _canonical

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t10/synthetic_ingest.json"


def test_approved_metadata_validates_and_remains_active_without_changing_proposals():
    from zucai.sources.apif_aliases import validate_approvals

    aliases = json.loads(FIXTURE.read_bytes())["aliases"]
    aliases["日职"]["551"].update(
        approved_by="repository-owner",
        approved_utc="2026-11-03T00:00:00Z",
        approval_pr="https://github.com/qw5563680/FOOTBALL/pull/99",
    )
    before = json.loads(json.dumps(aliases))
    validate_approvals(aliases)
    assert aliases == before and _canonical(aliases, "日职", "551") == "北港"
    aliases["日职"]["551"]["status"] = "proposed"
    assert _canonical(aliases, "日职", "551") is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved_by", ""),
        ("approved_utc", "2026-11-03"),
        ("approval_pr", "https://example.com/99"),
        ("approval_pr", []),
        (
            "approval_pr",
            ["https://github.com/qw5563680/FOOTBALL/pull/99", "https://example.com/99"],
        ),
        ("approval_pr", [None]),
        ("approved_utc", "tomorrow"),
    ],
)
def test_incomplete_or_invalid_approval_metadata_is_rejected(field, value):
    from zucai.sources.apif_aliases import validate_approvals

    aliases = json.loads(FIXTURE.read_bytes())["aliases"]
    entry = aliases["日职"]["551"]
    entry.update(
        approved_by="owner",
        approved_utc="2026-11-03T00:00:00Z",
        approval_pr="https://github.com/qw5563680/FOOTBALL/pull/99",
    )
    entry[field] = value
    with pytest.raises(ValueError, match="审批"):
        validate_approvals(aliases)
    entry.pop("approved_by")
    with pytest.raises(ValueError, match="审批"):
        validate_approvals(aliases)


def test_legacy_approved_entries_remain_compatible():
    from zucai.sources.apif_aliases import validate_approvals

    validate_approvals(json.loads(FIXTURE.read_bytes())["aliases"])


def test_formal_approved_entries_cannot_omit_all_provenance_fields():
    from zucai.sources.apif_aliases import validate_approvals

    with pytest.raises(ValueError, match="审批"):
        validate_approvals(json.loads(FIXTURE.read_bytes())["aliases"], require_metadata=True)
