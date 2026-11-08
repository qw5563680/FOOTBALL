"""National-team fixtures use verified names, dates and a separate national corpus."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from zucai.db import get_engine, init_db
from zucai.models import Match, Team
from zucai.predict.parameters import read_parameters
from zucai.sources.sfc import _map_fixture


def test_production_model_registered_for_national_sources():
    leagues = read_parameters()[0]["leagues"]
    assert set(leagues.get("欧国联", {}).get("sources", [])) == {"jc", "apif"}
    assert not leagues["欧国联"]["p2_ready"]
    assert "欧国联" not in leagues["欧战"]["training_leagues"]


def test_national_abbreviations_match_exact_sides_clock_and_provider_ids(tmp_path):
    engine = get_engine(tmp_path / "game.db")
    init_db(engine)
    when = datetime(2026, 11, 12, 17, tzinfo=UTC)
    with Session(engine) as session:
        home = Team(name="亚美尼亚", aliases=["apif:欧国联:team:1094"])
        away = Team(name="塞浦路斯", aliases=["apif:欧国联:team:1106"])
        session.add_all([home, away])
        session.flush()
        session.add(
            Match(
                match_id="apif:1528959",
                league="欧国联",
                source="apif",
                home_team_id=home.id,
                away_team_id=away.id,
                status="scheduled",
                kickoff_utc=when,
            )
        )
        session.flush()
        fx = SimpleNamespace(
            fixture_id="1398678",
            league="欧国联",
            home="亚美尼",
            away="塞浦路",
            home_id="101",
            away_id="84",
            kickoff_utc=when,
        )

        def link():
            return _map_fixture(session, fx, {}, mapped_only=True)[0]

        assert link() and link().match_id == "apif:1528959"
        fx.kickoff_utc += timedelta(minutes=1)
        assert link() is None
        fx.kickoff_utc = when
        fx.home_id = "84"
        with pytest.raises(ValueError, match="编号"):
            link()
        fx.home_id = "101"
        fx.league = "欧冠"
        assert link() is None
    engine.dispose()


def test_five_editions_resume_and_refresh_only_current_once_daily(tmp_path):
    from zucai import nations

    times = []

    def fetch(**kwargs):
        (year,) = kwargs["seasons"]
        times.append(year)
        return dict(
            files=[dict(raw_sha256="a" * 64, outcomes=[{"outcome": "unchanged"}])],
            requests=1,
            rejected_rows=0,
            suspected_duplicates=0,
        )

    when = datetime(2026, 11, 9, tzinfo=UTC)
    common = {"audit_dir": tmp_path / "audit"}
    for date in (when, when, when + timedelta(days=1)):
        list(nations.collect_editions(date, tmp_path, common, fetch=fetch))
    assert times == [2026, 2024, 2022, 2020, 2018, 2026]


def test_still_open_issue_extends_window_and_closed_issue_disappears(tmp_path):
    import json
    from pathlib import Path

    from zucai.cloud_predict import open_national_fixtures
    from zucai.models import SfcIssue

    original = json.loads((Path(__file__).parents[1] / "data/fixtures/t50/26163.json").read_bytes())
    engine = get_engine(tmp_path / "games.db")
    init_db(engine)
    with Session(engine) as session:
        sides = [Team(name=name, aliases=[]) for name in ("亚美尼亚", "塞浦路斯")]
        session.add_all(sides)
        session.flush()
        game = Match(
            match_id="apif:1528959",
            league="欧国联",
            source="apif",
            home_team_id=sides[0].id,
            away_team_id=sides[1].id,
            status="scheduled",
            kickoff_utc=datetime(2026, 11, 12, 17, tzinfo=UTC),
        )
        issue = SfcIssue(**original)
        session.add_all([game, issue])
        session.flush()
        at = datetime(2026, 11, 9, tzinfo=UTC)
        assert open_national_fixtures(session, at) == {
            datetime(2026, 11, 13).date(): {"apif:1528959"}
        }
        assert open_national_fixtures(session, datetime.fromisoformat(issue.sale_end_utc)) == {}
        assert open_national_fixtures(session, at - timedelta(days=2)) == {}
    engine.dispose()


def test_existing_jc_counterpart_cannot_bypass_national_fixture_clock_or_orientation(tmp_path):
    engine = get_engine(tmp_path / "game.db")
    init_db(engine)
    when = datetime(2026, 11, 12, 17, tzinfo=UTC)
    with Session(engine) as session:
        home, away = (Team(name=n, aliases=[]) for n in ("亚美尼亚", "塞浦路斯"))
        session.add_all([home, away])
        session.flush()
        session.add(
            Match(
                match_id="jc:1",
                counterpart_id="c500:1",
                league="欧国联",
                source="jc",
                home_team_id=home.id,
                away_team_id=away.id,
                status="scheduled",
                kickoff_utc=when,
            )
        )
        session.flush()
        fx = SimpleNamespace(
            fixture_id="1",
            league="欧国联",
            home="亚美尼",
            away="塞浦路",
            home_id="101",
            away_id="84",
            kickoff_utc=when + timedelta(days=1),
        )
        with pytest.raises(ValueError):
            _map_fixture(session, fx, {}, mapped_only=True)
        fx.kickoff_utc = when
        fx.home, fx.away, fx.home_id, fx.away_id = "塞浦路", "亚美尼", "84", "101"
        with pytest.raises(ValueError):
            _map_fixture(session, fx, {}, mapped_only=True)
    engine.dispose()


def test_digest_shows_concurrent_open_issues_before_higher_closed_issue(tmp_path):
    import json

    from zucai.sfc_digest import section

    folder = tmp_path / "state/sfc"
    folder.mkdir(parents=True)
    files = []
    for number, end in (("26163", "2026-11-12"), ("26164", "2026-11-13"), ("26165", "2026-11-08")):
        issue = dict(
            issue_no=number,
            sale_end_utc=end + "T12:00:00Z",
            observed_utc="2026-11-08T05:00:00Z",
            snapshot=[],
        )
        path = folder / (number + ".json")
        path.write_text(json.dumps({"issue": issue}))
        files.append(path.relative_to(tmp_path).as_posix())
    text = "\n".join(
        section(tmp_path, {"files": files}, {}, datetime(2026, 11, 9, tzinfo=UTC), str)
    )
    assert "26163" in text and "26164" in text and "26165" not in text


def test_advance_sfc_freeze_does_not_cover_jc_day_or_hide_missing_jc_run():
    from zucai.digest_context import jc_digest_batch, prediction_window

    day = "2026-11-13"
    at = datetime(2026, 11, 12, tzinfo=UTC)
    normal = {
        "input": {"kickoff_date_beijing": day},
        "mode": "prematch",
        "generated_utc": "2026-11-09T01:00:00Z",
        "predictions": [{"match_id": "jc:1"}, {"match_id": "apif:2", "sfc_only": True}],
    }
    early = {**normal, "input": {**normal["input"], "scope": "sfc_only"}}
    assert jc_digest_batch(early) is None
    assert jc_digest_batch(normal)["predictions"] == [{"match_id": "jc:1"}]
    assert day in prediction_window([], at)["missing_batch_dates"]


def test_open_national_issue_will_not_show_old_picks_after_fixture_changes():
    from zucai.sfc_digest import _issue_lines

    now = datetime(2026, 11, 9, tzinfo=UTC)
    fixture = dict(
        seq=1,
        fixture_id="1",
        league="欧国联",
        home="法国",
        away="德国",
        kickoff_utc="2026-11-13T19:45:00Z",
        source_ended=False,
    )
    issue = dict(issue_no="26163", sale_end_utc="2026-11-12T14:00:00Z", snapshot=[fixture])
    report = dict(
        issue_no="26163",
        generated_utc="2026-11-08T10:00:00Z",
        published_utc="2026-11-08T11:00:00Z",
        rows=[{**fixture, "away": "比利时"}],
    )
    text = "\n".join(_issue_lines("state/sfc/26163.json", issue, [("old", report)], [], now, str))
    assert "德国" in text and "比利时" not in text and "不展示任九" in text
