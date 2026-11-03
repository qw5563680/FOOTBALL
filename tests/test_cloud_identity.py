"""Identity evidence generation has no authority to import or approve anything."""

import csv
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
import responses

from test_cloud_apis import environment as environment
from test_identity_probe import FIXTURE, QUOTA
from test_identity_probe import actions as actions
from zucai.cloud_feeds import restore_feeds

ROOT = Path(__file__).parents[1]


def metadata_proof(database):
    from zucai.cloud_identity_probe import FIELDS

    path = database.parent / "state/audit/apif_league_probes.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, FIELDS)
        writer.writeheader()
        for row in FIXTURE["leagues"]:
            writer.writerow(
                dict(
                    run_id="offline-probe",
                    observed_utc="2026-11-02T00:00:00Z",
                    league=row["code"],
                    api_name=row["api_name"],
                    country=row["country"],
                    seasons="[2025,2026]",
                    http=200,
                    raw_sha256="a" * 64,
                    outcome="ok",
                    reason="",
                )
            )


def fixture_doc(league, season):
    doc = json.loads((ROOT / "data/fixtures/t20/apif_3_2024.json").read_bytes())
    doc["parameters"] = {"league": league["code"], "season": str(season)}
    doc["response"] = doc["response"][:1]
    doc["results"] = 1
    row = doc["response"][0]
    row["league"].update(id=int(league["code"]), name=league["api_name"], season=season)
    instant = datetime.fromisoformat(str(season) + FIXTURE["kickoff_suffix"])
    row["fixture"].update(
        id=int(league["code"]) * 10000 + season,
        date=instant.isoformat(),
        timestamp=int(instant.timestamp()),
    )
    for side, team in zip(("home", "away"), FIXTURE["teams"], strict=True):
        row["teams"][side].update(id=team["id"], name=team["name"])
    return doc


@pytest.fixture
def identity_database(environment, tmp_path, monkeypatch):
    database = tmp_path / "identity/data/zucai.db"
    restore_feeds(environment, database)
    metadata_proof(database)
    with sqlite3.connect(database) as con:
        con.execute(
            "INSERT INTO teams VALUES(3,?,'[]'),(4,?,'[]')",
            tuple(t["canonical"] for t in FIXTURE["teams"]),
        )
        for index, league in enumerate(FIXTURE["leagues"]):
            source = "jc" if index < 3 else "fd"
            for year in FIXTURE["seasons"]:
                con.execute(
                    "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                    "kickoff_utc,status) VALUES(?,?,?,3,4,?,'scheduled')",
                    (
                        f"{source}:{league['fd_code']}:{year}",
                        source,
                        league["league"],
                        str(year) + FIXTURE["kickoff_suffix"],
                    ),
                )
    fd_aliases = tmp_path / "fd_aliases.json"
    fd_aliases.write_text(
        json.dumps(
            {
                league["league"]: {
                    team["name"]: dict(canonical=team["canonical"], status="auto_verified")
                    for team in FIXTURE["teams"]
                }
                for league in FIXTURE["leagues"]
            }
        )
    )
    monkeypatch.setattr("zucai.cloud_identity.FD_ALIASES", fd_aliases)
    return database


def test_identity_seven_leagues_two_seasons_fourteen_requests_no_import(identity_database, actions):
    from zucai.cloud_identity import collect_identity

    database = identity_database
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    aliases_file = ROOT / "data/team_aliases/api_football.json"
    original = aliases_file.read_bytes()
    with responses.RequestsMock() as http:
        for league in FIXTURE["leagues"]:
            for year in FIXTURE["seasons"]:
                http.get(
                    f"https://v3.football.api-sports.io/fixtures?league={league['code']}&season={year}",
                    json=fixture_doc(league, year),
                    headers=QUOTA["paid_headers"],
                )
        report = collect_identity(database, "identity-1")
    assert report["status"] == "complete" and report["requests"] == 14
    assert report["auto_verified"] == 14 and report["limit_day"] == 7500
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert aliases_file.read_bytes() == original
    candidates = json.loads((database.parent / "state/alias_candidates/apif.json").read_bytes())
    assert set(candidates) == {league["league"] for league in FIXTURE["leagues"]}
    sources = set()
    for entries in candidates.values():
        assert set(entries) == {"9101", "9102"}
        for record in entries.values():
            assert record["status"] == "auto_verified"
            evidence = record["evidence"][0]
            sources.add(evidence["counterpart_id"].split(":")[0])
            assert len(evidence["raw_sha256"]) == 64 and evidence["difference_minutes"] == 0
    assert sources == {"jc", "fd"}
    rows = list(csv.DictReader((database.parent / "state/audit/apif_identity.csv").open()))
    assert len(rows) == 14 and all(row["status"] == "auto_verified" for row in rows)


@pytest.mark.parametrize("problem", ["pagination", "rejected", "ambiguous"])
def test_incomplete_or_ambiguous_season_never_activates_identities(
    identity_database, actions, problem
):
    from zucai.cloud_identity import collect_identity

    doc = fixture_doc(FIXTURE["leagues"][0], 2025)
    if problem == "pagination":
        doc["paging"]["total"] = 2
    elif problem == "rejected":
        doc["response"][0]["score"]["fulltime"]["home"] = -1
    else:
        competitor = json.loads(json.dumps(doc["response"][0]))
        competitor["fixture"]["id"] += 1
        competitor["teams"]["home"]["id"] += 10
        competitor["teams"]["away"]["id"] += 10
        doc["response"].append(competitor)
        doc["results"] += 1
    with responses.RequestsMock() as http:
        http.get("https://v3.football.api-sports.io/fixtures?league=39&season=2025", json=doc)
        report = collect_identity(identity_database, "identity-bad", codes="39", seasons="2025")
    assert report["requests"] == 1 and report["auto_verified"] == 0
    candidates = json.loads(
        (identity_database.parent / "state/alias_candidates/apif.json").read_bytes()
    )
    assert not candidates.get("英超")


@pytest.mark.parametrize(
    ("codes", "seasons"), [("3", "2025"), ("39", "2024"), ("39,39", "2025"), ("39", "2025,2025")]
)
def test_identity_scope_rejected_before_any_request(actions, tmp_path, codes, seasons):
    from zucai.cloud_identity import collect_identity

    with responses.RequestsMock() as http, pytest.raises(ValueError):
        collect_identity(tmp_path / "data/zucai.db", "bad", codes=codes, seasons=seasons)
    assert not http.calls


def test_identity_archive_roundtrip_keeps_database_watermark_and_frozen_forecasts(
    identity_database, actions, tmp_path, monkeypatch
):
    import sys
    from datetime import date

    from test_cloud_archive import table_rows
    from test_forecast_archive import staged_run
    from zucai.cloud_feeds import archive_info, export_feeds, main
    from zucai.forecast_archive import publish_forecasts

    archive = tmp_path / "identity-archive"
    original = export_feeds(identity_database, archive, date(2026, 11, 2), "seed")
    stage, run = staged_run(tmp_path / "stage", "frozen-forecast")
    frozen_manifest = publish_forecasts(archive, stage, run)
    frozen = {
        name: (archive / name).read_bytes()
        for name in [*frozen_manifest["files"], "forecast_manifest.json"]
    }
    for attempt in range(2):
        work = tmp_path / f"identity-{attempt}"
        work.mkdir()
        monkeypatch.chdir(work)
        monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "feeds",
                "identity",
                "--archive",
                str(archive),
                "--run-id",
                work.name,
                "--source",
                "apif",
                "--leagues",
                "39",
                "--seasons",
                "2025,2026",
            ],
        )
        with responses.RequestsMock() as http:
            for year in FIXTURE["seasons"]:
                http.get(
                    f"https://v3.football.api-sports.io/fixtures?league=39&season={year}",
                    json=fixture_doc(FIXTURE["leagues"][0], year),
                    headers=QUOTA["paid_headers"],
                )
            main()
            assert len(http.calls) == 2
        meta = archive_info(archive)
        assert meta["sources"] == original["sources"]
        assert meta["through_beijing"] == original["through_beijing"]
        restored = tmp_path / f"restored-{attempt}/data/zucai.db"
        restore_feeds(archive, restored)
        assert table_rows(restored) == table_rows(identity_database)
        assert all((archive / name).read_bytes() == raw for name, raw in frozen.items())
        assert "7500" in (work / "summary.md").read_text()
    candidates = json.loads((archive / "state/alias_candidates/apif.json").read_bytes())
    assert all(row["status"] == "auto_verified" for row in candidates["英超"].values())


@pytest.mark.parametrize("problem", ["unverified_name", "duplicate_fd"])
def test_fd_counterparts_require_verified_names_and_keep_competing_fd_rows(
    identity_database, problem
):
    from zucai.cloud_identity import FD_ALIASES, FD_LEAGUES
    from zucai.identity_evidence import read_counterparts

    league = FIXTURE["leagues"][3]
    if problem == "unverified_name":
        aliases = json.loads(FD_ALIASES.read_bytes())
        for row in aliases[league["league"]].values():
            row["status"] = "proposed"
        FD_ALIASES.write_text(json.dumps(aliases))
    else:
        with sqlite3.connect(identity_database) as con:
            con.execute(
                "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) VALUES(?,?,?,3,4,?,'scheduled')",
                (
                    f"fd:{league['fd_code']}:competing",
                    "fd",
                    league["league"],
                    "2025" + FIXTURE["kickoff_suffix"],
                ),
            )
    rows, _ = read_counterparts(identity_database, league, FD_LEAGUES, FD_ALIASES)
    assert len(rows) == (0 if problem == "unverified_name" else 3)
