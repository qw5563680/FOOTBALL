"""Real freeze/scoreboard/digest integration, synthetic on-disk archives only."""

import copy
import csv
import hashlib
import json
from datetime import date, datetime

import pytest

from test_parlay_paper import AFTER, BEFORE, results
from test_parlay_single_double import FIX
from zucai.cloud_feeds import export_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.forecast_archive import attach_experiments, publish_forecasts, verify_forecasts
from zucai.models import Match, Result, Team
from zucai.parlay.archive import freeze, load_documents, publish_document, raw_json
from zucai.parlay.paper import LEDGER, advance_ledger, summarize
from zucai.report.render import write_report
from zucai.scoreboard.score import instant

DAY = "2026-11-06"
RUN = "offline-t45"
STAMP = "2026-11-06T02:01:00+00:00"


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def archive_setup(tmp_path):
    sample = json.loads((FIX / "input.json").read_bytes())
    database, archive, stage = tmp_path / "data/feeds.db", tmp_path / "archive", tmp_path / "stage"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        for row in sample["identities"].values():
            for side in ("home", "away"):
                session.add(Team(id=row[side + "_team_id"], name=row[side]))
            session.flush()
            session.add(Match(**{k: v for k, v in row.items() if k not in {"home", "away"}}))
    engine.dispose()
    audit = database.parent / "state/audit/jc_catalog/2026-11.csv"
    audit.parent.mkdir(parents=True)
    with audit.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(sample["catalog"][0]))
        writer.writeheader()
        writer.writerows(sample["catalog"])
    export_feeds(database, archive, date(2026, 11, 6), RUN)
    model = f"models/{RUN}/英超.pkl"
    train = f"models/{RUN}/英超.train.json"
    (stage / model).parent.mkdir(parents=True)
    (stage / model).write_bytes(b"t45-offline-model")
    (stage / train).write_text("{}")
    run = dict(
        run_id=RUN,
        generated_utc=sample["batch"]["generated_utc"],
        database_sha256="b" * 64,
        leagues={
            "英超": dict(
                status="trained",
                model_path=model,
                training_json_path=train,
                sha256=hashlib.sha256(b"t45-offline-model").hexdigest(),
                training_database_sha256="b" * 64,
            )
        },
        forecasts={},
    )
    for day in (DAY, "2026-11-07"):
        batch = copy.deepcopy(sample["batch"])
        if day != DAY:
            batch["predictions"] = []
            batch["batch_id"] += "-empty"
        batch["input"]["kickoff_date_beijing"] = day
        path = f"forecasts/{day}/{RUN}"
        write_report(batch, stage / path)
        run["forecasts"][day] = dict(
            path=path, batch_id=batch["batch_id"], generated_utc=batch["generated_utc"]
        )
    publish_forecasts(archive, stage, run)
    attach_experiments(
        archive,
        {
            DAY: dict(
                batch_id=sample["batch"]["batch_id"],
                predictions=[],
                generated_utc=STAMP,
                as_of_utc=sample["batch"]["as_of_utc"],
            )
        },
        RUN,
    )
    return archive, database, sample


def frozen_doc(archive_setup):
    archive, db, sample = archive_setup
    run = freeze(archive, db, RUN, injuries=sample["injuries"], clock=lambda: instant(STAMP))
    manifest = verify_forecasts(archive)
    doc = load_documents(archive, manifest, BEFORE)[0]
    return run, doc


def install_ledger(archive, document):
    ledger = advance_ledger([document], {}, [], BEFORE)
    target = archive / LEDGER
    target.parent.mkdir(exist_ok=True)
    raw = b"".join(raw_json(row).replace(b"\n", b" ").strip() + b"\n" for row in ledger)
    target.write_bytes(raw)
    summary = summarize(ledger, {}, BEFORE)
    summary["ledger_sha256"] = hashlib.sha256(raw).hexdigest()
    (target.parent / "summary.json").write_bytes(raw_json({"parlay_sd": summary}))
    return ledger


def test_append_manifest_and_original_quartet_and_experiments_never_change(archive_setup):
    archive, db, sample = archive_setup
    before = files(archive)
    run, doc = frozen_doc(archive_setup)
    assert len(doc["groups"]) == 3
    manifest = verify_forecasts(archive)
    name = run["forecasts"][DAY]["parlay_path"]
    assert manifest["files"][name] == hashlib.sha256((archive / name).read_bytes()).hexdigest()
    assert all(
        (archive / p).read_bytes() == raw
        for p, raw in before.items()
        if p != "forecast_manifest.json"
    )
    frozen = files(archive)
    freeze(archive, db, RUN, injuries=sample["injuries"], clock=lambda: instant(STAMP))
    assert files(archive) == frozen
    assert load_documents(archive, manifest, "2026-11-06T02:00:59Z") == []
    (archive / name).write_bytes((archive / name).read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA256"):
        verify_forecasts(archive)


def test_revision_preserved_and_ledger_batch_locks_new_selection(archive_setup):
    archive, db, sample = archive_setup
    _, original = frozen_doc(archive_setup)
    old = (archive / original["path"]).read_bytes()
    sample["injuries"] = {}
    changed = freeze(archive, db, RUN, injuries={}, clock=lambda: instant(BEFORE))
    entry = changed["forecasts"][DAY]
    revised = next(
        d for d in load_documents(archive, verify_forecasts(archive), BEFORE) if d["day"] == DAY
    )
    assert revised["groups"] == []
    assert len(revised["replaced_groups"]) == 3
    assert (archive / entry["parlay_versions"][0]).read_bytes() == old
    assert len(entry["parlay_versions"]) == 2
    install_ledger(archive, revised)
    before = files(archive)
    fresh = json.loads((FIX / "input.json").read_bytes())
    freeze(archive, db, RUN, injuries=fresh["injuries"], clock=lambda: instant(BEFORE))
    assert files(archive) == before


def test_archive_failure_rolls_back_and_kickoff_boundary_refuses(archive_setup, monkeypatch):
    from zucai.parlay import archive as module

    archive, _, _ = archive_setup
    _, doc = frozen_doc(archive_setup)
    doc["input_sha256"] = "changed"
    before = files(archive)
    with pytest.raises(ValueError, match="开球"):
        publish_document(archive, RUN, DAY, doc, clock=lambda: instant("2026-11-06T15:00:00Z"))
    assert files(archive) == before
    original = module._atomic_write

    def fail(path, raw):
        original(path, raw)
        if path.name == "forecast_manifest.json":
            raise OSError("offline publication failure")

    monkeypatch.setattr(module, "_atomic_write", fail)
    with pytest.raises(OSError, match="offline"):
        publish_document(archive, RUN, DAY, doc, clock=lambda: instant(BEFORE))
    assert files(archive) == before


def test_scoreboard_ae_bytes_preserved_f_hash_checked_and_repeat_stable(archive_setup, monkeypatch):
    from zucai.scoreboard import command

    archive, db, _ = archive_setup

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant(AFTER)

    monkeypatch.setattr(command, "datetime", Clock)
    command.run(archive, as_of=BEFORE)
    original = {
        name: (archive / "scoreboard" / name).read_bytes()
        for name in ("scored.jsonl", "ledger.jsonl")
    }
    frozen_doc(archive_setup)
    summary = command.run(archive, as_of=BEFORE)
    assert all(
        (archive / "scoreboard" / name).read_bytes() == raw for name, raw in original.items()
    )
    assert summary["parlay_sd"]["totals"]["groups"] == 3
    before = (archive / LEDGER).read_bytes()
    command.run(archive, as_of=BEFORE)
    assert (archive / LEDGER).read_bytes() == before
    engine = get_engine(db)
    with session_scope(engine) as session:
        for result in results().values():
            session.get(Match, result["match_id"]).status = "finished"
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
    export_feeds(db, archive, date(2026, 11, 6), "settlement")
    summary = command.run(archive, as_of=AFTER)
    assert summary["parlay_sd"]["totals"]["settled_groups"] == 3
    before = (archive / LEDGER).read_bytes()
    command.run(archive, as_of=AFTER)
    assert (archive / LEDGER).read_bytes() == before
    (archive / LEDGER).write_bytes(before + b"{}\n")
    with pytest.raises(ValueError, match="F 账本 SHA256"):
        command.run(archive, as_of=AFTER)


def test_daily_section_uses_frozen_numbers_and_filters_started_groups(archive_setup, tmp_path):
    from zucai.daily_digest import generate

    archive, _, _ = archive_setup
    _, doc = frozen_doc(archive_setup)
    before = files(archive)
    report = generate(archive, tmp_path / "daily", now=instant(BEFORE), run_id="first").read_text()
    assert files(archive) == before
    assert "二串一候选（单选＋双选，纸面）" in report
    assert "本版合格主候选 3 组" in report
    assert "51.00%" in report and "15.00%" in report
    assert "覆盖最高" in report and "盈利概率最高" in report
    assert "市场参考榜" in report
    assert "第1注中" in report and "整组净收益" in report
    after = generate(
        archive, tmp_path / "daily", now=instant("2026-11-06T15:05:00Z"), run_id="second"
    ).read_text()
    assert "本版合格主候选 0 组" in after
    assert "移出当前候选" in after
    assert "### 球队" not in after
    assert doc["groups"]  # Presentation filter did not mutate the freeze.


def test_daily_f_previous_pending_and_covered_loss_are_distinct(archive_setup):
    from zucai.digest_parlay import section

    archive, _, _ = archive_setup
    _, doc = frozen_doc(archive_setup)
    ledger = install_ledger(archive, doc)
    summary = json.loads((archive / "scoreboard/summary.json").read_bytes())
    manifest = verify_forecasts(archive)
    first, pending, _ = section(archive, manifest, summary, BEFORE, None, matches={})
    assert "发报时未开球 3 组" in "\n".join(first)
    ledger = advance_ledger([doc], results(), ledger, AFTER)
    target = archive / LEDGER
    raw = b"".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True).encode() + b"\n" for r in ledger
    )
    target.write_bytes(raw)
    stats = summarize(ledger, results(), AFTER)
    stats["ledger_sha256"] = hashlib.sha256(raw).hexdigest()
    summary = {"parlay_sd": stats}
    last, pending_now, _ = section(
        archive, manifest, summary, AFTER, None, matches={}, previous={"parlay_pending": pending}
    )
    assert not pending_now
    text = "\n".join(last)
    assert "F 较上一版：原待结 3 组，本版补结 3 组" in text
    assert "亏损" in text and "组覆盖率" in text and "组盈利率" in text


def test_ledger_and_summary_publication_roll_back_together(archive_setup, monkeypatch):
    from pathlib import Path

    from zucai.scoreboard import command

    archive, _, _ = archive_setup
    frozen_doc(archive_setup)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant(AFTER)

    monkeypatch.setattr(command, "datetime", Clock)
    command.run(archive, as_of=BEFORE)
    before = files(archive)
    replace = Path.replace

    def fail(source, target):
        if target.name == "parlay_sd_ledger.jsonl":
            raise OSError("offline F ledger write failure")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="offline F"):
        command.run(archive, as_of=BEFORE)
    assert files(archive) == before


def test_latest_forecast_without_parlay_cannot_fall_back_to_earlier_candidates(
    archive_setup, tmp_path
):
    from zucai.daily_digest import generate

    archive, _, sample = archive_setup
    frozen_doc(archive_setup)
    manifest = verify_forecasts(archive)
    previous = manifest["runs"][RUN]
    run = copy.deepcopy(previous)
    run["run_id"] = "later-t45"
    run["generated_utc"] = "2026-11-06T02:10:00+00:00"
    stage = tmp_path / "later-stage"
    for item in run["leagues"].values():
        for field in ("model_path", "training_json_path"):
            old = item[field]
            item[field] = old.replace(RUN, run["run_id"])
            target = stage / item[field]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((archive / old).read_bytes())
    run["forecasts"] = {}
    for day in (DAY, "2026-11-07"):
        batch = copy.deepcopy(sample["batch"])
        batch.update(
            batch_id=f"later-{day}",
            generated_utc=run["generated_utc"],
            as_of_utc=run["generated_utc"],
        )
        batch["input"]["kickoff_date_beijing"] = day
        directory = f"forecasts/{day}/{run['run_id']}"
        write_report(batch, stage / directory)
        run["forecasts"][day] = dict(
            path=directory, batch_id=batch["batch_id"], generated_utc=batch["generated_utc"]
        )
    publish_forecasts(archive, stage, run)
    report = generate(archive, tmp_path / "daily", now=instant("2026-11-06T02:15:00Z")).read_text()
    assert "本版合格主候选 0 组" in report
    assert "### 球队" not in report


def test_daily_main_groups_do_not_repeat_teams_across_today_and_tomorrow(tmp_path, monkeypatch):
    from zucai import digest_parlay
    from zucai.parlay.single_double import build

    sample = json.loads((FIX / "input.json").read_bytes())
    sample["batch"]["predictions"] = sample["batch"]["predictions"][:4]
    original = build(**sample)
    first = dict(original, day=DAY, path="first.json", sha256="a" * 64)
    second = copy.deepcopy(first)
    second.update(day="2026-11-07", batch_id="tomorrow", path="second.json", sha256="b" * 64)
    for group in second["groups"]:
        for side in ("single", "double"):
            group[side]["kickoff_utc"] = "2026-11-07T12:00:00Z"
    documents = [first, second]
    before = raw_json(documents)
    monkeypatch.setattr(digest_parlay, "load_documents", lambda *a: documents)
    manifest = dict(
        runs={
            RUN: dict(
                forecasts={
                    d["day"]: dict(generated_utc=d["generated_utc"], batch_id=d["batch_id"])
                    for d in documents
                }
            )
        }
    )
    lines, _, _ = digest_parlay.section(tmp_path, manifest, {}, BEFORE, None, matches={})
    text = "\n".join(lines)
    assert text.count("| 单选 ") == 2
    assert "本版合格主候选 2 组" in text
    assert "跨今明批次重复比赛/球队或达到展示上限" in text
    assert raw_json(documents) == before
