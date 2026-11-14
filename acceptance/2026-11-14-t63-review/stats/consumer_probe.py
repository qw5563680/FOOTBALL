"""Synthetic read-only consumer comparison; no training or external requests."""

import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from zucai.db import get_engine, init_db, session_scope
from zucai.features.injuries import InjuryFeatures
from zucai.models import ApifFixtureIndex, Match, Odds, Team
from zucai.predict.experiments.command import attach_market
from zucai.predict.experiments.history import unified_history
from zucai.result_source_links import record_result_link

ROOT = Path(__file__).parent


def inspect_consumers(session):
    builder, history = unified_history(session, provider="apif")
    injuries = InjuryFeatures(session)
    _, injury_reason = injuries._index(injuries.matches["apif:1"])
    rows = [dict(match_id="apif:1", actual=0, feature_cutoff="2026-11-10T10:00Z", variants={})]
    attach_market(session, rows, builder)
    return dict(aligned_history_match_ids=[game["history_match_ids"] for game in history],
                injury_index_missing_reason=injury_reason, market_available=rows[0]["variants"]["market"] is not None)


def main():
    with TemporaryDirectory() as directory:
        engine = get_engine(Path(directory) / "consumer.db")
        init_db(engine)
        with session_scope(engine) as session:
            session.add_all([Team(id=1, name="Synthetic Home"), Team(id=2, name="Synthetic Away")])
            session.flush()
            for source, kickoff in (("jc", "2026-11-10T10:00Z"), ("apif", "2026-11-10T10:30Z")):
                session.add(Match(match_id=source+":1", source=source, league="英超", home_team_id=1, away_team_id=2,
                                  kickoff_utc=kickoff, status="finished"))
            session.flush()
            record_result_link(session, session.get(Match, "apif:1"), session.get(Match, "jc:1"), datetime.fromisoformat("2026-11-10T08:00Z"))
            session.add(ApifFixtureIndex(provider_fixture_id="1", match_id="jc:1", league="英超", provider_league_id=39,
                                        season=2026, kickoff_utc="2026-11-10T10:00Z", home_provider_id=1, away_provider_id=2,
                                        home_team_id=1, away_team_id=2, captured_utc="2026-11-10T08:00Z", raw_sha256="a"*64,
                                        raw_location="fixture://synthetic-consumer-probe", parser_version="synthetic", evidence={}))
            for selection in "hda":
                session.add(Odds(match_id="jc:1", pool="had", line=0, selection=selection, price=3.0, snapshot_utc="2026-11-10T09:00Z"))
            session.flush()
            proof_only = inspect_consumers(session)
            session.get(Match, "jc:1").counterpart_id = "apif:1"
            session.flush()
            with_legacy_pointer = inspect_consumers(session)
            report = dict(disclaimer="Synthetic proof/legacy-pointer contrast, not a real API response or deployed-feature acceptance.",
                          proof_only=proof_only, with_legacy_pointer=with_legacy_pointer)
            (ROOT / "remaining-consumer-probe.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))
        engine.dispose()


if __name__ == "__main__":
    main()
