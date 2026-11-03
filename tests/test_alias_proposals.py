"""Owner proposal tables are references, never an identity import."""

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from test_cloud_apis import environment as environment
from zucai.cli import main
from zucai.cloud_feeds import export_feeds, restore_feeds

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((ROOT / "data/fixtures/t20/identity.json").read_bytes())


@pytest.fixture
def proposal_archive(environment, tmp_path):
    from datetime import date

    db = tmp_path / "proposals/data/zucai.db"
    restore_feeds(environment, db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE teams SET name=? WHERE id=1", (FIXTURE["official"][0]["home"],))
        con.execute("UPDATE teams SET name=? WHERE id=2", (FIXTURE["official"][0]["away"],))
        con.execute(
            "UPDATE matches SET league='欧罗巴',kickoff_utc=?",
            (FIXTURE["official"][0]["kickoff_utc"].replace("2024", "2026"),),
        )
    audit = db.parent / "state/audit"
    audit.mkdir(parents=True, exist_ok=True)
    with (audit / "apif_cup_aliases.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, ("league", "team_id", "name", "canonical", "status", "reason", "evidence")
        )
        writer.writeheader()
        for key, row in FIXTURE["domestic_aliases"]["合成国内联赛"].items():
            writer.writerow(
                dict(
                    league="欧罗巴",
                    team_id=key,
                    name=row["name"],
                    canonical="",
                    status="proposed",
                    reason="缺少证据",
                    evidence="[]",
                )
            )
    with (audit / "apif_suspected_duplicates.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            (
                "league",
                "provider_match_id",
                "home_provider_id",
                "away_provider_id",
                "home",
                "away",
                "kickoff_utc",
                "raw_sha256",
                "reason",
                "candidates",
            ),
        )
        writer.writeheader()
        game = FIXTURE["games"][0]
        writer.writerow(
            dict(
                league="欧罗巴",
                provider_match_id=game["provider_match_id"],
                home_provider_id=game["home_provider_id"],
                away_provider_id=game["away_provider_id"],
                home=game["home_raw"],
                away=game["away_raw"],
                kickoff_utc=game["kickoff_utc"].replace("2024", "2026"),
                raw_sha256="a" * 64,
                reason="jc 候选身份不全",
                candidates='["jc:1"]',
            )
        )
    export_feeds(db, environment, date(2026, 11, 2), "proposal-seed")
    return environment


def inventory(path):
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


def test_proposals_cli_lists_jc_names_as_unverified_and_changes_only_output(
    proposal_archive, tmp_path
):
    before = inventory(proposal_archive)
    aliases = (ROOT / "data/team_aliases/api_football.json").read_bytes()
    out = tmp_path / "owner.md"
    assert (
        main(
            [
                "aliases",
                "proposals",
                "--provider",
                "apif",
                "--leagues",
                "3",
                "--archive",
                str(proposal_archive),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    text = out.read_text()
    assert "9101" in text and "Synthetic Alpha" in text and "合成甲" in text
    assert "未核验" in text and "建议中文规范名" in text and "未知" in text
    assert "赛季字段" in text  # Old audits cannot prove a precise season appearance count.
    assert inventory(proposal_archive) == before
    assert (ROOT / "data/team_aliases/api_football.json").read_bytes() == aliases
    assert (
        main(
            [
                "aliases",
                "proposals",
                "--provider",
                "apif",
                "--leagues",
                "3",
                "--archive",
                str(proposal_archive),
                "--out",
                str(out),
            ]
        )
        == 2
    )


def test_proposals_cannot_write_into_data_archive(proposal_archive):
    before = inventory(proposal_archive)
    assert (
        main(
            [
                "aliases",
                "proposals",
                "--archive",
                str(proposal_archive),
                "--out",
                str(proposal_archive / "owner.md"),
            ]
        )
        == 2
    )
    assert inventory(proposal_archive) == before


def test_first_cup_batch_excludes_2025_windows_and_formal_bridge_ids(
    proposal_archive, tmp_path, monkeypatch
):
    from copy import deepcopy

    from zucai.alias_proposals import write_proposals
    from zucai.aliases_snapshot import snapshot

    data = snapshot(proposal_archive)
    monkeypatch.setattr("zucai.alias_proposals.snapshot", lambda path: data)
    domestic = deepcopy(FIXTURE["domestic_aliases"]["合成国内联赛"])
    domestic["9102"]["status"] = "proposed"
    monkeypatch.setattr("zucai.alias_proposals.formal_aliases", lambda: {"英超": domestic})
    out = tmp_path / "remaining.md"
    assert write_proposals(proposal_archive, "3", out) == 1
    assert "| 9101 |" not in out.read_text() and "| 9102 |" in out.read_text()
    for game in data["matches"] + data["audits"]["suspected_duplicates"]:
        game["kickoff_utc"] = game["kickoff_utc"].replace("2026", "2025")
    assert write_proposals(proposal_archive, "3", tmp_path / "old-only.md") == 0


def test_proposal_names_are_escaped_and_do_not_fill_owner_column(
    proposal_archive, tmp_path, monkeypatch
):
    from zucai.alias_proposals import write_proposals
    from zucai.aliases_snapshot import snapshot

    data = snapshot(proposal_archive)
    data["audits"]["cup_aliases"][0]["name"] = "<script>|name\nnew row"
    monkeypatch.setattr("zucai.alias_proposals.snapshot", lambda path: data)
    out = tmp_path / "escaped.md"
    assert write_proposals(proposal_archive, "3", out) == 2
    text = out.read_text()
    assert "&lt;script&gt;&#124;name new row" in text
    assert "<script>" not in text
    assert all(line.endswith(" |  |") for line in text.splitlines() if line.startswith("| 91"))
