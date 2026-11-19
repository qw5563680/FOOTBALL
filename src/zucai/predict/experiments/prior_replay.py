"""T76: walk-forward replay of the small-sample prior against the formal model.

Four versions are replayed on the same fixtures with the same weekly cut-offs:

``dc``             the formal per-league model (project class, the season's own tuned
                   half-life and draw calibration);
``dc-prior``       the same fit, each team's attack/defence pulled towards a prior target
                   with weight n / (n + k), n being its known league matches in the window;
``dc-tier``        both tiers of a country fitted together (strength carries across tiers);
``dc-prior-tier``  the joint fit with the same pull.

Training rows always come from ``select_training_history(history, cutoff)``; prior targets
are computed from the same cut-off fit. No odds or other market information is read.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
from math import log
from pathlib import Path

import numpy as np
import pandas as pd
from penaltyblog.models import DixonColesGoalModel, dixon_coles_weights

from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes, InsufficientHistory
from zucai.predict.experiments.metrics import paired, summarize
from zucai.predict.pooled import evaluation_history, select_training_history
from zucai.predict.pooled_fit import fit_pooled_engine
from zucai.predict.seasons import season_bounds
from zucai.sources.football_data import CONFIG_ROOT

LOG = logging.getLogger(__name__)
CONFIG = CONFIG_ROOT / "experiments/dc-prior/replay.json"
OUTCOMES = "hda"


def load_config(path=None):
    config = json.loads(Path(path or CONFIG).read_bytes())
    bounds = config.get("segment_upper_bounds")
    candidates = config.get("k_candidates")
    seasons = [config.get(key) for key in ("selection_seasons", "validation_seasons")]
    if (
        config.get("version") != 1
        or not isinstance(bounds, list)
        or bounds != sorted(set(bounds))
        or not bounds
        or bounds[0] != 0
        or not isinstance(candidates, list)
        or not candidates
        or any(type(k) is not int or k <= 0 for k in candidates)
        or any(not isinstance(value, list) or not value for value in seasons)
        or set(seasons[0]) & set(seasons[1])
        or type(config.get("history_days")) is not int
        or config["history_days"] <= 0
        or not config.get("pairs")
        or any(len(pair) != 2 for pair in config["pairs"])
    ):
        raise ValueError("先验回放参数文件无效")
    return config


def segment(n_home, n_away, bounds):
    """Label of the smaller side's known-match count: 0 / 1-5 / 6-12 / 13+ by default."""
    least = min(n_home, n_away)
    lower = 0
    for upper in bounds:
        if least <= upper:
            return str(upper) if lower == upper else f"{lower}-{upper}"
        lower = upper + 1
    return f"{lower}+"


def known_counts(eligible, cutoff, days):
    """Known league matches per team inside the window that ends at the cut-off."""
    recent = eligible.loc[eligible.kickoff_utc >= cutoff - pd.Timedelta(days=days)]
    return pd.concat([recent.home, recent.away]).value_counts().to_dict()


def prior_targets(params, teams, weak):
    """(weak-team target, league-mean target) from one cut-off fit."""
    attack = [params[f"attack_{team}"] for team in teams if f"attack_{team}" in params]
    defence = [params[f"defence_{team}"] for team in teams if f"defence_{team}" in params]
    return (weak["attack"], weak["defence"]), (float(np.mean(attack)), float(np.mean(defence)))


def weak_target(params, teams):
    """The formal model's default strength: weakest quartile, halfway back to the mean."""
    attack = np.sort([params[f"attack_{team}"] for team in teams])
    defence = np.sort([params[f"defence_{team}"] for team in teams])
    count = max(1, int(np.ceil(len(teams) / 4)))
    return {
        "attack": float((attack[:count].mean() + attack.mean()) / 2),
        "defence": float((defence[-count:].mean() + defence.mean()) / 2),
    }


def shrunk(params, team, *, n, k, in_league_last_season, weak, mean):
    """(n * estimate + k * target) / (n + k); k = 0 returns the estimate unchanged."""
    known = f"attack_{team}" in params
    estimate = (params[f"attack_{team}"], params[f"defence_{team}"]) if known else weak
    if k == 0:
        return estimate
    target = mean if in_league_last_season else weak
    return tuple(
        (n * value + k * goal) / (n + k) for value, goal in zip(estimate, target, strict=True)
    )


class _Frozen:
    """Read-only parameter set standing in for a fitted engine."""

    def __init__(self, params):
        self._params = params

    def get_params(self):
        return self._params


def view_probabilities(model, row, strengths, *, advantage=None, rho=None):
    """Home/draw/away from the formal prediction path with substituted team strengths.

    The league model's own rho guard, score-grid size and draw calibration apply, so the
    only difference from the formal forecast is the four strengths (and, for joint fits,
    the home advantage and rho that belong to them).
    """
    params = dict(model.engine.get_params())
    home, away = row["home"], row["away"]
    (params[f"attack_{home}"], params[f"defence_{home}"]) = strengths[0]
    (params[f"attack_{away}"], params[f"defence_{away}"]) = strengths[1]
    if advantage is not None:
        params["home_advantage"] = advantage
    if rho is not None:
        params["rho"] = rho
    view = copy.copy(model)
    view.engine, view.teams, view.unknown_team_prior = _Frozen(params), frozenset(), True
    matrix, _ = view.predict_with_metadata(row)
    return had(matrix)


def joint_fit(frames, half_life, cutoff):
    """Both tiers in one fit; returns the fitted parameters or None when it cannot be fitted."""
    rows = pd.concat(frames, ignore_index=True)
    teams = set(rows.home) | set(rows.away)
    if len(teams) < 2 or len(rows) < 2 * len(teams) + 1:
        return None
    weights = dixon_coles_weights(
        rows.kickoff_utc.dt.normalize(), xi=log(2) / half_life, base_date=cutoff.normalize()
    )
    engine = DixonColesGoalModel(
        rows.home_goals, rows.away_goals, rows.home, rows.away, weights=weights
    )
    fit_pooled_engine(engine)
    return engine.get_params()


def season_settings(entry, season):
    """The half-life and draw calibration the formal protocol used for that season."""
    for window in entry.get("evaluation_windows", []):
        if window["validation_season"] == season:
            return float(window["half_life_days"]), bool(window["draw_calibration"])
    raise ValueError(f"参数文件没有 {season} 赛季的选参记录")


def previous_season(season):
    return f"{int(season[:2]) - 1:02d}{season[:2]}"


def replay_league(frame, other, league, entry, season, config, *, end, max_weeks=None):
    """Yield one record per fixture of a league season, all versions on the same cut-off."""
    half_life, calibrate = season_settings(entry, season)
    low, high = season_bounds(season, league)
    previous_low, _ = season_bounds(previous_season(season), league)
    labels = evaluation_history(frame, end) if frame.attrs.get("linked_source_selection") else frame
    fixtures = labels.loc[(labels.kickoff_utc >= low) & (labels.kickoff_utc < min(high, end))]
    fixtures = fixtures.loc[
        fixtures.available_utc.notna()
        & (fixtures.available_utc < end)
        & (fixtures.available_utc > fixtures.kickoff_utc)
    ].copy()
    if fixtures.empty:
        return
    fixtures["week"] = fixtures.kickoff_utc.dt.normalize() - pd.to_timedelta(
        fixtures.kickoff_utc.dt.day_of_week, unit="D"
    )
    for week in sorted(fixtures.week.unique())[:max_weeks]:
        cutoff = max(pd.Timestamp(week), low) - pd.Timedelta(microseconds=1)
        try:
            model = DixonColes(half_life, unknown_team_prior=True, draw_calibration=calibrate)
            model.fit(frame, as_of=cutoff)
        except InsufficientHistory:
            continue
        eligible = select_training_history(frame, cutoff)
        own = model.engine.get_params()
        weak, mean = prior_targets(own, model.teams, model.prior_parameters)
        counts = known_counts(eligible, cutoff, config["history_days"])
        stayed = set(
            pd.concat(
                [
                    eligible.loc[
                        (eligible.kickoff_utc >= previous_low) & (eligible.kickoff_utc < low),
                        side,
                    ]
                    for side in ("home", "away")
                ]
            )
        )
        tier = None
        if other is not None:
            elsewhere = select_training_history(other, cutoff)
            tier = joint_fit([eligible, elsewhere], half_life, cutoff)
        if tier is not None:
            members = [team for team in model.teams if f"attack_{team}" in tier]
            tier_weak, tier_mean = prior_targets(tier, members, weak_target(tier, members))
            joint = pd.concat([eligible, elsewhere], ignore_index=True)
            tier_counts = known_counts(joint, cutoff, config["history_days"])
        for row in fixtures.loc[fixtures.week == week].to_dict("records"):
            home, away = row["home"], row["away"]
            if home == away:
                continue
            matrix, _ = model.predict_with_metadata(row)
            goals = row["home_goals"] - row["away_goals"]
            record = dict(
                league=league,
                season=season,
                cutoff_utc=cutoff.isoformat(),
                match_id=row["match_id"],
                kickoff_utc=row["kickoff_utc"].isoformat(),
                home=home,
                away=away,
                actual=0 if goals > 0 else 1 if goals == 0 else 2,
                n_home=int(counts.get(home, 0)),
                n_away=int(counts.get(away, 0)),
                stayed_home=int(home in stayed),
                stayed_away=int(away in stayed),
            )
            record["segment"] = segment(
                record["n_home"], record["n_away"], config["segment_upper_bounds"]
            )
            versions = {"dc": had(matrix)}

            def pulled(params, team, tallies, k, weak_goal, mean_goal, stayed=stayed):
                return shrunk(
                    params,
                    team,
                    n=int(tallies.get(team, 0)),
                    k=k,
                    in_league_last_season=team in stayed,
                    weak=weak_goal,
                    mean=mean_goal,
                )

            for k in config["k_candidates"]:
                versions[f"dc-prior-{k}"] = view_probabilities(
                    model,
                    row,
                    [pulled(own, team, counts, k, weak, mean) for team in (home, away)],
                )
            if tier is not None:
                shared = dict(advantage=tier["home_advantage"], rho=tier["rho"])
                versions["dc-tier"] = view_probabilities(
                    model,
                    row,
                    [
                        pulled(tier, team, tier_counts, 0, tier_weak, tier_mean)
                        for team in (home, away)
                    ],
                    **shared,
                )
                for k in config["k_candidates"]:
                    versions[f"dc-prior-tier-{k}"] = view_probabilities(
                        model,
                        row,
                        [
                            pulled(tier, team, tier_counts, k, tier_weak, tier_mean)
                            for team in (home, away)
                        ],
                        **shared,
                    )
            for name, probabilities in versions.items():
                for outcome in OUTCOMES:
                    record[f"{name}:{outcome}"] = probabilities[outcome]
            yield record


def columns(config, *, tier=True):
    names = ["dc", *(f"dc-prior-{k}" for k in config["k_candidates"])]
    if tier:
        names += ["dc-tier", *(f"dc-prior-tier-{k}" for k in config["k_candidates"])]
    return names


def replay(db, params_file, config, out, *, max_weeks=None, end=None):
    from zucai.predict.command import load_corpus

    leagues = json.loads(Path(params_file).read_bytes())["leagues"]
    end = pd.Timestamp(end) if end is not None else pd.Timestamp.now(tz="UTC")
    seasons = [
        *config["selection_seasons"],
        *config["validation_seasons"],
        *config.get("reported_separately", []),
    ]
    fields = [
        "league",
        "season",
        "cutoff_utc",
        "match_id",
        "kickoff_utc",
        "home",
        "away",
        "actual",
        "n_home",
        "n_away",
        "stayed_home",
        "stayed_away",
        "segment",
        *(f"{name}:{outcome}" for name in columns(config) for outcome in OUTCOMES),
    ]
    count = 0
    with Path(out).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fields, restval="")
        writer.writeheader()
        for pair in config["pairs"]:
            frames = {
                league: load_corpus(
                    db, league, ",".join(leagues[league]["sources"]), params_file=params_file
                )[0]
                for league in pair
            }
            for league, other in (pair, pair[::-1]):
                for season in seasons:
                    for record in replay_league(
                        frames[league],
                        frames[other],
                        league,
                        leagues[league],
                        season,
                        config,
                        end=end,
                        max_weeks=max_weeks,
                    ):
                        writer.writerow(record)
                        count += 1
                    stream.flush()
                    LOG.info("%s %s 回放完成，累计 %d 场", league, season, count)
    return count


def read_rows(path):
    """Replay records in the shape the shared paired-metrics helpers expect."""
    rows = []
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for record in csv.DictReader(stream):
            variants = {}
            for key in record:
                name, _, outcome = key.rpartition(":")
                if outcome == "h" and name and record[key] != "":
                    variants[name] = {
                        "probabilities": [float(record[f"{name}:{o}"]) for o in OUTCOMES]
                    }
            rows.append(
                dict(
                    league=record["league"],
                    season=record["season"],
                    segment=record["segment"],
                    kickoff_utc=record["kickoff_utc"],
                    actual=int(record["actual"]),
                    variants=variants,
                )
            )
    return rows


def choose_k(rows, config, prefix="dc-prior"):
    """Smallest mean log loss on the selection seasons only; ties go to the smaller k."""
    chosen = [row for row in rows if row["season"] in config["selection_seasons"]]
    scores = {}
    for k in config["k_candidates"]:
        value = summarize(chosen, f"{prefix}-{k}")["log_loss"]
        if value is not None:
            scores[k] = value
    if not scores:
        raise ValueError("选参季没有可用的回放记录")
    return min(scores, key=lambda k: (scores[k], k)), scores


def favourites(rows, name, threshold):
    picks = [
        (max(row["variants"][name]["probabilities"]), row)
        for row in rows
        if name in row["variants"] and max(row["variants"][name]["probabilities"]) >= threshold
    ]
    if not picks:
        return dict(n=0, mean_probability=None, hit_rate=None)
    hits = [row["variants"][name]["probabilities"][row["actual"]] == top for top, row in picks]
    return dict(
        n=len(picks),
        mean_probability=float(np.mean([top for top, _ in picks])),
        hit_rate=float(np.mean(hits)),
    )


def score(rows, config):
    """Segmented comparison on the validation seasons with k fixed on the selection seasons."""
    k, selection = choose_k(rows, config)
    tier_k, tier_selection = choose_k(rows, config, "dc-prior-tier")
    bootstrap = config["bootstrap"]
    labels = sorted(
        {row["segment"] for row in rows}, key=lambda s: int(s.split("-")[0].rstrip("+"))
    )
    names = ["dc", f"dc-prior-{k}", "dc-tier", f"dc-prior-tier-{tier_k}"]
    report = dict(
        selected_k=k,
        selection_log_loss={str(key): value for key, value in selection.items()},
        selected_tier_k=tier_k,
        selection_tier_log_loss={str(key): value for key, value in tier_selection.items()},
        versions=names,
        groups={},
    )
    for group, seasons in (
        ("validation", config["validation_seasons"]),
        ("reported_separately", config.get("reported_separately", [])),
    ):
        chosen = [row for row in rows if row["season"] in seasons]
        table = {}
        for label in [*labels, "all"]:
            part = [row for row in chosen if label == "all" or row["segment"] == label]
            if not part:
                continue
            kwargs = dict(replicates=bootstrap["resamples"], seed=bootstrap["seed"])
            table[label] = dict(
                n=len(part),
                summary={
                    name: {key: summarize(part, name)[key] for key in ("n", "log_loss", "brier")}
                    for name in names
                },
                prior_minus_dc=paired(part, names[1], "dc", **kwargs),
                tier_minus_dc=paired(part, "dc-tier", "dc", **kwargs),
                prior_tier_minus_prior=paired(part, names[3], names[1], **kwargs),
                favourites={
                    name: favourites(part, name, config["favourite_threshold"])
                    for name in names[:2]
                },
            )
        report["groups"][group] = table
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("replay", help="逐周回放四个版本，写出逐场 CSV")
    run.add_argument("--db", required=True, type=Path)
    run.add_argument("--params", required=True, type=Path)
    run.add_argument("--out", required=True, type=Path)
    run.add_argument("--config", type=Path)
    run.add_argument("--max-weeks", type=int, help="每个联赛季只回放前 N 周（冒烟用）")
    run.add_argument("--end", help="回放截止时刻（ISO，默认当前时刻）")
    tally = commands.add_parser("score", help="按分段记分，写出 JSON")
    tally.add_argument("--rows", required=True, type=Path)
    tally.add_argument("--out", required=True, type=Path)
    tally.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    if args.command == "replay":
        total = replay(
            args.db, args.params, config, args.out, max_weeks=args.max_weeks, end=args.end
        )
        LOG.info("回放完成：%d 场", total)
    else:
        report = score(read_rows(args.rows), config)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        LOG.info("记分完成：k=%s", report["selected_k"])


if __name__ == "__main__":
    main()
