"""Authenticate real cropped team payloads using the production archive shape."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zucai import identity_team_countries

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t99"
PROVENANCE = json.loads((FIXTURES / "teams-real-provenance.json").read_bytes())
NOW = datetime(2026, 11, 27, 10, tzinfo=UTC)
EXPECTED = {
    "254": "Scotland",
    "397": "Denmark",
    "406": "Denmark",
    "598": "Serbia",
    "608": "Croatia",
    "617": "Greece",
    "664": "Kazakhstan",
    "667": "Gibraltar",
    "735": "Belgium",
    "1012": "Switzerland",
    "3403": "Cyprus",
}


def seal_country_crops(root, *, edit=None):
    """Stage genuine cropped rows with new fixture-only hashes in archive metadata."""
    index_path = PROVENANCE["probe_index_path"]
    files = {}
    items = []

    def store(relative, obj):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(obj, ensure_ascii=False).encode()
        path.write_bytes(raw)
        files[relative] = hashlib.sha256(raw).hexdigest()
        return files[relative]

    for entry in PROVENANCE["sources"]:
        raw = (FIXTURES / entry["fixture"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["fixture_sha256"]
        code = entry["league_id"]
        body = json.loads(raw)
        meta = {
            "url": entry["source_url"],
            "status_code": 200,
            "fetched_at_utc": entry["observed_utc"],
        }
        item = {
            "league_id": code,
            "season": entry["season"],
            "outcome": "complete",
            "http": 200,
            "raw_path": entry["original_path"],
            "metadata_path": entry["original_metadata_path"],
            "observed_utc": entry["observed_utc"],
            "results": len(body["response"]),
        }
        if edit:
            edit(code, body, meta, item)
        item["raw_sha256"] = store(entry["original_path"], body)
        meta["sha256"] = item["raw_sha256"]
        store(entry["original_metadata_path"], meta)
        items.append(item)
    store(
        index_path,
        {
            "run_id": "45976356364-1",
            "evidence_index": index_path,
            "collection": "apif_team_countries",
            "status": "complete" if all(r["outcome"] == "complete" for r in items) else "partial",
            "probes": items,
        },
    )
    manifest_path = root / "feed_manifest.json"
    manifest = (
        json.loads(manifest_path.read_bytes())
        if manifest_path.exists()
        else {"created_utc": "2026-11-27T08:56:05Z", "files": {}}
    )
    manifest["files"].update(files)
    manifest_path.write_text(json.dumps(manifest))
    return files


def test_genuine_country_rows_verify_after_older_fixture_snapshot(tmp_path):
    hashes = seal_country_crops(tmp_path)
    countries = identity_team_countries.load_team_countries(tmp_path, now=NOW)
    for provider_id, expected in EXPECTED.items():
        proofs = countries[provider_id]
        assert {p["country"] for p in proofs} == {expected}
        assert len(proofs) == (1 if provider_id == "617" else 2)
        for p in proofs:
            assert p["path"] in hashes and p["sha256"] == hashes[p["path"]]
            assert p["pointer"].endswith("/team/country")


@pytest.mark.parametrize(
    "fault",
    ["tamper", "missing", "wrong-url", "bad-pagination", "future", "wrong-scope", "symlink"],
)
def test_one_unverifiable_complete_page_cannot_hide_country_conflicts(tmp_path, fault):
    raw_path = tmp_path / PROVENANCE["sources"][0]["original_path"]

    def edit(code, body, meta, item):
        if code == 3:
            if fault == "wrong-url":
                meta["url"] += "&page=1"
            if fault == "bad-pagination":
                body["paging"]["total"] = 2
            if fault == "future":
                meta["fetched_at_utc"] = item["observed_utc"] = "2026-11-27T10:01:00Z"
            if fault == "wrong-scope":
                body["parameters"]["league"] = "848"

    seal_country_crops(tmp_path, edit=edit)
    if fault == "tamper":
        raw_path.write_bytes(raw_path.read_bytes() + b" ")
    if fault == "missing":
        raw_path.unlink()
    if fault == "symlink":
        alternate = raw_path.with_name("copy.json")
        raw_path.rename(alternate)
        raw_path.symlink_to(alternate)
    assert identity_team_countries.load_team_countries(tmp_path, now=NOW) == {}


def test_incomplete_probe_page_supplies_no_country_evidence(tmp_path):
    def edit(code, body, meta, item):
        if code == 3:
            body["paging"]["total"] = 2
            item["outcome"] = "error"

    seal_country_crops(tmp_path, edit=edit)
    proofs = identity_team_countries.load_team_countries(tmp_path, now=NOW)
    assert len(proofs["254"]) == 1
    assert proofs["254"][0]["path"] == PROVENANCE["sources"][1]["original_path"]


def test_same_id_from_multiple_originals_keeps_empty_and_conflicting_observations(tmp_path):
    def edit(code, body, meta, item):
        if code == 3:
            for row in body["response"]:
                if row["team"]["id"] == 254:
                    row["team"]["country"] = ""
                if row["team"]["id"] == 397:
                    row["team"]["country"] = "Scotland"

    seal_country_crops(tmp_path, edit=edit)
    proofs = identity_team_countries.load_team_countries(tmp_path, now=NOW)
    assert {row["country"] for row in proofs["254"]} == {"", "Scotland"}
    assert {row["country"] for row in proofs["397"]} == {"Denmark", "Scotland"}
