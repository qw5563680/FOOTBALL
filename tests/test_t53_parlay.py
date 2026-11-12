"""Default-strength paper views preserve probabilities but never promote either leg."""

import copy
import hashlib
import json

import pytest

from test_parlay_archive import install_ledger
from test_parlay_paper import BEFORE
from test_parlay_single_double import FIX
from zucai.digest_parlay import section
from zucai.parlay.archive import raw_json
from zucai.parlay.single_double import build


@pytest.fixture
def sample():
    return json.loads((FIX / "input.json").read_bytes())


def mark_prior(row, home=False, away=True):
    row["metadata"] = {
        "unknown_team_prior": True,
        "home_unknown_team": home,
        "away_unknown_team": away,
        "prior_method": "weakest_quartile_half_shrinkage",
    }


@pytest.mark.parametrize("sides", [(True, False), (False, True), (True, True)])
def test_default_strength_cannot_be_either_leg_or_rank_leader(sample, sides):
    row = sample["batch"]["predictions"][0]
    mark_prior(row, *sides)
    before = copy.deepcopy(sample)
    doc = build(**sample)
    prepared = doc["boards"][0]["matches"][0]
    assert prepared["probabilities"] == row["model_probabilities"]
    assert prepared["eligibility"]["status"] == "excluded"
    assert "unknown_team_prior" in prepared["eligibility"]["reasons"]
    for board in [*doc["boards"], *doc["experimental_boards"]]:
        groups = board["groups"] + board["reference_groups"]
        groups += [board[key] for key in ("highest_coverage", "highest_profit") if board[key]]
        assert all(row["match_id"] not in group["match_ids"] for group in groups)
    assert all(row["match_id"] not in group["match_ids"] for group in doc["groups"])
    diagnostic = next(r for r in doc["diagnostics"] if r["match_id"] == row["match_id"])
    assert diagnostic["prior_disclosure"]["home"] is sides[0]
    assert diagnostic["prior_disclosure"]["away"] is sides[1]
    assert "weakest_quartile_half_shrinkage" in diagnostic["prior_label"]
    assert sample == before


def test_known_team_probabilities_and_candidate_decisions_are_identical(sample):
    before = build(**sample)
    for row in sample["batch"]["predictions"]:
        row["metadata"] = {
            "unknown_team_prior": False,
            "home_unknown_team": False,
            "away_unknown_team": False,
            "prior_method": None,
        }
    assert build(**sample) == before


def test_market_reference_keeps_its_own_source_and_values(sample):
    before = build(**sample)
    for row in sample["batch"]["predictions"]:
        mark_prior(row)
    doc = build(**sample)
    assert not doc["groups"]
    assert doc["market_reference"] == before["market_reference"]
    assert all(r["model_probabilities"] is None for r in doc["market_reference"]["matches"])


def test_experiment_uses_its_own_prior_evidence(sample):
    baseline = sample["batch"]["predictions"][0]
    experiment = copy.deepcopy(baseline)
    experiment.update(model_name="independent-test", model_version="independent-version")
    experiment["metadata"] = {"unknown_team_prior": False}
    mark_prior(baseline)
    baseline["prior_disclosure"] = {
        "used": True,
        "home": False,
        "away": True,
        "method": "weakest_quartile_half_shrinkage",
        "evidence": "propagated",
    }
    extra = {
        "batch_id": sample["batch"]["batch_id"],
        "generated_utc": sample["frozen_utc"],
        "as_of_utc": sample["batch"]["as_of_utc"],
        "predictions": [experiment],
    }
    doc = build(**sample, experimental=extra)
    independent = doc["experimental_boards"][0]["matches"][0]
    assert independent["eligibility"]["status"] == "eligible"
    mark_prior(experiment, True, True)
    independent = build(**sample, experimental=extra)["experimental_boards"][0]["matches"][0]
    assert independent["eligibility"]["status"] == "excluded"


def install_legacy_view(sample, tmp_path, monkeypatch):
    # Simulate the legacy selector ignoring metadata: freeze before adding that
    # already-existing evidence to its original forecast. All files are synthetic.
    doc = build(**sample)
    affected = sample["batch"]["predictions"][0]
    mark_prior(affected)
    name = "forecasts/2026-11-06/offline-t45/forecast.json"
    raw = raw_json(sample["batch"])
    sha = hashlib.sha256(raw).hexdigest()
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    doc.update(
        day="2026-11-06",
        path="parlay_sd.json",
        sha256="a" * 64,
        sources={"forecast": {"path": name, "sha256": sha}, "experimental": None},
        published_utc=doc["generated_utc"],
    )
    monkeypatch.setattr("zucai.digest_parlay.load_documents", lambda *a: [doc])
    manifest = {
        "files": {name: sha},
        "runs": {
            "offline-t45": {
                "forecasts": {
                    "2026-11-06": {
                        "batch_id": doc["batch_id"],
                        "generated_utc": doc["generated_utc"],
                    }
                }
            }
        },
    }
    return doc, manifest, affected, path


def test_old_frozen_groups_are_only_filtered_in_view_not_reselected_or_rewritten(
    sample, tmp_path, monkeypatch
):
    doc, manifest, row, path = install_legacy_view(sample, tmp_path, monkeypatch)
    original = copy.deepcopy(doc)
    before = path.read_bytes()
    install_ledger(tmp_path, doc)
    ledger_path = tmp_path / "scoreboard/parlay_sd_ledger.jsonl"
    ledger_before = ledger_path.read_bytes()
    summary = json.loads((tmp_path / "scoreboard/summary.json").read_text())
    lines, pending, _ = section(tmp_path, manifest, summary, BEFORE, None, matches={})
    text = "\n".join(lines)
    assert "默认实力" in text
    assert "weakest_quartile_half_shrinkage" in text.replace("\\_", "_")
    assert row["away"] in text
    assert "本版合格主候选 2 组" in text
    assert f"### {row['home']} VS {row['away']} ×" not in text
    assert doc == original and path.read_bytes() == before
    assert len(pending) == 3  # The historical third group remains in the old ledger.
    assert ledger_path.read_bytes() == ledger_before


def test_old_disclosure_requires_original_forecast_sha(sample, tmp_path, monkeypatch):
    _, manifest, _, path = install_legacy_view(sample, tmp_path, monkeypatch)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA256"):
        section(tmp_path, manifest, {}, BEFORE, None, matches={})


@pytest.mark.parametrize("wrong", ["batch", "identity"])
def test_original_source_must_match_frozen_group_identity(sample, tmp_path, monkeypatch, wrong):
    doc, manifest, _, path = install_legacy_view(sample, tmp_path, monkeypatch)
    source = json.loads(path.read_bytes())
    if wrong == "batch":
        source["batch_id"] = "different-batch"
    else:
        source["predictions"][0]["away"] = "另一支球队"
    raw = raw_json(source)
    path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    name = doc["sources"]["forecast"]["path"]
    doc["sources"]["forecast"]["sha256"] = sha
    manifest["files"][name] = sha
    with pytest.raises(ValueError, match="批次|身份"):
        section(tmp_path, manifest, {}, BEFORE, None, matches={})
