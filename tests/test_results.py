import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses

from zucai.cli import main

FIX = Path(__file__).parents[1] / "data/fixtures"
BASE = "https://webapi.sporttery.cn/gateway/uniform/football/"
PAGE = BASE + "getUniformMatchResultV1.qry"
HEAD = BASE + "getMatchHeadV1.qry"
DAY = "2026-10-31"


def test_real_zero_match_day_uses_page_number_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    empty = {
        "success": True,
        "errorCode": "0",
        "value": {
            "pageNo": 0,
            "pages": 0,
            "pageSize": 0,
            "resultCount": 0,
            "total": 0,
            "matchResult": [],
        },
    }
    with responses.RequestsMock() as api:
        api.add(responses.GET, PAGE, json=empty)
        assert main(["fetch", "results", "--from", "2026-10-01", "--to", "2026-10-01"]) == 0


def test_backfill_official_page_links_real_kickoff_and_preserves_first_observation(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    with responses.RequestsMock() as api:
        api.add(responses.GET, PAGE, body=(FIX / "sporttery_results_20261031.json").read_bytes())
        api.add(responses.GET, HEAD, body=(FIX / "sporttery_head_2042404.json").read_bytes())
        assert main(["fetch", "results", "--from", DAY, "--to", DAY]) == 0
        with sqlite3.connect("data/zucai.db") as db:
            row = db.execute(
                "select home_goals,away_goals,ht_home_goals,ht_away_goals,"
                "available_source,available_utc from results"
            ).fetchone()
            assert row[:5] == (3, 0, 1, 0, "captured")
            assert db.execute("select match_id,kickoff_utc,status from matches").fetchone() == (
                "jc:2042404",
                "2026-10-31T15:15:00.000000Z",
                "finished",
            )
        assert (
            main(["fetch", "results", "--from", DAY, "--to", DAY, "--assume-known-after", "3h"])
            == 0
        )
        with sqlite3.connect("data/zucai.db") as db:
            assert db.execute("select available_utc from results").fetchone() == (row[5],)
            assert db.execute("select count(*) from results").fetchone() == (1,)


def test_incomplete_or_repeated_pagination_cannot_commit_half_a_day(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = json.loads((FIX / "sporttery_results_20261031.json").read_bytes())
    p["value"].update(pages=2, total=2, pageSize=1)
    with responses.RequestsMock() as api:
        api.add(responses.GET, PAGE, json=p)
        assert main(["fetch", "results", "--from", DAY, "--to", DAY]) != 0
        assert len(api.calls) == 2
    with sqlite3.connect("data/zucai.db") as db:
        assert db.execute("select count(*) from results").fetchone() == (0,)


def test_corrections_voids_and_stale_imports_leave_frozen_predictions_alone(tmp_path, monkeypatch):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Match, Prediction, Result
    from zucai.sources.results import SportteryResults, apply_result

    monkeypatch.chdir(tmp_path)
    eng = get_engine()
    init_db(eng)
    base = datetime(2026, 11, 1, 12, tzinfo=UTC)
    kickoff = datetime(2026, 10, 31, 15, 15, tzinfo=UTC)
    game = replace(
        SportteryResults().parse((FIX / "sporttery_results_20261031.json").read_bytes())[0],
        kickoff_utc=kickoff,
    )

    def write(g, h, assume=None):
        with session_scope(eng) as s:
            return apply_result(s, g, base + timedelta(hours=h), assume)

    assert write(game, 0, timedelta(hours=3)) == "assumed"
    with session_scope(eng) as s:
        assert s.get(Result, game.match_id).available_utc.startswith("2026-10-31T18:15:00")
        s.add(
            Prediction(
                match_id=game.match_id,
                model_name="frozen",
                model_version="v1",
                p_home=0.4,
                p_draw=0.3,
                p_away=0.3,
                score_matrix=[],
                created_utc=kickoff - timedelta(hours=1),
            )
        )
    assert write(game, 1) == "unchanged"
    changed = replace(game, full=(1, 1))
    assert write(changed, 2, timedelta(hours=3)) == "captured"
    write(game, 1)
    with session_scope(eng) as s:
        res = s.get(Result, game.match_id)
        assert (res.home_goals, res.away_goals, res.available_source) == (1, 1, "captured")
        assert datetime.fromisoformat(res.available_utc) == base + timedelta(hours=2)
    write(replace(game, status="cancelled", full=None, half=None), 3)
    write(changed, 2)
    with session_scope(eng) as s:
        assert s.get(Result, game.match_id) is None
        assert s.get(Match, game.match_id).status == "cancelled"
        assert s.get(Prediction, 1).p_home == 0.4
    eng.dispose()


def test_backup_uses_settled_exact_90_minute_score_and_keeps_half_unknown():
    from zucai.sources.c500 import C500JcHistory

    raw = (FIX / "c500_20261031.html").read_bytes()
    (game,) = C500JcHistory().parse(raw)
    assert (game.match_id, game.official_id, game.home, game.full, game.half) == (
        "c500:1419347",
        "jc:2042385",
        "冈山绿雉",
        (0, 4),
        None,
    )
    assert C500JcHistory().parse(raw.replace(b"sbetbtn-ok", b"sbetbtn-off")) == []


def test_pending_cup_and_refunds_do_not_import_an_extra_time_or_placeholder_score():
    from zucai.sources.results import SportteryResults

    p = json.loads((FIX / "sporttery_results_20261031.json").read_bytes())
    orig = p["value"]["matchResult"][0]
    p["value"]["matchResult"] = [
        dict(
            orig,
            matchId=11,
            sectionsNo999="1:1",
            sectionsNo1="1:1",
            penaltyScore="4:3",
            overtimeScore="2:2",
        ),
        dict(orig, matchId=12, matchResultStatus="1", sectionsNo999="0:0"),
        dict(orig, matchId=13, poolStatus="Refund", sectionsNo999="无效场次"),
        dict(orig, matchId=14, matchResultStatus="0", poolStatus="", sectionsNo999="取消"),
    ]
    r = SportteryResults().parse(json.dumps(p).encode())
    assert [(v.match_id, v.status, v.full) for v in r] == [
        ("jc:11", "finished", (1, 1)),
        ("jc:13", "cancelled", None),
        ("jc:14", "cancelled", None),
    ]


def test_unchanged_assumptions_are_not_counted_as_new_and_void_reversal_is_timed(
    tmp_path, monkeypatch
):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Result
    from zucai.sources.results import SportteryResults, apply_result

    monkeypatch.chdir(tmp_path)
    engine = get_engine()
    init_db(engine)
    t = datetime(2026, 11, 1, 12, tzinfo=UTC)
    g = replace(
        SportteryResults().parse((FIX / "sporttery_results_20261031.json").read_bytes())[0],
        kickoff_utc=t - timedelta(days=1),
    )
    with session_scope(engine) as session:
        assert apply_result(session, g, t, timedelta(hours=3)) == "assumed"
        assert apply_result(session, g, t + timedelta(hours=1), timedelta(hours=3)) == "unchanged"
        with pytest.raises(ValueError, match="冲突"):
            apply_result(session, replace(g, half=(0, 0)), t + timedelta(hours=1))
        apply_result(
            session, replace(g, status="cancelled", full=None, half=None), t + timedelta(hours=2)
        )
        assert apply_result(session, g, t + timedelta(hours=3), timedelta(hours=3)) == "captured"
        session.flush()
        assert datetime.fromisoformat(
            session.get(Result, g.match_id).available_utc
        ) == t + timedelta(hours=3)
    engine.dispose()


def test_an_invalid_archived_header_does_not_block_later_valid_retry(tmp_path, monkeypatch):
    from zucai.sources.results import SportteryResults

    monkeypatch.chdir(tmp_path)
    feed = SportteryResults()
    g = feed.parse((FIX / "sporttery_results_20261031.json").read_bytes())[0]
    invalid = b'{"success":false,"errorCode":"P0001"}'
    feed.store.save("sporttery-head", "match-2042404", invalid, "json", status_code=200)
    with responses.RequestsMock() as api:
        api.add(responses.GET, HEAD, body=(FIX / "sporttery_head_2042404.json").read_bytes())
        assert feed.kickoff(g) == datetime(2026, 10, 31, 15, 15, tzinfo=UTC)


def test_only_verified_publication_replaces_unchanged_assumed_time(tmp_path, monkeypatch):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Result
    from zucai.sources.results import SportteryResults, apply_result

    monkeypatch.chdir(tmp_path)
    eng = get_engine()
    init_db(eng)
    k = datetime(2026, 10, 31, 15, 15, tzinfo=UTC)
    t = k + timedelta(days=2)
    game = replace(
        SportteryResults().parse((FIX / "sporttery_results_20261031.json").read_bytes())[0],
        kickoff_utc=k,
    )
    with session_scope(eng) as s:
        assert apply_result(s, game, t, timedelta(hours=3)) == "assumed"
        assert apply_result(s, game, t + timedelta(hours=1)) == "unchanged"
        assert s.get(Result, game.match_id).available_source == "assumed"
        pub = k + timedelta(hours=2)
        assert (
            apply_result(s, replace(game, published_utc=pub), t + timedelta(hours=2)) == "published"
        )
        assert (
            apply_result(
                s, replace(game, published_utc=pub + timedelta(minutes=8)), t + timedelta(hours=3)
            )
            == "unchanged"
        )
        assert datetime.fromisoformat(s.get(Result, game.match_id).available_utc) == pub
    eng.dispose()


def test_legacy_database_keeps_old_rows_and_clocks_score_corrections(tmp_path):
    from zucai.db import get_engine, init_db

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "create table results (match_id text primary key, home_goals int, "
            "away_goals int, ht_home_goals int, ht_away_goals int)"
        )
        db.execute("insert into results values ('jc:old',2,1,1,0)")
    engine = get_engine(path)
    init_db(engine)
    init_db(engine)
    with sqlite3.connect(path) as db:
        assert db.execute(
            "select home_goals, available_utc, available_source from results"
        ).fetchone() == (2, None, None)
        db.execute("update results set home_goals=1")
        when, source = db.execute("select available_utc,available_source from results").fetchone()
        assert source == "captured" and datetime.fromisoformat(when).tzinfo == UTC
    engine.dispose()


def test_backup_can_fill_identical_fixture_but_cannot_replace_official_score(tmp_path, monkeypatch):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Match, Result
    from zucai.sources.c500 import C500JcHistory
    from zucai.sources.results import apply_result
    from zucai.sources.sporttery import JcMatch, save_schedule

    monkeypatch.chdir(tmp_path)
    engine = get_engine()
    init_db(engine)
    b = C500JcHistory().parse((FIX / "c500_20261031.html").read_bytes())[0]
    t = datetime(2026, 11, 1, 12, tzinfo=UTC)
    home = JcMatch(
        b.official_id, b.play_date, b.jc_num, b.league, b.home, b.away, b.kickoff_utc, []
    )
    with session_scope(engine) as s:
        save_schedule(s, [home], b.kickoff_utc - timedelta(hours=1))
        assert apply_result(s, b, t) == "captured"
        assert s.get(Match, b.official_id).result_provider == "c500"
        official = replace(b, match_id=b.official_id, official_id=None, half=(0, 1))
        apply_result(s, official, t + timedelta(minutes=1))
        assert apply_result(s, replace(b, full=(0, 1)), t + timedelta(minutes=2)) == "unchanged"
        assert s.get(Result, b.official_id).away_goals == 4
        assert s.get(Result, b.match_id) is None
    engine.dispose()


def test_fallback_first_reconciles_canonical_id_without_losing_frozen_predictions(
    tmp_path, monkeypatch
):
    from sqlalchemy import func, select

    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Match, Prediction, Result
    from zucai.sources.c500 import C500JcHistory
    from zucai.sources.results import apply_result

    monkeypatch.chdir(tmp_path)
    engine = get_engine()
    init_db(engine)
    b = C500JcHistory().parse((FIX / "c500_20261031.html").read_bytes())[0]
    t = datetime(2026, 11, 1, 12, tzinfo=UTC)
    with session_scope(engine) as s:
        apply_result(s, b, t)
        s.add(
            Prediction(
                match_id=b.match_id,
                model_name="archive",
                model_version="original",
                p_home=0.5,
                p_draw=0.2,
                p_away=0.3,
                score_matrix=[],
                created_utc=b.kickoff_utc - timedelta(days=1),
            )
        )
        s.flush()
        apply_result(
            s,
            replace(b, match_id=b.official_id, official_id=None, half=(0, 1)),
            t + timedelta(hours=1),
        )
        apply_result(s, b, t + timedelta(hours=2))
        assert s.scalar(select(func.count()).select_from(Match)) == 1
        assert s.scalar(select(func.count()).select_from(Result)) == 1
        assert s.get(Match, b.official_id).counterpart_id == b.match_id
        assert (s.get(Prediction, 1).match_id, s.get(Prediction, 1).p_home) == (b.official_id, 0.5)
    engine.dispose()


def test_later_relisting_with_new_kickoff_reopens_a_deferred_fixture(tmp_path, monkeypatch):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Match
    from zucai.sources.results import GameResult, apply_result
    from zucai.sources.sporttery import JcMatch, OddsQuote, save_schedule

    monkeypatch.chdir(tmp_path)
    engine = get_engine()
    init_db(engine)
    t = datetime(2026, 11, 1, 12, tzinfo=UTC)
    g = GameResult("jc:1", "联赛", "周日001", "主队", "客队", t.date(), "cancelled", None, None, t)
    q = JcMatch(
        g.match_id,
        g.play_date,
        g.jc_num,
        g.league,
        g.home,
        g.away,
        t + timedelta(days=4),
        [OddsQuote("had", 0, opt, 2.0) for opt in "hda"],
    )
    with session_scope(engine) as s:
        apply_result(s, g, t + timedelta(hours=1))
        assert save_schedule(s, [q], t) == (0, 0)
        assert save_schedule(s, [q], t + timedelta(hours=2)) == (1, 3)
        apply_result(s, g, t + timedelta(hours=3))
        m = s.get(Match, g.match_id)
        assert m.status == "scheduled" and datetime.fromisoformat(m.kickoff_utc) == q.kickoff_utc
    engine.dispose()
