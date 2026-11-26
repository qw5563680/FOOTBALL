"""Synthetic T72 examples and production-entry-point isolation checks."""

import json
from copy import deepcopy

import pytest

from test_cloud_predict import NOW
from test_cloud_predict import scheduled_archive as scheduled_archive
from zucai import market_blend
from zucai.report.batch import MARKET_OPERATOR, MARKET_REFERENCE_VERSION, MARKET_SUPPLIER

CUTOFF = "2026-11-02T18:00:00+00:00"
TEAM = {"h": 0.6, "d": 0.3, "a": 0.1}
MARKET = {"h": 0.1, "d": 0.3, "a": 0.6}


def _protocol(path, **overrides):
    version = {
        "schema_version": 1,
        "model_name": "team-market-blend-v1",
        "weight_version": "v1",
        "market_weight": 0.5,
        "effective_from_utc": CUTOFF,
        "retrospective": {
            "protocol_sha256": "a" * 64,
            "archive_commit": "b" * 40,
            "report_sha256": "c" * 64,
            "training_end_utc": "2022-01-01T00:00:00+00:00",
            "validation_end_utc": "2023-01-01T00:00:00+00:00",
            "holdout_end_utc": "2024-01-01T00:00:00+00:00",
        },
        **overrides,
    }
    path.write_text(json.dumps(version), encoding="utf-8")
    return path


@pytest.fixture
def batch():
    return {
        "mode": "prematch",
        "as_of_utc": CUTOFF,
        "input": {"market_weight": 0},
        "predictions": [
            {
                "match_id": "jc:314",
                "source": "jc",
                "status": "predicted",
                "model_name": "dixon-coles",
                "kickoff_utc": "2026-11-03T12:00:00+00:00",
                "model_probabilities": TEAM,
                "market": {
                    "method_version": MARKET_REFERENCE_VERSION,
                    "operator": MARKET_OPERATOR,
                    "supplier": MARKET_SUPPLIER,
                    "type": "P3",
                    "source": "stored_had_snapshot",
                    "execution_verified": False,
                    "snapshot_utc": CUTOFF,
                    "collected_utc": CUTOFF,
                    "snapshot_id": CUTOFF,
                    "prices": {"h": 10.0, "d": 10 / 3, "a": 10 / 6},
                    "probabilities": MARKET,
                },
                "experimental_blend": None,
            }
        ],
    }


def test_log_linear_endpoints_and_stable_normalization():
    assert market_blend.blend_probabilities(TEAM, MARKET, 0) == TEAM
    assert market_blend.blend_probabilities(TEAM, MARKET, 1) == MARKET
    actual = market_blend.blend_probabilities(TEAM, MARKET, 0.5)
    assert actual == pytest.approx(
        {
            "h": (0.06**0.5) / (2 * (0.06**0.5) + 0.3),
            "d": 0.3 / (2 * (0.06**0.5) + 0.3),
            "a": (0.06**0.5) / (2 * (0.06**0.5) + 0.3),
        }
    )
    assert sum(actual.values()) == pytest.approx(1)
    assert market_blend.blend_probabilities({"h": 0, "d": 1, "a": 0}, MARKET, 0.5) == {
        "h": 0,
        "d": 1,
        "a": 0,
    }


def test_approved_initial_weight_freezes_only_from_recorded_public_approval(batch):
    before = "2026-11-26T14:32:09+00:00"
    effective = "2026-11-26T14:32:10+00:00"
    row = batch["predictions"][0]
    row["kickoff_utc"] = "2026-11-26T16:00:00+00:00"
    row["market"].update(snapshot_utc=before, snapshot_id=before, collected_utc=before)
    batch["as_of_utc"] = before
    market_blend.freeze_blends(batch)
    assert row["experimental_blend"]["reason"] == "该版融合权重尚未生效"

    batch["as_of_utc"] = effective
    market_blend.freeze_blends(batch)
    blend = row["experimental_blend"]
    assert blend["market_weight"] == 1.0
    assert blend["probabilities"] == row["market"]["probabilities"]
    assert row["model_probabilities"] == TEAM
    assert blend["paper_eligible"] is False
    market_blend.validate_frozen_blend(row, effective)


def test_clock_and_prices_are_verified_from_exact_t71_reference(batch, tmp_path):
    path = _protocol(tmp_path / "weight.json")
    row = batch["predictions"][0]
    market_blend.freeze_blends(batch, protocol_path=path)
    blend = row["experimental_blend"]
    assert blend["status"] == "generated"
    assert (
        blend["as_of_utc"] == blend["market_collected_utc"] == blend["market_snapshot_id"] == CUTOFF
    )
    assert blend["paper_eligible"] is False and blend["market_dependent"] is True
    assert blend["model_version"] == "v1"
    market_blend.validate_frozen_blend(row, CUTOFF)
    row["market"]["probabilities"] = TEAM
    with pytest.raises(ValueError, match="市场快照"):
        market_blend.validate_frozen_blend(row, CUTOFF)


def test_future_capture_excluded_at_the_exact_prediction_cutoff(batch, tmp_path):
    future = "2026-11-02T18:00:00.000001+00:00"
    batch["predictions"][0]["market"].update(
        snapshot_utc=future, snapshot_id=future, collected_utc=future
    )
    market_blend.freeze_blends(batch, protocol_path=_protocol(tmp_path / "weight.json"))
    blend = batch["predictions"][0]["experimental_blend"]
    assert blend["status"] == "not_generated"
    assert "probabilities" not in blend


def test_publisher_closing_quote_cannot_fill_prospective_market(batch, tmp_path):
    batch["predictions"][0]["market"].update(source="publisher_closing", collected_utc=CUTOFF)
    market_blend.freeze_blends(batch, protocol_path=_protocol(tmp_path / "weight.json"))
    assert batch["predictions"][0]["experimental_blend"]["status"] == "not_generated"


def test_target_results_are_never_read_for_fusion(batch, tmp_path):
    changed = deepcopy(batch)
    changed["predictions"][0].update(actual="a", home_goals=1, away_goals=2, result={"actual": "a"})
    path = _protocol(tmp_path / "weight.json")
    for document in (batch, changed):
        market_blend.freeze_blends(document, protocol_path=path)
    assert (
        changed["predictions"][0]["experimental_blend"]
        == batch["predictions"][0]["experimental_blend"]
    )


def test_excluded_team_and_missing_market_remain_ungenerated(batch, tmp_path):
    source = batch["predictions"][0]
    excluded = {
        "match_id": "jc:315",
        "source": "jc",
        "status": "excluded",
        "reason": "没有球队模型",
        "market_reference": source["market"],
        "baseline_probabilities": TEAM,
    }
    no_market = dict(source, match_id="jc:316", market=None, market_missing_reason="报价不完整")
    batch["predictions"] = [excluded, no_market]
    market_blend.freeze_blends(batch, protocol_path=_protocol(tmp_path / "weight.json"))
    assert excluded["experimental_blend"]["reason"] == "球队模型未生成"
    assert no_market["experimental_blend"]["reason"] == "报价不完整"
    for row in batch["predictions"]:
        market_blend.validate_frozen_blend(row, CUTOFF)
        assert row["experimental_blend"]["status"] == "not_generated"
        assert "probabilities" not in row["experimental_blend"]


@pytest.mark.parametrize("error", ["missing", "invalid", "not_effective"])
def test_no_unregistered_or_future_weight_is_used(batch, tmp_path, error):
    path = tmp_path / "weight.json"
    if error == "invalid":
        _protocol(path, market_weight=True)
    elif error == "not_effective":
        _protocol(path, effective_from_utc="2026-11-02T18:00:01+00:00")
    market_blend.freeze_blends(batch, protocol_path=path)
    blend = batch["predictions"][0]["experimental_blend"]
    assert blend["status"] == "not_generated" and "probabilities" not in blend


def test_reviewed_revision_starts_at_its_cutoff_and_keeps_prior_weight(tmp_path):
    path = _protocol(tmp_path / "weight.json")
    first = json.loads(path.read_bytes())
    initial, _ = market_blend.prepare_blends(CUTOFF, protocol_path=path)
    revision_cutoff = "2026-12-01T18:00:00+00:00"
    proof = {
        "checkpoint": 200,
        "as_of_utc": "2026-12-01T17:00:00+00:00",
        "score_sha256": "d" * 64,
        "report_sha256": "e" * 64,
        "previous_weight_version": "v1",
    }
    second = {
        **first,
        "previous": first,
        "prospective": proof,
        "market_weight": 0.73,
        "weight_version": "v2",
        "effective_from_utc": revision_cutoff,
    }
    path.write_text(json.dumps(second), encoding="utf-8")
    earlier, _ = market_blend.prepare_blends(proof["as_of_utc"], protocol_path=path)
    new, _ = market_blend.prepare_blends(revision_cutoff, protocol_path=path)
    assert earlier == initial
    assert (new.version, new.value) == ("v2", 0.73)

    for bad_proof in (
        {**proof, "checkpoint": 400},
        {**proof, "as_of_utc": revision_cutoff},
        {**proof, "score_sha256": "missing"},
        {**proof, "previous_weight_version": "other"},
    ):
        path.write_text(json.dumps({**second, "prospective": bad_proof}), encoding="utf-8")
        unavailable, reason = market_blend.prepare_blends(revision_cutoff, protocol_path=path)
        assert unavailable is None
        assert reason == "融合权重协议不合法或无法读取"


def test_legacy_manual_and_sfc_only_rows_do_not_enter_fusion(batch, tmp_path):
    legacy = deepcopy(batch)
    legacy["input"]["market_weight"] = 0.5
    legacy["predictions"][0]["experimental_blend"] = {"model_name": "dixon-coles+market"}
    unchanged = deepcopy(legacy)
    batch["predictions"][0]["sfc_only"] = True
    path = _protocol(tmp_path / "weight.json")
    for document in (batch, legacy):
        market_blend.freeze_blends(document, protocol_path=path)
    assert batch["predictions"][0]["experimental_blend"] is None
    assert unchanged == legacy


def test_unexpected_calculation_failure_stays_with_its_match(batch, tmp_path, monkeypatch):
    good = dict(batch["predictions"][0], match_id="jc:71")
    bad = batch["predictions"][0]
    batch["predictions"].append(good)
    real = market_blend.blend_probabilities
    calls = 0

    def unreliable(team, market, weight):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("calculation fault fixture")
        return real(team, market, weight)

    monkeypatch.setattr(market_blend, "blend_probabilities", unreliable)
    market_blend.freeze_blends(batch, protocol_path=_protocol(tmp_path / "weight.json"))
    assert bad["experimental_blend"]["status"] == "not_generated"
    assert good["experimental_blend"]["status"] == "generated"
    assert bad["model_probabilities"] == good["model_probabilities"] == TEAM


@pytest.mark.parametrize(
    ("stopped", "reason"),
    [(True, "融合实验达到预定关停门槛"), (False, "融合关停记录无法核实")],
)
def test_prior_stop_gates_only_the_experiment(batch, tmp_path, monkeypatch, stopped, reason):
    from zucai.scoreboard import team_market_blend

    def gate(archive, cutoff, version):
        assert (archive, cutoff, version) == (tmp_path, CUTOFF, "v1")
        if not stopped:
            raise ValueError("corrupt score fixture")
        return True

    monkeypatch.setattr(team_market_blend, "read_stop", gate)
    market_blend.freeze_blends(
        batch, archive=tmp_path, protocol_path=_protocol(tmp_path / "weight.json")
    )
    row = batch["predictions"][0]
    assert row["experimental_blend"]["reason"] == reason
    assert row["model_probabilities"] == TEAM


def test_production_replay_changes_only_optional_fusion(scheduled_archive, tmp_path, monkeypatch):
    from zucai import cloud_predict
    from zucai.report.render import load_batch

    archive, params = scheduled_archive
    path = tmp_path / "weight.json"
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    monkeypatch.setattr(market_blend, "PROTOCOL_PATH", path)
    lookups = []
    read_weight = market_blend._read_weight

    def observed_read(path):
        lookups.append(path)
        return read_weight(path)

    monkeypatch.setattr(market_blend, "_read_weight", observed_read)
    inactive = cloud_predict.run(archive, params, "blend-off", production_state=tmp_path / "off")
    _protocol(path)
    active = cloud_predict.run(archive, params, "blend-on", production_state=tmp_path / "on")
    assert lookups == [path, path]
    for league, entry in inactive["leagues"].items():
        after = active["leagues"][league]
        assert entry.get("training_rows") == after.get("training_rows")
        if entry["status"] == "trained":
            assert (archive / entry["model_path"]).read_bytes() == (
                archive / after["model_path"]
            ).read_bytes()
    for day, entry in inactive["forecasts"].items():
        before = load_batch(archive / entry["path"] / "forecast.json")
        after = load_batch(archive / active["forecasts"][day]["path"] / "forecast.json")
        for old, new in zip(before["predictions"], after["predictions"], strict=True):
            assert old.pop("experimental_blend")["status"] == "not_generated"
            experimental = new.pop("experimental_blend")
            assert old == new
            assert experimental["status"] == (
                "generated"
                if old["status"] == "predicted" and old.get("market")
                else "not_generated"
            )


def test_forecast_archive_reader_does_not_import_optional_model_engine():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['penaltyblog'] = None; "
            "from zucai.forecast_archive import verify_forecasts",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
