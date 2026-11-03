"""T29 fixtures: human review references and guarded same-provider identity merges."""

import csv
import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from zucai.cli import main
from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.db import get_engine, init_db

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((ROOT / "data/fixtures/t29/history.json").read_bytes())


@pytest.fixture
def history(tmp_path, monkeypatch):
    db = tmp_path / "seed/data/zucai.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(db) as con:
        con.executemany(
            "INSERT INTO teams VALUES(?,?,?)",
            [(i, n, json.dumps(a)) for i, n, a in FIXTURE["teams"]],
        )
        for prefix, source, league, homes, aways, start, count in FIXTURE["groups"]:
            for index in range(count):
                kickoff = datetime.fromisoformat(start) + timedelta(days=index)
                con.execute(
                    "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                    "kickoff_utc,status) VALUES(?,?,?,?,?,?,'scheduled')",
                    (
                        f"{prefix}-{index}",
                        source,
                        league,
                        homes[index % len(homes)],
                        aways[index % len(aways)],
                        kickoff.isoformat(),
                    ),
                )
    audit = db.parent / "state/audit"
    audit.mkdir(parents=True)
    for filename, fields, rows in (
        ("apif_cup_aliases.csv", ["league", "team_id", "name"], FIXTURE["apif_names"]),
        (
            "fdorg_unmatched_teams.csv",
            ["league", "team_id", "english_name"],
            FIXTURE["fdorg_names"],
        ),
    ):
        with (audit / filename).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)
    paths = {}
    for provider in ("apif", "fdorg"):
        path = tmp_path / f"{provider}-aliases.json"
        path.write_text(json.dumps(FIXTURE[provider + "_aliases"], ensure_ascii=False))
        paths[provider] = path
    monkeypatch.setattr("zucai.aliases_snapshot.ALIASES", paths["apif"])
    monkeypatch.setattr("zucai.sources.apif_reconcile.ALIASES", paths["apif"])
    # raising=False permits the RED run before the fdorg extension exists.
    monkeypatch.setattr("zucai.aliases_snapshot.FDORG_ALIASES", paths["fdorg"], raising=False)
    monkeypatch.setattr("zucai.sources.apif_reconcile.FDORG_ALIASES", paths["fdorg"], raising=False)
    return db, paths


def inventory(path):
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


def export_history(db, tmp_path):
    archive = tmp_path / "archive"
    export_feeds(db, archive, date.fromisoformat(FIXTURE["through_beijing"]), "t29-fixture")
    return archive


def test_cups_proposals_rows_order_opponents_reference_and_read_only(history, tmp_path):
    db, paths = history
    archive = export_history(db, tmp_path)
    before, formal = inventory(archive), {k: p.read_bytes() for k, p in paths.items()}
    out = tmp_path / "owner.md"
    args = [
        "aliases",
        "proposals",
        "--scope",
        "cups-history",
        "--archive",
        str(archive),
        "--out",
        str(out),
    ]
    assert main(args) == 0
    text = out.read_text()
    apif = text.split("## fdorg")[0]
    rows = [line for line in apif.splitlines() if line.startswith("| 91")]
    assert [line.split("|")[1].strip() for line in rows] == ["9103", "9102", "9101"]
    assert all(row.endswith(" |  |") for row in rows)
    assert "| 9103 | Synthetic Regular | 2024/25–2026/27 | 8 | 7 |" in text
    opponents = rows[0].split("|")[6].strip().split("；")
    assert len(opponents) == 3
    assert set(opponents) <= {"规范甲", "规范乙", "规范丙", "规范丁", "规范戊"}
    assert "旧季对手" not in rows[0] and "Unbound English" not in rows[0]
    assert "apif:team:" not in rows[0] and "jc:current" not in text
    assert "同窗" not in text
    fdorg = text.split("## fdorg")[1].split("## 参考")[0]
    assert "| 9102 | Synthetic FD Proposed |" in fdorg
    assert "| 9103 | Synthetic FD Unknown |" in fdorg
    assert "| 9101 |" not in fdorg
    references = text.split("## 参考")[1]
    assert "规范丙" in references and "英超 / 9101 / Synthetic FD Approved" in references
    assert "| 规范甲 |" not in references and "| 规范乙 |" not in references
    assert "仅老季竞彩" not in references and "不是绑定证据" in references
    assert before["feed_manifest.json"] in text
    assert "少于 20 场：3" in text and "少于 60 场：4" in text and "20–59 场：1" in text
    assert "合并身份：0；重定向比赛：0；冲突跳过：0（本命令只读，不执行合并）" in text
    assert inventory(archive) == before
    assert {k: p.read_bytes() for k, p in paths.items()} == formal
    frozen = out.read_bytes()
    assert main(args) == 2 and out.read_bytes() == frozen
    args[-1] = str(archive / "owner.md")
    assert main(args) == 2 and inventory(archive) == before


def test_reconcile_formal_id_crosses_leagues_and_providers_and_is_idempotent(
    history, tmp_path, monkeypatch
):
    from test_forecast_archive import staged_run
    from zucai.forecast_archive import publish_forecasts, verify_forecasts
    from zucai.sources.apif_reconcile import reconcile, write_summary

    db, paths = history
    original = inventory(db.parent)
    formal = {k: p.read_bytes() for k, p in paths.items()}
    archive = export_history(db, tmp_path)
    stage, run = staged_run(tmp_path / "stage", "unchanged")
    forecasts = publish_forecasts(archive, stage, run)
    frozen_files = {
        name: (archive / name).read_bytes()
        for name in [*forecasts["files"], "forecast_manifest.json"]
    }
    count = json.loads((archive / "feed_manifest.json").read_bytes())["counts"]["matches"]
    with sqlite3.connect(db) as con:
        ids = con.execute("SELECT match_id FROM matches ORDER BY match_id").fetchall()
    report = reconcile(db, "first")
    assert report["merged_teams"] == report["deleted_teams"] == 2
    assert report["rekeyed_matches"] == 21 and report["skipped_conflicts"] == 0
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT home_team_id FROM matches WHERE match_id='apif:danish-0'"
        ).fetchone() == (1,)
        assert con.execute(
            "SELECT home_team_id FROM matches WHERE match_id='fdorg:approved-0'"
        ).fetchone() == (3,)
        assert con.execute(
            "SELECT home_team_id FROM matches WHERE match_id='apif:proposed-cup-0'"
        ).fetchone() == (11,)
        assert con.execute(
            "SELECT home_team_id FROM matches WHERE match_id='fdorg:proposed-0'"
        ).fetchone() == (17,)
        assert con.execute("SELECT count(*) FROM matches").fetchone()[0] == count
        assert con.execute("SELECT match_id FROM matches ORDER BY match_id").fetchall() == ids
    assert report["coverage_before"]["provisional"]["apif"] == {"teams": 6, "cup_appearances": 46}
    assert report["coverage_after"]["provisional"]["apif"] == {"teams": 5, "cup_appearances": 36}
    assert report["coverage_before"]["provisional"]["fdorg"] == {"teams": 3, "cup_appearances": 6}
    assert report["coverage_after"]["provisional"]["fdorg"] == {"teams": 2, "cup_appearances": 3}
    assert report["coverage_before"]["jc_under_20"] == 3
    assert report["coverage_after"]["jc_under_20"] == 2  # 规范甲 now has exactly 20 games.
    assert report["coverage_before"]["jc_under_60"] == report["coverage_after"]["jc_under_60"] == 4
    assert report["coverage_before"]["jc_20_to_59"] == 1
    assert report["coverage_after"]["jc_20_to_59"] == 2
    for provider in ("apif", "fdorg"):
        audit = db.parent / f"state/audit/{provider}_identity_merges.csv"
        rows = list(csv.DictReader(audit.open()))
        merged = [row for row in rows if row["reason"] == "merged"]
        assert merged
        for row in merged:
            source = json.loads(row["alias_source"])
            assert source["provider"] == provider
            assert source["kind"] == "same_provider_id"
            assert source["source_leagues"] == (["欧协联"] if provider == "apif" else ["英超"])
    before_repeat = inventory(db.parent)
    repeat = reconcile(db, "repeat")
    assert repeat["merged_teams"] == repeat["rekeyed_matches"] == 0
    assert inventory(db.parent) == before_repeat and original != before_repeat
    assert {k: p.read_bytes() for k, p in paths.items()} == formal
    export = export_feeds(db, archive, date(2026, 11, 3), "after", collection={"apif": report})
    assert export["counts"]["matches"] == count
    restored = tmp_path / "restored/data/zucai.db"
    restore_feeds(archive, restored)
    assert (restored.parent / "state/audit/fdorg_identity_merges.csv").is_file()
    assert verify_forecasts(archive) == forecasts
    assert {name: (archive / name).read_bytes() for name in frozen_files} == frozen_files
    # The production Actions summary must expose both snapshot coverage and this operation.
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    write_summary(report)
    assert "少于 20 场" in summary.read_text() and "fdorg" in summary.read_text()


@pytest.mark.parametrize(
    "provider,target,temporary,league", [("apif", 1, 10, "丹超"), ("fdorg", 3, 16, "欧冠")]
)
@pytest.mark.parametrize(
    "problem",
    [
        "proposed",
        "missing",
        "ambiguous",
        "binding",
        "owner",
        "aliases",
        "window",
        "foreign",
        "cross_league",
        "reverse_alias",
    ],
)
def test_reconcile_skips_unsafe_identity(history, provider, target, temporary, league, problem):
    from zucai.sources.apif_reconcile import reconcile

    db, paths = history
    aliases = json.loads(paths[provider].read_bytes())
    source = "欧协联" if provider == "apif" else "英超"
    canonical = aliases[source]["9101"]["canonical"]
    with sqlite3.connect(db) as con:
        if problem == "proposed":
            aliases[source]["9101"]["status"] = "proposed"
        elif problem == "missing":
            con.execute("UPDATE teams SET name='目标缺失' WHERE id=?", (target,))
        elif problem == "ambiguous":
            con.execute("UPDATE teams SET aliases=? WHERE id=8", (json.dumps([canonical]),))
        elif problem == "binding":
            con.execute(
                "UPDATE teams SET aliases=? WHERE id=?",
                (json.dumps([f"{provider}:{league}:team:9999"]), target),
            )
        elif problem == "owner":
            con.execute(
                "UPDATE teams SET aliases=? WHERE id=8",
                (json.dumps([f"{provider}:{league}:team:9101"]),),
            )
        elif problem == "aliases":
            con.execute("UPDATE teams SET aliases='[\"unexpected\"]' WHERE id=?", (temporary,))
        elif problem == "window":
            con.execute(
                "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) VALUES('jc:conflict','jc',?,?,2,"
                "'2024-08-01T21:00:00+00:00','scheduled')",
                (league, target),
            )
        elif problem == "foreign":
            con.execute(
                "UPDATE matches SET source='fd',"
                "match_id='fd:' || substr(match_id,instr(match_id,':')+1) "
                "WHERE home_team_id=? AND league=?",
                (temporary, league),
            )
        elif problem == "cross_league":
            aliases["冲突联赛"] = {"9101": {"canonical": "规范戊", "status": "auto_verified"}}
        else:
            aliases[source]["9999"] = {"canonical": canonical, "status": "auto_verified"}
    paths[provider].write_text(json.dumps(aliases))
    report = reconcile(db, problem)
    with sqlite3.connect(db) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM matches WHERE home_team_id=? AND league=?",
                (temporary, league),
            ).fetchone()[0]
            > 0
        )
    if problem != "proposed":
        assert report["skipped_conflicts"] > 0 and report["status"] == "partial"
        rows = list(
            csv.DictReader((db.parent / f"state/audit/{provider}_identity_merges.csv").open())
        )
        assert any(
            row["team_id"] == "9101" and row["league"] == league and row["reason"] != "merged"
            for row in rows
        )


def test_both_provider_audits_and_db_rollback_together(history, monkeypatch):
    from zucai.sources import apif_reconcile

    db, _ = history
    before = inventory(db.parent)
    real_write = apif_reconcile.write_audit

    def fail_fdorg(path, rows):
        if path.name == "fdorg_identity_merges.csv":
            raise OSError("fixture second audit failure")
        return real_write(path, rows)

    monkeypatch.setattr(apif_reconcile, "write_audit", fail_fdorg)
    with pytest.raises(OSError, match="fixture second audit failure"):
        apif_reconcile.reconcile(db, "failed")
    assert inventory(db.parent) == before


@pytest.mark.parametrize("existing_audits", [False, True])
def test_failed_commit_restores_both_provider_audits(history, monkeypatch, existing_audits):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from zucai.sources.apif_reconcile import FIELDS, reconcile

    db, _ = history
    if existing_audits:
        for provider in ("apif", "fdorg"):
            (db.parent / f"state/audit/{provider}_identity_merges.csv").write_text(
                ",".join(FIELDS) + "\n"
            )
    before = inventory(db.parent)

    def short_timeout(path):
        engine = get_engine(path)

        @event.listens_for(engine, "connect")
        def timeout(connection, _record):
            connection.execute("PRAGMA busy_timeout=1")

        return engine

    monkeypatch.setattr("zucai.sources.apif_reconcile.get_engine", short_timeout)
    with sqlite3.connect(db) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM teams").fetchall()
        with pytest.raises(OperationalError, match="database is locked"):
            reconcile(db, "commit-failed")
        reader.rollback()
    assert inventory(db.parent) == before


def test_cups_proposals_missing_and_escaped_names_do_not_fill_owner_columns(history, tmp_path):
    db, _ = history
    path = db.parent / "state/audit/apif_cup_aliases.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["league", "team_id", "name"])
        writer.writerow(["欧罗巴", "9103", "<name>|line\nbreak"])
    archive = export_history(db, tmp_path)
    out = tmp_path / "names.md"
    assert (
        main(
            [
                "aliases",
                "proposals",
                "--scope",
                "cups-history",
                "--archive",
                str(archive),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    text = out.read_text()
    assert "&lt;name&gt;&#124;line break" in text
    assert "| 9102 | 缺失（apif_cup_aliases.csv 未记录） |" in text
    assert all(line.endswith(" |  |") for line in text.splitlines() if line.startswith("| 91"))


def test_formal_alias_alone_is_required_persisted_binding_is_not_authorization(history):
    from zucai.sources.apif_reconcile import reconcile

    db, paths = history
    aliases = json.loads(paths["apif"].read_bytes())
    aliases["欧协联"]["9101"]["status"] = "proposed"
    paths["apif"].write_text(json.dumps(aliases))
    with sqlite3.connect(db) as con:
        con.execute("UPDATE teams SET aliases='[\"apif:欧协联:team:9101\"]' WHERE id=1")
    reconcile(db, "no-formal")
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM matches WHERE home_team_id=10").fetchone() == (18,)


def test_cross_league_history_merge_does_not_expand_importer_authorization(history):
    """Pin the documented rollout blocker; no real aliases are promoted by T29."""
    from zucai.db import session_scope
    from zucai.sources.apif_ingest import _Teams
    from zucai.sources.apif_reconcile import reconcile

    db, paths = history
    aliases = json.loads(paths["apif"].read_bytes())
    engine = get_engine(db)
    try:
        with session_scope(engine) as session:
            assert _Teams(session, aliases).resolve(session, "丹超", 9101).name == "apif:team:9101"
        reconcile(db, "history-only")
        with session_scope(engine) as session:
            with pytest.raises(ValueError, match="新别名未获核准"):
                _Teams(session, aliases).resolve(session, "丹超", 9101)
    finally:
        engine.dispose()
