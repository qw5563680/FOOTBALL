"""The published baseline has its own append-only pre-match score record."""

import hashlib
import json
from copy import deepcopy
from datetime import date

import pytest

from sfc_publication_support import commit_report, prepare_remote
from test_scoreboard_command import scoreboard_archive as scoreboard_archive
from test_scoreboard_score import NOW
from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.coverage_baseline import MODEL_NAME, MODEL_VERSION
from zucai.db import get_engine, session_scope
from zucai.forecast_publication import confirm_publication, publication_times
from zucai.models import Match, Result, Team
from zucai.scoreboard.command import run
from zucai.scoreboard.score import instant

BASELINE = "scoreboard/baseline.jsonl"
IDENTITY = {
    "source": "jc",
    "league": "未覆盖联赛",
    "home": "独立主队",
    "away": "独立客队",
    "kickoff_utc": "2026-11-01T15:00:00+00:00",
}


@pytest.fixture
def baseline_archive(scoreboard_archive, tmp_path):
    archive = scoreboard_archive
    db = tmp_path / "baseline.db"
    restore_feeds(archive, db)
    engine = get_engine(db)
    with session_scope(engine) as session:
        home, away = Team(name=IDENTITY["home"]), Team(name=IDENTITY["away"])
        session.add_all([home, away])
        session.flush()
        session.add(
            Match(
                match_id="jc:baseline",
                source="jc",
                league=IDENTITY["league"],
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=IDENTITY["kickoff_utc"],
                status="finished",
            )
        )
        session.flush()
        session.add(
            Result(
                match_id="jc:baseline",
                home_goals=1,
                away_goals=0,
                available_source="captured",
                available_utc="2026-11-01T17:00:00+00:00",
            )
        )
    engine.dispose()
    export_feeds(db, archive, date(2026, 11, 1), "baseline-seed")
    return archive


def coverage_snapshot(archive, *, kickoff=None):
    path = archive / "forecast_manifest.json"
    manifest = json.loads(path.read_bytes())
    run_info = manifest["runs"]["early"]
    name = run_info["forecasts"]["2026-11-01"]["path"] + "/coverage.json"
    run_info["coverage_path"] = name
    row = {
        "match_id": "jc:baseline",
        "jc_num": "周日001",
        "seq": None,
        "league": IDENTITY["league"],
        "home": IDENTITY["home"],
        "away": IDENTITY["away"],
        "kickoff_utc": kickoff or IDENTITY["kickoff_utc"],
        "identity": {**IDENTITY, "kickoff_utc": kickoff or IDENTITY["kickoff_utc"]},
        "formal": False,
        "reason": "联赛未建模型",
        "action": "所有者决定是否立项",
        "baseline": {
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "history_scope": "global",
            "history_count": 4,
            "probabilities": {"h": 0.5, "d": 0.25, "a": 0.25},
        },
    }
    sfc = [
        {**row, "jc_num": None, "seq": 1},
        *[
            {
                **row,
                "match_id": None,
                "jc_num": None,
                "seq": seq,
                "kickoff_utc": None,
                "identity": None,
                "reason": "缺少赛程",
                "baseline": None,
                "baseline_unavailable": "缺少可核实的开球时间",
            }
            for seq in range(2, 15)
        ],
    ]
    document = {
        "schema_version": 1,
        "run_id": "early",
        "as_of_utc": run_info["generated_utc"],
        "generated_utc": run_info["generated_utc"],
        "jc": {"2026-11-01": [row], "2026-11-02": []},
        "sfc": {"26170": sfc},
        "sfc_sale_end_utc": {"26170": "2026-11-01T14:30:00+00:00"},
        "gaps": [
            {"scope": "jc", "group": "2026-11-01", **row},
            *[{"scope": "sfc", "group": "26170", **entry} for entry in sfc],
        ],
        "jc_leagues_without_model": {IDENTITY["league"]: 1},
    }
    raw = (json.dumps(document, ensure_ascii=False) + "\n").encode()
    (archive / name).write_bytes(raw)
    manifest["files"][name] = hashlib.sha256(raw).hexdigest()
    path.write_text(json.dumps(manifest))
    return name


def test_baseline_receipt_is_required_and_independent_of_formal_freeze(baseline_archive):
    archive = baseline_archive
    prepare_remote(archive, "2026-11-01T12:00:00Z")
    path = coverage_snapshot(archive)
    assert confirm_publication(archive, clock=lambda: instant("2026-11-01T13:00:00Z")) == []
    assert path not in publication_times(
        archive, json.loads((archive / "forecast_manifest.json").read_bytes())
    )
    commit = commit_report(archive, "2026-11-01T13:20:00Z")
    stamp = instant("2026-11-01T14:00:00Z")
    assert confirm_publication(archive, clock=lambda: stamp) == [path]
    manifest = json.loads((archive / "forecast_manifest.json").read_bytes())
    receipt = publication_times(archive, manifest)[path]
    assert receipt["candidate_sha256"] == hashlib.sha256((archive / path).read_bytes()).hexdigest()
    assert receipt["commit"] == commit
    assert receipt["published_utc"] == stamp.isoformat()
    # Neither a formal receipt nor a mismatched hash can confirm this sidecar.
    formal_receipt = manifest["runs"]["early"]["forecasts"]["2026-11-01"]["publication_evidence"]
    manifest["runs"]["early"]["coverage_publication_evidence"] = deepcopy(formal_receipt)
    assert path not in publication_times(archive, manifest)


@pytest.mark.parametrize(
    ("confirmation", "kickoff", "reason"),
    [
        (None, None, "publication_unknown"),
        ("2026-11-01T15:00:00Z", None, "published_after_kickoff"),
        ("2026-11-01T14:00:00Z", "2026-11-01T15:20:00Z", "identity_changed"),
    ],
)
def test_baseline_scoring_rejects_missing_late_and_moved_fixture(
    baseline_archive, confirmation, kickoff, reason
):
    archive = baseline_archive
    coverage_snapshot(archive, kickoff=kickoff)
    if confirmation:
        prepare_remote(archive, "2026-11-01T13:20:00Z")
        confirm_publication(archive, clock=lambda: instant(confirmation))
    summary = run(archive, as_of=NOW)
    assert summary["coverage_baseline"]["active_keys"] == []
    assert reason in summary["coverage_baseline"]["excluded"]
    assert (archive / BASELINE).read_bytes() == b""


def test_baseline_score_stays_out_of_all_formal_and_paper_outputs(baseline_archive):
    archive = baseline_archive
    formal_only = run(archive, as_of=NOW)
    tracked = ["scoreboard/scored.jsonl", "scoreboard/ledger.jsonl", "sfc/settled.jsonl"]
    before = {path: (archive / path).read_bytes() for path in tracked}
    path = coverage_snapshot(archive)
    prepare_remote(archive, "2026-11-01T13:20:00Z")
    confirm_publication(archive, clock=lambda: instant("2026-11-01T14:00:00Z"))
    summary = run(archive, as_of=NOW)
    assert before == {path: (archive / path).read_bytes() for path in tracked}
    for key in ("probability", "accounts", "gates", "experiments", "sfc"):
        assert summary[key] == formal_only[key]
    raw = (archive / BASELINE).read_bytes()
    row = json.loads(raw)
    assert row["match_id"] == "jc:baseline"
    assert row["forecast_path"] == path
    assert row["actual"] == "h"
    assert row["brier"] == pytest.approx(0.375)
    assert summary["coverage_baseline"]["models"] == [
        {
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "n": 1,
            "log_loss": pytest.approx(0.69314718),
            "brier": pytest.approx(0.375),
        }
    ]
    assert "基准（无球队信息）独立计分" in (archive / "scoreboard/scoreboard.md").read_text()
    run(archive, as_of=NOW)
    assert (archive / BASELINE).read_bytes() == raw
    (archive / BASELINE).write_bytes(raw + b"{}\n")
    with pytest.raises(ValueError, match="基准评分 SHA256"):
        run(archive, as_of=NOW)
