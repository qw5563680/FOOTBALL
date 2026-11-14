"""The evidence command keeps numeric choices and the real gate unchanged."""

import json
from pathlib import Path

import pytest

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.predict.revalidate_sources import revalidate

ROOT = Path(__file__).parents[1]


def test_offline_revalidation_only_writes_a_candidate_and_never_retunes(tmp_path):
    source = ROOT / "data/model/params.json"
    before = source.read_bytes()
    fixture = json.loads((ROOT / "data/fixtures/t63/source-latency.json").read_text())
    database = tmp_path / "fixture.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        teams = {
            name: Team(name=name)
            for name in {r[k] for r in fixture["base_rows"] for k in ("home", "away")}
        }
        session.add_all(teams.values())
        session.flush()
        for row in fixture["base_rows"]:
            session.add(
                Match(
                    match_id=row["match_id"],
                    source=row["source"],
                    league=row["league"],
                    kickoff_utc=row["kickoff_utc"],
                    home_team_id=teams[row["home"]].id,
                    away_team_id=teams[row["away"]].id,
                    status="finished",
                )
            )
            session.flush()
            session.add(
                Result(
                    **{
                        k: row[k]
                        for k in [
                            "match_id",
                            "home_goals",
                            "away_goals",
                            "available_utc",
                            "available_source",
                        ]
                    }
                )
            )
    engine.dispose()
    db_before = database.read_bytes()
    output = tmp_path / "candidate"
    report = revalidate(
        db=database, output=output, league="日职", as_of="2026-11-14T00:00Z", params_file=source
    )
    assert report["parameter_selection_performed"] is False
    assert report["gate"]["p2_ready"] is False  # Fixture has no latest two complete seasons.
    candidate = json.loads((output / "candidate-params.json").read_text())
    original = json.loads(before)
    assert candidate["gate"] == original["gate"]
    for key in ("half_life_days", "draw_calibration", "evaluation_windows", "selection_as_of_utc"):
        assert candidate["leagues"]["日职"][key] == original["leagues"]["日职"][key]
    assert database.read_bytes() == db_before and source.read_bytes() == before
    with pytest.raises(ValueError, match="新目录"):
        revalidate(db=database, output=output, league="日职", as_of="2026-11-14T00:00Z")
