"""Frozen-batch replays for the A–E paper accounts, without sports requests."""

import hashlib
import json
from copy import deepcopy
from datetime import date, timedelta

import pytest

from forecast_publication_support import confirm_fixture
from test_scoreboard_score import FIXTURES, samples
from zucai.cloud_feeds import export_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.forecast_archive import publish_forecasts
from zucai.models import Match, Result, Team
from zucai.report.render import write_report
from zucai.scoreboard.command import run
from zucai.scoreboard.score import record_key

DAY = date(2026, 11, 1)
EARLY_FREEZE = "2026-10-30T12:00:00Z"
EARLY_VIEW = "2026-10-30T13:00:00Z"
FULL_FREEZE = "2026-11-01T09:00:00Z"
FULL_VIEW = "2026-11-01T10:00:00Z"
AFTER_RESULTS = "2026-11-02T00:00:00Z"
LEDGER = "scoreboard/ledger.jsonl"
EARLY_ID = f"early-{DAY}"
FULL_ID = f"full-{DAY}"
MATCHES = ("jc:one", "jc:two", "jc:three")


@pytest.fixture
def new_archive(tmp_path):
    database = tmp_path / "seed.db"
    engine = get_engine(database)
    init_db(engine)
    rows, results = samples()
    rows = {r["match_id"]: r for r in rows}
    with session_scope(engine) as session:
        for key in MATCHES:
            row, result = rows[key], results[key]
            home, away = Team(name=row["home"]), Team(name=row["away"])
            session.add_all([home, away])
            session.flush()
            session.add(
                Match(
                    match_id=key,
                    source="jc",
                    league=row["league"],
                    home_team_id=home.id,
                    away_team_id=away.id,
                    kickoff_utc=row["kickoff_utc"],
                    status="finished",
                )
            )
            session.flush()
            session.add(
                Result(
                    **{
                        k: result[k]
                        for k in (
                            "match_id",
                            "home_goals",
                            "away_goals",
                            "available_utc",
                            "available_source",
                        )
                    }
                )
            )
    engine.dispose()

    def create(name):
        archive = tmp_path / name
        export_feeds(database, archive, DAY, "seed")
        return archive

    return create


def _freeze(archive, name, *, scope=None, without_prices=False):
    stage = archive.parent / f"{archive.name}-stage" / name
    batch = json.loads((FIXTURES / "forecasts.json").read_bytes())[1]
    league = batch["models"][0]["league"]
    model_path = f"models/{name}/{league}.pkl"
    train_path = f"models/{name}/{league}.train.json"
    (stage / model_path).parent.mkdir(parents=True)
    raw = f"synthetic artifact: {name}".encode()
    (stage / model_path).write_bytes(raw)
    (stage / train_path).write_text("{}\n")
    sha = hashlib.sha256(raw).hexdigest()
    database_sha = "b" * 64
    batch["models"][0].update(
        model_version=sha,
        model_as_of_utc="2026-10-30T10:00:00Z",
        last_available_utc="2026-10-30T09:00:00Z",
    )
    selected = (*MATCHES, "jc:nosp") if name == "full" else MATCHES
    rows = [r for r in batch["predictions"] if r["match_id"] in selected]
    for row in rows:
        row["model_version"] = sha
        if without_prices:
            row["market"] = None
        if row["market"]:
            row["market"]["snapshot_utc"] = "2026-10-30T11:00:00Z"
        if scope == "sfc_only":
            row["sfc_only"] = True
    batch_start = DAY if name == "full" else DAY - timedelta(days=2)
    days = (batch_start, batch_start + timedelta(days=1))
    extra = DAY not in days
    forecasts = {}
    stamp = FULL_FREEZE if name == "full" else EARLY_FREEZE
    for day in (*days, DAY) if extra else days:
        directory = f"forecasts/{day}/{name}"
        inputs = dict(batch["input"], kickoff_date_beijing=str(day))
        if day == DAY and scope:
            inputs["scope"] = scope
            if scope == "jc_spillover":
                inputs["jc_scope_match_ids"] = [r["match_id"] for r in rows]
        frozen = dict(
            batch,
            batch_id=f"{name}-{day}",
            generated_utc=stamp,
            as_of_utc=stamp,
            input=inputs,
            predictions=deepcopy(rows) if day == DAY else [],
        )
        write_report(frozen, stage / directory)
        forecasts[str(day)] = dict(path=directory, batch_id=frozen["batch_id"], generated_utc=stamp)
    run = dict(
        run_id=name,
        generated_utc=stamp,
        database_sha256=database_sha,
        leagues={
            league: dict(
                status="trained",
                model_path=model_path,
                training_json_path=train_path,
                sha256=sha,
                training_database_sha256=database_sha,
            )
        },
        forecasts=forecasts,
    )
    if extra:
        dates_key = "sfc_extra_dates" if scope == "sfc_only" else "jc_extra_dates"
        run[dates_key] = [str(DAY)]
    publish_forecasts(archive, stage, run)
    confirm_fixture(archive, only_unpublished=True)


@pytest.mark.parametrize(
    "scope",
    ["jc_spillover", None, "sfc_only"],
    ids=["spillover", "run-marker-only", "sfc-only"],
)
def test_partial_batch_stays_in_scores_outside_accounts(new_archive, scope):
    archive = new_archive("partial")
    _freeze(archive, "early", scope=scope)
    run(archive, as_of=EARLY_VIEW)
    assert (archive / LEDGER).read_bytes() == b""
    run(archive, as_of=AFTER_RESULTS)
    assert (archive / LEDGER).read_bytes() == b""
    scored = [
        json.loads(line) for line in (archive / "scoreboard/scored.jsonl").read_bytes().splitlines()
    ]
    assert {r["match_id"] for r in scored} == set(MATCHES)
    assert {r["batch_id"] for r in scored} == {EARLY_ID}


def test_full_batch_gets_same_tickets_with_or_without_prior_spillover(new_archive):
    sequential, full_only = new_archive("sequential"), new_archive("full-only")
    _freeze(sequential, "early", scope="jc_spillover")
    run(sequential, as_of=EARLY_VIEW)
    for archive in (sequential, full_only):
        _freeze(archive, "full")
        run(archive, as_of=FULL_VIEW)

    reference = (full_only / LEDGER).read_bytes()
    assert (sequential / LEDGER).read_bytes() == reference
    ledger = [json.loads(line) for line in reference.splitlines()]
    assert {r["batch_id"] for r in ledger} == {FULL_ID}
    daily_tickets = {
        (r["account"], len(r["legs"]))
        for r in ledger
        if r["record_type"] == "ticket" and r["account"] in {"D", "E"} and r["threshold"] == "1.05"
    }
    assert daily_tickets == {("D", 2), ("E", 3)}
    print(f"D/E 均来自 {FULL_ID}，两个账本各 {len(ledger)} 行逐字节一致")


def test_existing_spillover_skip_and_marker_bytes_survive_full_batch(new_archive):
    archive = new_archive("history")
    _freeze(archive, "early", scope="jc_spillover", without_prices=True)
    summary = run(archive, as_of=EARLY_VIEW)
    skip_scope = "jc:one"
    reason = "no_frozen_sp"
    legacy = [
        dict(
            record_type="skip",
            record_id=record_key("skip", "A", "1.05", EARLY_ID, skip_scope, reason),
            account="A",
            threshold="1.05",
            batch_id=EARLY_ID,
            scope=skip_scope,
            reason=reason,
        ),
        dict(
            record_type="batch",
            record_id=record_key("batch", EARLY_ID),
            batch_id=EARLY_ID,
            decision_utc="2026-10-30T12:00:00+00:00",
            reason="processed",
        ),
    ]
    historical = b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode() for row in legacy
    )
    (archive / LEDGER).write_bytes(historical)
    summary["files"]["ledger.jsonl"] = hashlib.sha256(historical).hexdigest()
    (archive / "scoreboard/summary.json").write_text(json.dumps(summary))

    _freeze(archive, "full")
    run(archive, as_of=FULL_VIEW)
    combined = (archive / LEDGER).read_bytes()
    assert combined.startswith(historical)
    appended = [json.loads(line) for line in combined[len(historical) :].splitlines()]
    assert {r["batch_id"] for r in appended} == {FULL_ID}
    print(f"已存跨天 skip / batch 两行逐字节保留，完整批次追加 {len(appended)} 行")
