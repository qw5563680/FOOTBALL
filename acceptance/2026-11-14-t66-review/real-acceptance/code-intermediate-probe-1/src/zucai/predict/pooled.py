"""European corpus configuration; no source ingestion or identity changes."""

from datetime import timedelta

from sqlalchemy import select

from zucai.models import Match

CUPS = ("欧冠", "欧罗巴", "欧协联")
POOL = "欧战"
SOURCES = ("jc", "fd", "fdorg", "apif")
POOL_FITTING_METHOD = "pooled_dc_full_gradient_al_slsqp_v1"


def configuration(league, entry):
    if league != POOL:
        return None
    targets = entry.get("leagues", list(CUPS))
    scope = entry.get("training_leagues")
    if sorted(targets) != sorted(CUPS) or (scope is not None and not set(targets) <= set(scope)):
        raise ValueError("欧战须覆盖三项目标赛事，训练范围须包含目标赛事")
    return dict(leagues=sorted(targets), training_leagues=sorted(scope) if scope else None)


def default_scope(session, domestic, *, as_of):
    cup_rows = session.execute(
        select(Match.home_team_id, Match.away_team_id).where(
            Match.league.in_(CUPS), Match.kickoff_utc < as_of
        )
    ).all()
    teams = {team for row in cup_rows for team in row}
    leagues = session.scalars(
        select(Match.league)
        .where(
            Match.league.in_(domestic),
            Match.kickoff_utc < as_of,
            (Match.home_team_id.in_(teams)) | (Match.away_team_id.in_(teams)),
        )
        .distinct()
    )
    return sorted(set(leagues) | set(CUPS))


def linked_representatives(frame):
    """Select the existing higher-priority counterpart, never infer a new link.

    This is a pooled materialized view only. All unlinked/conflicting overlaps
    remain subject to the unchanged training guards.
    """
    rank = {source: i for i, source in enumerate(SOURCES)}
    indexed = frame.set_index("match_id")
    from zucai.sources.fd_apif_counterpart import FixtureIdentity, find_apif_candidate

    # Names here come from the unique Team table, so exact equality represents
    # the same stored ID. Keep the original fields and all unknown team labels.
    identities = [
        FixtureIdentity(
            row.match_id,
            row.source,
            row.league,
            row.home,
            row.away,
            row.kickoff_utc,
            row.counterpart_id if isinstance(row.counterpart_id, str) else None,
        )
        for row in frame.itertuples()
    ]
    by_id = {row.match_id: row for row in identities}
    names = {name: name for row in identities for name in (row.home_team_id, row.away_team_id)}
    excluded = set()
    for row in frame.itertuples():
        if row.counterpart_id not in indexed.index:
            continue
        other = indexed.loc[row.counterpart_id]
        if row.source == "apif" and other.source == "fd":
            candidate = find_apif_candidate(by_id[row.counterpart_id], identities, names)
            if candidate is None or candidate.match_id != row.match_id:
                continue
        elif (row.league, row.home, row.away) != (
            other.league,
            other.home,
            other.away,
        ) or abs(row.kickoff_utc - other.kickoff_utc) > timedelta(hours=3):
            continue
        left, right = rank[row.source], rank[other.source]
        if left != right:
            excluded.add(row.match_id if left > right else row.counterpart_id)
    result = frame.loc[~frame.match_id.isin(excluded)].copy()
    result.attrs["linked_rows_excluded"] = sorted(excluded)
    return result


def model_targets(model):
    """Frozen registration labels, while the model itself retains the pool name."""
    if isinstance(model, dict):
        pooled, league = model.get("pooled", False), model["league"]
        targets = model.get("prediction_leagues")
    else:
        pooled, league = getattr(model, "pooled", False), model.league
        targets = getattr(model, "prediction_leagues", None)
    if not pooled:
        return [league]
    if league != POOL or not isinstance(targets, list) or sorted(targets) != sorted(CUPS):
        raise ValueError("欧战模型目标赛事元数据无效")
    return targets
