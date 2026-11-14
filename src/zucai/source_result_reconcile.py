"""Record newly observed exact T63 result relationships without editing old rows."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select

from zucai.db import get_engine, session_scope
from zucai.models import Match, Team
from zucai.predict.training_sources import actual_result_leagues
from zucai.result_source_identity import record_result_identity
from zucai.result_source_links import record_result_link


def reconcile_source_results(database, observed_utc):
    """Daily local identity check; each new relation starts at this real check time."""
    if not isinstance(observed_utc, datetime) or observed_utc.utcoffset() is None:
        raise ValueError("赛果关系核验时间须带时区")
    report = dict(linked=0, unchanged=0, skipped=0, reasons={})
    reasons = Counter()
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            matches = list(
                session.scalars(
                    select(Match).where(
                        Match.league.in_(actual_result_leagues()),
                        Match.source.in_(("apif", "fd", "fdorg", "jc")),
                    )
                )
            )
            teams = {team.id: team for team in session.scalars(select(Team))}
            groups = defaultdict(list)
            for row in matches:
                groups[row.league].append(row)
            for paid in (row for row in matches if row.source in {"apif", "fdorg"}):
                pair = paid.home_team_id, paid.away_team_id
                prefix = f"{paid.source}:{paid.league}:team:"
                if any(
                    not any(alias.startswith(prefix) for alias in teams[t].aliases) for t in pair
                ):
                    reasons["unverified_provider_identity"] += 1
                    report["skipped"] += 1
                    continue
                try:
                    record_result_identity(session, paid, observed_utc)
                except ValueError:
                    reasons["unverified_provider_identity"] += 1
                    report["skipped"] += 1
                    continue
                when = datetime.fromisoformat(paid.kickoff_utc)
                candidates = defaultdict(list)
                for other in groups[paid.league]:
                    if (
                        (paid.source == "fdorg" and other.source == "apif")
                        or other.match_id == paid.match_id
                        or abs(datetime.fromisoformat(other.kickoff_utc) - when)
                        > timedelta(hours=3)
                    ):
                        continue
                    if not set(pair).isdisjoint((other.home_team_id, other.away_team_id)):
                        candidates[other.source].append(other)
                if any(
                    source == paid.source
                    or len(rows) != 1
                    or (rows[0].home_team_id, rows[0].away_team_id) != pair
                    for source, rows in candidates.items()
                ):
                    reasons["ambiguous_or_reversed_identity"] += 1
                    report["skipped"] += 1
                    continue
                for rows in candidates.values():
                    try:
                        with session.begin_nested():
                            added = record_result_link(session, paid, rows[0], observed_utc)
                        report["linked" if added else "unchanged"] += 1
                    except ValueError:
                        reasons["conflicting_saved_relationship"] += 1
                        report["skipped"] += 1
    finally:
        engine.dispose()
    report["reasons"] = dict(sorted(reasons.items()))
    return report
