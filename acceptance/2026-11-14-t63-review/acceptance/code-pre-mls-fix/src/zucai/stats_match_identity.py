"""Read current same-match proofs without changing retained rows or their indices."""

from datetime import timedelta

from sqlalchemy import select

from zucai.models import Match
from zucai.result_source_links import load_result_links
from zucai.sources.apif_stats import instant


def verified_match_groups(session, *, as_of=None):
    """Include the complete proof graph, including scheduled/out-of-window members.

    The proof loader checks current identity snapshots and rejects competing
    claims from the same source. Names, scores and legacy pointers add no edges.
    ``as_of`` is for operational scheduling; archived fixture pages may be indexed
    using a proof verified later than the page's original capture.
    """
    links = load_result_links(session)
    if not links:
        return {}
    ids = {key for left, right, _ in links for key in (left, right)}
    matches = {
        match.match_id: match
        for match in session.scalars(select(Match).where(Match.match_id.in_(ids)))
    }
    cutoff = instant(as_of) if as_of is not None else None
    groups = {}
    for left, right, verified in links:
        if cutoff is not None and instant(verified) > cutoff:
            continue
        if abs(
            instant(matches[left].kickoff_utc) - instant(matches[right].kickoff_utc)
        ) > timedelta(hours=3):
            raise ValueError("赛果关联开球快照超过三小时")
        members = groups.get(left, frozenset({left})) | groups.get(right, frozenset({right}))
        for member in members:
            groups[member] = members
    return groups
