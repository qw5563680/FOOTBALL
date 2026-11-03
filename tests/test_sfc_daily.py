"""Daily SFC pipeline using only the existing trimmed T07 page and synthetic results."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from test_sfc_report import NOW  # noqa: F401
from test_sfc_report import complete_issue as complete_issue
from zucai.cloud_feeds import export_feeds
from zucai.db import get_engine, session_scope
from zucai.forecast_archive import publish_forecasts
from zucai.models import Match, Result
from zucai.report.render import write_report
from zucai.report.sfc import build_sfc_report
from zucai.sfc_reports import publish_reports, read_reports

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t26"


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def freeze(archive, stage, batch, run_id, generated):
    batch = deepcopy(batch)
    batch["generated_utc"] = generated.isoformat()
    leagues = {}
    for model in batch["models"]:
        league = model["league"]
        path = f"models/{run_id}/{league}.pkl"
        train = f"models/{run_id}/{league}.train.json"
        (stage / path).parent.mkdir(parents=True, exist_ok=True)
        (stage / path).write_bytes(league.encode())
        (stage / train).write_text("{}")
        leagues[league] = {
            "status": "trained",
            "model_path": path,
            "training_json_path": train,
            "sha256": hashlib.sha256(league.encode()).hexdigest(),
            "training_database_sha256": "b" * 64,
        }
    forecasts = {}
    for index, day in enumerate(("2026-11-02", "2026-11-03")):
        frozen = deepcopy(batch)
        frozen["batch_id"] = f"{run_id}-{day}"
        frozen["predictions"] = batch["predictions"][index * 7 : (index + 1) * 7]
        path = f"forecasts/{day}/{run_id}"
        write_report(frozen, stage / path)
        forecasts[day] = {
            "path": path,
            "batch_id": frozen["batch_id"],
            "generated_utc": generated.isoformat(),
        }
    publish_forecasts(
        archive,
        stage,
        {
            "run_id": run_id,
            "generated_utc": generated.isoformat(),
            "database_sha256": "b" * 64,
            "leagues": leagues,
            "forecasts": forecasts,
        },
    )


@pytest.fixture
def sfc_archive(complete_issue, tmp_path):
    engine, issue, batch = complete_issue
    database = Path(engine.url.database)
    archive = tmp_path / "archive"
    export_feeds(database, archive, date(2026, 11, 1), "offline")
    freeze(archive, tmp_path / "stage", batch, "early", NOW)
    return archive, database, batch, issue


@pytest.mark.parametrize("missing", [0, 1, 4, 14])
def test_missing_models_keep_null_probability_and_force_three(complete_issue, missing):
    engine, issue, batch = complete_issue
    batch["predictions"] = batch["predictions"][missing:]
    with session_scope(engine) as session:
        report = build_sfc_report(
            session, issue, batch, forecast_sha256="a" * 64, generated=NOW, allow_missing=True
        )
    assert len(report["rows"]) == 14
    assert report["minimum_lines"] == 3**missing
    for row in report["rows"][:missing]:
        assert row["probabilities"] is None and row["top_outcome"] is None
        assert row["selection"] == list("hda") and "无模型" in row["reason"]
    if missing < 4:
        assert report["combo"]["lines"] <= 64
    else:
        assert report["combo"] is None


def test_report_two_batches_history_idempotency_and_latest(sfc_archive, tmp_path):
    archive, _, batch, issue = sfc_archive
    before = files(archive)
    assert publish_reports(archive, now=NOW) == [issue.issue_no]
    first = files(archive)
    assert all(first[path] == raw for path, raw in before.items())
    assert publish_reports(archive, now=NOW) == []
    assert files(archive) == first
    report = json.loads((archive / f"sfc/{issue.issue_no}/sfc.json").read_bytes())
    assert len(report["rows"]) == 14 and len(report["forecast_sources"]) == 2
    assert all(row["probabilities"] == {"h": 0.5, "d": 0.3, "a": 0.2} for row in report["rows"])
    assert report["combo"]["lines"] == 64
    later = NOW + timedelta(minutes=15)
    freeze(archive, tmp_path / "later", batch, "late", later)
    publish_reports(archive, now=later)
    assert len(read_reports(archive)) == 2
    assert json.loads((archive / f"sfc/{issue.issue_no}/sfc.json").read_bytes())["run_id"] == "late"
    assert all((archive / p).read_bytes() == b for p, b in first.items() if "/history/" in p)
    deadline = datetime.fromisoformat(issue.sale_end_utc)
    freeze(archive, tmp_path / "closed", batch, "closed", deadline)
    assert publish_reports(archive, now=deadline) == []
    assert len(read_reports(archive)) == 2


def install_results(database, count=14):
    rows = json.loads((FIXTURES / "results.json").read_bytes())
    engine = get_engine(database)
    from zucai.models import SfcIssue

    with session_scope(engine) as session:
        issue = session.get(SfcIssue, "26156")
        for row, outcome in zip(issue.snapshot[:count], rows[:count], strict=True):
            session.get(Match, row["match_id"]).status = "finished"
            result = session.get(Result, row["match_id"])
            if result is None:
                session.add(
                    Result(
                        match_id=row["match_id"],
                        home_goals=outcome["home_goals"],
                        away_goals=outcome["away_goals"],
                        available_utc="2026-11-03T10:00:00+00:00",
                        available_source="captured",
                    )
                )
    engine.dispose()


def test_all_results_required_settlement_idempotent_and_digest(sfc_archive, tmp_path):
    from zucai.daily_digest import generate
    from zucai.scoreboard.command import run

    archive, database, _, _ = sfc_archive
    publish_reports(archive, now=NOW)
    originals = files(archive / "sfc")
    at = datetime(2026, 11, 3, 12, tzinfo=UTC)
    install_results(database, count=13)
    export_feeds(database, archive, date(2026, 11, 3), "partial")
    first = run(archive, as_of=at)
    assert first["sfc"]["issues"] == 0
    install_results(database)
    export_feeds(database, archive, date(2026, 11, 3), "complete")
    summary = run(archive, as_of=at)
    assert summary["sfc"]["issues"] == 1
    assert summary["sfc"]["average_single_hits"] == 10
    assert summary["sfc"]["hit_distribution"] == {"10": 1}
    record = summary["sfc"]["latest"]
    assert record["single_evaluated"] == 14 and len(record["rows"]) == 14
    assert isinstance(record["combo_all_hit"], bool)
    raw = (archive / "sfc/settled.jsonl").read_bytes()
    assert len(raw.splitlines()) == 1
    repeated = run(archive, as_of=at)
    assert repeated["sfc"] == summary["sfc"]
    assert (archive / "sfc/settled.jsonl").read_bytes() == raw
    assert repeated["accounts"] == summary["accounts"]
    assert all((archive / "sfc" / p).read_bytes() == b for p, b in originals.items())
    markdown = (archive / "scoreboard/scoreboard.md").read_text()
    assert "## 14 场胜负彩诊断" in markdown and "平均单选命中：10" in markdown
    text = generate(archive, tmp_path / "daily", now=at).read_text()
    assert "## 14 场胜负彩" in text and "期号：26156" in text and "复式 64 注" in text
    assert text.split("## 14 场胜负彩", 1)[1].split("## 昨日结算", 1)[0].count("| 模型诊断 |") == 14
    # Next synthetic issue lets the digest quote the previous settlement without rescoring.
    source = archive / "state/sfc/26156.json"
    data = json.loads(source.read_bytes())
    data["issue"]["issue_no"] = "26157"
    for row in data["matches"]:
        row["issue_no"] = "26157"
    next_issue = archive / "state/sfc/26157.json"
    next_issue.write_text(json.dumps(data))
    manifest_path = archive / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"]["state/sfc/26157.json"] = hashlib.sha256(next_issue.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    text = generate(archive, tmp_path / "daily", now=at).read_text()
    assert "上一已结算期 26156：单选命中 10 / 14" in text
    assert "无模型：" in text
    (archive / "sfc/settled.jsonl").write_bytes(raw + b"{}\n")
    with pytest.raises(ValueError, match="SHA256"):
        generate(archive, tmp_path / "daily", now=at)


def test_unknown_mapping_generates_fourteen_missing_rows(sfc_archive, tmp_path):
    from sqlalchemy import delete

    from zucai.models import SfcIssue, SfcIssueMatch

    archive, database, _, _ = sfc_archive
    engine = get_engine(database)
    with session_scope(engine) as session:
        issue = session.get(SfcIssue, "26156")
        rows = deepcopy(issue.snapshot)
        for row in rows:
            row.update(match_id=None, matched_identity=None, mapping="unmatched")
        issue.snapshot = rows
        session.execute(delete(SfcIssueMatch))
    engine.dispose()
    export_feeds(database, archive, date(2026, 11, 1), "unmapped")
    publish_reports(archive, now=NOW)
    report = read_reports(archive)[0][1]
    assert report["missing_models"] == 14 and report["combo"] is None
    assert "超过 64 注上限" in (archive / "sfc/26156/report.md").read_text()


def test_settlement_failure_rolls_back_board_and_sfc_log(sfc_archive, monkeypatch):
    from zucai.scoreboard.command import run

    archive, database, _, _ = sfc_archive
    publish_reports(archive, now=NOW)
    at = datetime(2026, 11, 3, 12, tzinfo=UTC)
    run(archive, as_of=at)
    install_results(database)
    export_feeds(database, archive, date(2026, 11, 3), "complete")
    before = files(archive)
    original = Path.replace

    def fail_settled(path, destination):
        if path.name == "sfc-settled":
            raise OSError("offline SFC write failure")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", fail_settled)
    with pytest.raises(OSError, match="SFC write failure"):
        run(archive, as_of=at)
    assert files(archive) == before


def test_report_failure_removes_incomplete_history(sfc_archive, monkeypatch):
    import zucai.sfc_reports as module

    archive, _, _, _ = sfc_archive
    before = files(archive)
    original = module._atomic_write

    def fail_latest(path, raw):
        if path.name == "manifest.json" and path.parent.name == "26156":
            raise OSError("offline report failure")
        original(path, raw)

    monkeypatch.setattr(module, "_atomic_write", fail_latest)
    with pytest.raises(OSError, match="report failure"):
        publish_reports(archive, now=NOW)
    assert files(archive) == before
    assert read_reports(archive) == []
