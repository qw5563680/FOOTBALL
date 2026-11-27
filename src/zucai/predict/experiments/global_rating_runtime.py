"""Daily T100 fit using the same score-only history view as the approved replay."""

from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from zucai.db import get_engine
from zucai.models import Match, Result, Team
from zucai.predict.backtest import read_archive_evidence, read_history
from zucai.predict.base import utc
from zucai.predict.dixon_coles import InsufficientHistory
from zucai.predict.experiments.global_rating import (
    SOURCES,
    RatingParameters,
    RatingResult,
    fit_ratings,
)
from zucai.predict.experiments.global_rating_contract import RATING_NAME, RATING_VERSION
from zucai.predict.experiments.parallel import AVAILABILITY_MODES, sha
from zucai.predict.pooled import select_training_history, source_eligible_history

PARAMETERS = RatingParameters(16, 65, 200, 0.6)
PROTOCOL_SHA256 = "77e20057f4c9c081904c736092fc05cb6fab30eb5e7b25a10402bf8645ec366e"
SELECTION_SHA256 = "aa7bdaa5b154d469d91475512222537e12c6deecda25ddac1c83e568088a1d2a"


@contextmanager
def score_session(database, end):
    """Hide unavailable labels even from eager result-source validation."""
    upper = utc(end).to_pydatetime()

    def could_know(available, kickoff):
        if available is None:
            return False
        return datetime.fromisoformat(kickoff) < datetime.fromisoformat(available) < upper

    engine = get_engine(database)
    try:
        with engine.connect() as connection:
            connection.connection.driver_connection.create_function(
                "t100_could_know", 2, could_know
            )
            connection.exec_driver_sql("""
                CREATE TEMP VIEW results AS
                SELECT result.* FROM main.results AS result
                JOIN main.matches AS match ON match.match_id = result.match_id
                WHERE t100_could_know(result.available_utc, match.kickoff_utc)
            """)
            connection.exec_driver_sql("PRAGMA query_only=ON")
            with Session(bind=connection) as session:
                yield session
    finally:
        engine.dispose()


def rating_history(session, cutoff, leagues, failures, *, archive_evidence):
    """Select one safe source representative per entity at the requested cutoff."""
    for league in leagues:
        try:
            games = read_history(
                session,
                source=",".join(sorted(SOURCES)),
                training_leagues=[league],
                source_selection="cutoff",
                archive_evidence=archive_evidence,
            )
            known = source_eligible_history(games)["available_utc"]
            training = select_training_history(games.loc[known.index[known < cutoff]], cutoff)
        except InsufficientHistory:
            continue
        except ValueError as exc:
            failures.append(dict(league=league, cutoff_utc=cutoff.isoformat(), reason=str(exc)))
            continue
        for row in training.to_dict("records"):
            yield RatingResult.from_row(row)


def _forecast(session, snapshot, target):
    cutoff = snapshot.as_of_utc
    kickoff = utc(target["kickoff_utc"])
    target_cutoff = utc(target["target_cutoff_utc"])
    model_cutoff = target["formal_model_cutoff_utc"]
    if model_cutoff is None or utc(model_cutoff) != cutoff or not cutoff <= target_cutoff < kickoff:
        return dict(fallback_reason="primary_cutoff_mismatch")
    match = session.get(Match, target["match_id"])
    if (
        match is None
        or match.status != "scheduled"
        or (match.source, match.league, utc(match.kickoff_utc))
        != (target["source"], target["league"], kickoff)
    ):
        return dict(fallback_reason="target_identity_unverified")
    teams = (("home", match.home_team_id), ("away", match.away_team_id))
    if any(session.get(Team, team_id).name != target[side] for side, team_id in teams):
        return dict(fallback_reason="target_identity_unverified")
    forecast = snapshot.predict(match.home_team_id, match.away_team_id, kickoff_utc=kickoff)
    if forecast.probabilities is None:
        return dict(
            fallback_reason=forecast.reason,
            missing_details=[
                dict(team_id=team_id, side=side, team=target[side])
                for side, team_id in teams
                if team_id in forecast.missing_team_ids
            ],
        )
    return dict(
        status="predicted",
        fallback_reason=None,
        model_probabilities=dict(zip("hda", forecast.probabilities, strict=True)),
        features={
            side: dict(team_id=team_id, rating=snapshot.ratings[team_id]) for side, team_id in teams
        },
    )


def fit_and_forecast(database, targets, *, cutoff):
    at = utc(cutoff)
    failures = []
    with score_session(database, at) as session:
        leagues = session.scalars(
            select(Match.league)
            .join(Result, Match.match_id == Result.match_id)
            .where(Match.status == "finished", Match.source.in_(SOURCES))
            .distinct()
            .order_by(Match.league)
        ).all()
        history = rating_history(
            session, at, leagues, failures, archive_evidence=read_archive_evidence(session)
        )
        (snapshot,) = fit_ratings(history, as_of=at, parameters=(PARAMETERS,))
        document = snapshot.as_dict()
        forecasts = {
            target["match_id"]: {
                **_forecast(session, snapshot, target),
                "formal_model_version": target["formal_model_version"],
                "target_cutoff_utc": target["target_cutoff_utc"],
                "coefficients_sha256": sha(document["parameters"]),
            }
            for target in targets
        }
    fit = dict(
        model_name=RATING_NAME,
        model_version=RATING_VERSION,
        training_cutoff_utc=at.isoformat(),
        protocol_sha256=PROTOCOL_SHA256,
        parameter_selection_sha256=SELECTION_SHA256,
        snapshot_sha256=sha(document),
        snapshot=document,
        scope_failures=failures,
        **AVAILABILITY_MODES,
    )
    return forecasts, fit
