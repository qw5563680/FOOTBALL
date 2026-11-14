"""Quarantine unresolved source overlaps without inferring identity or rewriting results."""

from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from zucai.models import Match, Result, Team
from zucai.predict.training_sources import actual_result_leagues, apif_history_sources
from zucai.result_source_links import _canonical_team
from zucai.sources.apif_stats import instant


def _legacy_fd_identity_overlaps(session, frame, identities, links):
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
        when = instant(row.kickoff_utc)
        return [
            other
            for other in candidates.values()
            if other.match_id != row.match_id
            and abs(instant(other.kickoff_utc) - when) <= timedelta(hours=3)
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


def _validate_bridge_components(matches, teams, links, bridge_nodes):
    """Inspect all claims before accepting any old-source bridge in a component."""
    neighbors = defaultdict(set)
    pointers = [
        (row.match_id, row.counterpart_id)
        for row in matches.values()
        if row.counterpart_id is not None
    ]
    for edge in [*links, *pointers]:
        left, right = edge[:2]
        neighbors[left].add(right)
        neighbors[right].add(left)
    visited = set()
    for seed in sorted(bridge_nodes):
        if seed in visited:
            continue
        pending, sources = [seed], {}
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            visited.add(key)
            row = matches.get(key)
            if row is None:
                raise ValueError(f"旧来源桥接组件存在断裂声明：{key}")
            if row.source in sources and sources[row.source] != key:
                raise ValueError("旧来源桥接组件存在同来源的多个比赛声明")
            sources[row.source] = key
            for target in neighbors[key]:
                other = matches.get(target)
                if (
                    other is None
                    or row.source not in {"jc", "fd", "apif", "fdorg"}
                    or other.source not in {"jc", "fd", "apif", "fdorg"}
                    or row.source == other.source
                    or (row.league, row.home_team_id, row.away_team_id)
                    != (other.league, other.home_team_id, other.away_team_id)
                    or row.home_team_id == row.away_team_id
                    or any(
                        not _canonical_team(teams.get(team))
                        for team in (row.home_team_id, row.away_team_id)
                    )
                    or abs(instant(row.kickoff_utc) - instant(other.kickoff_utc))
                    > timedelta(hours=3)
                ):
                    raise ValueError(f"旧来源桥接组件存在冲突或断裂声明：{key} / {target}")
                if target not in visited:
                    pending.append(target)


def complete_old_source_links(session, frame, links):
    """Retain old JC/FD bridges only after checking their complete claim graph.

    All incoming/outgoing claims, including unselected, non-result, cross-league
    and broken references, participate in validation. Invalid claims must not
    disappear while their component's surviving edge grants an exemption.
    Paid links still require the existing dated proofs; nothing is written.
    """
    from zucai.predict.pooled import related_result_links

    scope = set(frame.league) & actual_result_leagues()
    if not scope:
        return links
    matches = {row.match_id: row for row in session.scalars(select(Match))}
    teams = {team.id: team.name for team in session.scalars(select(Team))}
    old_links = []
    for row in matches.values():
        if row.league not in scope or row.source not in {"jc", "fd"}:
            continue
        other = matches.get(row.counterpart_id)
        if other is None or other.source not in {"jc", "fd"} or row.source == other.source:
            continue
        if (row.league, row.home_team_id, row.away_team_id) != (
            other.league,
            other.home_team_id,
            other.away_team_id,
        ):
            continue
        if any(not _canonical_team(teams.get(key)) for key in (row.home_team_id, row.away_team_id)):
            continue
        if abs(instant(row.kickoff_utc) - instant(other.kickoff_utc)) <= timedelta(hours=3):
            old_links.append((row.match_id, other.match_id))
    related = related_result_links(frame, [*links, *old_links])
    included = {tuple(edge) for edge in related}
    bridge_nodes = {key for edge in old_links if edge in included for key in edge}
    _validate_bridge_components(matches, teams, links, bridge_nodes)
    return related


def pending_fd_identity_overlaps(session, frame, identities, links):
    """Keep every same-ID cross-source risk until the caller's actual cutoff.

    The original one-unknown-FD branch remains a separate three-hour rule.
    No link (including future evidence) exempts a pair at this discovery step.
    """
    pending = _legacy_fd_identity_overlaps(session, frame, identities, links)
    policy = apif_history_sources()
    added = frame.loc[
        frame.source.isin(("apif", "fdorg")) & frame.league.isin(actual_result_leagues())
    ]
    if added.empty:
        return pending
    wanted = set(added.match_id) & set(identities)
    matches = list(
        session.scalars(
            select(Match)
            .join(Result)
            .where(Match.league.in_(set(added.league)), Match.status == "finished")
        )
    )
    teams = {team.id: team for team in session.scalars(select(Team))}
    pairs = defaultdict(list)
    for row in matches:
        if row.source not in policy.get(row.league, set()):
            continue
        if row.home_team_id == row.away_team_id or any(
            key not in teams or not _canonical_team(teams[key].name)
            for key in (row.home_team_id, row.away_team_id)
        ):
            continue
        pairs[row.league, row.home_team_id, row.away_team_id].append(row)
    for rows in pairs.values():
        for paid in rows:
            if paid.match_id not in wanted:
                continue
            for other in rows:
                if paid.source == other.source or abs(
                    instant(paid.kickoff_utc) - instant(other.kickoff_utc)
                ) > timedelta(hours=36):
                    continue
                pending.append(
                    dict(
                        match_id=paid.match_id,
                        fd_candidate_id=other.match_id,
                        comparison_source=other.source,
                        reason="unverified_same_identity_overlap",
                    )
                )
    return sorted(pending, key=lambda row: (row["match_id"], row["fd_candidate_id"]))


def exclude_pending_fd_overlaps(frame, *, groups=None, as_of=None):
    """Apply to eligible rows BEFORE representatives collapse old FD into JC.

    Both rows must have survived availability and identity proof filters. A
    complete, cutoff-valid component can prove this exact pair already linked;
    an unrelated third-source relation cannot.
    """
    present = set(frame.match_id)
    groups = groups or {key: key for key in present}
    from zucai.predict.pooled import SOURCES

    decisions = {
        frozenset(row["match_ids"]): row
        for row in frame.attrs.get("cross_day_decisions", [])
        if as_of is not None and instant(row["effective_utc"]) < as_of
    }
    withheld, applied, excluded = [], {}, set()
    indexed = frame.set_index("match_id")
    for row in frame.attrs.get("pending_fd_identity_overlaps", []):
        left, right = row["match_id"], row["fd_candidate_id"]
        if left not in present or right not in present or groups[left] == groups[right]:
            continue
        decision = decisions.get(frozenset((left, right)))
        if decision is not None:
            applied[decision["decision_id"]] = decision
            if decision["decision"] == "different_match":
                continue
            pair = indexed.loc[[left, right]]
            if (
                not pair.source.isin(("jc", "fd")).any()
                and len(pair[["home_goals", "away_goals"]].drop_duplicates()) > 1
            ):
                withheld.append(dict(row, reason="cross_day_same_match_score_conflict"))
                excluded.update((left, right))
                continue
            loser = max((left, right), key=lambda key: SOURCES.index(indexed.at[key, "source"]))
            excluded.add(loser)
            continue
        withheld.append(row)
        excluded.add(left)
    result = frame.loc[~frame.match_id.isin(excluded)].copy()
    result.attrs["unverified_fd_identity_overlaps"] = withheld
    result.attrs["cross_day_decisions_applied"] = list(applied.values())
    return result
