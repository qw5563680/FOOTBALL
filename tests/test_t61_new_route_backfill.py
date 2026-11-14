"""A registry-only synthetic route must traverse real import and identity audit code."""

import copy
import csv
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
import responses

from test_cloud_apis import environment as environment
from test_t22_backfill import mock_paid_season, paid_status
from zucai import cloud_catalogue_backfill, league_registry
from zucai.cloud_feeds import export_feeds, restore_feeds

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data/fixtures"
LEGACY = ROOT / "data/api_football/leagues.json"


@pytest.fixture
def registry(monkeypatch, tmp_path):
    """Inject one validated registry file everywhere defaults read it; no parser/audit mocks."""
    original = league_registry.load_registry
    document = original(FIXTURES / "t61/registry_policy.json")
    identity = copy.deepcopy(document["providers"]["apif"]["1001"])
    identity.update(
        mode="identity_only",
        groups=["B"],
        daily=False,
        backfill=False,
        config={
            "league": "Synthetic bridge",
            "identity_only": True,
            "seasons_available": [],
            "api_name": "Synthetic Bridge",
            "country": "Testland",
        },
    )
    document["providers"]["apif"]["1004"] = identity
    fdorg = copy.deepcopy(document["providers"]["apif"]["1001"])
    fdorg.update(
        groups=["B"],
        config={"league": "Synthetic FDORG", "earliest_season": 2025, "api_id": 990001},
    )
    document["providers"]["fdorg"]["TEST"] = fdorg
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document))
    original(path)

    def configured(path=league_registry.REGISTRY_PATH):
        selected = registry_path if Path(path) == league_registry.REGISTRY_PATH else path
        return original(selected)

    registry_path = path
    monkeypatch.setattr(league_registry, "load_registry", configured)
    monkeypatch.setattr(cloud_catalogue_backfill, "load_registry", configured)
    return document


def csv_rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def catalogue(root):
    document = json.loads((FIXTURES / "t61/backfill-catalogue.json").read_bytes())["apif"]
    document["leagues"] = [
        {
            "id": "1001",
            "name": "Synthetic one",
            "country": "Testland",
            "type": "League",
            "seasons": [{"year": 2025, "fixtures": None, "results": None, "access": "unverified"}],
        }
    ]
    payload = json.dumps(document).encode()
    folder = root / "state/catalogue/synthetic-new-route"
    folder.mkdir(parents=True)
    (folder / "apif.json").write_bytes(payload)
    (folder.parent / "latest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {
                    "apif": {
                        "path": "state/catalogue/synthetic-new-route/apif.json",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                },
            }
        )
    )


def stored_rows(database):
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT m.match_id,m.league,m.source,h.name,a.name,r.home_goals,r.away_goals,"
            "r.available_source FROM matches m JOIN teams h ON h.id=m.home_team_id "
            "JOIN teams a ON a.id=m.away_team_id JOIN results r USING(match_id) "
            "WHERE m.source='apif' ORDER BY m.match_id"
        ).fetchall()


def test_registry_only_new_route_download_import_audit_checkpoint_and_archive(
    registry, environment, tmp_path, monkeypatch
):
    legacy_before = LEGACY.read_bytes()
    aliases_path = ROOT / "data/team_aliases/api_football.json"
    aliases_before = aliases_path.read_bytes()
    assert "1001" not in json.loads(legacy_before)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    paid_status(environment)
    database = tmp_path / "new-route/zucai.db"
    restore_feeds(environment, database)
    catalogue(database.parent)
    with responses.RequestsMock() as http:
        expected = mock_paid_season(http, "1001", 2025)
        report = cloud_catalogue_backfill.collect_catalogue_backfill(
            database, "new-import", provider="apif", codes="1001", max_requests=1
        )
        assert report["status"] == "complete"
        assert report["requests"] == len(http.calls) == 1
        assert report["inserted"] == expected == 1
        assert report["rejected_rows"] == report["suspected_duplicates"] == 0
    rows = stored_rows(database)
    assert rows == [
        (
            "apif:10001500",
            "Synthetic one",
            "apif",
            "apif:team:551",
            "apif:team:552",
            1,
            1,
            "assumed",
        )
    ]
    audit = database.parent / "state/audit"
    matches = csv_rows(audit / "apif_identity_matches.csv")
    assert len(matches) == 1
    assert matches[0]["league_id"] == "1001" and matches[0]["league"] == "Synthetic one"
    assert matches[0]["home_provider_id"] == "551" and matches[0]["away_provider_id"] == "552"
    assert matches[0]["home"] == "Northport FC" and matches[0]["away"] == "Eastbank FC"
    scopes = csv_rows(audit / "apif_identity_scopes.csv")
    assert len(scopes) == 1 and scopes[0]["complete"] == "true"
    assert scopes[0]["league_id"] == "1001" and scopes[0]["season"] == "2025"
    unmatched = csv_rows(audit / "apif_unmatched_teams.csv")
    assert {(row["league"], row["team_id"], row["english_name"]) for row in unmatched} == {
        ("Synthetic one", "551", "Northport FC"),
        ("Synthetic one", "552", "Eastbank FC"),
    }
    checkpoint = database.parent / "state/catalogue/backfill-checkpoint.json"
    completed = json.loads(checkpoint.read_bytes())["completed"]
    assert len(completed) == 1
    proof = next(iter(completed.values()))
    raw = (database.parent / proof["raw_path"]).read_bytes()
    assert proof["raw_sha256"] == hashlib.sha256(raw).hexdigest() == matches[0]["raw_sha256"]
    archive = tmp_path / "new-route-archive"
    manifest = export_feeds(database, archive, date(2026, 11, 2), "new-export")
    assert proof["raw_path"] in manifest["files"]
    restored = tmp_path / "new-route-restored/zucai.db"
    restore_feeds(archive, restored)
    assert stored_rows(restored) == rows
    assert (restored.parent / proof["raw_path"]).read_bytes() == raw
    for name in (
        "apif_identity_matches.csv",
        "apif_identity_scopes.csv",
        "apif_unmatched_teams.csv",
    ):
        assert (restored.parent / "state/audit" / name).read_bytes() == (audit / name).read_bytes()
    with responses.RequestsMock() as http:
        resumed = cloud_catalogue_backfill.collect_catalogue_backfill(
            restored, "new-resume", provider="apif", codes="1001"
        )
        assert resumed["requests"] == resumed["inserted"] == len(http.calls) == 0
        assert resumed["resumed_scopes"] == 1
    with responses.RequestsMock() as http:
        mock_paid_season(http, "1001", 2025)
        replay = cloud_catalogue_backfill.collect_catalogue_backfill(
            restored, "new-replay", provider="apif", codes="1001", resume=False
        )
        assert replay["status"] == "complete" and replay["requests"] == len(http.calls) == 1
        assert replay["inserted"] == 0 and replay["unchanged"] == 1
    assert stored_rows(restored) == rows
    assert LEGACY.read_bytes() == legacy_before and aliases_path.read_bytes() == aliases_before


def test_registry_only_routes_remain_available_to_alias_review_and_reconcile(registry):
    from zucai.aliases_snapshot import selected_leagues
    from zucai.cloud_identity_probe import identity_codes
    from zucai.sources.apif_reconcile import _daily_domestic_leagues

    assert selected_leagues("1001")["1001"]["league"] == "Synthetic one"
    assert identity_codes("1004")["1004"]["identity_only"] is True
    assert set(identity_codes()) == {"1004"}
    with pytest.raises(ValueError):
        identity_codes("1001")  # Import routes cannot silently become identity-only routes.
    with pytest.raises(ValueError):
        selected_leagues("1002")
    assert _daily_domestic_leagues() == {"apif": {"Synthetic one"}, "fdorg": {"Synthetic FDORG"}}


@pytest.mark.parametrize("explicit_argument", [False, True])
def test_identity_audit_preserves_explicit_legacy_path_injection(
    registry, tmp_path, monkeypatch, explicit_argument
):
    from zucai.apif_identity_metrics import record_matches

    path = tmp_path / "legacy-fixture.json"
    path.write_text(json.dumps({"9991": {"league": "Explicit fixture league"}}))
    kwargs = {"leagues_path": path} if explicit_argument else {}
    if not explicit_argument:
        monkeypatch.setattr("zucai.sources.api_football.LEAGUES", str(path))
    audit = tmp_path / "audit/apif_identity_matches.csv"
    record_matches(
        audit,
        [
            {
                "games": [],
                "scope": {"league_id": "9991", "season": 2025},
                "complete": False,
                "raw_sha256": "a" * 64,
            }
        ],
        **kwargs,
    )
    assert (
        csv_rows(audit.with_name("apif_identity_scopes.csv"))[0]["league"]
        == "Explicit fixture league"
    )


def test_unapproved_audit_route_fails_before_writing(registry, tmp_path):
    from zucai.apif_identity_metrics import record_matches

    path = tmp_path / "audit/apif_identity_matches.csv"
    with pytest.raises(ValueError, match="核准"):
        record_matches(
            path,
            [
                {
                    "games": [],
                    "scope": {"league_id": "1002", "season": 2025},
                    "complete": False,
                    "raw_sha256": "a" * 64,
                }
            ],
        )
    assert not path.parent.exists()


def test_alias_and_identity_helpers_preserve_explicit_legacy_fixture_paths(
    registry, tmp_path, monkeypatch
):
    from zucai import aliases_snapshot, cloud_apis, cloud_identity_probe
    from zucai.sources import apif_reconcile

    apif = {
        "39": {"league": "Explicit bridge", "identity_only": True},
        "98": {"league": "Explicit domestic"},
    }
    apif_path, fdorg_path = tmp_path / "apif.json", tmp_path / "fdorg.json"
    apif_path.write_text(json.dumps(apif))
    fdorg_path.write_text(json.dumps({"FX": {"league": "Explicit FDORG"}}))
    monkeypatch.setattr(aliases_snapshot, "LEAGUES", apif_path)
    monkeypatch.setattr(cloud_identity_probe, "LEAGUES", apif_path)
    monkeypatch.setattr(apif_reconcile, "APIF_LEAGUES", apif_path)
    monkeypatch.setattr(apif_reconcile, "COMPETITIONS_PATH", fdorg_path)
    monkeypatch.setattr(cloud_apis, "COMPETITION_CODES", "FX")
    assert aliases_snapshot.selected_leagues("98") == {"98": apif["98"]}
    assert cloud_identity_probe.identity_codes("39") == {"39": apif["39"]}
    with pytest.raises(ValueError):
        cloud_identity_probe.identity_codes("1004")
    assert apif_reconcile._daily_domestic_leagues() == {
        "apif": {"Explicit domestic"},
        "fdorg": {"Explicit FDORG"},
    }
