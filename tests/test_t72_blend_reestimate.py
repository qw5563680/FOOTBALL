"""Prospective re-estimation reads marked common scores at exact frozen milestones."""

import hashlib
import json
from datetime import datetime, timedelta

import pytest

from zucai.market_blend_reestimate import reestimate
from zucai.scoreboard.team_market_blend import SCORES, checkpoint

AT = datetime.fromisoformat("2026-11-26T00:00:00+00:00")
NOW = AT + timedelta(hours=1)
EFFECTIVE = NOW + timedelta(hours=1)


def outcome_row(index):
    # A simulated, explicitly captured post-kickoff score and matching prematch quote.
    kickoff = AT - timedelta(days=21) + timedelta(hours=index)
    cutoff = kickoff - timedelta(hours=1)
    collected = cutoff - timedelta(minutes=3)
    return {
        "record_id": f"score-{index}",
        "match_id": f"jc:{index}",
        "kickoff_utc": kickoff.isoformat(),
        "as_of_utc": cutoff.isoformat(),
        "generated_utc": cutoff.isoformat(),
        "published_utc": (cutoff + timedelta(minutes=1)).isoformat(),
        "available_utc": (kickoff + timedelta(hours=2)).isoformat(),
        "available_source": "captured",
        "market_snapshot_id": collected.isoformat(),
        "market_collected_utc": collected.isoformat(),
        "home_goals": 1,
        "away_goals": 0,
        "actual": "h",
        "team": {"h": 0.2, "d": 0.2, "a": 0.6},
        "market": {"h": 0.6, "d": 0.2, "a": 0.2},
        "fusion": {"h": 0.7, "d": 0.2, "a": 0.1},
        "closing": {"h": 0.1, "d": 0.1, "a": 0.8},
    }


def _archive(root, rows, *, active=None, scored_at=AT):
    ids = [row["record_id"] for row in rows] if active is None else active
    raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
    stream = root / SCORES
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_bytes(raw)
    current = [row for row in rows if row["record_id"] in ids]
    (stream.parent / "summary.json").write_text(
        json.dumps(
            {
                "as_of_utc": (scored_at - timedelta(seconds=1)).isoformat(),
                "generated_utc": scored_at.isoformat(),
                "team_market_blend": {
                    "scored_sha256": hashlib.sha256(raw).hexdigest(),
                    "common_n": len(ids),
                    "common_keys": ids,
                    "active_keys": ids,
                    "checkpoint_300": checkpoint(current, None),
                },
            }
        )
    )
    return root


def _estimate(archive, out, *, previous=None, at=AT, effective=EFFECTIVE):
    return reestimate(archive, out, as_of=at, effective_from=effective, previous=previous, now=NOW)


def test_waits_for_two_hundred_eligible_distinct_scores(tmp_path):
    archive = _archive(tmp_path / "archive", [outcome_row(i) for i in range(199)])
    output = tmp_path / "proposal.json"
    with pytest.raises(ValueError, match="199.*200"):
        _estimate(archive, output)
    assert not output.exists()


def test_fixes_first_200_and_never_reads_publisher_closing(tmp_path):
    rows = [outcome_row(i) for i in range(201)]
    rows[-1].update(home_goals=0, away_goals=1, actual="a")
    for row in rows:
        row["closing"] = {"deliberately_not_a_probability": "publisher closing not used"}
    archive = _archive(tmp_path / "archive", rows[::-1])
    output = tmp_path / "proposal.json"
    report = _estimate(archive, output)
    assert report["checkpoint"] == 200
    assert report["market_weight"] == 1
    assert report["effective_from_utc"] == EFFECTIVE.isoformat()
    assert report["record_ids"] == [f"score-{index}" for index in range(200)]
    assert report["score_sha256"] == hashlib.sha256((archive / SCORES).read_bytes()).hexdigest()
    assert report["status"] == "proposed"
    fixed = output.read_bytes()
    with pytest.raises(FileExistsError):
        _estimate(archive, output)
    assert output.read_bytes() == fixed
    assert "closing" not in json.dumps(report)


@pytest.mark.parametrize(
    "change",
    [
        {"available_utc": (AT + timedelta(seconds=1)).isoformat()},
        {"available_source": "assumed"},
        {"market_collected_utc": AT.isoformat(), "market_snapshot_id": AT.isoformat()},
        {"published_utc": AT.isoformat()},
    ],
)
def test_will_not_use_late_or_unproven_result_market_or_publication(tmp_path, change):
    rows = [outcome_row(i) for i in range(200)]
    rows[0].update(change)
    archive = _archive(tmp_path / "archive", rows)
    output = tmp_path / "proposal.json"
    with pytest.raises(ValueError, match="赛前快照或赛果时点"):
        _estimate(archive, output)
    assert not output.exists()


def test_requires_summary_known_by_cutoff_and_future_activation(tmp_path):
    archive = _archive(tmp_path / "archive", [outcome_row(i) for i in range(200)])
    with pytest.raises(ValueError, match="摘要.*截点"):
        _estimate(archive, tmp_path / "bad.json", at=AT - timedelta(seconds=1))
    with pytest.raises(ValueError, match="生效时间"):
        _estimate(archive, tmp_path / "backdated.json", effective=NOW)
    assert list(tmp_path.iterdir()) == [archive]


def test_next_exact_checkpoint_reuses_previous_evidence_prefix(tmp_path):
    rows = [outcome_row(i) for i in range(401)]
    root = tmp_path / "archive"
    archive = _archive(root, rows[:200])
    first = tmp_path / "first.json"
    _estimate(archive, first)
    _archive(root, rows[:399])
    with pytest.raises(ValueError, match="399.*400"):
        _estimate(archive, tmp_path / "next.json", previous=first, at=NOW)
    _archive(root, rows)
    report = _estimate(archive, tmp_path / "next.json", previous=first, at=NOW)
    assert report["checkpoint"] == 400
    assert report["previous_report_sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert report["record_ids"][-1] == "score-399"

    rows[0].update(actual="d", home_goals=0)
    _archive(root, rows)
    with pytest.raises(ValueError, match="上次检查点.*证据"):
        _estimate(archive, tmp_path / "changed.json", previous=first, at=NOW)
