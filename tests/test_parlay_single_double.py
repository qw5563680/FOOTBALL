"""Owner's T45 examples and boundary rules, using synthetic archived fixtures only."""

import copy
import json
from pathlib import Path

import pytest

from zucai.parlay.single_double import Config, build, cashflow, double_options

FIX = Path(__file__).parents[1] / "data/fixtures/t45"


@pytest.fixture
def sample():
    return json.loads((FIX / "input.json").read_bytes())


def generate(sample, **kwargs):
    return build(**sample, **kwargs)


def test_owner_fixed_example_distinguishes_coverage_profit_and_hit_but_loss():
    result = cashflow(**json.loads((FIX / "cashflow.json").read_bytes()))
    assert [b["probability"] for b in result["branches"]] == pytest.approx([0.30, 0.18, 0.52])
    assert [b["return"] for b in result["branches"]] == [3.6, 8.0, 0]
    assert [b["profit"] for b in result["branches"]] == [-0.4, 4.0, -4.0]
    assert result["branches"][0]["profit_status"] == "loss"
    assert result["coverage_probability"] == pytest.approx(0.48)
    assert result["profit_probability"] == pytest.approx(0.18)
    assert result["expected_profit"] == pytest.approx(-1.48)
    assert result["roi"] == pytest.approx(-0.37)


def test_three_groups_use_six_games_and_twelve_distinct_ids(sample):
    doc = generate(sample)
    assert len(doc["groups"]) == 3
    assert len({mid for g in doc["groups"] for mid in g["match_ids"]}) == 6
    assert len({tid for g in doc["groups"] for tid in g["team_ids"]}) == 12
    assert doc["boards"][0]["scope"]["enumerated"] == 9 * 6 * 5
    assert all(g["p2_label"] == "P2 实验" for g in doc["groups"])
    assert all(g["execution_status"] == "未核实" for g in doc["groups"])


@pytest.mark.parametrize("price,eligible", [(1.19, False), (1.20, True)])
def test_single_price_lower_bound_does_not_promote_another_outcome(sample, price, eligible):
    sample["batch"]["predictions"][0]["market"]["prices"]["h"] = price
    doc = generate(sample)
    single = doc["boards"][0]["matches"][0]["single"]
    assert single["selection"] == "h"
    assert single["eligible"] is eligible
    if eligible:
        assert single["price_status"] == "继续审核"


def test_double_compares_all_three_and_never_applies_single_price_floor(sample):
    row = sample["batch"]["predictions"][1]
    row["model_probabilities"] = dict(h=0.45, d=0.20, a=0.35)
    row["market"]["prices"]["a"] = 1.1
    doc = generate(sample)
    match = doc["boards"][0]["matches"][1]
    assert len(match["double_options"]) == 3
    assert match["double"]["selections"] == ["h", "a"]
    assert match["double"]["omitted"] == "d"
    assert match["double"]["omitted_probability"] == 0.2
    assert match["double"]["eligible"]


@pytest.mark.parametrize("missing", [None, "injuries", "probabilities"])
def test_missing_probabilities_or_injuries_never_become_zero_or_baseline(sample, missing):
    if missing == "injuries":
        sample["injuries"] = {}
    elif missing == "probabilities":
        for row in sample["batch"]["predictions"]:
            row["model_probabilities"] = None
    else:
        for row in sample["batch"]["predictions"]:
            row["model_probabilities"] = dict(h=0.4, d=0.35, a=0.25)
    doc = generate(sample)
    assert doc["groups"] == []
    assert doc["diagnostics"]
    if missing == "probabilities":
        assert doc["market_reference"]["groups"]


def test_disagreement_is_diagnostic_and_experimental_boards_are_separate(sample):
    row = copy.deepcopy(sample["batch"]["predictions"][0])
    row.update(model_name="dc-sot-v1", model_probabilities=dict(h=0.1, d=0.2, a=0.7))
    doc = generate(
        sample,
        experimental=dict(
            batch_id="offline-t45",
            predictions=[row],
            generated_utc=sample["frozen_utc"],
            as_of_utc=sample["batch"]["as_of_utc"],
        ),
    )
    match = doc["boards"][0]["matches"][0]
    assert not match["single"]["eligible"]
    assert "independent_direction_disagrees" in match["single"]["reasons"]
    assert doc["experimental_boards"][0]["model_name"] == "dc-sot-v1"


def test_same_team_as_opponent_or_another_name_is_still_occupied(sample):
    mid = "jc:t45-3"
    sample["identities"][mid]["away_team_id"] = 1
    sample["identities"][mid]["away"] = "国米"
    sample["batch"]["predictions"][2]["away"] = "国米"
    doc = generate(sample)
    assert len(doc["groups"]) == 2
    ids = [tid for group in doc["groups"] for tid in group["team_ids"]]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("count,groups", [(0, 0), (1, 0), (2, 1), (4, 2)])
def test_fewer_qualified_matches_mean_fewer_groups(sample, count, groups):
    sample["batch"]["predictions"] = sample["batch"]["predictions"][:count]
    assert len(generate(sample)["groups"]) == groups


def test_double_fragility_boundary_is_inclusive():
    assert max(
        double_options(dict(h=0.5, d=0.275, a=0.225)), key=lambda d: d["coverage_probability"]
    )["fragile"]
    assert not max(
        double_options(dict(h=0.5, d=0.276, a=0.224)), key=lambda d: d["coverage_probability"]
    )["fragile"]


@pytest.mark.parametrize("maximum", [0, 6, True])
def test_invalid_configuration_rejected(maximum):
    with pytest.raises(ValueError):
        Config(max_groups=maximum)


def test_sort_uses_full_precision_stable_ids_and_lists_profit_leader(sample):
    doc = generate(sample)
    assert doc["groups"][0]["match_ids"] == ["jc:t45-1", "jc:t45-3"]
    assert doc["boards"][0]["highest_profit"]["match_ids"] == ["jc:t45-2", "jc:t45-4"]
    sample["batch"]["predictions"][2]["model_probabilities"] = dict(
        h=0.6000000001, d=0.2499999999, a=0.15
    )
    doc = generate(sample)
    assert doc["groups"][0]["single"]["match_id"] == "jc:t45-3"
    assert doc["groups"][0]["coverage_probability"] > 0.51
    sample["batch"]["predictions"].reverse()
    assert generate(sample) == doc


def test_no_fifty_percent_gate_and_configurable_direction_gaps(sample):
    for row in sample["batch"]["predictions"]:
        row["model_probabilities"] = dict(h=0.49, d=0.31, a=0.20)
    assert len(generate(sample)["groups"]) == 3
    assert generate(sample, config=Config(single_direction_gap=0.19))["groups"] == []
    assert generate(sample, config=Config(double_fragile_gap=0.12))["groups"] == []


@pytest.mark.parametrize("problem", ["started", "finished", "canceled", "stopped", "identity"])
def test_ineligible_match_never_appears_in_current_model_or_market_groups(sample, problem):
    mid = "jc:t45-1"
    if problem == "started":
        sample["frozen_utc"] = "2026-11-06T15:00:00+00:00"
    elif problem in {"finished", "canceled"}:
        sample["identities"][mid]["status"] = problem
    elif problem == "stopped":
        sample["catalog"][0]["sellStatus"] = 0
    else:
        sample["identities"][mid]["away_team_id"] = sample["identities"][mid]["home_team_id"]
    doc = generate(sample)
    groups = doc["groups"] + doc["market_reference"]["groups"]
    assert all(mid not in g["match_ids"] for g in groups)


def test_single_only_unavailable_does_not_block_parlay_and_unknown_is_reference(sample):
    # Synthetic catalog has single=0, allUp=1 for every game.
    assert len(generate(sample)["groups"]) == 3
    sample["catalog"] = []
    doc = generate(sample)
    assert not doc["groups"]
    assert len(doc["boards"][0]["reference_groups"]) == 3
    assert doc["boards"][0]["scope"]["reference_only"] > 0


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        {"h": 0.6, "d": 0.4},
        {"h": 0.7, "d": 0.2, "a": 0.2},
        {"h": True, "d": 0, "a": 0},
        {"h": float("nan"), "d": 0.2, "a": 0.2},
    ],
)
def test_missing_or_invalid_team_probability_stays_blank_with_market_only(sample, invalid):
    for row in sample["batch"]["predictions"]:
        row["model_probabilities"] = invalid
    doc = generate(sample)
    assert doc["boards"] == [] and doc["groups"] == []
    assert doc["market_reference"]["groups"]
    assert all(r["model_probabilities"] is None for r in doc["market_reference"]["matches"])


@pytest.mark.parametrize("problem", ["foreign", "handicap", "future", "partial", "hybrid"])
def test_price_or_mixed_model_cannot_impersonate_team_model(sample, problem):
    for row in sample["batch"]["predictions"]:
        if problem == "foreign":
            row["market"]["source"] = "overseas"
        elif problem == "handicap":
            row["market"]["pool"] = "hhad"
        elif problem == "future":
            row["market"]["snapshot_utc"] = "2026-11-06T03:00:00+00:00"
        elif problem == "partial":
            del row["market"]["prices"]["d"]
        else:
            sample["batch"]["input"]["market_weight"] = 0.1
    assert not generate(sample)["groups"]


@pytest.mark.parametrize(
    "capture,usable",
    [
        ("2026-11-06T01:59:59Z", True),
        ("2026-11-06T02:00:00Z", False),
        ("2026-11-06T02:00:01Z", False),
    ],
)
def test_injury_missing_and_capture_cutoff_are_not_invented_zero(sample, capture, usable):
    for injury in sample["injuries"].values():
        for cell in injury.values():
            cell["captured_utc"] = capture
    assert bool(generate(sample)["groups"]) is usable
    sample["injuries"]["jc:t45-1"]["home"]["confirmed_injured_out"] = None
    first = generate(sample)["boards"][0]["matches"][0]
    assert not first["single"]["eligible"]
    assert first["injuries"]["home"]["confirmed_injured_out"] is None


def test_independent_wrong_batch_or_identity_is_not_used(sample):
    row = copy.deepcopy(sample["batch"]["predictions"][0])
    row.update(model_name="dc-sot-v1", model_probabilities=dict(h=0.1, d=0.2, a=0.7))
    doc = dict(
        batch_id="other",
        generated_utc=sample["frozen_utc"],
        as_of_utc=sample["batch"]["as_of_utc"],
        predictions=[row],
    )
    assert generate(sample, experimental=doc)["groups"]
    doc["batch_id"] = sample["batch"]["batch_id"]
    row["home"] = "不同球队"
    assert not generate(sample, experimental=doc)["experimental_boards"]


def test_evidence_tiers_never_mix_legs_and_share_team_occupancy(sample):
    for row in sample["batch"]["predictions"][:2]:
        row.update(p2_ready=True, p2_label="P2 可用")
    doc = generate(sample)
    assert len(doc["boards"]) == 2 and len(doc["groups"]) == 3
    for group in doc["groups"]:
        assert group["single"]["p2_ready"] == group["double"]["p2_ready"]
    assert len({tid for g in doc["groups"] for tid in g["team_ids"]}) == 12


def test_rounding_is_explicit_and_breakeven_is_not_profit():
    doc = cashflow(0.6, [0.5, 0.3], 1.25, [1.6, 3.203], config=Config())
    assert doc["branches"][0]["profit_status"] == "breakeven"
    assert doc["breakeven_probability"] == 0.3
    assert doc["profit_probability"] == 0.18
    assert doc["branches"][1]["return"] == 8.00
    other = cashflow(0.6, [0.5, 0.3], 1.25, [1.6, 3.203], config=Config(rounding="ROUND_HALF_UP"))
    assert other["branches"][1]["return"] == 8.01
    assert other["rounding_status"] == "未核实"


def test_maximum_five_groups_and_all_four_teams_occupied(sample):
    original = copy.deepcopy(sample)
    for index in range(6, 10):
        base = original["batch"]["predictions"][index % 2]
        row = copy.deepcopy(base)
        row.update(
            match_id=f"jc:t45-{index + 1}", home=f"球队{2 * index + 1}", away=f"球队{2 * index + 2}"
        )
        sample["batch"]["predictions"].append(row)
        sample["identities"][row["match_id"]] = {
            **copy.deepcopy(original["identities"][base["match_id"]]),
            **{k: row[k] for k in ("match_id", "home", "away")},
            "home_team_id": 2 * index + 1,
            "away_team_id": 2 * index + 2,
        }
        sample["injuries"][row["match_id"]] = copy.deepcopy(original["injuries"][base["match_id"]])
        catalog = copy.deepcopy(original["catalog"][index % 2])
        catalog.update({k: row[k] for k in ("match_id", "home", "away")})
        sample["catalog"].append(catalog)
    doc = generate(sample, config=Config(max_groups=5))
    assert len(doc["groups"]) == 5
    assert len({t for g in doc["groups"] for t in g["team_ids"]}) == 20
    assert doc["boards"][0]["scope"]["enumerated"] == 9 * 10 * 9


def test_missing_and_tied_independent_model_do_not_promote_other_single(sample):
    row = copy.deepcopy(sample["batch"]["predictions"][0])
    row.update(model_name="dc-sot-v1", model_probabilities=dict(h=0.45, d=0.1, a=0.45))
    extra = dict(
        batch_id=sample["batch"]["batch_id"],
        predictions=[row],
        as_of_utc=sample["batch"]["as_of_utc"],
        generated_utc=sample["frozen_utc"],
    )
    assert not generate(sample, experimental=extra)["boards"][0]["matches"][0]["single"]["eligible"]
    row.update(status="missing", model_probabilities=None)
    first = generate(sample, experimental=extra)["boards"][0]["matches"][0]
    assert first["single"]["eligible"]  # Task says agreement only when an independent value exists.
    assert first["independent_models_available"] == 0
