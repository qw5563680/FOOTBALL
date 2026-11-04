"""Read-only, version-aware pre-match technical features; no prices or HTTP."""

from bisect import bisect_left
from collections import defaultdict
from math import isfinite, log

import numpy as np
from sqlalchemy import select

from zucai.models import Match, MatchStat, Team
from zucai.predict.base import utc

STAT_KEYS = ("expected_goals", "shots", "shots_on_target", "corners", "red_cards")
MODES = ("strict", "backfill_replay")
VERSION = "t33-stats-v1"


class StatsFeatures:
    """An immutable in-memory view of existing identities and technical versions.

    Windows stay within the target match source/league. Provider is independent:
    apif statistics can be attached to an existing fd match, without another
    appearance from its jc/fdorg counterpart or the opponent's perspective.
    """

    def __init__(self, session, *, provider):
        if provider not in {"fd", "apif"}:
            raise ValueError("统计供应商须为 fd 或 apif")
        self.provider = provider
        names = dict(session.execute(select(Team.id, Team.name)).all())
        self.matches, self.histories, self.schedules = {}, defaultdict(list), defaultdict(list)
        for row in session.execute(select(Match.__table__)).mappings():
            game = dict(row)
            game.update(home=names[game["home_team_id"]], away=names[game["away_team_id"]])
            game["kickoff_utc"] = utc(game["kickoff_utc"])
            self.matches[game["match_id"]] = game
            for side in ("home", "away"):
                team = game[f"{side}_team_id"]
                if game["status"] != "cancelled":
                    self.schedules[game["source"], team].append(game)
                if game["status"] == "finished":
                    self.histories[game["source"], game["league"], team].append(game)
        for group in (*self.histories.values(), *self.schedules.values()):
            group.sort(key=lambda g: (g["kickoff_utc"], g["match_id"]))
        self.times = {
            key: [g["kickoff_utc"].value for g in games] for key, games in self.histories.items()
        }
        self.schedule_times = {
            key: [g["kickoff_utc"].value for g in games] for key, games in self.schedules.items()
        }
        self.statistics = defaultdict(list)
        query = select(MatchStat.__table__).where(
            MatchStat.provider == provider, MatchStat.stat_key.in_(STAT_KEYS)
        )
        for row in session.execute(query).mappings():
            row = dict(row)
            row["captured_utc"] = utc(row["captured_utc"])
            row["available_utc"] = utc(row["available_utc"]) if row["available_utc"] else None
            self.statistics[row["match_id"], row["team_id"], row["stat_key"]].append(row)

    def _value(self, game, team, key, cutoff, mode):
        versions = self.statistics.get((game["match_id"], team, key), [])
        eligible, rejected = [], []
        for row in versions:
            if row["availability_basis"] == "backfilled":
                if mode == "strict":
                    rejected.append("backfilled_not_known")
                    continue
                effective = game["kickoff_utc"] + np.timedelta64(3, "h")
            else:
                effective = row["available_utc"]
                if effective is None:
                    rejected.append("missing_available_utc")
                    continue
                # Never accept a mislabeled future capture as contemporaneous evidence.
                if row["captured_utc"] >= cutoff or effective < row["captured_utc"]:
                    rejected.append("not_known_before_cutoff")
                    continue
            if effective >= cutoff:
                rejected.append("not_known_before_cutoff")
                continue
            eligible.append((row, effective))
        if not eligible:
            return dict(
                match_id=game["match_id"],
                value=None,
                missing_reason=sorted(set(rejected))[0] if rejected else "not_provided",
            )
        row, effective = max(
            eligible, key=lambda item: (item[0]["version"], item[0]["captured_utc"], item[0]["id"])
        )
        reason = None
        value = row["value"]
        if row["period_basis"] != "90" or not row["eligible_90"] or row["extra_time_included"]:
            reason = "not_90_minutes"
        elif value is None:
            reason = row["missing_reason"] or "null"
        elif not isfinite(value) or value < 0:
            reason = "invalid_value"
        return dict(
            match_id=game["match_id"],
            value=float(value) if reason is None else None,
            missing_reason=reason,
            provider=self.provider,
            team_id=team,
            stat_id=row["id"],
            raw_sha256=row["raw_sha256"],
            version=row["version"],
            captured_utc=row["captured_utc"].isoformat(),
            available_utc=row["available_utc"].isoformat() if row["available_utc"] else None,
            effective_available_utc=effective.isoformat(),
            availability_basis=row["availability_basis"],
        )

    def _cell(self, games, team, key, direction, cutoff, mode, n):
        evidence = []
        for game in games:
            subject = team
            if direction == "against":
                subject = (
                    game["away_team_id"] if game["home_team_id"] == team else game["home_team_id"]
                )
            evidence.append(self._value(game, subject, key, cutoff, mode))
        valid = [e for e in evidence if e["value"] is not None]
        reasons = sorted({e["missing_reason"] for e in evidence if e["missing_reason"]})
        if len(games) < n:
            reasons.append("insufficient_window")
        mean = decay = None
        if len(valid) == n:
            values = np.array([e["value"] for e in evidence])
            ages = np.array(
                [(cutoff - game["kickoff_utc"]).total_seconds() / 86400 for game in games]
            )
            weights = np.exp(-log(2) * ages / 90)
            mean, decay = float(values.mean()), float(np.dot(weights, values) / weights.sum())
        return dict(
            value=mean,
            decay=decay,
            red_card_matches=[e["match_id"] for e in valid if e["value"] > 0]
            if key == "red_cards"
            else None,
            n=len(valid),
            N=n,
            sample_n=len(games),
            missing_reasons=reasons,
            match_ids=[g["match_id"] for g in games],
            backfilled_n=sum(e.get("availability_basis") == "backfilled" for e in valid),
            evidence=evidence,
        )

    def build(
        self,
        target,
        *,
        as_of,
        mode="strict",
        windows=(5, 10),
        stat_keys=STAT_KEYS,
        research=True,
        opponent_model=None,
    ):
        if mode not in MODES or not windows or any(n not in (5, 10) for n in windows):
            raise ValueError("须指定 strict/backfill_replay 与近5/10场窗口")
        if any(key not in STAT_KEYS for key in stat_keys):
            raise ValueError("技术特征不允许白名单以外字段（含赔率）")
        game = self.matches.get(target["match_id"], target)
        kickoff, at = utc(game["kickoff_utc"]), utc(as_of)
        cutoff = min(at, kickoff)
        cells, rest, opponents = {}, {}, {}
        for side in ("home", "away"):
            team = game[f"{side}_team_id"]
            key = game["source"], game["league"], team
            games = self.histories.get(key, [])
            limit = (cutoff - np.timedelta64(3, "h")).value
            before = games[: bisect_left(self.times.get(key, []), limit)]
            # Target exclusion is explicit even for synthetic post-match callers.
            before = [g for g in before if g["match_id"] != game["match_id"]]
            groups = {"all": before}
            if research:
                groups[side] = [g for g in before if g[f"{side}_team_id"] == team]
            for venue, group in groups.items():
                for n in windows:
                    selected = group[-n:]
                    for stat in stat_keys:
                        for direction in ("for", "against"):
                            cells[f"{side}_{venue}_{n}_{stat}_{direction}"] = self._cell(
                                selected, team, stat, direction, cutoff, mode, n
                            )
            if research:
                schedule_key = game["source"], team
                index = bisect_left(self.schedule_times.get(schedule_key, []), kickoff.value)
                previous = self.schedules[schedule_key][index - 1] if index else None
                rest[side] = dict(
                    value=(kickoff - previous["kickoff_utc"]).total_seconds() / 86400
                    if previous
                    else None,
                    match_id=previous["match_id"] if previous else None,
                    basis="t13_schedule_before_target_kickoff",
                )
                other = game["away"] if side == "home" else game["home"]
                params = {}
                if opponent_model is not None and utc(opponent_model.as_of) < cutoff:
                    params = opponent_model.engine.get_params()
                opponents[side] = dict(
                    attack=params.get(f"attack_{other}"),
                    defence=params.get(f"defence_{other}"),
                    missing_reason=None
                    if f"attack_{other}" in params
                    else "no_pre_cutoff_dc_parameters",
                )
        return dict(
            feature_version=VERSION,
            provider=self.provider,
            match_id=game["match_id"],
            feature_cutoff=cutoff.isoformat(),
            availability_mode=mode,
            validation_status="historical_replay_unvalidated"
            if mode == "backfill_replay"
            else "experimental_unvalidated",
            cells=cells,
            rest=rest,
            opponent_strength=opponents,
            lineup=dict(value=None, reason="not_used_in_first_round"),
        )


def intensity_inputs(features, stat_key, window):
    """Same four-field completeness mask for full and ablated models."""
    values, reasons = {}, []
    for side in ("home", "away"):
        for direction in ("for", "against"):
            name = f"{side}_{direction}"
            cell = features["cells"][f"{side}_all_{window}_{stat_key}_{direction}"]
            values[name] = cell["decay"]
            if cell["decay"] is None:
                reasons.extend(f"{name}:{reason}" for reason in cell["missing_reasons"])
    return values, sorted(set(reasons))
