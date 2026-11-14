"""Replay clocks use synthetic T09/T10 captures, never invented historical proof."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.result_source_identity import load_result_identities
from zucai.sources.api_common import ApiGame
from zucai.sources.apif_ingest import import_apif_games
from zucai.sources.fdorg_ingest import import_fdorg_games

SEEN = datetime(2026, 10, 12, 10, tzinfo=UTC)
IMPORTED = SEEN + timedelta(days=20)


@pytest.fixture(params=["apif", "fdorg"])
def capture(request, tmp_path):
    source = request.param
    folder = "t10" if source == "apif" else "t09"
    sample = json.loads(
        (Path(__file__).parents[1] / f"data/fixtures/{folder}/synthetic_ingest.json").read_bytes()
    )
    values = sample["fixture"]
    values["kickoff_utc"] = datetime.fromisoformat(values["kickoff_utc"])
    values["full"], values["half"] = tuple(values["full"]), tuple(values["half"])
    engine = get_engine(tmp_path / "clocks.db")
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all([Team(name="北港"), Team(name="东岸")])
    yield engine, ApiGame(**values), sample["aliases"]
    engine.dispose()


def ingest(session, game, aliases, *, seen=SEEN, imported=IMPORTED, actual=True):
    importer = import_apif_games if game.provider == "apif" else import_fdorg_games
    mode = {"retain_results" if game.provider == "apif" else "actual_results": actual}
    return importer(
        session,
        [game],
        seen=seen,
        assume=timedelta(hours=3),
        aliases=aliases,
        imported_utc=imported,
        **mode,
    )


def test_old_capture_cannot_backdate_result_or_identity_and_repeat_keeps_first_proof(capture):
    engine, game, aliases = capture
    key = f"{game.provider}:{game.provider_match_id}"
    with session_scope(engine) as session:
        assert ingest(session, game, aliases)["inserted"] == 1
        stored = session.get(Result, key)
        assert datetime.fromisoformat(stored.available_utc) == IMPORTED
        assert stored.available_source == "captured"
        assert datetime.fromisoformat(load_result_identities(session)[key]) == IMPORTED
        assert (
            ingest(session, game, aliases, imported=IMPORTED + timedelta(days=1))["unchanged"] == 1
        )
        assert datetime.fromisoformat(stored.available_utc) == IMPORTED
        assert datetime.fromisoformat(load_result_identities(session)[key]) == IMPORTED


def test_unknown_numeric_identity_has_no_proof_even_though_capture_is_stored(capture):
    engine, game, _ = capture
    with session_scope(engine) as session:
        assert ingest(session, game, {})["inserted"] == 1
        stored = session.get(Result, f"{game.provider}:{game.provider_match_id}")
        assert datetime.fromisoformat(stored.available_utc) == IMPORTED
        assert load_result_identities(session) == {}


def test_legacy_import_does_not_gain_actual_clock_constraints(capture):
    engine, game, aliases = capture
    with session_scope(engine) as session:
        assert (
            ingest(session, game, aliases, imported=SEEN - timedelta(days=1), actual=False)[
                "inserted"
            ]
            == 1
        )
        stored = session.get(Result, f"{game.provider}:{game.provider_match_id}")
        assert stored.available_source == "assumed"
        assert load_result_identities(session) == {}


def test_later_source_correction_survives_delayed_import_and_uses_new_actual_clock(capture):
    engine, game, aliases = capture
    key = f"{game.provider}:{game.provider_match_id}"
    with session_scope(engine) as session:
        ingest(session, game, aliases)
        corrected = replace(game, full=(3, 1))
        later_import = IMPORTED + timedelta(days=1)
        result = ingest(
            session, corrected, aliases, seen=SEEN + timedelta(days=1), imported=later_import
        )
        assert result["corrected"] == 1
        stored = session.get(Result, key)
        session.refresh(stored)
        assert (stored.home_goals, stored.away_goals) == (3, 1)
        assert datetime.fromisoformat(stored.available_utc) == later_import
        assert datetime.fromisoformat(
            session.get(Match, key).result_checked_utc
        ) == SEEN + timedelta(days=1)
        assert datetime.fromisoformat(load_result_identities(session)[key]) == IMPORTED


def test_same_source_capture_conflict_is_rejected_despite_later_import(capture):
    engine, game, aliases = capture
    key = f"{game.provider}:{game.provider_match_id}"
    with session_scope(engine) as session:
        ingest(session, game, aliases)
        result = ingest(
            session, replace(game, full=(3, 1)), aliases, imported=IMPORTED + timedelta(days=1)
        )
        assert result["rejected_rows"] == 1
        assert "同一采集" in result["rejected"][0]["reason"]
        stored = session.get(Result, key)
        assert (stored.home_goals, stored.away_goals) == game.full
        assert datetime.fromisoformat(stored.available_utc) == IMPORTED


def test_nonfinal_availability_repair_cannot_backdate_to_an_archived_capture(capture):
    engine, game, aliases = capture
    key = f"{game.provider}:{game.provider_match_id}"
    with session_scope(engine) as session:
        ingest(session, game, aliases, actual=False)
        stored = session.get(Result, key)
        assert stored.available_source == "assumed"
        session.get(Match, key).result_checked_utc = SEEN
        ongoing = replace(
            game, status="1H" if game.provider == "apif" else "IN_PLAY", full=None, half=None
        )
        result = ingest(session, ongoing, aliases, seen=SEEN - timedelta(hours=1))
        assert result["availability_corrected"] == 1
        session.refresh(stored)
        assert datetime.fromisoformat(stored.available_utc) == IMPORTED
        assert stored.available_source == "captured"
