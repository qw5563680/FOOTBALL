"""Identity-only fixtures must never enter the collection/import path."""

import csv
import hashlib
import json
from pathlib import Path

import pytest
import responses

from zucai.sources.api_football import ApiFootball
from zucai.sources.apif_command import fetch_apif

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((ROOT / "data/fixtures/t23/identity.json").read_bytes())
BRIDGES = {row["code"] for row in FIXTURE["leagues"]}


def test_verified_bridge_metadata_matches_actions_audit_and_stays_identity_only():
    config = json.loads((ROOT / "data/api_football/leagues.json").read_bytes())
    with (ROOT / "data/fixtures/t23/apif_league_probes.csv").open() as stream:
        probes = {row["league"]: row for row in csv.DictReader(stream)}
    assert BRIDGES <= config.keys()
    for code in BRIDGES:
        assert config[code]["identity_only"] is True
        assert config[code]["verification_status"] == "verified"
        assert config[code]["verified_utc"] == probes[code]["observed_utc"]
        assert config[code]["metadata_probe"]["raw_sha256"] == probes[code]["raw_sha256"]
        assert probes[code]["outcome"] == "ok" and probes[code]["http"] == "200"
        assert config[code]["seasons_available"] == []
    source = ApiFootball()
    try:
        assert not BRIDGES & source.leagues.keys()
    finally:
        source.session.close()


def test_promoted_bridge_aliases_keep_all_130_original_records_and_evidence():
    provenance = json.loads((ROOT / "data/fixtures/t23/stage1_provenance.json").read_bytes())
    metadata = (ROOT / "data/fixtures/t23/apif_league_probes.csv").read_bytes()
    assert hashlib.sha256(metadata).hexdigest() == provenance["metadata_fixture_sha256"]
    aliases = json.loads((ROOT / "data/team_aliases/api_football.json").read_bytes())
    count = 0
    for league, proof in provenance["promoted"].items():
        # T32 adds owner-approved historical IDs beside the original T23 batch.
        # Keep the original 130 auto-verified records and their evidence hash exact.
        records = {
            key: row for key, row in aliases[league].items() if row["status"] == "auto_verified"
        }
        assert len(records) == proof["count"]
        assert all(row["status"] == "auto_verified" and row["evidence"] for row in records.values())
        encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(encoded.encode()).hexdigest() == proof["canonical_json_sha256"]
        count += len(records)
    assert count == 130
    for league, key in (("英超", "51"), ("葡超", "225")):
        assert aliases[league][key]["status"] == "approved"
        assert aliases[league][key]["approval_pr"].endswith("/49#issuecomment-6627072639")


def test_formal_cup_aliases_preserve_inferred_names_and_entire_audit_evidence():
    fixture = ROOT / "data/fixtures/t23"
    provenance = json.loads((fixture / "stage3_provenance.json").read_bytes())
    for name, expected_sha in provenance["fixture_sha256"].items():
        assert hashlib.sha256((fixture / name).read_bytes()).hexdigest() == expected_sha
    aliases = json.loads((ROOT / "data/team_aliases/api_football.json").read_bytes())
    with (fixture / "stage3_cup_aliases.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == provenance["selected_rows"] == 2
    for row in rows:
        evidence = json.loads(row["evidence"])
        assert row["status"] == "auto_verified"
        assert any(e["counterpart_id"].startswith("jc:") for e in evidence)
        assert aliases[row["league"]][row["team_id"]] == {
            "name": row["name"],
            "canonical": row["canonical"],
            "status": row["status"],
            "evidence": evidence,
        }


def test_owner_approved_batch_exactly_matches_the_41_row_comment_table():
    from zucai.sources.apif_aliases import validate_approvals
    from zucai.sources.fd_ingest import _canonical

    fixture = ROOT / "data/fixtures/t23"
    aliases = json.loads((ROOT / "data/team_aliases/api_football.json").read_bytes())
    validate_approvals(aliases, require_metadata=True)
    expected = {}
    for line in (fixture / "stage3_approval_table.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) == 6 and cells[1].isdigit():
            league, key, name, _, canonical, _ = cells
            expected[league, key] = dict(
                name=name,
                canonical=canonical,
                status="approved",
                approved_by="qw5563680",
                approved_utc="2026-11-03T11:11:00+00:00",
                approval_pr="https://github.com/qw5563680/FOOTBALL/pull/34",
            )
    actual = {
        (league, key): row
        for league, records in aliases.items()
        for key, row in records.items()
        if row.get("approved_utc") == "2026-11-03T11:11:00+00:00"
    }
    assert actual == expected and len(actual) == 41
    for (league, key), row in expected.items():
        assert _canonical(aliases, league, key) == row["canonical"]


def test_identity_source_explicitly_selects_only_the_seven_bridges():
    source = ApiFootball(identity_only=True)
    try:
        assert source.leagues.keys() == BRIDGES
    finally:
        source.session.close()


def test_identity_fixture_is_excluded_from_date_parse():
    doc = json.loads((ROOT / "data/fixtures/t14/apif.json").read_bytes())
    doc["response"][0]["league"]["id"] = 39
    source = ApiFootball()
    try:
        assert source.parse(json.dumps(doc).encode(), date=doc["parameters"]["date"]) == []
    finally:
        source.session.close()


def test_explicit_identity_import_is_rejected_before_network_or_database(tmp_path):
    from datetime import timedelta

    with responses.RequestsMock() as http, pytest.raises(ValueError):
        fetch_apif(league_id="39", seasons=[2025], assume=timedelta(hours=3), db=tmp_path / "x.db")
    assert not http.calls and not (tmp_path / "x.db").exists()


def test_backfill_rejects_identity_even_if_permissions_are_present(tmp_path, monkeypatch):
    from zucai.cloud_api_backfill import api_backfill_scopes

    config = json.loads((ROOT / "data/api_football/leagues.json").read_bytes())
    config["39"] = {"league": "英超", "identity_only": True, "seasons_available": [2025, 2026]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr("zucai.cloud_api_backfill.LEAGUES", path)
    with pytest.raises(ValueError):
        api_backfill_scopes("apif", "39", "2025", "2026")
