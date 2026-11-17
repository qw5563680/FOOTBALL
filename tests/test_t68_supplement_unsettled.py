"""Real unsettled rows remain pending; cloud entry never certifies or loses them."""

import hashlib
import json
import re
import shutil
import sqlite3
import sys
from datetime import UTC, date, datetime

import pytest
import responses

from test_t68_daily_gate_entry import bundle  # noqa: F401
from test_t68_evidence_generation import _entry_archive
from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from test_t68_identity_archive_entry import seal, table
from test_t68_jc_supplement import (
    API_URL,
    FIXTURES,
    run_entry,
    seed_archive,
    sidecar_bytes,
)
from zucai.cloud_feeds import (
    STATE_FILE,
    TABLES,
    archive_info,
    main,
    restore_feeds,
    source_inventory,
)

DAY = "2026-11-17"
KEY = f"state/catalogue/supplement/jc/2026-11/{DAY}-p1.json"
RAW = FIXTURES / "unsettled-2026-11-17.json"
PENDING = {"jc:2042830", "jc:2042829"}


def prepare_supplement(tmp_path, monkeypatch, *, existing=False):
    archive = seed_archive(tmp_path)
    empty = (FIXTURES / "day-d-p1.json").read_bytes()
    # Fully settled days already exist, so only the real 11-17 page is requested.
    for number in range(1, 18 if existing else 17):
        day = f"2026-11-{number:02}"
        value = json.loads(empty)
        value["value"].update(matchResult=[], total=0, pages=0, pageNo=0)
        raw = RAW.read_bytes() if day == DAY else json.dumps(value).encode()
        path = archive / f"state/catalogue/supplement/jc/2026-11/{day}-p1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.with_suffix(".json.meta.json").write_bytes(
            sidecar_bytes(day, 1, raw, fetched="2026-11-17T17:06:30.803269Z")
        )
    seal(archive)
    monkeypatch.setattr("test_t68_jc_supplement.TEST_NOW", datetime(2026, 11, 18, 3, tzinfo=UTC))
    return archive


@responses.activate
def test_cloud_supplement_unsettled_day_is_not_archived(tmp_path, monkeypatch):
    archive = prepare_supplement(tmp_path, monkeypatch)
    responses.add(responses.GET, API_URL, body=RAW.read_bytes(), status=200)
    work, summary = run_entry(
        tmp_path, monkeypatch, archive, fetch=datetime(2026, 11, 18, 2, tzinfo=UTC)
    )
    meta = archive_info(archive)
    report = meta["collection"]["jc_supplement"]
    assert report["status"] == "partial"
    assert report["requests"] == 1
    assert "未结算" in report["days"]["failed"][DAY]
    assert DAY not in report["days"]["archived"]
    assert KEY not in meta["files"] and not (archive / KEY).exists()
    assert not (work / "data" / KEY).exists()
    assert "还差：2026-11-17" in summary.read_text()
    assert "未结算" in summary.read_text().split("还差：", 1)[1]


@responses.activate
def test_cloud_retries_existing_unsettled_and_preserves_old_originals(tmp_path, monkeypatch):
    archive = prepare_supplement(tmp_path, monkeypatch, existing=True)
    old_raw = (archive / KEY).read_bytes()
    old_meta = (archive / (KEY + ".meta.json")).read_bytes()
    # Synthetic settlement response only; no claim these are real final scores.
    settled = json.loads(old_raw)
    for row in settled["value"]["matchResult"]:
        if f"jc:{row['matchId']}" in PENDING:
            row.update(matchResultStatus="2", sectionsNo999="0:0", sectionsNo1="0:0")
    new_raw = json.dumps(settled, ensure_ascii=False).encode()
    responses.add(responses.GET, API_URL, body=new_raw, status=200)
    run_entry(tmp_path, monkeypatch, archive, fetch=datetime(2026, 11, 18, 2, tzinfo=UTC))
    report = archive_info(archive)["collection"]["jc_supplement"]
    assert report["status"] == "complete" and report["requests"] == 1
    assert DAY in report["days"]["archived"] and DAY not in report["days"]["skipped"]
    assert (archive / KEY).read_bytes() == new_raw
    backup = (
        archive / f"state/catalogue/supplement/superseded/jc-supp-offline/jc/2026-11/{DAY}-p1.json"
    )
    assert backup.read_bytes() == old_raw
    assert backup.with_suffix(".json.meta.json").read_bytes() == old_meta
    restored = tmp_path / "restored/data/feeds.db"
    restore_feeds(archive, restored)
    assert (restored.parent / backup.relative_to(archive)).read_bytes() == old_raw
    # A fresh command run must now skip 11-17 without any request.
    second = tmp_path / "second"
    second.mkdir()
    run_entry(second, monkeypatch, archive, fetch=datetime(2026, 11, 18, 2, tzinfo=UTC))
    assert archive_info(archive)["collection"]["jc_supplement"]["requests"] == 0
    assert len(responses.calls) == 1


@responses.activate
def test_cloud_retry_still_unsettled_keeps_existing_bytes(tmp_path, monkeypatch):
    archive = prepare_supplement(tmp_path, monkeypatch, existing=True)
    before = [(archive / (KEY + suffix)).read_bytes() for suffix in ("", ".meta.json")]
    responses.add(responses.GET, API_URL, body=RAW.read_bytes(), status=200)
    run_entry(tmp_path, monkeypatch, archive, fetch=datetime(2026, 11, 18, 2, tzinfo=UTC))
    report = archive_info(archive)["collection"]["jc_supplement"]
    assert report["status"] == "partial" and report["requests"] == 1
    assert [(archive / (KEY + suffix)).read_bytes() for suffix in ("", ".meta.json")] == before
    assert not (archive / "state/catalogue/supplement/superseded").exists()


@pytest.mark.parametrize("location", ["supplement", "census", "superseded"])
def test_cloud_evidence_entry_ignores_unsettled_stored_rows(
    bundle,  # noqa: F811
    tmp_path,
    monkeypatch,
    location,  # noqa: F811
):
    from zucai import identity_binding_archive as reader

    clean, _ = _entry_archive(bundle, tmp_path, monkeypatch)
    key = KEY
    if location == "census":
        from zucai import jc_catalogue_kickoffs as kickoffs

        folder = clean / "state/catalogue/catalogue"
        shutil.rmtree(folder / "jc")
        report = json.loads((folder / "jc.json").read_bytes())
        report["window"] = dict(start_beijing=DAY, end_beijing=DAY)
        (folder / "jc.json").write_text(json.dumps(report))
        monkeypatch.setattr(kickoffs, "CATALOGUE_WINDOW", (date(2026, 11, 17),) * 2)
        key = f"state/catalogue/catalogue/jc/2026-11/{DAY}-p1.json"
    elif location == "superseded":
        key = f"state/catalogue/supplement/superseded/older-run/jc/2026-11/{DAY}-p1.json"
    path = clean / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(RAW.read_bytes())
    path.with_suffix(".json.meta.json").write_bytes(
        sidecar_bytes(DAY, 1, RAW.read_bytes(), fetched="2026-11-17T17:06:30.803269Z")
    )
    manifest = json.loads((clean / "feed_manifest.json").read_bytes())
    manifest["created_utc"] = "2026-11-18T00:00:00Z"
    (clean / "feed_manifest.json").write_text(json.dumps(manifest))
    tables, _ = reader._tables(clean)
    matches = [dict(r) for r in tables["matches"]]
    matches.extend(
        dict(
            matches[0],
            match_id=mid,
            status="scheduled",
            result_provider=None,
            result_checked_utc=None,
            league="国际赛",
            kickoff_utc="2026-11-17T12:00:00Z",
            counterpart_id=None,
        )
        for mid in sorted(PENDING)
    )
    listed = {k for k in manifest["files"] if (clean / k).is_file()} | {key, key + ".meta.json"}
    table(clean, "matches", matches)
    manifest = json.loads((clean / "feed_manifest.json").read_bytes())
    manifest["files"] = {k: v for k, v in manifest["files"].items() if k in listed}
    with sqlite3.connect(":memory:") as db:
        for name, columns in TABLES.items():
            db.execute(f"CREATE TABLE {name} ({','.join(columns)})")
            path = clean / f"state/{name}.jsonl"
            for line in path.read_text().splitlines() if path.exists() else []:
                values = json.loads(line)
                db.execute(f"INSERT INTO {name} VALUES ({','.join('?' for _ in values)})", values)
        manifest["sources"] = source_inventory(db)
    (clean / "feed_manifest.json").write_text(json.dumps(manifest))
    captured = []
    original = reader.load_archive
    from zucai import identity_official_ids

    associate = identity_official_ids.associate_official

    def check_before_association(rows, *args, **kwargs):
        if location == "census":
            pending = [row for row in rows if row["match_id"] in PENDING]
            assert {row["match_id"] for row in pending} == PENDING
            assert all(row["status"] == "unverified" and not row["complete"] for row in pending)
            assert all(row["score"] is None for row in pending)
        return associate(rows, *args, **kwargs)

    monkeypatch.setattr(identity_official_ids, "associate_official", check_before_association)

    def read(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr(reader, "load_archive", read)
    main()
    assert captured
    assert not any(r["match_id"] in PENDING for facts in captured for r in facts["official"])
    issues = [i for f in captured for i in f["issues"] if i.get("match_id") in PENDING]
    if location == "superseded":
        assert not issues  # Retained originals must not re-enter current evidence.
        return
    assert {i["match_id"] for i in issues} == PENDING
    assert any("stored_official_result_unsettled" in i["reasons"] for i in issues)
    if location == "census":
        assert any("official_result_unverified" in i["reasons"] for i in issues)
    assert not any(
        "AttributeError" in p.read_text()
        for p in (clean / "state/identity_gate_evidence").rglob("*.json")
    )


@pytest.mark.parametrize("league,blocked", [("国际赛", True), ("欧国联", False)])
def test_recent_official_pending_is_scoped_and_incomplete(tmp_path, league, blocked):
    from zucai.identity_daily_gate import _recent_official

    path = tmp_path / KEY
    path.parent.mkdir(parents=True)
    path.write_bytes(RAW.read_bytes())
    path.with_suffix(".json.meta.json").write_bytes(
        sidecar_bytes(DAY, 1, RAW.read_bytes(), fetched="2026-11-17T17:06:30.803269Z")
    )
    facts = dict(
        catalogue_window=dict(start_beijing="2025-11-01", end_beijing="2026-10-31"),
        configs={"test": {"league": league}},
        official=[],
        matches=[],
        official_leagues={},
    )
    section = {
        "official_originals": [
            {"path": KEY, "sha256": hashlib.sha256(RAW.read_bytes()).hexdigest()}
        ]
    }
    if blocked:
        with pytest.raises(ValueError, match="该日官方页面仍有未结算比赛"):
            _recent_official(
                facts, "test", section, tmp_path, date(2026, 11, 17), date(2026, 11, 17)
            )
    else:
        assert (
            _recent_official(
                facts, "test", section, tmp_path, date(2026, 11, 17), date(2026, 11, 17)
            )["official_match_ids"]
            == []
        )


def test_cloud_opening_evidence_reports_unsettled_target_league(bundle, tmp_path, monkeypatch):  # noqa: F811
    """A test-only route uses the real international page; production registry is untouched."""
    from zucai import identity_binding_archive as reader
    from zucai import identity_gate_evidence_gen as generator
    from zucai import league_registry

    clean, _ = _entry_archive(bundle, tmp_path, monkeypatch)
    registry = league_registry.load_registry()
    registry["providers"]["apif"]["98"]["config"]["league"] = "国际赛"
    registry_path = tmp_path / "test-only-registry.json"
    registry_path.write_text(json.dumps(registry))
    monkeypatch.setattr(reader, "REGISTRY", registry_path)
    monkeypatch.setattr(league_registry, "load_registry", lambda *args, **kwargs: registry)
    monkeypatch.setattr(generator, "load_registry", lambda *args, **kwargs: registry)
    from zucai import identity_categories

    categories = json.loads(identity_categories.PATH.read_bytes())
    categories["competitions"]["98"]["league"] = "国际赛"
    identity_categories.PATH.write_text(json.dumps(categories))
    for number in (15, 16, 17):
        day = f"2026-11-{number}"
        value = json.loads(RAW.read_bytes())
        if number != 17:
            value["value"].update(matchResult=[], total=0, pages=0, pageNo=0)
        raw = json.dumps(value, ensure_ascii=False).encode()
        path = clean / f"state/catalogue/supplement/jc/2026-11/{day}-p1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.with_suffix(".json.meta.json").write_bytes(
            sidecar_bytes(day, 1, raw, fetched="2026-11-17T17:06:30.803269Z")
        )
    cutoff = "2026-11-17T18:00:00Z"  # Beijing 11-18, ending the window on 11-17.
    for path in clean.glob("state/catalogue/**/fixtures_one.json.meta.json"):
        meta = json.loads(path.read_bytes())
        meta["fetched_at_utc"] = "2026-11-17T17:06:30.803269Z"
        path.write_text(json.dumps(meta))
    manifest_path = clean / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["created_utc"] = cutoff
    for path in clean.glob("state/catalogue/**/*"):
        if path.is_file() and re.fullmatch(STATE_FILE, path.relative_to(clean).as_posix()):
            manifest["files"][path.relative_to(clean).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr("zucai.cloud_feeds.datetime", datetime)
    monkeypatch.setattr("sys.argv", [*sys.argv, "--cutoff", cutoff])
    main()
    index = json.loads((clean / "state/identity_gate_evidence/ev-1/98/index.json").read_bytes())
    reason = index["generation_errors"]["recent_replay"]
    assert "该日官方页面仍有未结算比赛" in reason
    assert DAY in reason and "jc:2042830" in reason
    register = json.loads((clean / "state/identity_gate_evidence/ev-1/register.json").read_bytes())
    entry = register["leagues"][0]
    assert entry["status"] != "ready_for_review"
    assert "recent_replay" in entry["missing_evidence"]
