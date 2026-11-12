"""Real frozen inputs remain unchanged while default strength is disclosed."""

import hashlib
import json
from pathlib import Path

import pytest

from zucai.prediction_disclosure import prior_disclosure, prior_label, uses_unknown_team_prior
from zucai.report.render import _markdown

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t53"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def batch():
    return json.loads((FIXTURE / "forecast-three.json").read_text())


@pytest.mark.parametrize(
    "match_id,home_unknown,away_unknown",
    [("jc:2042519", False, False), ("jc:2042520", True, False), ("jc:2042523", False, True)],
)
def test_actual_archived_sides_are_disclosed_without_rewriting_prediction(
    batch, match_id, home_unknown, away_unknown
):
    row = next(row for row in batch["predictions"] if row["match_id"] == match_id)
    before = canonical(row)
    disclosure = prior_disclosure(row)
    assert disclosure["home"] is home_unknown
    assert disclosure["away"] is away_unknown
    assert disclosure["used"] is (home_unknown or away_unknown)
    assert uses_unknown_team_prior(row) is (home_unknown or away_unknown)
    if disclosure["used"]:
        assert disclosure["method"] == "weakest_quartile_half_shrinkage"
        assert row["home" if home_unknown else "away"] in prior_label(row)
    else:
        assert not prior_label(row)
    assert canonical(row) == before


def test_actual_ac_milan_research_probability_is_retained_in_single_match_report(batch):
    before = canonical(batch)
    text = _markdown(batch)
    line = next(line for line in text.splitlines() if "jc:2042523" in line and "AC米兰" in line)
    assert "默认实力" in line and "费伦茨" in line
    assert "weakest_quartile_half_shrinkage" not in line.replace("\\_", "_")
    assert "weakest_quartile_half_shrinkage" in text.replace("\\_", "_")
    assert text.count("默认实力说明：") == 1
    assert "96.29%" in line
    assert canonical(batch) == before


def test_actual_legacy_flag_does_not_invent_an_unrecorded_side():
    row = json.loads((FIXTURE / "legacy-unknown.json").read_text())
    before = canonical(row)
    disclosure = prior_disclosure(row)
    assert disclosure["used"] is True
    assert disclosure["home"] is None and disclosure["away"] is None
    assert uses_unknown_team_prior(row)
    assert "未记录" in prior_label(row)
    assert canonical(row) == before


def test_synthetic_absent_metadata_is_unrecorded_instead_of_known(batch):
    # Explicitly remove the archived evidence for this control, not a real missing-data claim.
    original = batch["predictions"][0]
    before = canonical(original)
    row = {key: value for key, value in original.items() if key != "metadata"}
    disclosure = prior_disclosure(row)
    assert disclosure["used"] is None
    assert disclosure["home"] is None and disclosure["away"] is None
    assert canonical(original) == before


def test_real_fixture_hashes():
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    for name, meta in provenance["files"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == meta["sha256"]
