"""Score research from pre-kickoff freezes and observed regular-time results."""

import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from test_scoreboard_command import scoreboard_archive  # noqa: F401
from test_scoreboard_score import NOW, samples
from test_t70_archive import case, clock  # noqa: F401
from zucai.scoreboard.score import instant


def _record(match_id, source_rows, *, judgment="h", probabilities=None, status="ok"):
    forecast = next(
        row for row in source_rows if row["match_id"] == match_id and row["batch_id"] == "late"
    )
    kickoff = instant(forecast["kickoff_utc"])
    return {
        "match_id": match_id,
        "run_id": "offline-a",
        "forecast_run_id": "late",
        "forecast_path": "forecasts/2026-11-01/late/forecast.json",
        "forecast_sha256": "f" * 64,
        "forecast_published_utc": forecast["published_utc"],
        "archive_path": "forecasts/2026-11-01/late/web_research/offline-a/web_research.json",
        "archive_sha256": "c" * 64,
        "kickoff_utc": kickoff.isoformat(),
        "completed_utc": (kickoff - timedelta(minutes=15)).isoformat(),
        "frozen_utc": (kickoff - timedelta(minutes=14)).isoformat(),
        "status": status,
        "identity": {key: forecast[key] for key in ("home", "away", "league")},
        "structured": {"judgment": judgment, "probabilities": probabilities}
        if status == "ok"
        else None,
        "model_probabilities": forecast["model_probabilities"],
        "market_probabilities": {"h": 0.5, "d": 0.25, "a": 0.25} if match_id != "jc:nosp" else None,
    }


def _sample_records():
    sources, results = samples()
    probabilities = {"h": 0.6, "d": 0.3, "a": 0.1}
    first = _record("jc:one", sources, judgment="a", probabilities=probabilities)
    newer = _record("jc:one", sources, judgment="h", probabilities=probabilities)
    newer.update(
        run_id="offline-b",
        completed_utc=(instant(first["completed_utc"]) + timedelta(minutes=1)).isoformat(),
        frozen_utc=(instant(first["frozen_utc"]) + timedelta(minutes=1)).isoformat(),
    )
    later_failure = dict(newer, run_id="offline-fail", status="failed", structured=None)
    later_failure["frozen_utc"] = (instant(newer["frozen_utc"]) + timedelta(minutes=1)).isoformat()
    no_opinion = _record("jc:two", sources, judgment=None)
    malformed = _record("jc:three", sources, status="unparsed")
    no_market = _record("jc:nosp", sources, judgment="h", probabilities=probabilities)
    return [first, newer, later_failure, no_opinion, malformed, no_market], results


def test_research_score_keeps_direction_probability_and_paired_denominators_distinct():
    from zucai.web_research_score import score

    attempts, results = _sample_records()
    summary = score(attempts[::-1], results, NOW)
    overall = summary["overall"]
    assert overall["settled_n"] == 4
    assert overall["direction_n"] == 2
    assert overall["direction_coverage"] == 0.5
    assert overall["no_tendency_n"] == 1
    assert overall["probability"]["n"] == 2
    assert overall["probability"]["log_loss"] is not None
    chosen = {r["match_id"]: r for r in summary["matches"]}
    assert chosen["jc:one"]["run_id"] == "offline-b"
    assert chosen["jc:one"]["hit"] is True
    assert chosen["jc:two"]["hit"] is None
    assert chosen["jc:three"]["research_probabilities"] is None
    assert overall["direction_hit_rate"] == sum(r["hit"] is True for r in chosen.values()) / 2
    cohort = overall["common"]
    assert cohort["probability_match_ids"] == cohort["direction_match_ids"] == ["jc:one"]
    assert cohort["probability"]["research"]["log_loss"] == pytest.approx(-math.log(0.6))
    assert cohort["probability"]["research"]["brier"] == pytest.approx(0.26)
    assert cohort["probability"]["team"]["log_loss"] == pytest.approx(-math.log(0.48))
    assert cohort["probability"]["market"]["log_loss"] == pytest.approx(-math.log(0.5))
    assert all(value["n"] == 1 for value in cohort["probability"].values())
    assert next(iter(summary["leagues"].values())) == overall


def test_research_rejects_late_freeze_wrong_identity_and_nonobserved_results():
    from zucai.web_research_score import score

    attempts, results = _sample_records()
    attempts = attempts[2:]
    attempts[0]["frozen_utc"] = attempts[0]["kickoff_utc"]
    results["jc:two"]["home"] = "改名且无已核映射"
    results["jc:three"]["available_source"] = "assumed"
    results["jc:nosp"]["available_utc"] = attempts[-1]["kickoff_utc"]
    summary = score(attempts, results, NOW)
    assert summary["overall"]["settled_n"] == 0
    assert summary["matches"] == []
    assert summary["excluded"] == {
        "freeze_not_prematch": 1,
        "identity_changed": 1,
        "result_not_after_kickoff": 1,
        "result_not_observed": 1,
    }
    assert score(_sample_records()[0], results, NOW - timedelta(days=20))["matches"] == []


def test_zero_research_probability_for_actual_outcome_is_an_infinite_loss():
    from zucai.web_research_score import render, score

    attempts, results = _sample_records()
    row = attempts[1]
    row["structured"]["probabilities"] = {"h": 0.0, "d": 0.3, "a": 0.7}
    summary = score([row], results, NOW)
    assert summary["overall"]["probability"]["log_loss"] == "Infinity"
    assert summary["overall"]["common"]["probability"]["research"]["log_loss"] == "Infinity"
    assert summary["overall"]["probability"]["brier"] == pytest.approx(1.58)
    assert "∞" in render(summary)


def test_sidecar_isolation_and_graceful_archive_failure(scoreboard_archive, monkeypatch):  # noqa: F811
    from zucai import web_research_score
    from zucai.scoreboard import command

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(command, "datetime", FixedDatetime)
    command.run(scoreboard_archive, as_of=NOW)
    # The standard scoreboard's per-update new-count settles on the second pass.
    command.run(scoreboard_archive, as_of=NOW)
    board = scoreboard_archive / "scoreboard"
    original = {p.name: p.read_bytes() for p in board.iterdir()}
    assert set(original) == set(command.FILES)

    attempts, _ = _sample_records()
    monkeypatch.setattr(web_research_score, "load", lambda archive, as_of: attempts)
    command.run(scoreboard_archive, as_of=NOW)
    assert {k: (board / k).read_bytes() for k in original} == original
    view = board / "web-research.md"
    data = board / "web_research.json"
    assert "联网研究线（实验）" in view.read_text()
    assert json.loads(data.read_text())["overall"]["common"]["probability_match_ids"] == ["jc:one"]
    frozen = (view.read_bytes(), data.read_bytes())

    def corrupt(archive, as_of):
        raise ValueError("离线失败夹具")

    monkeypatch.setattr(web_research_score, "load", corrupt)
    command.run(scoreboard_archive, as_of=NOW)
    assert (view.read_bytes(), data.read_bytes()) == frozen
    assert {k: (board / k).read_bytes() for k in original} == original

    replace = Path.replace

    def fail_score_data(self, target):
        if target.name == data.name:
            raise OSError("离线写入失败夹具")
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_score_data)
    monkeypatch.setattr(web_research_score, "load", lambda archive, as_of: attempts[:-2])
    command.run(scoreboard_archive, as_of=NOW)
    assert (view.read_bytes(), data.read_bytes()) == frozen
    assert {k: (board / k).read_bytes() for k in original} == original


def test_archive_roundtrip_marks_probabilities_even_without_clear_tendency(case):  # noqa: F811
    from zucai import web_research_score
    from zucai.web_research_archive import publish

    archive, run, artifacts = case
    run["records"][1]["structured"]["judgment"] = None
    publish(archive, run, artifacts, clock=clock())
    (archive / "scoreboard").mkdir()
    data = archive / web_research_score.SCORE_DATA
    results = {}
    for row in run["records"]:
        results[row["match_id"]] = {
            **row["identity"],
            "source": "jc",
            "status": "finished",
            "home_goals": 2,
            "away_goals": 1,
            "available_utc": (instant(row["kickoff_utc"]) + timedelta(hours=2)).isoformat(),
            "available_source": "captured",
        }

    web_research_score.update(archive, results, run["generated_utc"])
    assert not data.exists()  # The research report itself was not yet frozen.
    web_research_score.update(archive, results, "2026-11-30T00:00:00+00:00")
    view = json.loads(data.read_text())
    assert view["overall"]["settled_n"] == view["overall"]["probability"]["n"] == 2
    assert view["overall"]["direction_n"] == view["overall"]["no_tendency_n"] == 1
    for row in view["matches"]:
        assert (
            row["archive_sha256"]
            == hashlib.sha256((archive / row["archive_path"]).read_bytes()).hexdigest()
        )
