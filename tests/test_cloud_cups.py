"""Offline cup backfill, archive roundtrip and unchanged daily request budgets."""

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import responses

from test_cloud_apis import environment as environment
from zucai.cloud_feeds import archive_info, export_feeds, main, restore_feeds

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "data/fixtures/t20"


def test_verified_api_backfill_defaults_and_july_boundary():
    from zucai.cloud_api_backfill import api_backfill_scopes

    assert api_backfill_scopes("fdorg", today=date(2026, 11, 2)) == [
        ("CL", {"competition": "CL", "season": year}) for year in range(2023, 2027)
    ]
    assert api_backfill_scopes("fdorg", today=date(2026, 6, 30))[-1][1]["season"] == 2025
    assert api_backfill_scopes("apif") == [
        (code, {"league_id": code, "season": year})
        for code in ("3", "848")
        for year in range(2021, 2027)
    ]


@pytest.mark.parametrize(
    ("provider", "codes", "first", "last"),
    [
        ("apif", "2", None, None),
        ("fdorg", "EC", None, None),
        ("fdorg", "WC", None, None),
        ("fdorg", "PL", None, None),
        ("apif", "3,3", None, None),
        ("apif", "", None, None),
        ("apif", "3", "2027", "2027"),
        ("apif", "98", "2021", "2021"),
        ("fdorg", "CL", "2022", "2023"),
        ("fdorg", "CL", "2324", "2627"),
        ("apif", "3", "2024", "2022"),
    ],
)
def test_unverified_or_out_of_scope_api_history_is_rejected_before_network(
    provider,
    codes,
    first,
    last,
):
    from zucai.cloud_api_backfill import api_backfill_scopes

    with responses.RequestsMock() as api, pytest.raises(ValueError):
        api_backfill_scopes(provider, codes, first, last, today=date(2026, 11, 2))
    assert not api.calls


def mock_season(api, provider, code, year=2024, failure=None):
    filename = f"fdorg_cl_{year}.json" if provider == "fdorg" else f"apif_{code}_{year}.json"
    doc = json.loads((FIXTURES / filename).read_bytes())
    if failure == "plan":
        doc = json.loads((FIXTURES / "apif_plan_2026.json").read_bytes())
        doc["parameters"] = {"league": code, "season": str(year)}
    if failure == "page":
        doc["paging"]["total"] = 2
    url = (
        f"https://api.football-data.org/v4/competitions/{code}/matches"
        if provider == "fdorg"
        else "https://v3.football.api-sports.io/fixtures"
    )
    query = {"season": str(year)}
    if provider == "apif":
        query["league"] = code
    api.get(url, json=doc, match=[responses.matchers.query_param_matcher(query)])
    if failure == "page":
        api.get(
            url,
            status=403,
            json={"errors": {"plan": "denied"}},
            match=[responses.matchers.query_param_matcher({**query, "page": "2"})],
        )
    return doc


def run_backfill(archive, work, monkeypatch, provider, codes, first=2024, last=2024):
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "feeds",
            "backfill",
            "--archive",
            str(archive),
            "--run-id",
            work.name,
            "--source",
            provider,
            "--leagues",
            codes,
            "--from",
            str(first),
            "--to",
            str(last),
        ],
    )
    main()
    return archive_info(archive)["collection"][provider]


@pytest.mark.parametrize(("provider", "codes"), [("fdorg", "CL"), ("apif", "3,848")])
def test_cup_backfill_roundtrips_and_second_run_inserts_zero(
    environment,
    tmp_path,
    monkeypatch,
    provider,
    codes,
):
    initial = archive_info(environment)
    aliases = {p: p.read_bytes() for p in (ROOT / "data/team_aliases").glob("*.json")}
    for label in ("first", "second"):
        with responses.RequestsMock() as api:
            for code in codes.split(","):
                mock_season(api, provider, code)
            report = run_backfill(environment, tmp_path / label, monkeypatch, provider, codes)
            assert report["requests"] == len(api.calls) == len(codes.split(","))
        assert report["status"] == "complete"
        assert (report["inserted"] > 0) if label == "first" else (report["inserted"] == 0)
        assert report["rejected_rows"] == report["suspected_duplicates"] == 0
        for item in report["scopes"]:
            assert item["parsed_matches"] > 0 and item["results_after"] > 0
            assert item["aligned_teams"] + item["unmatched_teams"] == item["teams"]
            assert item["raw"]["sha256"]
        assert (environment / f"state/audit/{provider}_cup_aliases.csv").is_file()
        summary = (tmp_path / label / "summary.md").read_text()
        assert "对齐率" in summary and "2024" in summary
    final = archive_info(environment)
    assert final["through_beijing"] == initial["through_beijing"]
    assert final["sources"]["jc"] == initial["sources"]["jc"]
    assert final["sources"][provider]["counts"]["results"] > 0
    restored = tmp_path / "restored/data/zucai.db"
    restore_feeds(environment, restored)
    with sqlite3.connect(restored) as a, sqlite3.connect(tmp_path / "second/data/zucai.db") as b:
        for table in ("teams", "matches", "results", "odds", "closing_odds"):
            assert (
                a.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                == b.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            )
        assert a.execute("SELECT DISTINCT available_source FROM results").fetchall() == [
            ("assumed",)
        ]
    assert {p: p.read_bytes() for p in aliases} == aliases


def test_repeated_apif_backfill_keeps_suspected_and_rejected_audits_unchanged(
    environment, tmp_path, monkeypatch
):
    import csv

    doc = json.loads((FIXTURES / "apif_3_2024.json").read_bytes())
    doc["response"][0]["score"]["fulltime"]["home"] = -1
    stamp = doc["response"][1]["fixture"]["date"]
    seed = tmp_path / "ambiguous/data/zucai.db"
    restore_feeds(environment, seed)
    with sqlite3.connect(seed) as con:
        con.execute("UPDATE matches SET league='欧罗巴',kickoff_utc=?", (stamp,))
        con.execute(
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES('jc:ambiguous','jc','欧罗巴',2,1,?,'scheduled')",
            (stamp,),
        )
    export_feeds(seed, environment, date(2026, 11, 2), "ambiguous")
    aliases = tmp_path / "empty.json"
    aliases.write_text("{}")
    monkeypatch.setattr("zucai.cloud_api_backfill.APIF_ALIASES", aliases)
    first = {}
    for label in ("dedup-first", "dedup-second"):
        with responses.RequestsMock() as api:
            api.get("https://v3.football.api-sports.io/fixtures?league=3&season=2024", json=doc)
            run_backfill(environment, tmp_path / label, monkeypatch, "apif", "3")
        for filename in ("apif_suspected_duplicates.csv", "apif_rejected_rows.csv"):
            path = environment / "state/audit" / filename
            assert list(csv.DictReader(path.open()))
            if label == "dedup-first":
                first[filename] = path.read_bytes()
            else:
                assert path.read_bytes() == first[filename]


@pytest.mark.parametrize("failure", ["plan", "page"])
def test_failed_apif_scope_keeps_other_cup_and_never_imports_partial_pages(
    environment,
    tmp_path,
    monkeypatch,
    failure,
):
    with responses.RequestsMock() as api:
        mock_season(api, "apif", "3", failure=failure)
        mock_season(api, "apif", "848")
        report = run_backfill(environment, tmp_path / failure, monkeypatch, "apif", "3,848")
        assert report["requests"] == len(api.calls) == (3 if failure == "page" else 2)
    assert report["status"] == "partial"
    assert report["scopes"][0]["status"] == "partial"
    assert report["scopes"][1]["status"] == "complete"
    with sqlite3.connect(tmp_path / failure / "data/zucai.db") as db:
        assert db.execute("SELECT count(*) FROM matches WHERE league='欧罗巴'").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM matches WHERE league='欧协联'").fetchone()[0] > 0


def test_backfill_respects_persisted_reserve_without_http(environment, tmp_path, monkeypatch):
    from zucai.cloud_feeds import digest

    quota = environment / "state/api_football_quota.json"
    quota.write_text(json.dumps({"date_utc": "2026-11-02", "used": 90, "remaining": 10}))
    manifest = json.loads((environment / "feed_manifest.json").read_bytes())
    manifest["files"]["state/api_football_quota.json"] = digest(quota)
    (environment / "feed_manifest.json").write_text(json.dumps(manifest))
    with responses.RequestsMock() as api:
        report = run_backfill(environment, tmp_path / "reserve", monkeypatch, "apif", "3,848")
        assert report["requests"] == len(api.calls) == 0
    assert report["status"] == "partial" and report["inserted"] == 0
    assert json.loads(quota.read_bytes())["remaining"] == 10


@pytest.mark.parametrize("command", ["export", "apis", "verify", "sync"])
def test_source_is_only_a_backfill_option(tmp_path, monkeypatch, command):
    monkeypatch.setattr(
        sys,
        "argv",
        ["feeds", command, "--archive", str(tmp_path), "--run-id", "test", "--source", "apif"],
    )
    with pytest.raises(SystemExit):
        main()


@pytest.mark.parametrize(("provider", "code"), [("fdorg", "CL"), ("apif", "3")])
def test_cup_history_links_only_empty_counterpart_and_preserves_jc_score(
    environment,
    tmp_path,
    monkeypatch,
    provider,
    code,
):
    from zucai.sources.api_football import ApiFootball
    from zucai.sources.football_data_org import FootballDataOrg

    source = FootballDataOrg() if provider == "fdorg" else ApiFootball()
    payload = (
        FIXTURES / ("fdorg_cl_2024.json" if provider == "fdorg" else "apif_3_2024.json")
    ).read_bytes()
    scope = (
        {"competition": code, "season": 2024}
        if provider == "fdorg"
        else {"league_id": code, "season": 2024}
    )
    game = source.parse(payload, **scope)[0]
    source.session.close()
    seed = tmp_path / "official/data/zucai.db"
    restore_feeds(environment, seed)
    with sqlite3.connect(seed) as db:
        db.execute(
            "UPDATE matches SET league=?,kickoff_utc=?,status='finished' WHERE source='jc'",
            (game.league, game.kickoff_utc.isoformat()),
        )
        db.execute(
            "INSERT INTO results(match_id,home_goals,away_goals,available_utc,available_source) "
            "VALUES('jc:1',9,8,'2026-11-01T00:00:00.000000Z','captured')"
        )
    export_feeds(seed, environment, date(2026, 11, 2), "official")
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    monkeypatch.setattr("zucai.cloud_api_backfill." + provider.upper() + "_ALIASES", empty)
    for label in ("linked", "repeat"):
        with responses.RequestsMock() as api:
            mock_season(api, provider, code)
            report = run_backfill(environment, tmp_path / label, monkeypatch, provider, code)
        assert report["status"] == "complete"
        assert report["linked_existing"] == 1
        assert report["scopes"][0]["aligned_teams"] == 2
        if provider == "apif":
            item = report["scopes"][0]
            # Ten distinct teams, two verified by JC. Linking that match does not
            # persist team aliases; a repeat must still verify the same evidence.
            assert item["alignment_rate_before"] == 0.0
            assert item["alignment_rate_after"] == 0.2
            assert item["new_linked_jc"] == (1 if label == "linked" else 0)
            assert report["new_linked_jc"] == item["new_linked_jc"]
            assert report["isolated_2026"] == 0
            assert item["jc_window_teams"] >= item["jc_window_aligned_teams"] == 2
        if label == "repeat":
            assert report["inserted"] == 0
        with sqlite3.connect(tmp_path / label / "data/zucai.db") as db:
            assert db.execute(
                "SELECT home_goals,away_goals,available_source FROM results WHERE match_id='jc:1'"
            ).fetchone() == (9, 8, "captured")
            assert db.execute(
                "SELECT counterpart_id FROM matches WHERE match_id='jc:1'"
            ).fetchone() == (f"{provider}:{game.provider_match_id}",)
            assert (
                db.execute(
                    "SELECT count(*) FROM teams WHERE name LIKE ?", (f"{provider}:team:%",)
                ).fetchone()[0]
                > 0
            )


def test_all_seasons_are_resolved_before_first_import(environment, tmp_path, monkeypatch):
    from zucai.sources.football_data_org import FootballDataOrg

    source = FootballDataOrg()
    games = source.parse(
        (FIXTURES / "fdorg_cl_2024.json").read_bytes(), competition="CL", season=2024
    )
    # Both IDs 78 and 86 also occur in the earlier 2023 fixture. Their only JC
    # witness here is in 2024, so importing 2023 before inference would lock in
    # provisional identities and make the existing identity guard reject 2024.
    game = games[1]
    assert (game.home_provider_id, game.away_provider_id) == (78, 86)
    source.session.close()
    seed = tmp_path / "official/data/zucai.db"
    restore_feeds(environment, seed)
    with sqlite3.connect(seed) as db:
        db.execute(
            "UPDATE matches SET league='欧冠',kickoff_utc=?", (game.kickoff_utc.isoformat(),)
        )
    export_feeds(seed, environment, date(2026, 11, 2), "official")
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    monkeypatch.setattr("zucai.cloud_api_backfill.FDORG_ALIASES", empty)
    with responses.RequestsMock() as api:
        mock_season(api, "fdorg", "CL", 2023)
        mock_season(api, "fdorg", "CL", 2024)
        report = run_backfill(
            environment, tmp_path / "union", monkeypatch, "fdorg", "CL", 2023, 2024
        )
    assert report["status"] == "complete" and report["rejected_rows"] == 0
    with sqlite3.connect(tmp_path / "union/data/zucai.db") as db:
        assert db.execute(
            "SELECT count(*) FROM teams WHERE name IN ('fdorg:team:78','fdorg:team:86')"
        ).fetchone() == (0,)


def test_daily_cups_share_existing_one_fdorg_and_three_apif_requests(
    environment,
    tmp_path,
    monkeypatch,
):
    from test_cloud_apis import CLOCK, run_apis
    from zucai.cloud_apis import COMPETITION_CODES

    with responses.RequestsMock() as api:
        doc = json.loads((FIXTURES / "fdorg_cl_2024.json").read_bytes())
        doc.pop("competition", None)
        doc["filters"] = {
            "dateFrom": "2026-10-30",
            "dateTo": "2026-11-06",
            "competitions": COMPETITION_CODES,
        }
        doc["matches"] = [doc["matches"][0]]
        doc["matches"][0]["utcDate"] = "2026-11-01T18:00:00Z"
        doc["resultSet"]["count"] = 1
        api.get(
            "https://api.football-data.org/v4/matches",
            json=doc,
            match=[responses.matchers.query_param_matcher(doc["filters"])],
        )
        for offset in range(3):
            day = (CLOCK - timedelta(days=offset)).date().isoformat()
            stamp = datetime.fromisoformat(day + "T00:00:00+00:00")
            doc = json.loads((FIXTURES / "apif_3_2024.json").read_bytes())
            other = json.loads((FIXTURES / "apif_848_2024.json").read_bytes())
            doc["parameters"], doc["response"] = (
                {"date": day},
                [doc["response"][0], other["response"][0]],
            )
            doc["results"] = 2
            for row in doc["response"]:
                row["fixture"].update(
                    id=row["fixture"]["id"] + offset,
                    date=stamp.isoformat(),
                    timestamp=int(stamp.timestamp()),
                )
            api.get(
                "https://v3.football.api-sports.io/fixtures",
                json=doc,
                match=[responses.matchers.query_param_matcher({"date": day})],
            )
        work = run_apis(tmp_path, monkeypatch, environment, "daily-cups", slot="late")
        assert len(api.calls) == 4
    report = archive_info(environment)["collection"]
    assert report["fdorg"]["requests"] == 1 and report["apif"]["requests"] == 3
    assert report["fdorg"]["cups"]["欧冠"]["matches_added"] == 1
    assert report["apif"]["cups"]["欧罗巴"]["matches_added"] == 3
    assert report["apif"]["cups"]["欧协联"]["matches_added"] == 3
    summary = (work / "summary.md").read_text()
    assert all(league in summary for league in ("欧冠", "欧罗巴", "欧协联"))


def mock_direct_history(api, provider, fail_last=False):
    """Derive a prior APIF season from the offline fixture, retaining team IDs."""
    if provider == "fdorg":
        first = mock_season(api, provider, "CL", 2023)
        last = json.loads((FIXTURES / "fdorg_cl_2024.json").read_bytes())
        url = "https://api.football-data.org/v4/competitions/CL/matches"
        query = {"season": "2024"}
    else:
        last = json.loads((FIXTURES / "apif_3_2024.json").read_bytes())
        first = json.loads(json.dumps(last))
        first["parameters"]["season"] = "2023"
        for row in first["response"]:
            row["league"]["season"] = 2023
            when = datetime.fromisoformat(row["fixture"]["date"]).replace(year=2023)
            row["fixture"].update(
                id=row["fixture"]["id"] + 10000000,
                date=when.isoformat(),
                timestamp=int(when.timestamp()),
            )
        url = "https://v3.football.api-sports.io/fixtures"
        api.get(
            url,
            json=first,
            match=[responses.matchers.query_param_matcher({"league": "3", "season": "2023"})],
        )
        query = {"league": "3", "season": "2024"}
    api.get(
        url,
        json=last if not fail_last else {"message": "denied"},
        status=403 if fail_last else 200,
        match=[responses.matchers.query_param_matcher(query)],
    )
    return last


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_direct_repeated_seasons_resolve_union_before_import(
    environment, tmp_path, monkeypatch, provider
):
    from zucai.sources.api_football import ApiFootball
    from zucai.sources.apif_command import fetch_apif
    from zucai.sources.fdorg_command import fetch_fdorg
    from zucai.sources.football_data_org import FootballDataOrg

    db = tmp_path / "direct/data/zucai.db"
    restore_feeds(environment, db)
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    monkeypatch.setattr(f"zucai.sources.{provider}_command.ALIASES", empty)
    with responses.RequestsMock() as api:
        last = mock_direct_history(api, provider)
        source = FootballDataOrg() if provider == "fdorg" else ApiFootball()
        scope = (
            dict(competition="CL", season=2024)
            if provider == "fdorg"
            else dict(league_id="3", season=2024)
        )
        game = source.parse(json.dumps(last).encode(), **scope)[1 if provider == "fdorg" else 0]
        source.session.close()
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE matches SET league=?,kickoff_utc=?",
                (game.league, game.kickoff_utc.isoformat()),
            )
        options = dict(
            seasons=[2023, 2024],
            assume=timedelta(hours=3),
            db=db,
            raw_root=tmp_path / "raw",
            audit_dir=tmp_path / "audit",
        )
        result = (
            fetch_fdorg(competition="CL", **options)
            if provider == "fdorg"
            else fetch_apif(league_id="3", quota_path=tmp_path / "quota.json", **options)
        )
        assert result["requests"] == len(api.calls) == 2
    assert result["rejected_rows"] == 0
    assert result["linked_existing"] == 1
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT count(*) FROM teams WHERE name IN (?,?)",
            (
                f"{provider}:team:{game.home_provider_id}",
                f"{provider}:team:{game.away_provider_id}",
            ),
        ).fetchone() == (0,)


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_direct_cup_union_keeps_completed_seasons_when_later_fetch_fails(
    environment,
    tmp_path,
    monkeypatch,
    provider,
):
    from zucai.sources.apif_command import fetch_apif
    from zucai.sources.fdorg_command import fetch_fdorg

    db = tmp_path / "direct/data/zucai.db"
    restore_feeds(environment, db)
    with responses.RequestsMock() as api:
        mock_direct_history(api, provider, fail_last=True)
        options = dict(
            seasons=[2023, 2024],
            assume=timedelta(hours=3),
            db=db,
            raw_root=tmp_path / "raw",
            audit_dir=tmp_path / "audit",
        )
        with pytest.raises(ValueError, match="403"):
            if provider == "fdorg":
                fetch_fdorg(competition="CL", **options)
            else:
                fetch_apif(league_id="3", quota_path=tmp_path / "quota.json", **options)
        assert len(api.calls) == 2
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT count(*) FROM matches WHERE source=?", (provider,)
        ).fetchone() == (5,)
