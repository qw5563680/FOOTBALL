"""European corpus configuration; no source ingestion or identity changes."""

from datetime import timedelta

import pandas as pd
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


def _linked_groups(frame, *, as_of=None):
    """Components of explicit, rechecked links, including non-result anchors."""
    parent = {key: key for key in frame.match_id}

    def root(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    indexed = frame.set_index("match_id")
    edges = list(frame.attrs.get("verified_result_links", []))
    timed_pairs = {frozenset(edge[:2]) for edge in edges if len(edge) == 3}
    for row in frame.itertuples():
        if row.counterpart_id not in indexed.index:
            continue
        other = indexed.loc[row.counterpart_id]
        if (row.league, row.home, row.away) == (other.league, other.home, other.away) and abs(
            row.kickoff_utc - other.kickoff_utc
        ) <= timedelta(hours=3):
            edges.append((row.match_id, row.counterpart_id))
    from zucai.predict.training_sources import actual_result_leagues

    strict_leagues = actual_result_leagues()
    strict_ids = set(
        frame.loc[
            frame.league.isin(strict_leagues) & frame.source.isin(("apif", "fdorg")),
            "match_id",
        ]
    )
    for edge in edges:
        left, right = edge[:2]
        # A scheduled API anchor need not have a result row in this frame.
        # Its existing endpoint supplies the league, never historical link timing.
        added_anchor = any(
            key.split(":", 1)[0] in {"apif", "fdorg"} for key in (left, right)
        ) and any(
            key in indexed.index and indexed.at[key, "league"] in strict_leagues
            for key in (left, right)
        )
        if len(edge) == 2 and (
            frozenset(edge) in timed_pairs
            or (as_of is not None and ({left, right} & strict_ids or added_anchor))
        ):
            continue
        if len(edge) == 3 and as_of is not None and pd.Timestamp(edge[2]) >= as_of:
            continue
        parent[root(left)] = root(right)
    seen = {}
    for key in parent:
        claim = (root(key), key.split(":", 1)[0])
        if claim in seen and seen[claim] != key:
            raise ValueError(f"同场关系组件包含同来源多个比赛：{seen[claim]} / {key}")
        seen[claim] = key
    return {key: root(key) for key in frame.match_id}


def linked_representatives(frame, *, groups=None):
    """Select the existing higher-priority counterpart, never infer a new link.

    This is a pooled materialized view only. All unlinked/conflicting overlaps
    remain subject to the unchanged training guards.
    """
    rank = {source: i for i, source in enumerate(SOURCES)}
    groups = _linked_groups(frame) if groups is None else groups
    ranks = frame.source.map(rank).fillna(len(rank))
    best = ranks.groupby(frame.match_id.map(groups), sort=False).transform("min")
    excluded = set(frame.loc[ranks > best, "match_id"])
    result = frame.loc[~frame.match_id.isin(excluded)].copy()
    result.attrs["linked_rows_excluded"] = sorted(excluded)
    return result


def source_eligible_history(frame):
    """A copied view; never relabel or overwrite stored assumed availability."""
    from zucai.predict.training_sources import actual_result_leagues

    result = frame.copy()
    added = result.league.isin(actual_result_leagues()) & result.source.isin(("apif", "fdorg"))
    unverified = added & (
        ~result.get("provider_identity_verified", pd.Series(False, index=result.index)).eq(True)
        | result.home.str.startswith(("apif:team:", "fdorg:team:", "jc:team:"))
        | result.away.str.startswith(("apif:team:", "fdorg:team:", "jc:team:"))
    )
    result.attrs["unverified_source_rows"] = sorted(result.loc[unverified, "match_id"])
    result = result.loc[~unverified].copy()
    added = added.reindex(result.index)
    assumed = added & result.available_source.eq("assumed")
    if assumed.any():
        observations = (
            pd.to_datetime(result.result_checked_utc, utc=True, format="ISO8601")
            if "result_checked_utc" in result
            else pd.Series(pd.NaT, index=result.index, dtype="datetime64[ns, UTC]")
        )
        result["stored_available_utc"] = result.available_utc
        result.loc[assumed, "available_utc"] = observations.loc[assumed].where(
            observations.loc[assumed] >= result.loc[assumed, "available_utc"]
        )
        result.attrs["actual_observation_bounded_rows"] = sorted(result.loc[assumed, "match_id"])
    # Deliberate replay is never accepted as real availability for the new source scope.
    result.loc[added & result.available_source.eq("replayed"), "available_utc"] = pd.NaT
    identity = pd.to_datetime(
        result.get("identity_verified_utc", pd.Series(pd.NaT, index=result.index)),
        utc=True,
        format="ISO8601",
    )
    known = result.available_utc
    bounded = known.mask(identity > known, identity).where(identity.notna() & known.notna())
    result.loc[added, "available_utc"] = bounded.loc[added]
    return result


def select_training_history(frame, as_of):
    """Filter actual known rows first, then apply the unchanged source priority."""
    from zucai.predict.base import reject_cross_source_overlaps, utc

    cutoff = utc(as_of)
    linked = frame.attrs.get("linked_source_selection", False)
    source = source_eligible_history(frame) if linked else frame
    groups = _linked_groups(source, as_of=cutoff) if linked else None
    known = source.available_utc
    eligible = source.loc[
        (source.kickoff_utc < cutoff) & (known < cutoff) & (known > source.kickoff_utc)
    ].copy()
    if linked:
        withheld = set()
        entities = eligible.match_id.map(groups)
        repeated = eligible.loc[entities.duplicated(keep=False)]
        for _, rows in repeated.groupby(entities, sort=False):
            if len(rows[["home_goals", "away_goals"]].drop_duplicates()) > 1 and not (
                rows.source.isin(("jc", "fd")).any()
            ):
                withheld.update(rows.match_id)
        eligible = eligible.loc[~eligible.match_id.isin(withheld)].copy()
        eligible.attrs["unresolved_score_conflict_rows"] = sorted(withheld)
        eligible = linked_representatives(eligible, groups=groups)
    reject_cross_source_overlaps(eligible)
    return eligible


def evaluation_history(frame, as_of):
    """One final label per entity, with unavailable entities still counted."""
    source = source_eligible_history(frame)
    groups = _linked_groups(source, as_of=pd.Timestamp(as_of))
    selected = select_training_history(frame, as_of)
    covered = {groups[key] for key in selected.match_id}
    missing = source.loc[~source.match_id.map(groups).isin(covered)].copy()
    missing = linked_representatives(missing, groups=groups)
    missing["available_utc"] = pd.NaT
    result = pd.concat([selected, missing]).sort_values(["kickoff_utc", "match_id"])
    result.attrs = frame.attrs.copy()
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
