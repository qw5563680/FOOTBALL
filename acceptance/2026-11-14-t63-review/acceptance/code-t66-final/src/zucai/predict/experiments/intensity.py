"""Dixon-Coles regression on lagged technical intensity, not integer-cast xG."""

import hashlib
import json
from math import log

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import poisson

from zucai.predict.base import utc
from zucai.predict.calibration import fit_draw_offset
from zucai.predict.dixon_coles import InsufficientHistory

INPUTS = ("home_for", "away_for", "home_against", "away_against")


def complete(inputs):
    if set(inputs) != set(INPUTS):
        raise ValueError("强度输入须严格使用四项白名单，不允许赔率或额外特征")
    return all(value is not None and np.isfinite(value) and value >= 0 for value in inputs.values())


class StatsIntensity:
    def __init__(self, *, stat_key, window, ablation=False, half_life_days=90):
        if stat_key not in {"expected_goals", "shots_on_target"} or window not in (5, 10):
            raise ValueError("首轮仅支持 xG/射正与近5/10场")
        if half_life_days != 90:
            raise ValueError("首轮冻结协议半衰期为90天")
        self.stat_key, self.window, self.ablation = stat_key, window, ablation
        self.name = "dc-xg-v1" if stat_key == "expected_goals" else "dc-sot-v1"
        if ablation:
            self.name += "-no-against"

    def _design(self, inputs):
        home = [1, 1, np.log1p(inputs["home_for"])]
        away = [1, 0, np.log1p(inputs["away_for"])]
        if not self.ablation:
            home.append(np.log1p(inputs["away_against"]))
            away.append(np.log1p(inputs["home_against"]))
        return home, away

    def fit(self, observations, *, as_of):
        self.as_of = utc(as_of)
        selected = []
        for row in observations:
            has_inputs = complete(row["inputs"])
            kickoff, known = utc(row["kickoff_utc"]), utc(row["available_utc"])
            if utc(row["feature_cutoff"]) > kickoff:
                raise ValueError("训练样本特征截点不得晚于自身开球")
            if kickoff < known < self.as_of and has_inputs:
                selected.append(row)
        self.training_ids = [r["match_id"] for r in selected]
        if len(set(self.training_ids)) != len(self.training_ids):
            raise ValueError("训练比赛ID重复")
        # Both arms use the full model's minimum, as well as its completeness mask.
        if len(selected) < 11:
            raise InsufficientHistory("完整统计训练场数不足")
        self.training_rows = len(selected)
        design = [self._design(row["inputs"]) for row in selected]
        h, a = np.array([x[0] for x in design]), np.array([x[1] for x in design])
        y, z = np.array([[r["home_goals"], r["away_goals"]] for r in selected], dtype=float).T
        if (
            not np.isfinite([y, z]).all()
            or (np.array([y, z]) < 0).any()
            or not np.equal([y, z], np.floor([y, z])).all()
        ):
            raise ValueError("须使用有效90分钟整数比分标签")
        ages = np.array(
            [(self.as_of - utc(r["kickoff_utc"])).total_seconds() / 86400 for r in selected]
        )
        weights = np.exp(-log(2) * ages / 90)
        weights /= weights.sum()

        def poisson_loss(beta):
            lh, la = h @ beta, a @ beta
            return float(
                weights
                @ (np.exp(lh) - y * lh + gammaln(y + 1) + np.exp(la) - z * la + gammaln(z + 1))
            )

        def poisson_jac(beta):
            return h.T @ (weights * (np.exp(h @ beta) - y)) + a.T @ (
                weights * (np.exp(a @ beta) - z)
            )

        initial = np.zeros(h.shape[1])
        initial[0] = np.clip(log(max(float((y.mean() + z.mean()) / 2), 1e-6)), -5, 5)
        start = minimize(
            poisson_loss,
            initial,
            jac=poisson_jac,
            method="L-BFGS-B",
            bounds=[(-5, 5)] * len(initial),
        )

        def constraints(params):
            lam, mu = np.exp(h @ params[:-1]), np.exp(a @ params[:-1])
            rho = params[-1]
            return (
                np.array([1 - rho * np.max(lam * mu), 1 + rho * max(lam.max(), mu.max()), 1 - rho])
                - 1e-9
            )

        def objective(params):
            lam, mu = np.exp(h @ params[:-1]), np.exp(a @ params[:-1])
            rho = params[-1]
            tau = np.ones(len(y))
            tau[(y == 0) & (z == 0)] = (1 - lam * mu * rho)[(y == 0) & (z == 0)]
            tau[(y == 0) & (z == 1)] = (1 + lam * rho)[(y == 0) & (z == 1)]
            tau[(y == 1) & (z == 0)] = (1 + mu * rho)[(y == 1) & (z == 0)]
            tau[(y == 1) & (z == 1)] = 1 - rho
            return poisson_loss(params[:-1]) - float(weights @ np.log(np.maximum(tau, 1e-12)))

        fit = minimize(
            objective,
            np.r_[start.x if start.success else initial, 0.0],
            method="SLSQP",
            bounds=[(-5, 5)] * len(initial) + [(-1, 1)],
            constraints={"type": "ineq", "fun": constraints},
            options={"maxiter": 500, "ftol": 1e-9},
        )
        if not fit.success or not np.isfinite(fit.fun) or constraints(fit.x).min() < -1e-7:
            raise ValueError("统计强度拟合失败：" + str(fit.message))
        self.parameters = fit.x
        self.coefficients = dict(
            zip(
                ["intercept", "home", "for"] + ([] if self.ablation else ["against"]) + ["rho"],
                map(float, fit.x),
                strict=True,
            )
        )
        self.draw_offset = 0.0
        self.version = "fitting"
        draws = [float(np.trace(self._matrix(row["inputs"])[0])) for row in selected]
        self.draw_offset = fit_draw_offset(draws, (y == z).astype(int), weights)
        payload = dict(
            name=self.name,
            window=self.window,
            as_of=self.as_of.isoformat(),
            coefficients=self.coefficients,
            draw_offset=self.draw_offset,
            training_ids=self.training_ids,
        )
        self.version = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.diagnostics = dict(
            optimizer="SLSQP",
            success=True,
            iterations=int(fit.nit),
            loss=float(fit.fun),
            poisson_initialization_success=bool(start.success),
        )
        return self

    def _matrix(self, inputs):
        if not complete(inputs):
            raise ValueError("missing technical inputs")
        h, a = self._design(inputs)
        lam, mu = np.exp(np.array([h, a]) @ self.parameters[:-1])
        rho = float(np.clip(self.parameters[-1], max(-1 / lam, -1 / mu), min(1.0, 1 / (lam * mu))))
        grid = np.outer(poisson.pmf(np.arange(10), lam), poisson.pmf(np.arange(10), mu))
        grid[0, 0] *= 1 - lam * mu * rho
        grid[0, 1] *= 1 + lam * rho
        grid[1, 0] *= 1 + mu * rho
        grid[1, 1] *= 1 - rho
        mass = float(grid.sum())
        if not np.isfinite(grid).all() or grid.min() < -1e-12 or not 0 < mass <= 1 + 1e-9:
            raise ValueError("统计强度未产生有效比分矩阵")
        grid = np.maximum(grid, 0)
        return grid / grid.sum(), dict(
            excluded_score_mass=max(0.0, 1 - mass),
            rho_clipped=rho != float(self.parameters[-1]),
            home_rate=float(lam),
            away_rate=float(mu),
        )

    def predict(self, inputs, *, calibrated=False):
        matrix, info = self._matrix(inputs)
        if calibrated:
            matrix[np.diag_indices(10)] *= np.exp(self.draw_offset)
            matrix /= matrix.sum()
        return matrix, dict(
            info,
            model_name=self.name,
            model_version=self.version,
            model_type="TEAM",
            p2_ready=False,
            bet_allowed=False,
            validation_status="experimental_unadopted",
            calibrated=calibrated,
            draw_offset=self.draw_offset if calibrated else 0,
            training_rows=self.training_rows,
            window=self.window,
            fit_cutoff=self.as_of.isoformat(),
            coefficients=self.coefficients,
        )
