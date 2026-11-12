"""Default-strength disclosure is presentation-only; old score/ledger facts stay frozen."""

import hashlib
import json
from copy import deepcopy
from datetime import datetime

import pytest

from test_digest_rework import _batch
from test_scoreboard_command import files
from test_scoreboard_command import scoreboard_archive as scoreboard_archive
from test_scoreboard_score import NOW, samples
from zucai.digest_context import frozen_rows
from zucai.digest_views import forecast_sections, review_sections
from zucai.scoreboard.paper import advance_ledger
from zucai.scoreboard.score import score_forecasts, summarize_scores

METHOD = "weakest_quartile_half_shrinkage"


def _metadata(used=True):
    return {
        "unknown_team_prior": used,
        "home_unknown_team": False,
        "away_unknown_team": used,
        "prior_method": METHOD if used else None,
    }


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_daily_predictions_disclose_side_and_method_without_changing_probabilities():
    batch = _batch()
    row = batch["predictions"][0]
    row["metadata"] = _metadata()
    original = _bytes(batch)
    now = datetime.fromisoformat("2026-11-03T10:00:00+00:00")
    text = "\n".join(
        forecast_sections([("2026-11-03", "f/forecast.json", batch)], now.date(), None, now=now)
    )
    assert "客队" in text and row["away"] in text and METHOD in text.replace("\\_", "_")
    assert "默认实力" in text
    assert "48.00% / 28.00% / 24.00%" in text
    assert _bytes(batch) == original


def test_new_scored_rows_keep_prior_disclosure_and_old_rows_remain_identical():
    forecasts, results = samples()
    for row in forecasts:
        row["metadata"] = _metadata(row["match_id"] == "jc:one")
    old, _ = score_forecasts(forecasts, results, [], NOW)
    original = _bytes(old)
    one = next(r for r in old if r["match_id"] == "jc:one")
    assert one["prior_disclosure"]["used"] is True
    assert one["prior_disclosure"]["home"] is False
    assert one["prior_disclosure"]["away"] is True
    rescored, _ = score_forecasts(forecasts, results, old, NOW)
    assert _bytes(rescored) == original


def test_old_scored_rows_join_exact_frozen_metadata_for_strata_without_rewriting():
    forecasts, results = samples()
    old, audit = score_forecasts(forecasts, results, [], NOW)
    for row in old:
        row.pop("prior_disclosure", None)
    for row in forecasts:
        if row["match_id"] == "jc:one":
            row["metadata"] = _metadata()
        elif row["match_id"] == "jc:two":
            row["metadata"] = _metadata(False)
    original = _bytes((old, forecasts))
    base = summarize_scores(old, NOW, active_keys=audit["active_keys"])
    result = summarize_scores(old, NOW, active_keys=audit["active_keys"], forecast_rows=forecasts)
    assert result["prior_strata"]["default_strength"]["n"] == 1
    assert result["prior_strata"]["known_teams"]["n"] == 1
    assert result["prior_strata"]["unrecorded"]["n"] == 2
    assert result["total"] == base["total"]
    assert result["leagues"] == base["leagues"]
    assert _bytes((old, forecasts)) == original


def test_wrong_model_or_archive_hash_cannot_supply_old_score_prior_evidence():
    forecasts, results = samples()
    old, audit = score_forecasts(forecasts, results, [], NOW)
    for row in old:
        row.pop("prior_disclosure", None)
        row["forecast_path"] = "forecasts/frozen/forecast.json"
        row["forecast_sha256"] = "a" * 64
    for row in forecasts:
        row["metadata"] = _metadata()
        row["forecast_path"] = "forecasts/frozen/forecast.json"
        row["forecast_sha256"] = "b" * 64
    result = summarize_scores(old, NOW, active_keys=audit["active_keys"], forecast_rows=forecasts)
    assert result["prior_strata"]["unrecorded"]["n"] == 4
    for row in forecasts:
        row["forecast_sha256"] = "a" * 64
        row["model_version"] = "different-model"
    result = summarize_scores(old, NOW, active_keys=audit["active_keys"], forecast_rows=forecasts)
    assert result["prior_strata"]["unrecorded"]["n"] == 4


def test_weekly_review_strata_use_only_active_exact_batch_scores():
    batch = _batch()
    batch["predictions"] = batch["predictions"][:1]
    batch["predictions"][0]["metadata"] = _metadata()
    now = datetime.fromisoformat("2026-11-03T14:00:00+00:00")
    latest, observations = frozen_rows(
        [("2026-11-03", "f/forecast.json", batch)],
        {},
        now,
        forecast_hashes={"f/forecast.json": "a" * 64},
        publications={"f/forecast.json": {"published_utc": batch["generated_utc"]}},
    )
    row = next(iter(latest.values()))
    score = {
        **row,
        "record_id": "active",
        "available_utc": "2026-11-03T13:30:00+00:00",
        "home_goals": 1,
        "away_goals": 0,
        "actual": "h",
    }
    score.pop("metadata")  # Old scored rows retain path/SHA but not original model metadata.
    inactive = {**score, "record_id": "old", "batch_id": "obsolete", "actual": "a"}
    original = _bytes((latest, score, inactive))
    text = "\n".join(
        review_sections(
            latest,
            observations,
            {},
            [score, inactive],
            {"audit": {"active_keys": ["active"]}},
            None,
            None,
            now,
        )
    )
    assert "默认实力分层" in text
    assert "使用默认实力 | 1 | 1 | 0 |" in text
    assert "客队" in text and METHOD in text.replace("\\_", "_")
    assert _bytes((latest, score, inactive)) == original


def test_ranked_old_paper_accounts_exclude_prior_only_for_new_d_e_decisions():
    forecasts, results = samples()
    before = advance_ledger(forecasts, results, [], NOW)
    changed = deepcopy(forecasts)
    for row in changed:
        row["metadata"] = _metadata(row["match_id"] == "jc:assumed")
    after = advance_ledger(changed, results, [], NOW)

    def unaffected(ledger):
        return [r for r in ledger if r.get("account") in ("A", "B", "C")]

    assert _bytes(unaffected(before)) == _bytes(unaffected(after))
    assert all(
        leg["match_id"] != "jc:assumed"
        for ticket in after
        if ticket.get("account") in ("D", "E") and ticket["record_type"] == "ticket"
        for leg in ticket["legs"]
    )
    assert any(r.get("reason") == "unknown_team_prior" for r in after)
    frozen = _bytes(before)
    assert _bytes(advance_ledger(changed, results, before, NOW)) == frozen


@pytest.mark.parametrize("scoreboard_archive", [{"metadata": _metadata()}], indirect=True)
def test_scoreboard_command_stratifies_legacy_rows_without_rewriting_old_files(scoreboard_archive):
    from zucai.scoreboard.command import run

    archive = scoreboard_archive
    run(archive, as_of=NOW)
    path = archive / "scoreboard/scored.jsonl"
    old = [json.loads(line) for line in path.read_text().splitlines()]
    for row in old:
        row.pop("prior_disclosure")
    # Construct an explicitly synthetic pre-T53 append log with valid recorded hash.
    path.write_bytes(b"\n".join(_bytes(row) for row in old) + b"\n")
    summary_path = archive / "scoreboard/summary.json"
    summary = json.loads(summary_path.read_text())
    summary["files"]["scored.jsonl"] = hashlib.sha256(path.read_bytes()).hexdigest()
    summary_path.write_text(json.dumps(summary))
    before = files(archive)
    result = run(archive, as_of=NOW)
    assert result["probability"]["prior_strata"]["default_strength"]["n"] == 4
    rendered = (archive / "scoreboard/scoreboard.md").read_text()
    assert "默认实力分层" in rendered and "| 使用默认实力 | 4 |" in rendered
    for name, value in before.items():
        if name.startswith("forecasts/") or name in {
            "scoreboard/scored.jsonl",
            "scoreboard/ledger.jsonl",
        }:
            assert (archive / name).read_bytes() == value
