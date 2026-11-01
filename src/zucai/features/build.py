"""Build traceable team features from outcomes known before each target cutoff."""

import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from sqlalchemy import select

from zucai.models import Match, Odds, Result, Team
from zucai.predict.base import utc

HALF_LIFE_DAYS = 90
WINDOWS = ("home_last5", "home_last10", "home_home5", "away_last5", "away_last10", "away_away5")
METRICS = ("goals_for", "goals_against", "points")
MODEL_FEATURE_COLUMNS = tuple(
    f"{prefix}_{metric}_{method}"
    for prefix in WINDOWS
    for metric in METRICS
    for method in ("mean", "decay90")
) + (
    "home_rest_days",
    "away_rest_days",
    "h2h_last3_home_win_rate",
    "h2h_last3_draw_rate",
    "h2h_last3_away_win_rate",
    "h2h_last3_home_goals_mean",
    "h2h_last3_away_goals_mean",
)
MARKET_COLUMNS = ("market_p_home", "market_p_draw", "market_p_away", "market_line")
AUDIT_WINDOWS = (*WINDOWS, "home_rest", "away_rest", "h2h_last3")
META_COLUMNS = (
    "meta_match_id",
    "meta_source",
    "meta_league",
    "meta_home",
    "meta_away",
    "meta_home_team_id",
    "meta_away_team_id",
    "meta_kickoff_utc",
    "meta_status",
    "meta_cutoff_utc",
    "meta_as_of_utc",
    "meta_generated_utc",
    "meta_history_basis",
    "meta_assumed_history_share",
    "meta_history_n",
    "meta_market_snapshot_utc",
    "meta_h2h_outcomes",
) + tuple(f"meta_{window}_{field}" for window in AUDIT_WINDOWS for field in ("n", "assumed_n"))
ALL_COLUMNS = (*META_COLUMNS, *MODEL_FEATURE_COLUMNS, *MARKET_COLUMNS)


def _iso(value):
    return utc(value).isoformat().replace("+00:00", "Z")


def _histories(session, as_of, strict):
    by_team = defaultdict(list)
    excluded = Counter()
    query = (
        select(Match, Result)
        .join(Result, Result.match_id == Match.match_id)
        .where(Match.status == "finished", Match.kickoff_utc < as_of.to_pydatetime())
        .order_by(Match.kickoff_utc.desc(), Match.match_id)
    )
    for match, result in session.execute(query):
        if result.available_utc is None:
            excluded["missing_available_utc"] += 1
            continue
        kickoff, available = utc(match.kickoff_utc), utc(result.available_utc)
        basis = "captured" if result.available_source is None else result.available_source
        if basis not in {"captured", "published", "assumed"}:
            excluded["invalid_available_source"] += 1
            continue
        if available < kickoff:
            excluded["available_before_kickoff"] += 1
            continue
        if strict and basis == "assumed":
            excluded["assumed_in_strict_mode"] += 1
            continue
        if available >= as_of:
            excluded["not_known_before_build_cutoff"] += 1
            continue
        scores = result.home_goals, result.away_goals
        if any(not isinstance(v, int) or v < 0 for v in scores):
            excluded["invalid_score"] += 1
            continue
        common = dict(
            match_id=match.match_id,
            source=match.source,
            league=match.league,
            kickoff=kickoff,
            available=available,
            basis=basis,
        )
        for team, opponent, gf, ga, venue in (
            (match.home_team_id, match.away_team_id, *scores, "home"),
            (match.away_team_id, match.home_team_id, scores[1], scores[0], "away"),
        ):
            by_team[match.source, team].append(
                dict(
                    common,
                    opponent=opponent,
                    goals_for=gf,
                    goals_against=ga,
                    points=3 if gf > ga else 1 if gf == ga else 0,
                    venue=venue,
                )
            )
    return by_team, dict(excluded)


def _evidence(games):
    return [
        dict(
            match_id=g["match_id"],
            source=g["source"],
            league=g["league"],
            kickoff_utc=_iso(g["kickoff"]),
            available_utc=_iso(g["available"]),
            available_source=g["basis"],
            venue=g["venue"],
            goals_for=g["goals_for"],
            goals_against=g["goals_against"],
            points=g["points"],
        )
        for g in games
    ]


def _aggregate(row, prefix, games):
    if not games:
        return
    # Removing a common exponent keeps normalized weights identical and avoids
    # underflow when all games are far before the target cutoff.
    latest = games[0]["kickoff"]
    weights = np.array(
        [2 ** (-((latest - g["kickoff"]).total_seconds() / 86400) / HALF_LIFE_DAYS) for g in games]
    )
    for metric in METRICS:
        values = np.array([g[metric] for g in games], dtype=float)
        row[f"{prefix}_{metric}_mean"] = float(values.mean())
        row[f"{prefix}_{metric}_decay90"] = float(np.average(values, weights=weights))


def _quotes(session, target_ids, as_of):
    grouped = defaultdict(lambda: defaultdict(dict))
    query = select(Odds).where(
        Odds.pool == "had", Odds.line == 0, Odds.snapshot_utc < as_of.to_pydatetime()
    )
    for quote in session.scalars(query):
        if quote.match_id in target_ids and quote.selection in {"h", "d", "a"}:
            grouped[quote.match_id][utc(quote.snapshot_utc)][quote.selection] = quote.price
    return grouped


def _market(snapshots, cutoff):
    for stamp, prices in sorted(snapshots.items(), reverse=True):
        if stamp >= cutoff or set(prices) != {"h", "d", "a"}:
            continue
        if not all(math.isfinite(p) and p > 1 for p in prices.values()):
            continue
        inverse = [1 / prices[k] for k in "hda"]
        total = sum(inverse)
        return [v / total for v in inverse], _iso(stamp), dict(prices)
    return None, None, None


def build_features(session, match_ids=None, as_of=None, *, strict_availability=False):
    """Rows contain numeric model features, isolated market values and audit metadata.

    Historical reconstruction filters the database's current versions; it does
    not recover superseded schedules/results or prove historical availability.
    """
    generated = utc(datetime.now(UTC))
    cutoff = generated if as_of is None else utc(as_of)
    if cutoff > generated:
        raise ValueError("as_of不能晚于实际提取时刻")
    matches = list(session.scalars(select(Match).order_by(Match.kickoff_utc, Match.match_id)))
    if match_ids is not None:
        if isinstance(match_ids, str):
            raise ValueError("match_ids须为ID列表")
        ids = list(match_ids)
        if len(ids) != len(set(ids)):
            raise ValueError("目标比赛ID重复")
        lookup = {m.match_id: m for m in matches}
        unknown = set(ids) - lookup.keys()
        if unknown:
            raise ValueError("未知比赛ID：" + ", ".join(sorted(unknown)))
        matches = [lookup[key] for key in ids]
    teams = {t.id: t.name for t in session.scalars(select(Team))}
    history, excluded = _histories(session, cutoff, strict_availability)
    quotes = _quotes(session, {m.match_id for m in matches}, cutoff)
    rows, audit = [], {}
    for match in matches:
        when = utc(match.kickoff_utc)
        effective = min(when, cutoff)
        row = {col: np.nan for col in (*MODEL_FEATURE_COLUMNS, *MARKET_COLUMNS)}
        row.update(
            meta_match_id=match.match_id,
            meta_source=match.source,
            meta_league=match.league,
            meta_home=teams[match.home_team_id],
            meta_away=teams[match.away_team_id],
            meta_home_team_id=match.home_team_id,
            meta_away_team_id=match.away_team_id,
            meta_kickoff_utc=_iso(when),
            meta_status=match.status,
            meta_cutoff_utc=_iso(effective),
            meta_as_of_utc=_iso(cutoff),
            meta_generated_utc=_iso(generated),
        )
        windows = {}
        for side, tid, venue in (
            ("home", match.home_team_id, "home"),
            ("away", match.away_team_id, "away"),
        ):
            known = [
                g
                for g in history[match.source, tid]
                if g["match_id"] != match.match_id
                and g["kickoff"] < effective
                and g["available"] < effective
            ]
            scoped = [g for g in known if g["league"] == match.league]
            windows[f"{side}_last5"] = scoped[:5]
            windows[f"{side}_last10"] = scoped[:10]
            windows[f"{side}_{venue}5"] = [g for g in scoped if g["venue"] == venue][:5]
            windows[f"{side}_rest"] = known[:1]
            if known:
                row[f"{side}_rest_days"] = (when - known[0]["kickoff"]).total_seconds() / 86400
            if side == "home":
                windows["h2h_last3"] = [g for g in scoped if g["opponent"] == match.away_team_id][
                    :3
                ]
        for prefix in WINDOWS:
            _aggregate(row, prefix, windows[prefix])
        h2h = windows["h2h_last3"]
        outcomes = [str(g["points"]) for g in h2h]
        if h2h:
            for outcome, name in (("3", "home_win"), ("1", "draw"), ("0", "away_win")):
                row[f"h2h_last3_{name}_rate"] = outcomes.count(outcome) / len(h2h)
            row["h2h_last3_home_goals_mean"] = sum(g["goals_for"] for g in h2h) / len(h2h)
            row["h2h_last3_away_goals_mean"] = sum(g["goals_against"] for g in h2h) / len(h2h)
        row["meta_h2h_outcomes"] = json.dumps(outcomes)
        used = {}
        for prefix, games in windows.items():
            row[f"meta_{prefix}_n"] = len(games)
            row[f"meta_{prefix}_assumed_n"] = sum(g["basis"] == "assumed" for g in games)
            used.update({g["match_id"]: g for g in games})
        assumed = sum(g["basis"] == "assumed" for g in used.values())
        row["meta_history_n"] = len(used)
        row["meta_assumed_history_share"] = assumed / len(used) if used else None
        row["meta_history_basis"] = (
            "no_usable_history" if not used else "mixed" if assumed else "published_or_captured"
        )
        probabilities, stamp, prices = _market(quotes.get(match.match_id, {}), effective)
        row["meta_market_snapshot_utc"] = stamp
        if probabilities is not None:
            row.update(zip(MARKET_COLUMNS, [*probabilities, 0.0], strict=True))
        audit[match.match_id] = dict(
            cutoff_utc=_iso(effective),
            windows={key: _evidence(value) for key, value in windows.items()},
            market=dict(
                snapshot_utc=stamp,
                pool="had",
                prices=prices,
                normalization="inverse_price_proportional",
                model_input=False,
            ),
        )
        rows.append(row)
    generated = utc(datetime.now(UTC))
    for row in rows:
        row["meta_generated_utc"] = _iso(generated)
    frame = pd.DataFrame(rows, columns=ALL_COLUMNS)
    for col in (*MODEL_FEATURE_COLUMNS, *MARKET_COLUMNS):
        frame[col] = frame[col].astype(float)
    frame.attrs = dict(
        schema_version=1,
        feature_version="t04-v1",
        generated_utc=_iso(generated),
        as_of_utc=_iso(cutoff),
        strict_availability=bool(strict_availability),
        half_life_days=HALF_LIFE_DAYS,
        model_feature_columns=list(MODEL_FEATURE_COLUMNS),
        market_columns=list(MARKET_COLUMNS),
        metadata_columns=list(META_COLUMNS),
        performance_scope="same_source_and_league_across_seasons",
        rest_scope="same_source_all_competitions_known_finished_kickoff_interval",
        history_versioning="current_db_values_filtered_by_available_time_not_versioned_replay",
        excluded_history_counts=excluded,
        audit=audit,
    )
    return frame
