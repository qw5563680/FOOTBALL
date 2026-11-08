import hashlib
import itertools
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match

NOW = datetime(2026, 11, 1, 15, tzinfo=UTC)
ROOT = Path(__file__).parents[1]


def test_optimizer_matches_independent_all_subsets_enumeration():
    from zucai.report.sfc import optimize_cover

    probs = [
        {"h": 0.7, "d": 0.2, "a": 0.1},
        {"h": 0.4, "d": 0.35, "a": 0.25},
        {"h": 0.1, "d": 0.6, "a": 0.3},
    ]
    choices = [c for k in [1, 2, 3] for c in itertools.combinations("hda", k)]
    for cap in [1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 18, 27]:
        brute = max(
            math.prod(sum(p[x] for x in s) for p, s in zip(probs, picks, strict=True))
            for picks in itertools.product(choices, repeat=3)
            if math.prod(map(len, picks)) <= cap
        )
        result = optimize_cover(probs, cap)
        assert result["coverage_probability"] == pytest.approx(brute)
        assert math.prod(map(len, result["selections"])) == result["lines"] <= cap
    assert optimize_cover(probs, 1)["selections"] == [["h"], ["h"], ["d"]]


@pytest.mark.parametrize(
    "probabilities,cap",
    [
        ([], 64),
        ([{"h": 1.0, "d": 0.0, "a": 0.0}], 0),
        ([{"h": 1.0, "d": 0.0, "a": 0.0}], True),
        ([{"h": 1.0, "d": 0.0, "a": 0.0}], 1.5),
        ([{"h": 0.5, "d": 0.5, "a": 0.5}], 64),
        ([{"h": float("nan"), "d": 0.5, "a": 0.5}], 64),
    ],
)
def test_optimizer_rejects_invalid_probabilities_or_budget(probabilities, cap):
    from zucai.report.sfc import optimize_cover

    with pytest.raises(ValueError):
        optimize_cover(probabilities, cap)


@pytest.fixture
def complete_issue(tmp_path, monkeypatch):
    from zucai.sources.c500 import C500Sfc
    from zucai.sources.sfc import ingest_issue

    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "db")
    init_db(engine)
    snapshot = C500Sfc().parse((ROOT / "data/fixtures/t07/26156.html").read_bytes())[0]
    with session_scope(engine) as s:
        issue = ingest_issue(
            s, snapshot, observed=NOW, url="https://trade.500.com/sfc/", raw_sha256="b" * 64
        )
    models = []
    predictions = []
    for f in issue.snapshot:
        version = hashlib.sha256(f["league"].encode()).hexdigest()
        models.append(
            dict(
                model_name="dixon-coles",
                model_version=version,
                source="c500",
                league=f["league"],
                availability_basis="strict",
                model_as_of_utc="2026-11-01T12:00:00Z",
                last_available_utc="2026-10-31T12:00:00Z",
                training_games=100,
                assumed_training_share=0,
            )
        )
        predictions.append(
            dict(
                match_id=f["match_id"],
                **f["matched_identity"],
                status="predicted",
                model_name="dixon-coles",
                model_version=version,
                model_probabilities={"h": 0.5, "d": 0.3, "a": 0.2},
                score_matrix=[[0.3, 0.2], [0.5, 0]],
                top_scores=[],
                market=None,
                ev=None,
            )
        )
    batch = dict(
        schema_version=1,
        batch_id="synthetic-test-only",
        generated_utc=NOW.isoformat(),
        as_of_utc=NOW.isoformat(),
        mode="prematch",
        probability_type="P2",
        validation_status="experimental_unadopted",
        bet_allowed=False,
        input={},
        models=models,
        predictions=predictions,
    )
    yield engine, issue, batch
    engine.dispose()


def build(engine, issue, batch, **kwargs):
    from zucai.report.sfc import build_sfc_report

    with session_scope(engine) as s:
        return build_sfc_report(
            s, issue, batch, forecast_sha256="a" * 64, generated=NOW, max_lines=64, **kwargs
        )


def test_full_cover_uses_pure_model_and_is_not_a_profit_probability(complete_issue):
    engine, issue, batch = complete_issue
    report = build(engine, issue, batch)
    assert report["combo"]["lines"] == 64
    assert report["combo"]["coverage_probability"] == pytest.approx(0.8**6 * 0.5**8)
    assert len(report["rows"]) == 14
    assert report["bet_allowed"] is False
    assert report["probability_type"] == "P2"
    for row in batch["predictions"]:
        row["market"] = {
            "prices": {"h": 10, "d": 10, "a": 1.1},
            "probabilities": {"h": 0.1, "d": 0.1, "a": 0.8},
            "snapshot_utc": "2026-11-01T14:00:00Z",
        }
        row["experimental_blend"] = {"probabilities": {"h": 0.1, "d": 0.1, "a": 0.8}}
    assert build(engine, issue, batch)["combo"] == report["combo"]


@pytest.mark.parametrize("failure", ["missing", "identity", "late", "cancelled", "model", "time"])
def test_one_invalid_leg_keeps_14_rows_but_no_complete_cover(complete_issue, failure):
    engine, issue, batch = complete_issue
    row = batch["predictions"][0]
    if failure == "missing":
        batch["predictions"].pop(0)
    if failure == "identity":
        row["home"] = "不同球队"
    if failure == "late":
        batch["generated_utc"] = "2026-11-03T01:00:00Z"
    if failure == "model":
        row["model_name"] = "market"
    if failure == "time":
        row["kickoff_utc"] = "2026-11-03T01:00:00Z"
    if failure == "cancelled":
        with session_scope(engine) as s:
            s.get(Match, row["match_id"]).status = "cancelled"
    report = build(engine, issue, batch)
    assert len(report["rows"]) == 14 and report["combo"] is None
    assert any(r["reason"] for r in report["rows"])


def test_replay_is_labelled_and_unknown_deadline_is_not_a_sale_window(complete_issue):
    engine, issue, batch = complete_issue
    batch["mode"] = "historical_replay"
    issue.sale_end_utc = None
    report = build(engine, issue, batch)
    assert report["mode"] == "historical_replay"
    assert report["sale_state"] == "deadline_unknown"
    assert report["execution_verified"] is False


def test_export_is_exclusive_hash_checked_and_cleans_its_partial_files(
    complete_issue, tmp_path, monkeypatch
):
    from zucai.report.sfc import write_sfc_report

    engine, issue, batch = complete_issue
    report = build(engine, issue, batch)
    out = tmp_path / "report"
    write_sfc_report(report, out)
    manifest = json.loads((out / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert json.loads((out / "sfc.json").read_text())["rows"][0]["probabilities"] == {
        "h": 0.5,
        "d": 0.3,
        "a": 0.2,
    }
    with pytest.raises(FileExistsError):
        write_sfc_report(report, out)
    original = Path.write_bytes

    def fail_csv(self, content):
        if self.name == "report.csv":
            raise OSError("disk failure")
        return original(self, content)

    monkeypatch.setattr(Path, "write_bytes", fail_csv)
    with pytest.raises(OSError):
        write_sfc_report(report, tmp_path / "broken")
    assert not (tmp_path / "broken").exists()


def test_cli_sfc_report_uses_real_frozen_t06_batch_and_keeps_missing_rows(tmp_path, monkeypatch):
    from zucai.cli import main
    from zucai.sources.c500 import C500Sfc
    from zucai.sources.sfc import ingest_issue

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "db"
    engine = get_engine(db)
    init_db(engine)
    snapshot = C500Sfc().parse((ROOT / "data/fixtures/t07/26155.html").read_bytes())[0]
    with session_scope(engine) as s:
        ingest_issue(s, snapshot, observed=NOW, url="url", raw_sha256="a" * 64)
    engine.dispose()
    batch = ROOT / "reports/2026-11-01-t06-acceptance/live-batch-v2/forecast.json"
    assert (
        main(
            [
                "report",
                "sfc",
                "--db",
                str(db),
                "--issue",
                "26155",
                "--batch",
                str(batch),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    report = json.loads((tmp_path / "out/sfc.json").read_text())
    assert len(report["rows"]) == 14 and report["combo"] is None
    assert all(r["probabilities"] is None for r in report["rows"])


def test_report_rejects_source_forecast_changed_during_read(tmp_path, monkeypatch):
    from argparse import Namespace

    from zucai.report import sfc

    source = ROOT / "reports/2026-11-01-t06-acceptance/live-batch-v2/forecast.json"
    target = tmp_path / "forecast.json"
    target.write_bytes(source.read_bytes())
    batch = json.loads(target.read_bytes())

    def changed(path):
        target.write_text("{}", encoding="utf8")
        return batch

    monkeypatch.setattr(sfc, "load_batch", changed)
    with pytest.raises(ValueError, match="变化"):
        sfc.run_sfc_report(
            Namespace(
                batch=str(target),
                db=str(tmp_path / "db"),
                issue="26155",
                out=str(tmp_path / "out"),
                max_lines=64,
            )
        )
    assert not (tmp_path / "out").exists()


def test_standalone_csv_retains_replay_probability_and_deadline_context(complete_issue, tmp_path):
    import csv

    from zucai.report.sfc import write_sfc_report

    engine, issue, batch = complete_issue
    batch["mode"] = "historical_replay"
    issue.sale_end_utc = None
    report = build(engine, issue, batch)
    output = tmp_path / "replay"
    write_sfc_report(report, output)
    with (output / "report.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 14
    for row in rows:
        assert row["mode"] == "historical_replay"
        assert row["probability_type"] == "P2" and row["bet_allowed"] == "False"
        assert row["sale_state"] == "deadline_unknown" and row["sale_end_utc"] == ""
        assert row["forecast_generated_utc"] == batch["generated_utc"]
        assert row["forecast_as_of_utc"] == batch["as_of_utc"]
        assert row["execution_verified"] == "False"


def test_checked_in_acceptance_artifacts_keep_their_original_hashes():
    manifests = list((ROOT / "reports/2026-11-01-t07-acceptance").glob("*/manifest.json"))
    assert len(manifests) == 6
    for path in manifests:
        manifest = json.loads(path.read_bytes())
        for name, digest in manifest["files"].items():
            assert hashlib.sha256((path.parent / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("source_ended,status", [(True, "scheduled"), (False, "finished")])
def test_ended_national_fixture_has_no_pool_pick(complete_issue, source_ended, status):
    from zucai.models import Match

    engine, issue, batch = complete_issue
    row = issue.snapshot[0]
    row.update(league="欧国联", source_ended=source_ended)
    row["matched_identity"]["league"] = "欧国联"
    batch["predictions"][0]["league"] = "欧国联"
    batch["models"][0]["league"] = "欧国联"
    with session_scope(engine) as session:
        game = session.get(Match, row["match_id"])
        game.league, game.status = "欧国联", status
    result = build(engine, issue, batch)["rows"][0]
    assert result["probabilities"] is None
    assert "赛前" in result["reason"]
