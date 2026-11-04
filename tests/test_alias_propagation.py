"""T29 review fix: formal propagation closes the post-reconcile import gap."""

import csv
import hashlib
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from test_cups_history import export_history, inventory
from test_cups_history import history as history
from zucai.cli import main
from zucai.db import get_engine, session_scope
from zucai.sources.apif_reconcile import reconcile

ROOT = Path(__file__).parents[1]


def test_propagate_cli_preserves_provenance_archive_and_existing_entries(history, tmp_path, caplog):
    db, paths = history
    archive = export_history(db, tmp_path)
    before_archive, before_db = inventory(archive), db.read_bytes()
    before = {p: paths[p].read_bytes() for p in paths}
    args = ["aliases", "propagate", "--archive", str(archive)]
    with caplog.at_level(logging.INFO):
        assert main(args) == 0
    for provider, destination, source in (("apif", "丹超", "欧协联"), ("fdorg", "欧冠", "英超")):
        old = json.loads(before[provider])
        new = json.loads(paths[provider].read_bytes())
        entry = new[destination]["9101"]
        original = old[source]["9101"]
        assert entry["canonical"] == original["canonical"]
        assert entry["status"] == original["status"]
        assert entry["name"] == old.get(destination, {}).get("9101", original)["name"]
        assert entry["evidence"] == [
            {
                "kind": "same_provider_id",
                "source_league": source,
                "source_status": original["status"],
                "source_aliases_sha256": hashlib.sha256(before[provider]).hexdigest(),
            }
        ]
        for field in ("approved_by", "approved_utc", "approval_pr"):
            assert entry.get(field) == original.get(field)
        assert "reason" not in entry
        for league, entries in old.items():
            for key, previous in entries.items():
                if (league, key) != (destination, "9101"):
                    assert new[league][key] == previous
        assert f"{provider} {destination}/9101" in caplog.text
        assert source in caplog.text and original["canonical"] in caplog.text
    assert inventory(archive) == before_archive and db.read_bytes() == before_db
    after = {p: paths[p].read_bytes() for p in paths}
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert main(args) == 0
    assert "传播 0 条" in caplog.text
    assert {p: paths[p].read_bytes() for p in paths} == after
    assert inventory(archive) == before_archive


@pytest.mark.parametrize("provider,league,temporary", [("apif", "丹超", 10), ("fdorg", "英冠", 16)])
def test_reconcile_skips_unpropagated_daily_league(history, provider, league, temporary):
    db, paths = history
    if provider == "fdorg":
        with sqlite3.connect(db) as con:
            con.execute("UPDATE matches SET league=? WHERE home_team_id=?", (league, temporary))
    report = reconcile(db, "unpropagated")
    assert report["status"] == "partial" and report["skipped_conflicts"] >= 1
    with sqlite3.connect(db) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM matches WHERE league=? AND home_team_id=?",
                (league, temporary),
            ).fetchone()[0]
            > 0
        )
    audit = db.parent / f"state/audit/{provider}_identity_merges.csv"
    rows = list(csv.DictReader(audit.open()))
    assert any(
        row["league"] == league
        and row["team_id"] == "9101"
        and row["reason"] == "目标联赛正式别名未传播"
        for row in rows
    )
    engine = get_engine(db)
    try:
        if provider == "apif":
            from zucai.sources.apif_ingest import _Teams
        else:
            from zucai.sources.fdorg_ingest import _Teams
        with session_scope(engine) as session:
            side = _Teams(session, json.loads(paths[provider].read_bytes())).resolve(
                session, league, 9101
            )
            assert side.name == f"{provider}:team:9101" and side.binding is None
    finally:
        engine.dispose()


def test_fdorg_propagation_then_reconcile_allows_existing_importer(history, tmp_path):
    db, paths = history
    with sqlite3.connect(db) as con:
        con.execute("UPDATE matches SET league='英冠' WHERE home_team_id=16")
    archive = export_history(db, tmp_path)
    assert main(["aliases", "propagate", "--archive", str(archive)]) == 0
    result = reconcile(db, "propagated")
    assert result["skipped_conflicts"] == 0
    from zucai.sources.fdorg_ingest import _Teams

    engine = get_engine(db)
    try:
        with session_scope(engine) as session:
            side = _Teams(session, json.loads(paths["fdorg"].read_bytes())).resolve(
                session, "英冠", 9101
            )
            assert side.name == "规范丙" and side.binding == "fdorg:英冠:team:9101"
    finally:
        engine.dispose()


@pytest.mark.parametrize("provider", ["apif", "fdorg"])
@pytest.mark.parametrize("problem", ["cross_league", "reverse", "approval"])
def test_propagation_conflicts_do_not_write_either_file(history, tmp_path, provider, problem):
    db, paths = history
    aliases = json.loads(paths[provider].read_bytes())
    source = "欧协联" if provider == "apif" else "英超"
    destination = "丹超" if provider == "apif" else "欧冠"
    canonical = aliases[source]["9101"]["canonical"]
    if problem == "cross_league":
        aliases["其他联赛"] = {"9101": {"canonical": "规范戊", "status": "auto_verified"}}
    elif problem == "reverse":
        aliases.setdefault(destination, {})["9999"] = {
            "canonical": canonical,
            "status": "auto_verified",
        }
    else:
        aliases[source]["9101"]["status"] = "approved"
        aliases[source]["9101"].pop("approved_by", None)
    paths[provider].write_text(json.dumps(aliases))
    archive = export_history(db, tmp_path)
    before = {p: paths[p].read_bytes() for p in paths}
    assert main(["aliases", "propagate", "--archive", str(archive)]) == 2
    assert {p: paths[p].read_bytes() for p in paths} == before


def test_propagation_write_failure_restores_both_formal_files(history, tmp_path, monkeypatch):
    from zucai import alias_propagation

    db, paths = history
    archive = export_history(db, tmp_path)
    before = {p: paths[p].read_bytes() for p in paths}
    write = alias_propagation._atomic_write

    def fail_fdorg(path, content):
        if path == paths["fdorg"]:
            raise OSError("fixture write failure")
        write(path, content)

    monkeypatch.setattr(alias_propagation, "_atomic_write", fail_fdorg)
    assert main(["aliases", "propagate", "--archive", str(archive)]) == 2
    assert {p: paths[p].read_bytes() for p in paths} == before


def test_approved_batch_and_propagated_entries_match_owner_record():
    approval = json.loads((ROOT / "data/fixtures/t29/approved_batch.json").read_bytes())
    formal = {
        provider: json.loads((ROOT / f"data/team_aliases/{filename}.json").read_bytes())
        for provider, filename in (("apif", "api_football"), ("fdorg", "football_data_org"))
    }
    assert len(approval["rows"]) == 15
    propagated = 0
    for row in approval["rows"]:
        for league in row["history_leagues"]:
            entry = formal[row["provider"]][league][row["team_id"]]
            assert entry["name"] == row["name"] and entry["canonical"] == row["canonical"]
            assert entry["status"] == "approved"
            for field in ("approved_by", "approved_utc", "approval_pr"):
                assert entry[field] == approval[field]
            if league != row["initial_league"]:
                assert entry["evidence"] == [
                    {
                        "kind": "same_provider_id",
                        "source_league": row["initial_league"],
                        "source_status": "approved",
                        "source_aliases_sha256": approval["propagation_source_sha256"][
                            row["provider"]
                        ],
                    }
                ]
                propagated += 1
    assert propagated == 6
