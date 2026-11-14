"""Identify unresolved FD identities for exclusion, never as same-match proofs."""

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select

from zucai.models import Match, Team
from zucai.predict.training_sources import actual_result_leagues


def pending_fd_identity_overlaps(session, frame, identities, links):
    """Only complete-DB, unique, unclaimed, one-side unknown FD overlaps qualify.

    Nothing is merged or rewritten. The caller must still require both result
    rows to be available at its cutoff before withholding the added API row.
    """
    added = frame.loc[frame.source.eq("apif") & frame.league.isin(actual_result_leagues())]
    if added.empty:
        return []
    matches = list(session.scalars(select(Match).where(Match.league.in_(set(added.league)))))
    teams = {team.id: team for team in session.scalars(select(Team))}
    by_team, claims = defaultdict(dict), defaultdict(set)
    proven = {key for edge in links for key in edge[:2]}
    for row in matches:
        for team in (row.home_team_id, row.away_team_id):
            by_team[row.league, team][row.match_id] = row
    for match_id, counterpart in session.execute(
        select(Match.match_id, Match.counterpart_id).where(Match.counterpart_id.is_not(None))
    ):
        claims[counterpart].add(match_id)

    def neighbors(row):
        candidates = {
            **by_team[row.league, row.home_team_id],
            **by_team[row.league, row.away_team_id],
        }
        when = datetime.fromisoformat(row.kickoff_utc)
        return [
            other
            for other in candidates.values()
            if other.match_id != row.match_id
            and abs(datetime.fromisoformat(other.kickoff_utc) - when) <= timedelta(hours=3)
        ]

    wanted = set(added.match_id) & set(identities)
    pending = []
    for paid in matches:
        if paid.match_id not in wanted:
            continue
        candidates = neighbors(paid)
        if len(candidates) != 1 or candidates[0].source != "fd":
            continue
        old = candidates[0]
        if len(neighbors(old)) != 1 or any(
            row.counterpart_id or claims[row.match_id] or row.match_id in proven
            for row in (paid, old)
        ):
            continue
        pair = paid.home_team_id, paid.away_team_id
        old_pair = old.home_team_id, old.away_team_id
        equal = [pair[side] == old_pair[side] for side in (0, 1)]
        if sum(equal) != 1 or pair[0] == old_pair[1] or pair[1] == old_pair[0]:
            continue
        unknown = old_pair[equal.index(False)]
        aliases = teams[unknown].aliases
        # Conflicting, malformed or multiple bindings are not an absent binding.
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or alias.startswith(f"apif:{paid.league}:team:")
            for alias in aliases
        ):
            continue
        pending.append(
            dict(
                match_id=paid.match_id,
                fd_candidate_id=old.match_id,
                unbound_fd_team_id=unknown,
                reason="unverified_fd_identity_overlap",
            )
        )
    return sorted(pending, key=lambda row: row["match_id"])


def exclude_pending_fd_overlaps(frame):
    """Both rows must have survived the caller's availability and proof filters."""
    present = set(frame.match_id)
    withheld = [
        row
        for row in frame.attrs.get("pending_fd_identity_overlaps", [])
        if row["match_id"] in present and row["fd_candidate_id"] in present
    ]
    result = frame.loc[~frame.match_id.isin({row["match_id"] for row in withheld})].copy()
    result.attrs["unverified_fd_identity_overlaps"] = withheld
    return result
