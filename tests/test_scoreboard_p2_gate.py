"""Frozen P2 flags gate new paper decisions without changing old tickets or scores."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from test_scoreboard_command import files
from test_scoreboard_command import scoreboard_archive as scoreboard_archive
from test_scoreboard_paper import DECISION, paper_samples, tickets
from test_scoreboard_score import NOW
from zucai.scoreboard.command import _jsonl, run
from zucai.scoreboard.paper import account_statistics, advance_ledger, cash_at

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t28"
CASES = json.loads((FIXTURES / "p2-gate.json").read_bytes())


def forecasts_with_flags(*fields):
    forecasts, results = paper_samples()
    forecasts = forecasts[: len(fields)]
    for row, values in zip(forecasts, fields, strict=True):
        row.pop("p2_ready", None)
        row.update(values)
    return forecasts, results


@pytest.mark.parametrize("case", CASES["not_ready"], ids=lambda case: case["name"])
def test_only_frozen_true_can_enter_any_account(case, monkeypatch, tmp_path):
    # Both rows have a positive edge above all three thresholds and the same league/model.
    # A missing current config must not override either frozen flag.
    monkeypatch.setenv("ZUCAI_PARAMS", str(tmp_path / "no-current-params.json"))
    forecasts, results = forecasts_with_flags(CASES["ready"], case["fields"])
    before = deepcopy(forecasts)
    ledger = advance_ledger(forecasts, results, [], DECISION)
    assert forecasts == before
    assert {leg["match_id"] for row in ledger for leg in row.get("legs", [])} == {"jc:one"}
    for threshold in ("1.00", "1.05", "1.10"):
        for account in ("A", "B", "C"):
            assert len(tickets(ledger, account, threshold)) == 1
        assert not tickets(ledger, "D", threshold)
        assert not tickets(ledger, "E", threshold)
    skipped = [row for row in ledger if row.get("reason") == "model_not_p2_ready"]
    assert len(skipped) == 9
    assert {(r["account"], r["threshold"], r["scope"]) for r in skipped} == {
        (account, threshold, "jc:two")
        for account in ("A", "B", "C")
        for threshold in ("1.00", "1.05", "1.10")
    }
    assert _jsonl(advance_ledger(forecasts, results, ledger, DECISION)) == _jsonl(ledger)


def test_experimental_only_batch_has_no_favourite_or_parlay():
    experimental = CASES["not_ready"][0]["fields"]
    forecasts, results = forecasts_with_flags(experimental, experimental, experimental)
    ledger = advance_ledger(forecasts, results, [], DECISION)
    assert not any(row["record_type"] == "ticket" for row in ledger)
    assert sum(row.get("reason") == "model_not_p2_ready" for row in ledger) == 27
    assert {
        (row["account"], row["threshold"], row["scope"])
        for row in ledger
        if row.get("reason") == "insufficient_candidates"
    } == {
        (account, threshold, "2026-11-01")
        for account in ("D", "E")
        for threshold in ("1.00", "1.05", "1.10")
    }


def test_existing_experimental_tickets_keep_decisions_settle_and_keep_cash_history():
    raw = (FIXTURES / "legacy-experimental-ledger.jsonl").read_bytes()
    existing = [json.loads(line) for line in raw.splitlines()]
    original = deepcopy(existing)
    forecasts, results = forecasts_with_flags(CASES["not_ready"][0]["fields"])
    pending = advance_ledger(forecasts, results, existing, DECISION)
    assert _jsonl(pending) == raw
    later = {**deepcopy(forecasts[0]), **CASES["next_batch"]}
    settled = advance_ledger([*forecasts, later], results, pending, NOW)
    assert existing == original
    assert len([r for r in settled if r["record_type"] == "ticket"]) == 9
    assert sum(r.get("reason") == "model_not_p2_ready" for r in settled) == 9
    for before, after in zip(existing, settled[: len(existing)], strict=True):
        assert {k: v for k, v in before.items() if k != "settlement"} == {
            k: v for k, v in after.items() if k != "settlement"
        }
    stats = account_statistics(settled, NOW)
    for threshold in ("1.00", "1.05", "1.10"):
        for account, stake, returned, funds in (
            ("A", 100, 250, 10150),
            ("B", 200, 500, 10300),
            ("C", 100, 250, 10150),
        ):
            ticket = tickets(settled, account, threshold)[0]
            assert ticket["stake"] == stake
            assert ticket["settlement"]["return"] == returned
            assert stats[threshold][account]["current_funds"] == funds
            assert stats[threshold][account]["available_cash"] == funds
            assert cash_at(settled, account, threshold, DECISION) == 10000 - stake
        for account in ("D", "E"):
            assert stats[threshold][account]["current_funds"] == 10000
    assert _jsonl(advance_ledger([*forecasts, later], results, settled, NOW)) == _jsonl(settled)


@pytest.mark.parametrize("scoreboard_archive", [CASES["archive_flags"]], indirect=True)
def test_archive_keeps_experimental_scores_records_skips_and_repeats_identically(
    scoreboard_archive,
):
    archive = scoreboard_archive
    inputs = files(archive)
    summary = run(archive, as_of=NOW)
    assert summary["probability"]["total"]["n"] == 4
    # Eight rows in each of two prematch batches, A/B/C across three thresholds.
    assert summary["audit"]["paper_skips"]["model_not_p2_ready"] == 144
    board = archive / "scoreboard"
    scored = (board / "scored.jsonl").read_bytes()
    assert any(json.loads(row)["match_id"] == "jc:two" for row in scored.splitlines())
    raw = (board / "ledger.jsonl").read_bytes()
    assert not any(json.loads(row)["record_type"] == "ticket" for row in raw.splitlines())
    assert "模型为 P2 实验，不下注：144 条记录。" in (board / "scoreboard.md").read_text()
    repeated = run(archive, as_of=NOW)
    assert (board / "ledger.jsonl").read_bytes() == raw
    assert (board / "scored.jsonl").read_bytes() == scored
    assert repeated["accounts"] == summary["accounts"]
    assert all((archive / name).read_bytes() == content for name, content in inputs.items())
