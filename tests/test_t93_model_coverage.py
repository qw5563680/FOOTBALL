from datetime import timedelta
from pathlib import Path

import pytest

from test_prediction import CUTOFF
from test_prediction import games as games
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team
from zucai.nations import LEAGUE as NATIONS_LEAGUE
from zucai.predict.base import SFC_FIXTURE_SOURCE
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.parameters import read_parameters
from zucai.predict.pooled import configuration
from zucai.report.batch import _models, predict_batch
from zucai.report.render import load_batch, write_report
from zucai.sources.c500 import SfcFixture
from zucai.sources.sfc import _map_fixture

PARAMS = Path(__file__).resolve().parents[1] / "data/model/params.json"


def test_configured_model_routes_and_every_single_league_sfc_forecast(games, tmp_path):
    entries = read_parameters(PARAMS)[0]["leagues"]
    assert entries
    paths, expected_routes, single_leagues = [], {}, {}
    for league, entry in entries.items():
        pool = configuration(league, entry)
        targets = pool["leagues"] if pool else [league]
        sources = entry["sources"]
        source = next((name for name in sources if name != "jc"), "jc")
        history = games.assign(
            source=source,
            league=[targets[i % len(targets)] for i in range(len(games))],
        )
        home, away = "A", "B"
        if league == NATIONS_LEAGUE:
            home, away = "亚美尼亚", "塞浦路斯"
            history = history.replace({"A": home, "B": away})
        history.attrs["sources"] = sources
        if pool and pool["training_leagues"]:
            history.attrs["training_leagues"] = pool["training_leagues"]
        model = DixonColes(
            half_life_days=entry["half_life_days"],
            draw_calibration=entry.get("draw_calibration", False),
            pooled=bool(pool),
        ).fit(history, as_of=CUTOFF)
        path = tmp_path / f"{league}.pkl"
        model.save(path)
        paths.append(path)
        expected_routes[league] = {
            (name, target) for name in {*sources, SFC_FIXTURE_SOURCE} for target in targets
        }
        if not pool:
            single_leagues[league] = (source if source != "jc" else SFC_FIXTURE_SOURCE, home, away)

    registry = _models(paths, CUTOFF, PARAMS, sfc_only=True)
    assert set(registry) == set().union(*expected_routes.values())
    for league, routes in expected_routes.items():
        assert {key for key, (model, _, _) in registry.items() if model.league == league} == routes

    engine = get_engine(tmp_path / "fixtures.db")
    init_db(engine)
    try:
        with session_scope(engine) as session:
            teams = {name: Team(name=name) for name in ("A", "B", "亚美尼亚", "塞浦路斯")}
            session.add_all(teams.values())
            session.flush()
            matches = {}
            kickoff = CUTOFF + timedelta(hours=10)
            for i, (league, (source, home, away)) in enumerate(single_leagues.items(), start=1):
                fixture_id = str(10_000 + i)
                key = f"{source}:{fixture_id}"
                session.add(
                    Match(
                        match_id=key,
                        source=source,
                        league=league,
                        home_team_id=teams[home].id,
                        away_team_id=teams[away].id,
                        kickoff_utc=kickoff,
                        status="scheduled",
                    )
                )
                # T78's fixture-only source requires a known historical appearance per side.
                if source == SFC_FIXTURE_SOURCE:
                    session.add(
                        Match(
                            match_id=f"jc:history-{fixture_id}",
                            source="jc",
                            league=league,
                            home_team_id=teams[home].id,
                            away_team_id=teams[away].id,
                            kickoff_utc=kickoff - timedelta(days=7),
                            status="finished",
                        )
                    )
                session.flush()
                fixture = SfcFixture(
                    seq=1,
                    fixture_id=fixture_id,
                    league=league,
                    home=home,
                    away=away,
                    kickoff_utc=kickoff,
                    ended=False,
                    home_id="101" if league == NATIONS_LEAGUE else None,
                    away_id="84" if league == NATIONS_LEAGUE else None,
                )
                matched, status, _, _ = _map_fixture(session, fixture, {}, mapped_only=True)
                assert status == "matched" and matched.match_id == key, league
                matches[key] = (league, source)

            batch = predict_batch(
                session,
                model_files=paths,
                as_of=CUTOFF,
                generated=CUTOFF,
                match_ids=list(matches),
                sfc_only_ids=matches,
                params_file=PARAMS,
                persist=False,
            )
        rows = {row["match_id"]: row for row in batch["predictions"]}
        assert rows.keys() == matches.keys()
        for key, (league, source) in matches.items():
            row = rows[key]
            assert (row["league"], row["source"], row["status"]) == (league, source, "predicted"), (
                row
            )
            assert row["sfc_only"] is True
            assert row["model_name"] == "dixon-coles"
            assert row["metadata"]["unknown_team_prior"] is False
            assert sum(row["model_probabilities"].values()) == pytest.approx(1)
        assert load_batch(write_report(batch, tmp_path / "frozen")) == batch
    finally:
        engine.dispose()
