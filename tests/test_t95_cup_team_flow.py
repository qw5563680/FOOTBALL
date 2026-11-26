"""Exercise the archive-to-reconciliation path with synthetic UEFA team identities."""

import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import pytest

from test_t68_identity_archive_entry import CAPTURED, raw
from test_t68_jc_kickoffs import catalogue, save_json
from zucai import cloud_feeds, identity_binding_archive, identity_canonical
from zucai import jc_catalogue_kickoffs as kickoffs
from zucai.db import get_engine, init_db
from zucai.sources import apif_reconcile, apif_scoped_reconcile
from zucai.sources.sporttery import BEIJING

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures"
DAY = date(2026, 10, 31)
CATEGORY = "club/men/senior/first"
OWNER = {
    "status": "approved",
    "approved_by": "qw5563680",
    "approved_utc": CAPTURED,
    "approval_pr": "https://github.com/qw5563680/FOOTBALL/pull/999999999",
}
TEAM_LABELS = ("合成新甲", "合成新乙", "合成既有甲", "合成既有乙")
JC_TEAM_IDS = (991001, 991002, 991003, 991004)
PROVIDER_IDS = (992001, 992002, 992003, 992004)


def _season_original(root, code, fixtures):
    template = json.loads((FIXTURES / "t68/identity/apif-page.json").read_bytes())
    original = template["response"][0]
    rows = []
    for mid, when, home, away in fixtures:
        rows.append(
            {
                **original,
                "fixture": {
                    **original["fixture"],
                    "id": mid,
                    "date": when,
                    "timestamp": int(datetime.fromisoformat(when).timestamp()),
                },
                "league": dict(id=int(code), season=2026),
                "teams": {
                    side: {"id": PROVIDER_IDS[i], "name": f"Synthetic Club {i}"}
                    for side, i in (("home", home), ("away", away))
                },
            }
        )
    page = {
        **template,
        "parameters": dict(league=code, season="2026"),
        "results": len(rows),
        "response": rows,
    }
    raw(
        root,
        "state/catalogue/apif-cup-test/backfill/api-football/2026-11-14/"
        "fixtures_202611140100Z_00112233445566778899aabbccddeeff.json",
        page,
        f"https://v3.football.api-sports.io/fixtures?league={code}&season=2026",
    )


def _official_results(root, official_league, fixtures):
    match_ids = tuple(993001 + i for i in range(len(fixtures)))
    catalogue(root, match_ids, [official_league] * len(match_ids))
    relative = f"state/catalogue/catalogue/jc/{DAY:%Y-%m}/{DAY}-p1.json"
    path = root / relative
    result = json.loads(path.read_bytes())
    for row, (_, _when, home, away) in zip(result["value"]["matchResult"], fixtures, strict=True):
        row.update(
            homeTeam=TEAM_LABELS[home],
            allHomeTeam=TEAM_LABELS[home],
            homeTeamId=JC_TEAM_IDS[home],
            awayTeam=TEAM_LABELS[away],
            allAwayTeam=TEAM_LABELS[away],
            awayTeamId=JC_TEAM_IDS[away],
            sectionsNo1="1:0",
            sectionsNo999="2:1",
        )
    meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
    raw(root, relative, result, meta["url"])
    return match_ids


def _official_heads(root, official_ids, fixtures):
    template = json.loads((FIXTURES / "t68/heads/success.json").read_bytes())
    for mid, (_, when, home, away) in zip(official_ids, fixtures, strict=True):
        head = {
            **template,
            "value": {
                **template["value"],
                "sportteryMatchId": mid,
                "sportteryHomeTeamId": JC_TEAM_IDS[home],
                "sportteryAwayTeamId": JC_TEAM_IDS[away],
                "matchDateTime": datetime.fromisoformat(when)
                .astimezone(BEIJING)
                .strftime("%Y-%m-%d %H:%M"),
                "homeTeamShortName": TEAM_LABELS[home],
                "awayTeamShortName": TEAM_LABELS[away],
                "fullCourtGoal": "2:1",
            },
        }
        raw(
            root,
            f"state/catalogue/cup-heads/jc-heads/{mid}.json",
            head,
            f"{kickoffs.API}getMatchHeadV1.qry?source=web&sportteryMatchId={mid}",
        )


def _match(con, mid, league, when, home, away):
    con.execute(
        "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,kickoff_utc,status) "
        "VALUES(?,?,?,?,?,?,'finished')",
        (f"apif:{mid}", "apif", league, home, away, when),
    )
    con.execute(
        "INSERT INTO results(match_id,home_goals,away_goals,available_utc) VALUES(?,?,?,?)",
        (f"apif:{mid}", 2, 1, CAPTURED),
    )


def _working_main(monkeypatch, archive, command, run_id, *args):
    work = Path.cwd() / run_id
    work.mkdir()
    database = work / "data" / "zucai.db"
    with monkeypatch.context() as run:
        run.chdir(work)
        run.setattr(
            sys,
            "argv",
            ["cloud_feeds", command, "--archive", str(archive), "--run-id", run_id, *args],
        )
        cloud_feeds.main()
    return cloud_feeds.archive_info(archive), database


def _rows(database):
    with closing(sqlite3.connect(database)) as con:
        matches = con.execute("SELECT * FROM matches ORDER BY match_id").fetchall()
        results = con.execute("SELECT * FROM results ORDER BY match_id").fetchall()
        return matches, results


@pytest.mark.parametrize(
    ("code", "official_league", "league"),
    [
        ("3", 70, "欧罗巴"),
        ("848", 1033103, "欧协联"),
    ],
)
def test_verified_cup_archive_can_apply_approvals_and_merge_stock(
    tmp_path, monkeypatch, code, official_league, league
):
    monkeypatch.setattr(kickoffs, "CATALOGUE_SOURCE_RUN", "catalogue")
    monkeypatch.setattr(kickoffs, "CATALOGUE_WINDOW", (DAY, DAY))
    fixtures = [
        (994001, "2026-10-31T02:15:00+00:00", 0, 1),
        (994002, "2026-10-31T07:15:00+00:00", 2, 3),
        (994003, "2026-10-31T12:15:00+00:00", 3, 2),
    ]
    seed = tmp_path / "seed" / "feeds.db"
    engine = get_engine(seed)
    init_db(engine)
    engine.dispose()
    legacy_provider, legacy_mid = 992005, 994004
    with sqlite3.connect(seed) as con:
        con.executemany(
            "INSERT INTO teams VALUES(?,?,'[]')",
            [
                (1, f"apif:team:{PROVIDER_IDS[0]}"),
                (2, f"apif:team:{PROVIDER_IDS[1]}"),
                (3, "Synthetic Previous Name A"),
                (4, "Synthetic Previous Name B"),
                (5, f"apif:team:{legacy_provider}"),
                (6, "合成旧名单"),
            ],
        )
        for mid, when, h, a in fixtures:
            _match(con, mid, league, when, h + 1, a + 1)
        _match(con, legacy_mid, league, "2026-10-28T12:15:00+00:00", 5, 3)
        # Reusing a numeric ID outside this cup cannot authorize a cross-league merge.
        _match(con, 994005, "未批准赛事", "2026-10-30T12:15:00+00:00", 1, 3)

    alias_path = tmp_path / "apif-aliases.json"
    old_aliases = {
        str(pid): {"canonical": name, **OWNER}
        for pid, name in zip(
            (*PROVIDER_IDS[2:], legacy_provider),
            ("Synthetic Previous Name A", "Synthetic Previous Name B", "合成旧名单"),
            strict=True,
        )
    }
    save_json(alias_path, {league: old_aliases})
    monkeypatch.setattr(identity_binding_archive, "FORMAL", alias_path)
    monkeypatch.setattr(apif_scoped_reconcile, "ALIASES", alias_path)
    monkeypatch.setattr(apif_reconcile, "ALIASES", alias_path)

    _season_original(seed.parent, code, fixtures)
    ids = _official_results(seed.parent, official_league, fixtures)
    archive = tmp_path / "archive"
    cloud_feeds.export_feeds(seed, archive, DAY, "before-clocks")
    before = identity_canonical.propose(archive, codes=[code])
    assert {v["status"] for v in before["existing_team_verdicts"].values()} == {"pending_review"}
    assert not before["link_decisions"]

    _official_heads(seed.parent, ids, fixtures)
    cloud_feeds.export_feeds(seed, archive, DAY, "with-clocks")
    plan = tmp_path / "plan.json"
    identity_canonical.main(
        ["plan", "--archive", str(archive), "--codes", code, "--out", str(plan)]
    )
    proposal = json.loads(plan.read_bytes())
    verdicts = proposal["existing_team_verdicts"]
    assert [verdicts[str(n)]["status"] for n in JC_TEAM_IDS] == [
        "new",
        "new",
        "existing",
        "existing",
    ]
    create = [
        row for row in proposal["decisions"] if verdicts[row["official_team_id"]]["status"] == "new"
    ]
    link = proposal["link_decisions"]
    assert {int(row["official_team_id"]) for row in create} == set(JC_TEAM_IDS[:2])
    assert {int(row["official_team_id"]) for row in link} == set(JC_TEAM_IDS[2:])
    assert all(row["blockers"] == [] for row in [*create, *link])
    approvals = tmp_path / "approvals.json"
    review = OWNER["approval_pr"] + "#synthetic-review"
    save_json(
        approvals,
        {
            **proposal,
            "decisions": [{**row, **OWNER, "review_url": review} for row in create],
            "link_decisions": [{**row, **OWNER, "review_url": review} for row in link],
        },
    )

    work = tmp_path / "runner"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))
    monkeypatch.setattr(cloud_feeds, "CANONICAL_APPROVALS_PATH", approvals)
    initial_matches, initial_results = _rows(seed)
    applied, database = _working_main(monkeypatch, archive, "canonical-apply", "t95-create")
    result = applied["collection"]["canonical_apply"]
    assert (result["created"], result["linked"], result["already_applied"]) == (2, 2, 0)
    assert _rows(database) == (initial_matches, initial_results)
    identity_facts = identity_binding_archive.load_archive(archive, leagues=[code])
    for official, label in zip(JC_TEAM_IDS, TEAM_LABELS, strict=True):
        row = identity_facts["teams"][f"jc-team:{official}"]
        assert row["identity_status"] == "existing_numbered", row
        assert row["name"] == label and row["project_team_id"] is not None

    # This is a disposable, explicitly approved alias file. Preserve legacy entries verbatim.
    new_aliases = {
        str(pid): {
            "canonical": label,
            **OWNER,
            "binding_scope": {"leagues": [league], "category": CATEGORY},
        }
        for pid, label in zip(PROVIDER_IDS[:2], TEAM_LABELS[:2], strict=True)
    }
    save_json(alias_path, {league: {**old_aliases, **new_aliases}})
    merged, database = _working_main(
        monkeypatch, archive, "scoped-reconcile", "t95-merge", "--leagues", code
    )
    result = merged["collection"]["apif_scoped"]
    assert (result["status"], result["rekeyed_matches"], result["linked"]) == ("complete", 1, 0)
    with closing(sqlite3.connect(database)) as con:
        assert (
            con.execute(
                "SELECT h.name,a.name FROM matches m "
                "JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id "
                "WHERE m.match_id='apif:994001'"
            ).fetchone()
            == TEAM_LABELS[:2]
        )
        assert con.execute(
            "SELECT home_team_id FROM matches WHERE match_id IN ('apif:994004','apif:994005') "
            "ORDER BY match_id"
        ).fetchall() == [(5,), (1,)]
    matches, results = _rows(database)
    assert len(matches) == len(initial_matches)
    assert results == initial_results
    repeated, database = _working_main(
        monkeypatch, archive, "scoped-reconcile", "t95-repeat", "--leagues", code
    )
    again = repeated["collection"]["apif_scoped"]
    assert (again["rekeyed_matches"], again["linked"]) == (0, 0)
    assert _rows(database) == (matches, results)
    assert {k: json.loads(alias_path.read_bytes())[league][k] for k in old_aliases} == old_aliases

    # The legacy reconciler still recognizes the old unscoped alias in this cup.
    legacy = apif_reconcile.reconcile(database, "t95-legacy")
    assert legacy["rekeyed_matches"] == 1
    with closing(sqlite3.connect(database)) as con:
        assert con.execute(
            "SELECT home_team_id FROM matches WHERE match_id=?", (f"apif:{legacy_mid}",)
        ).fetchone() == (6,)
    assert apif_reconcile.reconcile(database, "t95-legacy-repeat")["rekeyed_matches"] == 0
    assert len(_rows(database)[0]) == len(initial_matches)
    assert _rows(database)[1] == initial_results
