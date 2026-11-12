"""Offline post-push receipts and strict pre-kickoff selection."""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from sfc_publication_support import REF, commit_report, git, prepare_remote
from test_scoreboard_command import scoreboard_archive as scoreboard_archive
from test_scoreboard_score import NOW, samples
from zucai.scoreboard.score import instant, prematch_rows, score_forecasts


def test_late_and_unknown_publications_are_excluded_and_counted():
    forecasts, results = samples()
    rows = [
        deepcopy(r)
        for r in forecasts
        if r["batch_id"] == "early" and r["match_id"] in {"jc:one", "jc:two"}
    ]
    rows[0]["published_utc"] = rows[0]["kickoff_utc"]
    rows[1]["published_utc"] = None
    scored, audit = score_forecasts(rows, results, [], NOW)
    assert scored == []
    assert audit["excluded"] == {"published_after_kickoff": 1, "publication_unknown": 1}


def test_late_latest_version_falls_back_without_changing_previous_bytes():
    forecasts, results = samples()
    rows = [
        deepcopy(r)
        for r in forecasts
        if r["match_id"] == "jc:one" and r["batch_id"] in {"early", "late"}
    ]
    for row in rows:
        row["published_utc"] = row["generated_utc"]
    expected, _ = score_forecasts(rows[:1], results, [], NOW)
    rows[1]["published_utc"] = rows[1]["kickoff_utc"]
    actual, audit = score_forecasts(rows, results, [], NOW)
    assert (
        json.dumps(actual, sort_keys=True).encode() == json.dumps(expected, sort_keys=True).encode()
    )
    assert audit["excluded"]["published_after_kickoff"] == 1
    assert prematch_rows(rows, NOW) == rows[:1]


def test_publication_after_replay_cutoff_is_not_visible():
    row = samples()[0][0]
    row["published_utc"] = "2026-11-01T11:00:00Z"
    assert prematch_rows([row], "2026-11-01T10:00:00Z") == []


def _legacy(archive):
    path = archive / "forecast_manifest.json"
    doc = json.loads(path.read_bytes())
    for run in doc["runs"].values():
        for entry in run["forecasts"].values():
            for key in ("publication_required", "published_utc", "publication_evidence"):
                entry.pop(key, None)
    path.write_text(json.dumps(doc))
    return doc


def test_historical_first_remote_commit_exact_bytes_and_readonly(scoreboard_archive):
    from zucai.forecast_publication import publication_times

    archive = scoreboard_archive
    manifest = _legacy(archive)
    prepare_remote(archive, "2026-11-01T16:00:00Z")
    first = git(archive, "rev-parse", "HEAD")
    commit_report(archive, "2026-11-02T18:00:00Z")
    before = {
        p.relative_to(archive): p.read_bytes()
        for p in archive.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
    resolved = publication_times(archive, manifest)
    path = "forecasts/2026-11-01/early/forecast.json"
    assert resolved[path]["published_utc"] == "2026-11-01T16:00:00+00:00"
    assert resolved[path]["commit"] == first
    assert resolved[path]["method"] == "historical_first_containing_commit"
    assert before == {
        p.relative_to(archive): p.read_bytes()
        for p in archive.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
    (archive / path).write_bytes(b"changed")
    assert path not in publication_times(archive, manifest)


def test_no_git_or_only_local_commit_is_unknown(scoreboard_archive):
    from zucai.forecast_publication import publication_times

    archive = scoreboard_archive
    manifest = _legacy(archive)
    assert publication_times(archive, manifest) == {}
    prepare_remote(archive, "2026-11-01T16:00:00Z")
    git(archive, "update-ref", "-d", REF)
    assert publication_times(archive, manifest) == {}


def test_confirmation_uses_after_push_clock_and_never_local_commit_date(scoreboard_archive):
    from zucai.forecast_publication import confirm_publication, publication_times

    archive = scoreboard_archive
    manifest = _legacy(archive)
    for run in manifest["runs"].values():
        for entry in run["forecasts"].values():
            entry["publication_required"] = True
    (archive / "forecast_manifest.json").write_text(json.dumps(manifest))
    assert confirm_publication(archive) == []
    prepare_remote(archive, "2026-11-01T16:00:00Z")
    assert publication_times(archive, manifest) == {}
    after = datetime(2026, 11, 1, 17, tzinfo=UTC)
    paths = confirm_publication(archive, clock=lambda: after)
    assert len(paths) == 6
    confirmed = json.loads((archive / "forecast_manifest.json").read_bytes())
    receipt = confirmed["runs"]["early"]["forecasts"]["2026-11-01"]
    assert receipt["published_utc"] == after.isoformat()
    assert receipt["publication_evidence"]["method"] == "post_push_confirmation"
    assert confirm_publication(archive, clock=lambda: NOW) == []
    assert (
        publication_times(archive, confirmed)["forecasts/2026-11-01/early/forecast.json"][
            "published_utc"
        ]
        == after.isoformat()
    )


def test_workflow_confirms_formal_publication_only_after_push():
    from pathlib import Path

    workflow = (Path(__file__).parents[1] / ".github/workflows/football-feeds.yml").read_text()
    first = workflow.index("id: publish_forecasts")
    confirm = workflow.index("python -m zucai.forecast_publication")
    second = workflow.index("Publish confirmed", confirm)
    assert first < confirm < second
    assert "steps.publish_forecasts.outcome == 'success'" in workflow[first:confirm]


def test_paper_publication_view_reselects_without_mutating_legacy_decisions():
    from zucai.scoreboard.paper import advance_ledger, publication_view

    forecasts, results = samples()
    rows = [
        r for r in forecasts if r["match_id"] == "jc:one" and r["batch_id"] in {"early", "late"}
    ]
    old = advance_ledger(rows, results, [], NOW)
    frozen = json.dumps(old, sort_keys=True).encode()
    rows[0]["published_utc"] = rows[0]["kickoff_utc"]
    eligible, audit = publication_view(rows, results, old, NOW)
    assert {r["batch_id"] for r in eligible if r["record_type"] == "ticket"} == {"late"}
    assert audit["excluded_tickets"] > 0
    assert json.dumps(old, sort_keys=True).encode() == frozen


def test_experimental_pair_cannot_use_unpublished_formal_version():
    from test_experimental_scoreboard import scores
    from zucai.scoreboard.experimental import summarize_experiments

    rows, results, experiments, original, _ = scores()
    rows[0]["published_utc"] = rows[0]["kickoff_utc"]
    corrected, audit = score_forecasts(rows, results, original, NOW)
    summary = summarize_experiments(experiments, [], corrected, audit["active_keys"], NOW)
    group = next(
        g for g in summary["groups"] if g["model_name"] == "dc-sot-v1" and g["league"] == "英超"
    )
    assert group["production_n"] == group["common_n"] == 1


@pytest.mark.parametrize("removed", ["commit", "ref"])
def test_incomplete_receipt_fails_closed(scoreboard_archive, removed):
    from zucai.forecast_publication import publication_times

    manifest = json.loads((scoreboard_archive / "forecast_manifest.json").read_bytes())
    entry = manifest["runs"]["early"]["forecasts"]["2026-11-01"]
    entry["publication_evidence"].pop(removed)
    assert entry["path"] + "/forecast.json" not in publication_times(scoreboard_archive, manifest)


def test_overflowing_publication_is_unknown():
    from zucai.scoreboard.score import publication_reason

    row = samples()[0][0]
    row["published_utc"] = "0001-01-01T00:00:00+01:00"
    assert publication_reason(row, NOW) == "publication_unknown"


def test_f_view_excludes_old_unpublished_groups_without_mutation():
    from test_parlay_paper import AFTER, document, results
    from zucai.parlay.paper import advance_ledger, publication_view

    doc = document.__wrapped__()
    ledger = advance_ledger([doc], results(), [], AFTER)
    frozen = json.dumps(ledger, sort_keys=True).encode()
    view, audit = publication_view([], results(), ledger, AFTER)
    assert view == []
    assert audit["excluded_groups"] > 0
    assert json.dumps(ledger, sort_keys=True).encode() == frozen


def test_stored_late_scores_and_tickets_are_not_active_and_keep_original_bytes(
    scoreboard_archive, tmp_path
):
    from zucai.daily_digest import generate
    from zucai.scoreboard.command import run

    archive = scoreboard_archive
    run(archive, as_of=NOW)
    score_bytes = (archive / "scoreboard/scored.jsonl").read_bytes()
    ledger_bytes = (archive / "scoreboard/ledger.jsonl").read_bytes()
    path = archive / "forecast_manifest.json"
    manifest = json.loads(path.read_bytes())
    for info in manifest["runs"].values():
        for entry in info["forecasts"].values():
            entry["published_utc"] = "2026-11-09T00:00:00+00:00"
            entry["publication_evidence"]["published_utc"] = entry["published_utc"]
    path.write_text(json.dumps(manifest))
    summary = run(archive, as_of=NOW)
    assert summary["probability"]["total"]["n"] == 0
    assert summary["accounts"]["1.05"]["A"]["bets"] == 0
    assert summary["audit"]["paper_publication"]["excluded_tickets"] > 0
    assert (archive / "scoreboard/scored.jsonl").read_bytes() == score_bytes
    assert (archive / "scoreboard/ledger.jsonl").read_bytes() == ledger_bytes
    board = (archive / "scoreboard/scoreboard.md").read_text()
    assert "发布不早于开球而排除：" in board
    assert "发布时间未知而排除：0 条记录" in board
    digest = generate(archive, tmp_path / "digest", now=NOW).read_text()
    assert "已结 0 场、猜对 0 场、猜错 0 场" in digest


def test_legacy_unknown_odds_time_remains_readable_during_paper_reselection():
    from zucai.scoreboard.paper import advance_ledger, publication_view

    rows, results = samples()
    rows = [r for r in rows if r["match_id"] == "jc:one" and r["batch_id"] in {"early", "late"}]
    original = advance_ledger(rows, results, [], NOW)
    rows[0]["published_utc"] = rows[0]["kickoff_utc"]
    rows[1]["market"]["snapshot_utc"] = None
    view, audit = publication_view(rows, results, original, NOW)
    assert audit["reselected_in_memory"]
    assert not any(r["record_type"] == "ticket" for r in view)
    assert any(r.get("reason") == "no_frozen_sp" for r in view)


@pytest.mark.parametrize("published", ["2026-11-01T01:00:00Z", "9999-12-31T23:59:59-01:00"])
def test_new_receipts_do_not_use_git_second_tolerance_or_overflow(scoreboard_archive, published):
    from zucai.forecast_publication import publication_times

    manifest = json.loads((scoreboard_archive / "forecast_manifest.json").read_bytes())
    entry = manifest["runs"]["early"]["forecasts"]["2026-11-01"]
    entry["generated_utc"] = "2026-11-01T01:00:00.900000Z"
    entry["published_utc"] = entry["publication_evidence"]["published_utc"] = published
    assert entry["path"] + "/forecast.json" not in publication_times(scoreboard_archive, manifest)


def test_legacy_exclusion_still_persists_new_future_tickets_once(scoreboard_archive, tmp_path):
    from forecast_publication_support import confirm_fixture
    from zucai.forecast_archive import publish_forecasts
    from zucai.report.render import write_report
    from zucai.scoreboard.command import read_state, run

    archive = scoreboard_archive
    run(archive, as_of=NOW)
    original_raw = (archive / "scoreboard/ledger.jsonl").read_bytes()
    path = archive / "forecast_manifest.json"
    manifest = json.loads(path.read_bytes())
    for info in manifest["runs"].values():
        for entry in info["forecasts"].values():
            entry["published_utc"] = "2026-11-09T00:00:00Z"
            entry["publication_evidence"]["published_utc"] = entry["published_utc"]
    path.write_text(json.dumps(manifest))
    source = manifest["runs"]["late"]
    info = deepcopy(source)
    info["run_id"] = "future"
    info["generated_utc"] = "2026-11-02T06:00:00Z"
    info["forecasts"] = {}
    stage = tmp_path / "future-stage"
    for league in info["leagues"].values():
        for field in ("model_path", "training_json_path"):
            old = league[field]
            league[field] = old.replace("/late/", "/future/")
            target = stage / league[field]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((archive / old).read_bytes())
    for day in ("2026-11-03", "2026-11-04"):
        batch = json.loads(
            (archive / source["forecasts"]["2026-11-01"]["path"] / "forecast.json").read_bytes()
        )
        batch.update(
            batch_id="future-" + day,
            generated_utc=info["generated_utc"],
            as_of_utc=info["generated_utc"],
        )
        row = dict(
            batch["predictions"][0], match_id="jc:new-future", kickoff_utc="2026-11-03T12:00:00Z"
        )
        batch["predictions"] = [row] if day == "2026-11-03" else []
        directory = "forecasts/" + day + "/future"
        write_report(batch, stage / directory)
        info["forecasts"][day] = dict(
            path=directory, batch_id=batch["batch_id"], generated_utc=batch["generated_utc"]
        )
    publish_forecasts(archive, stage, info)
    confirm_fixture(archive)
    first = run(archive, as_of=instant("2026-11-02T08:00:00Z"))
    persisted = (archive / "scoreboard/ledger.jsonl").read_bytes()
    assert persisted.startswith(original_raw) and len(persisted) > len(original_raw)
    future = [
        r
        for r in read_state(archive)[1]
        if r["record_type"] == "ticket" and r["batch_id"].startswith("future-")
    ]
    assert future and all(r["cash_before"] == 10000 for r in future)
    second = run(archive, as_of=instant("2026-11-02T09:00:00Z"))
    assert first["accounts"] == second["accounts"]
    assert (archive / "scoreboard/ledger.jsonl").read_bytes() == persisted
    assert len({r["record_id"] for r in read_state(archive)[1]}) == len(read_state(archive)[1])
