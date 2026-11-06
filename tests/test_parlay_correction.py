"""Owner-authorized correction; all inputs are synthetic frozen offline fixtures."""

import json
from pathlib import Path

import pytest

from test_parlay_single_double import generate
from test_parlay_single_double import sample as sample
from zucai.digest_parlay import _group_lines
from zucai.parlay.archive import raw_json
from zucai.parlay.paper import advance_ledger, render, summarize
from zucai.parlay.single_double import RULE, Config

FIX = Path(__file__).parents[1] / "data/fixtures/t45-correction"


def test_small_gaps_and_unknown_injuries_are_descriptive_not_default_veto(sample):
    for row in sample["batch"]["predictions"]:
        row["model_probabilities"] = dict(h=0.38, d=0.33, a=0.29)
    sample["injuries"] = {}
    doc = generate(sample)
    assert len(doc["groups"]) == 3
    assert doc["rule"] != "single_plus_double_v1"
    assert doc["rule_effective_utc"] == doc["generated_utc"]
    assert doc["config"]["threshold_filters_enabled"] is False
    for row in doc["boards"][0]["matches"]:
        assert row["single"]["eligible"] and row["double"]["eligible"]
        assert row["single"]["top_probability"] == 0.38
        assert row["single"]["second_probability"] == 0.33
        assert row["single"]["direction_gap"] == pytest.approx(0.05)
        assert row["single"]["top_below_half"] is True
        assert row["double"]["selected_min_minus_omitted"] == pytest.approx(0.04)
        assert row["injuries"]["home"]["status"] == "unknown"
        assert row["injuries"]["home"]["confirmed_injured_out"] is None
    assert all(g["injury_coverage"] == "missing" and not g["bet_allowed"] for g in doc["groups"])
    text = "\n".join(_group_lines(doc["groups"][0]))
    assert "未知" in text and "另外两项合计更可能" in text
    assert "方向脆弱" not in text and "关键反证未解决" not in text
    impact = doc["legacy_filter_diagnostic"]
    assert impact["affects_selection"] is False
    assert len(impact["matches"]) == 6
    assert len(impact["restored_groups"]) == 30
    assert sum(r["affected_matches"] for r in impact["leagues"].values()) == 6
    altered = generate(sample, config=Config(single_direction_gap=0.9, double_fragile_gap=0.9))
    assert altered["groups"] == doc["groups"]


def test_exact_top_tie_is_not_resolved_by_outcome_order(sample):
    sample["batch"]["predictions"][0]["model_probabilities"] = dict(h=0.45, d=0.1, a=0.45)
    doc = generate(sample)
    row = doc["boards"][0]["matches"][0]
    assert row["single"]["selection"] is None and row["original_direction"] is None
    assert row["single"]["top_outcomes"] == ["h", "a"]
    assert row["single"]["reasons"] == ["single_top_tied"]
    assert all(g["single"]["match_id"] != row["match_id"] for g in doc["groups"])


def test_double_ties_have_explicit_stable_order_and_no_reliability_claim(sample):
    sample["batch"]["predictions"][0]["model_probabilities"] = dict(h=0.4, d=0.3, a=0.3)
    row = generate(sample)["boards"][0]["matches"][0]
    assert row["double"]["selections"] == ["h", "d"]
    assert row["double"]["numerical_tie"] is True
    assert row["double"]["tied_selections"] == [["h", "d"], ["h", "a"]]
    assert "fragile" not in row["double"]


def test_injury_four_states_and_only_evidenced_invalidation_vetoes(sample):
    injury = sample["injuries"]["jc:t45-1"]
    injury["away"]["confirmed_injured_out"] = 2
    row = generate(sample)["boards"][0]["matches"][0]
    assert row["injuries"]["home"]["status"] == "confirmed_none"
    assert row["injuries"]["away"]["status"] == "confirmed_absences"
    assert row["single"]["eligible"]
    injury["research_invalidation"] = dict(
        reason="fixture 中研究依赖的门将已确认不能出场",
        source="fixture:offline-evidence",
        observed_utc="2026-11-06T01:00:00Z",
    )
    doc = generate(sample)
    row = doc["boards"][0]["matches"][0]
    assert row["injuries"]["status"] == "research_invalidated"
    assert "research_invalidated" in row["eligibility"]["reasons"]
    assert all("jc:t45-1" not in g["match_ids"] for g in doc["groups"])
    injury["research_invalidation"]["observed_utc"] = "2026-11-06T02:01:00Z"
    assert generate(sample)["boards"][0]["matches"][0]["single"]["eligible"]


def test_legacy_ledger_stays_byte_identical_and_new_rule_cannot_reopen_same_batch(sample):
    old = json.loads((FIX / "legacy-v1.json").read_bytes())
    ledger = json.loads((FIX / "legacy-ledger.json").read_bytes())
    current = dict(
        generate(sample), path=old["path"], sha256="b" * 64, published_utc=old["published_utc"]
    )
    before = "2026-11-06T02:05:00Z"
    assert raw_json(advance_ledger([old, current], {}, ledger, before)) == raw_json(ledger)
    finished = json.loads((FIX.parent / "t45/results.json").read_bytes())
    settled = advance_ledger([current], finished, ledger, "2026-11-06T18:05:00Z")
    assert len(settled) == len(ledger)
    assert settled[2]["settlement"]["profit"] == -0.4
    for previous, after in zip(ledger, settled, strict=True):
        assert {k: v for k, v in previous.items() if k != "settlement"} == {
            k: v for k, v in after.items() if k != "settlement"
        }
    assert raw_json(advance_ledger([old], finished, settled, "2026-11-06T18:05:00Z")) == raw_json(
        settled
    )


def test_rule_versions_and_injury_coverage_have_separate_performance_rows(sample):
    ledger = json.loads((FIX / "legacy-ledger.json").read_bytes())
    sample["batch"]["batch_id"] = "offline-v2-new-batch"
    sample["injuries"] = {}
    current = dict(
        generate(sample), path="fixture:v2", sha256="b" * 64, published_utc="2026-11-06T02:02:00Z"
    )
    updated = advance_ledger([current], {}, ledger, "2026-11-06T02:05:00Z")
    summary = summarize(updated, {}, "2026-11-06T02:05:00Z")
    assert summary["rules"]["single_plus_double_v1"]["totals"]["groups"] == 1
    assert summary["rules"][RULE]["injury_coverage"]["missing"]["groups"] == 3
    assert summary["rules"][RULE]["injury_coverage"]["complete"]["groups"] == 0
    text = render(summary)
    assert RULE in text and "single_plus_double_v1" in text and "资料缺失" in text
    for row in updated[len(ledger) :]:
        if row["record_type"] != "batch":
            assert row["injury_coverage"] == "missing"
            assert row["rule_config"]["threshold_filters_enabled"] is False
    assert raw_json(advance_ledger([current], {}, updated, "2026-11-06T02:05:00Z")) == raw_json(
        updated
    )
