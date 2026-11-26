"""Independent T72 scoring against disposable and source-traceable forecast fixtures."""

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from test_parlay_archive import archive_setup as archive_setup
from test_scoreboard_command import scoreboard_archive as scoreboard_archive
from zucai.scoreboard import team_market_blend as blend_board


def records(n):
    start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    for number in range(n):
        kickoff = start + timedelta(days=number)
        yield {
            "record_id": f"result-{number:04d}",
            "match_id": f"jc:{number}",
            "kickoff_utc": kickoff.isoformat(),
            "available_utc": (kickoff + timedelta(hours=2)).isoformat(),
            "actual": "h",
            "team": {"h": 0.5, "d": 0.25, "a": 0.25},
            "market": {"h": 0.4, "d": 0.3, "a": 0.3},
            "fusion": {"h": 0.45, "d": 0.275, "a": 0.275},
            "model_version": "demo-1",
        }


def test_shared_pairing_and_checkpoints_can_be_recomputed_independently():
    data = list(records(300))
    summary = blend_board.comparison(data)
    assert summary["common_n"] == 300
    assert all(score["n"] == 300 for score in summary["common"].values())
    assert summary["common"]["team"]["log_loss"] == pytest.approx(-math.log(0.5))
    assert summary["common"]["market"]["brier"] == pytest.approx(0.6**2 + 0.3**2 * 2)
    assert summary["common"]["fusion"]["top_accuracy"] == 1
    assert summary["paired"]["team"]["log_loss"]["difference"] == pytest.approx(
        math.log(0.5 / 0.45)
    )
    assert summary["paired"]["market"]["log_loss"]["difference"] == pytest.approx(
        math.log(0.4 / 0.45)
    )
    assert blend_board.decision(199, summary["paired"])["team"] is None
    assert blend_board.decision(200, summary["paired"])["market"] == "融合比直接看市场有额外好处"
    assert blend_board.decision(299, summary["paired"])["stop_required"] is False
    assert blend_board.decision(300, summary["paired"])["stop_required"] is True
    assert blend_board.decision(300, summary["paired"])["team"] == "市场信息没有帮到球队模型"


def test_market_failure_alone_stops_after_300_even_when_fusion_improves_team():
    data = list(records(301))
    data = [
        {**row, "team": row["market"], "market": {"h": 0.8, "d": 0.1, "a": 0.1}} for row in data
    ]
    point = blend_board.checkpoint(data, None)
    assert point["decision"]["team"] == "市场信息帮到了球队模型"
    assert point["decision"]["stop_reasons"] == ["融合明显不如市场"]
    assert point["record_ids"] == [row["record_id"] for row in data[:300]]
    assert blend_board.checkpoint(data, point, active=[]) == point


@pytest.mark.parametrize("mixed_weights", [False, True])
def test_checkpoint_does_not_infer_market_advantage_from_market_only_predictions(mixed_weights):
    data = list(records(300))
    for i, row in enumerate(data):
        row.update(
            market_weight=0.5 if mixed_weights and i % 2 else 1.0,
            team={"h": 0.5, "d": 0.25, "a": 0.25},
            market={"h": 0.6, "d": 0.2, "a": 0.2},
        )
        row["fusion"] = (
            {"h": 0.55, "d": 0.225, "a": 0.225} if row["market_weight"] == 0.5 else row["market"]
        )
    point = blend_board.checkpoint(data, None)
    assert point["decision"]["team"] == "市场信息帮到了球队模型"
    assert point["decision"]["market"] == ("尚无证据" if mixed_weights else None)
    assert point["decision"]["stop_required"] is mixed_weights
    if not mixed_weights:
        assert point["paired"]["market"]["log_loss"]["difference"] == 0


def test_invalid_closing_prices_never_change_the_prematch_compared_population():
    data = list(records(4))
    data[0]["market"] = None
    data[0]["fusion"] = None
    data[1]["fusion"] = None
    plain = blend_board.comparison(data)
    assert plain["common_n"] == 2
    assert plain["closing"]["common_n"] == 0
    compared = blend_board.comparison(
        data,
        closing={data[-1]["match_id"]: {"h": 0.55, "d": 0.2, "a": 0.25}},
    )
    assert compared["closing"]["common_n"] == 1
    assert compared["closing"]["common"]["closing"]["log_loss"] == pytest.approx(-math.log(0.55))
    assert {k: v for k, v in compared.items() if k != "closing"} == {
        k: v for k, v in plain.items() if k != "closing"
    }


def test_stop_signal_validates_hashed_independent_score_evidence(tmp_path):
    from hashlib import sha256

    path = tmp_path / blend_board.SCORES
    path.parent.mkdir()
    assert blend_board.read_stop(tmp_path, "2026-11-26T01:00:00+00:00", "demo-1") is False
    data = list(records(300))
    raw = b"".join((json.dumps(row) + "\n").encode() for row in data)
    path.write_bytes(raw)
    when = "2026-11-26T00:00:00+00:00"
    state = {
        "generated_utc": when,
        "as_of_utc": when,
        "team_market_blend": {
            "scored_sha256": sha256(raw).hexdigest(),
            "checkpoint_300": blend_board.checkpoint(data, None),
        },
    }
    (path.parent / "summary.json").write_text(json.dumps(state))
    assert blend_board.read_stop(tmp_path, "2026-11-26T01:00:00+00:00", "demo-2") is True
    with pytest.raises(ValueError, match="截点"):
        blend_board.read_stop(tmp_path, "2026-11-25T01:00:00+00:00", "demo-2")
    path.write_bytes(raw + b"{}\n")
    with pytest.raises(ValueError, match="SHA256"):
        blend_board.read_stop(tmp_path, "2026-11-26T01:00:00+00:00", "demo-2")


def add_blend(archive, *, market_weight=0.5):
    """Write typed rows and synthetic receipts to an isolated forecast copy."""
    from hashlib import sha256
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from forecast_publication_support import confirm_fixture
    from zucai import market_blend
    from zucai.forecast_archive import REPORT_FILES
    from zucai.report.batch import MARKET_OPERATOR, MARKET_REFERENCE_VERSION, MARKET_SUPPLIER
    from zucai.report.render import write_report
    from zucai.scoreboard.score import instant

    manifest_path = archive / "forecast_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    for name in [path for path in manifest["files"] if path.endswith("/forecast.json")]:
        source = archive / name
        batch = json.loads(source.read_bytes())
        if batch["mode"] != "prematch":
            continue
        for row in batch["predictions"]:
            if market := row.get("market"):
                stamp = market["snapshot_utc"]
                market.update(
                    type="P3",
                    source="stored_had_snapshot",
                    execution_verified=False,
                    method_version=MARKET_REFERENCE_VERSION,
                    operator=MARKET_OPERATOR,
                    supplier=MARKET_SUPPLIER,
                    snapshot_id=stamp,
                    collected_utc=stamp,
                )
        weight = market_blend.Weight("demo-1", market_weight, instant(batch["as_of_utc"]), "c" * 64)
        market_blend.freeze_blends(batch, deployment=(weight, None))
        with TemporaryDirectory(dir=archive.parent) as staging:
            rendered = Path(staging) / "rendered"
            write_report(batch, rendered)
            for filename in REPORT_FILES:
                path = source.parent / filename
                data = (rendered / filename).read_bytes()
                path.write_bytes(data)
                manifest["files"][path.relative_to(archive).as_posix()] = sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    confirm_fixture(archive)


def test_board_discloses_effective_market_weight_even_before_first_match(
    scoreboard_archive, tmp_path, monkeypatch
):
    from test_scoreboard_score import NOW
    from test_t72_market_blend import _protocol
    from zucai import market_blend
    from zucai.scoreboard.command import run

    protocol = _protocol(
        tmp_path / "weight.json", market_weight=1.0, effective_from_utc=NOW.isoformat()
    )
    monkeypatch.setattr(market_blend, "PROTOCOL_PATH", protocol)
    report = scoreboard_archive / "scoreboard/scoreboard.md"
    notice = "当前权重 1.00，等于市场参考"
    before = run(scoreboard_archive, as_of=NOW - timedelta(seconds=1))
    assert "team_market_blend" not in before
    assert notice not in report.read_text()

    active = run(scoreboard_archive, as_of=NOW)["team_market_blend"]
    assert active["common_n"] == 0
    assert active["current_weight"] == {"version": "v1", "market_weight": 1.0}
    assert notice in report.read_text()


def test_board_reuses_legacy_score_evidence_without_rewriting_rows(scoreboard_archive):
    from hashlib import sha256

    from test_scoreboard_score import NOW
    from zucai.scoreboard.command import run

    add_blend(scoreboard_archive, market_weight=1.0)
    board = run(scoreboard_archive, as_of=NOW)["team_market_blend"]
    assert board["common_n"] == 3
    assert board["paired"]["market"]["log_loss"]["difference"] == 0
    score_path = scoreboard_archive / blend_board.SCORES
    summary_path = scoreboard_archive / "scoreboard/summary.json"
    rows = list(map(json.loads, score_path.read_bytes().splitlines()))
    assert all(row["market_weight"] == 1.0 for row in rows if row["fusion"] is not None)
    for row in rows:
        row.pop("market_weight")
    legacy_bytes = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    score_path.write_bytes(legacy_bytes)
    legacy = json.loads(summary_path.read_bytes())
    legacy["team_market_blend"]["scored_sha256"] = sha256(legacy_bytes).hexdigest()
    summary_path.write_text(json.dumps(legacy))

    replay = run(scoreboard_archive, as_of=NOW)["team_market_blend"]
    assert "共同样本全部使用权重 1.00" in replay["decision"]["market_note"]
    assert replay["paired"]["team"] == board["paired"]["team"]
    assert score_path.read_bytes() == legacy_bytes


def test_board_publishes_separate_scores_and_duplicate_replay_is_idempotent(
    scoreboard_archive, tmp_path, monkeypatch
):
    from shutil import copytree

    from test_scoreboard_score import NOW
    from zucai.scoreboard.command import run

    archive = scoreboard_archive
    add_blend(archive)
    control = copytree(archive, tmp_path / "without-blend-scorer")
    with monkeypatch.context() as omitted:
        omitted.setattr(blend_board, "marker", lambda row: False)
        original = run(control, as_of=NOW)
    current = run(archive, as_of=NOW)
    assert "team_market_blend" not in original
    assert current["probability"] == original["probability"]
    assert current["accounts"] == original["accounts"]
    assert current["team_market_blend"]["common_n"] == 3
    assert current["team_market_blend"]["coverage"]["fusion"]["missing"] >= 1
    assert "球队＋市场（实验）同批比较" in (archive / "scoreboard/scoreboard.md").read_text()
    for name in ("scoreboard/scored.jsonl", "scoreboard/ledger.jsonl"):
        assert (archive / name).read_bytes() == (control / name).read_bytes()
    path = archive / blend_board.SCORES
    original_score = path.read_bytes()
    assert all(row["batch_id"] == "late" for row in map(json.loads, original_score.splitlines()))
    run(archive, as_of=NOW)
    assert path.read_bytes() == original_score
    path.write_bytes(original_score + b"{}\n")
    # A damaged T72 file is quarantined. No rewrite of the scores or official tickets.
    after = run(archive, as_of=NOW)
    assert after["team_market_blend"]["status"] == "unavailable"
    assert path.read_bytes() == original_score + b"{}\n"
    assert (archive / "scoreboard/ledger.jsonl").read_bytes() == (
        control / "scoreboard/ledger.jsonl"
    ).read_bytes()


def test_both_ledgers_unchanged_when_same_frozen_probabilities_are_scored(
    archive_setup, tmp_path, monkeypatch
):
    from shutil import copytree

    from test_parlay_archive import frozen_doc
    from test_parlay_paper import AFTER
    from zucai.parlay.paper import LEDGER
    from zucai.scoreboard.command import run

    archive, _, _ = archive_setup
    add_blend(archive)
    frozen_doc(archive_setup)
    control = copytree(archive, tmp_path / "paper-control")
    with monkeypatch.context() as omitted:
        omitted.setattr(blend_board, "marker", lambda row: False)
        original = run(control, as_of=AFTER)
    blend = run(archive, as_of=AFTER)
    assert blend["team_market_blend"]["coverage"]["team"]["total"] > 0
    assert blend["parlay_sd"] == original["parlay_sd"]
    for name in (LEDGER, "scoreboard/ledger.jsonl"):
        raw = (archive / name).read_bytes()
        assert raw and raw == (control / name).read_bytes()
