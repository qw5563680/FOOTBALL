"""Real digest publication gates with pending scores and legacy disclosure joins."""

import hashlib
import json
import shutil
from copy import deepcopy

from forecast_publication_support import confirm_fixture
from test_scoreboard_command import files
from test_scoreboard_command import scoreboard_archive as scoreboard_archive
from test_scoreboard_score import NOW
from zucai.forecast_archive import publish_forecasts, verify_forecasts
from zucai.report.render import write_report
from zucai.scoreboard.command import _jsonl, run
from zucai.scoreboard.score import load_forecasts, published_score_keys, summarize_scores

EMPTY_LEAGUE = "无样本联赛"


def _write_manifest(archive, manifest):
    (archive / "forecast_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))


def _prepare_metadata(archive):
    manifest = verify_forecasts(archive)
    for info in manifest["runs"].values():
        for entry in info["forecasts"].values():
            directory = archive / entry["path"]
            batch = json.loads((directory / "forecast.json").read_bytes())
            for row in batch["predictions"]:
                if row["match_id"] in {"jc:one", "jc:two"}:
                    used = row["match_id"] == "jc:one"
                    row["metadata"] = dict(
                        unknown_team_prior=used,
                        home_unknown_team=False,
                        away_unknown_team=used,
                        prior_method="weakest_quartile_half_shrinkage" if used else None,
                    )
            shutil.rmtree(directory)  # Rebuild this synthetic fixture before any scoring.
            write_report(batch, directory)
            for path in directory.iterdir():
                name = path.relative_to(archive).as_posix()
                manifest["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    training = "models/after-kickoff/无样本联赛.train.json"
    (archive / training).write_text("{}")
    manifest["files"][training] = hashlib.sha256(b"{}").hexdigest()
    manifest["runs"]["after-kickoff"]["leagues"][EMPTY_LEAGUE] = dict(
        status="failed", training_json_path=training, p2_label="P2 实验"
    )
    _write_manifest(archive, manifest)
    confirm_fixture(archive)


def _add_single_match_batch(archive, stage):
    original = verify_forecasts(archive)["runs"]["late"]
    info = deepcopy(original)
    info.update(run_id="targeted", generated_utc="2026-11-01T10:00:00Z", forecasts={})
    for league in info["leagues"].values():
        for field in ("model_path", "training_json_path"):
            source = league[field]
            league[field] = source.replace("/late/", "/targeted/")
            target = stage / league[field]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((archive / source).read_bytes())
    source = original["forecasts"]["2026-11-01"]["path"]
    for day in ("2026-11-01", "2026-11-02"):
        batch = json.loads((archive / source / "forecast.json").read_bytes())
        batch.update(
            batch_id="targeted-" + day,
            as_of_utc=info["generated_utc"],
            generated_utc=info["generated_utc"],
        )
        batch["predictions"] = (
            [row for row in batch["predictions"] if row["match_id"] == "jc:two"]
            if day == "2026-11-01"
            else []
        )
        directory = "forecasts/" + day + "/targeted"
        write_report(batch, stage / directory)
        info["forecasts"][day] = dict(
            path=directory, batch_id=batch["batch_id"], generated_utc=batch["generated_utc"]
        )
    publish_forecasts(archive, stage, info)
    confirm_fixture(archive)


def _legacy_scores(archive):
    summary = run(archive, as_of=NOW)
    path = archive / "scoreboard/scored.jsonl"
    scored = [json.loads(line) for line in path.read_bytes().splitlines()]
    for row in scored:
        row.pop("prior_disclosure", None)
    path.write_bytes(_jsonl(scored))
    summary["files"]["scored.jsonl"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (archive / "scoreboard/summary.json").write_text(json.dumps(summary, ensure_ascii=False))
    assert len(summary["audit"]["active_keys"]) > len(scored)
    assert summary["probability"]["prior_strata"]["default_strength"]["n"] == 1
    assert summary["probability"]["leagues"][EMPTY_LEAGUE]["n"] == 0
    return scored, summary


def _generate_and_observe(archive, out, monkeypatch):
    from zucai import daily_digest

    captured = []
    original = daily_digest._settlement_sections

    def observe(scored, summary, *args, **kwargs):
        # Observe the real renderer input; selection, publication lookup and scoring
        # remain unmocked, and the original renderer still generates every line.
        captured.append(deepcopy(summary))
        return original(scored, summary, *args, **kwargs)

    monkeypatch.setattr(daily_digest, "_settlement_sections", observe)
    text = daily_digest.generate(archive, out, now=NOW).read_text()
    assert len(captured) == 1
    return text, captured[0]


def test_pending_keys_do_not_recompute_or_erase_probability_summary(
    scoreboard_archive, tmp_path, monkeypatch
):
    archive = scoreboard_archive
    _prepare_metadata(archive)
    _, summary = _legacy_scores(archive)
    before = files(archive)
    text, viewed = _generate_and_observe(archive, tmp_path / "digest", monkeypatch)
    assert viewed["probability"] == summary["probability"]
    assert viewed["audit"]["active_keys"] == summary["audit"]["active_keys"]
    assert f"{EMPTY_LEAGUE}：前瞻样本 0 / 200；证据不足。" in text
    assert files(archive) == before


def test_actual_score_exclusion_keeps_empty_leagues_and_frozen_prior_strata(
    scoreboard_archive, tmp_path, monkeypatch
):
    archive = scoreboard_archive
    _prepare_metadata(archive)
    _add_single_match_batch(archive, tmp_path / "targeted-stage")
    scored, old_summary = _legacy_scores(archive)
    manifest = verify_forecasts(archive)
    entry = manifest["runs"]["targeted"]["forecasts"]["2026-11-01"]
    entry.pop("publication_evidence")
    entry["published_utc"] = None
    _write_manifest(archive, manifest)
    forecasts = load_forecasts(archive, NOW)
    expected_keys = set(old_summary["audit"]["active_keys"]) & published_score_keys(
        scored, forecasts, NOW
    )
    expected = summarize_scores(scored, NOW, active_keys=expected_keys, forecast_rows=forecasts)
    assert expected["total"]["n"] == 3
    assert expected["prior_strata"]["default_strength"]["n"] == 1
    before = files(archive)
    text, viewed = _generate_and_observe(archive, tmp_path / "digest", monkeypatch)
    actual = viewed["probability"]
    assert actual["total"] == expected["total"]
    assert actual["prior_strata"] == expected["prior_strata"]
    assert actual["model_leagues"] == expected["model_leagues"]
    assert actual["leagues"][EMPTY_LEAGUE] == old_summary["probability"]["leagues"][EMPTY_LEAGUE]
    assert f"{EMPTY_LEAGUE}：前瞻样本 0 / 200；证据不足。" in text
    assert files(archive) == before
