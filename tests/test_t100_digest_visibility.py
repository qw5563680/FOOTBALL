"""The daily experiment row and score-only comparison use separate views."""
# ruff: noqa: F811

import json
from datetime import timedelta

from forecast_publication_support import confirm_fixture
from scoreboard_summary_support import assert_same_summary
from test_parlay_single_double import sample  # noqa: F401
from test_scoreboard_command import scoreboard_archive  # noqa: F401
from test_scoreboard_score import NOW
from zucai.daily_digest import generate
from zucai.forecast_archive import REPORT_FILES, attach_experiments
from zucai.parlay.single_double import build
from zucai.predict.experiments.global_rating_contract import (
    RATING_NAME,
    RATING_RUN_KEY,
    RATING_VERSION,
)
from zucai.predict.experiments.parallel import AVAILABILITY_MODES, empty_row
from zucai.scoreboard.command import run


def _attach_rating(archive):
    manifest_path = archive / "forecast_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    batch_run = manifest["runs"]["late"]
    first_forecast = batch_run["forecasts"]["2026-11-01"]
    first_batch = json.loads((archive / first_forecast["path"] / "forecast.json").read_bytes())
    cutoff = first_batch["models"][0]["model_as_of_utc"]
    batch_run.update({RATING_RUN_KEY: RATING_VERSION, "training_cutoff_utc": cutoff})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    documents, frozen = {}, {}
    for day, forecast in batch_run["forecasts"].items():
        directory = archive / forecast["path"]
        frozen.update(
            {directory / filename: (directory / filename).read_bytes() for filename in REPORT_FILES}
        )
        batch = json.loads((directory / "forecast.json").read_bytes())
        rows = []
        for target in batch["predictions"]:
            row = empty_row(target, RATING_NAME, cutoff, "missing_team_rating")
            row.update(model_version=RATING_VERSION, paper_eligible=False)
            if target["match_id"] == "jc:one":
                row.update(
                    status="predicted",
                    model_probabilities=dict(h=0.55, d=0.25, a=0.2),
                    fallback_reason=None,
                )
            else:
                row["missing_details"] = [dict(side="home", team=target["home"], team_id=1)]
            rows.append(row)
        documents[day] = dict(
            schema_version=1,
            batch_id=batch["batch_id"],
            as_of_utc=cutoff,
            generated_utc=batch["generated_utc"],
            mode=batch["mode"],
            availability_mode="strict",
            **AVAILABILITY_MODES,
            predictions=rows,
        )
    attach_experiments(archive, documents, "late")
    confirm_fixture(archive, only_unpublished=True)
    return frozen


def _section(text):
    return text.split("## 实验模型\n", 1)[1].split("\n## ", 1)[0]


def test_daily_shows_only_the_rating_experiment_and_keeps_old_version(scoreboard_archive):
    archive = scoreboard_archive
    match_day = NOW - timedelta(hours=8, minutes=1)
    output = archive / "daily"
    before = run(archive, as_of=match_day)
    legacy = generate(archive, output, now=match_day, run_id="old-eloless")
    old_versions = {
        path: path.read_bytes() for path in (output / legacy.stem).glob("old-eloless.*")
    }
    assert len(old_versions) == 2
    assert RATING_NAME not in _section(legacy.read_text(encoding="utf-8"))

    ledger = (archive / "scoreboard/ledger.jsonl").read_bytes()
    formal = _attach_rating(archive)
    after = run(archive, as_of=match_day)
    assert_same_summary(after["probability"], before["probability"])
    assert (archive / "scoreboard/ledger.jsonl").read_bytes() == ledger
    text = generate(archive, output, now=match_day, run_id="first-elo").read_text(encoding="utf-8")
    rating_rows = [line for line in _section(text).splitlines() if RATING_NAME in line]
    assert len(rating_rows) == 1
    assert "P2 实验" in rating_rows[0]
    assert "2026-11-01：1 / 7" in rating_rows[0]
    assert "球队缺少评分 7" in rating_rows[0]
    assert "各半平均" not in text

    run(archive, as_of=NOW)
    scoreboard = (archive / "scoreboard/scoreboard.md").read_text(encoding="utf-8")
    assert "各半平均" in scoreboard and "对照，不是一条实验线" in scoreboard
    settled = generate(archive, output, now=NOW, run_id="settled-elo").read_text(encoding="utf-8")
    assert RATING_NAME in _section(settled) and "各半平均" not in settled
    assert all(path.read_bytes() == raw for path, raw in old_versions.items())
    assert all(path.read_bytes() == raw for path, raw in formal.items())


def test_rating_cannot_supply_probabilities_to_paper_disagreement_or_reference_board(sample):
    first, second, *_ = sample["batch"]["predictions"]
    old_experiment = dict(first, model_name="dc-sot-v1")
    rating = dict(
        second,
        model_name=RATING_NAME,
        model_version=RATING_VERSION,
        model_probabilities=dict(h=0.05, d=0.05, a=0.9),
        paper_eligible=False,
    )
    attachment = dict(
        batch_id=sample["batch"]["batch_id"],
        as_of_utc=sample["batch"]["as_of_utc"],
        generated_utc=sample["frozen_utc"],
        predictions=[old_experiment],
    )
    legacy = build(**sample, experimental=attachment)
    rated = build(**sample, experimental=dict(attachment, predictions=[old_experiment, rating]))
    assert rated == legacy
    assert all(board["model_name"] != RATING_NAME for board in rated["experimental_boards"])
