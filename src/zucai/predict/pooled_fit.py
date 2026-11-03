"""Full DC gradient and constrained solver for new multi-league pooled fits.

The objective, bounds, attack-sum equality and final SLSQP tolerance match
penaltyblog 1.13.1. Its incomplete gradient is replaced only for this new pooled
path. Augmented Lagrangian L-BFGS-B supplies an initial point, never a final fit.
Domestic and single-league pooled models must keep upstream engine.fit().
"""

import logging

import numpy as np
from scipy.optimize import minimize

from zucai.predict.pooled import POOL_FITTING_METHOD

FITTING_METHOD = POOL_FITTING_METHOD
TAU_FLOOR = 1e-9  # Exact floor in penaltyblog 1.13.1 loss.pyx.
OUTER_STEPS = 8
INNER_MAXITER = 300
LOG = logging.getLogger(__name__)


def complete_gradient(engine, params):
    """Gradient of the original negative DC log likelihood, with its tau floor.

    At tau == TAU_FLOOR the loss has a kink; choose the flat-side derivative.
    Derivative verification must use points away from that kink.
    """
    n = engine.n_teams
    home, away = engine.home_idx, engine.away_idx
    home_flag = 1 - engine.neutral_venue
    home_rate = np.exp(params[home] + params[n + away] + params[-2] * home_flag)
    away_rate = np.exp(params[away] + params[n + home])
    rho = params[-1]
    home_goals, away_goals = engine.goals_home, engine.goals_away
    zero_zero = (home_goals == 0) & (away_goals == 0)
    zero_one = (home_goals == 0) & (away_goals == 1)
    one_zero = (home_goals == 1) & (away_goals == 0)
    one_one = (home_goals == 1) & (away_goals == 1)

    tau = np.ones(len(home), dtype=float)
    tau[zero_zero] = 1 - rho * home_rate[zero_zero] * away_rate[zero_zero]
    tau[zero_one] = 1 + rho * home_rate[zero_one]
    tau[one_zero] = 1 + rho * away_rate[one_zero]
    tau[one_one] = 1 - rho
    d_home = np.zeros(len(home), dtype=float)  # d tau / d log(home_rate)
    d_away = np.zeros(len(home), dtype=float)  # d tau / d log(away_rate)
    d_rho = np.zeros(len(home), dtype=float)
    d_home[zero_zero] = -rho * home_rate[zero_zero] * away_rate[zero_zero]
    d_away[zero_zero] = d_home[zero_zero]
    d_rho[zero_zero] = -home_rate[zero_zero] * away_rate[zero_zero]
    d_home[zero_one] = rho * home_rate[zero_one]
    d_rho[zero_one] = home_rate[zero_one]
    d_away[one_zero] = rho * away_rate[one_zero]
    d_rho[one_zero] = away_rate[one_zero]
    d_rho[one_one] = -1
    scale = np.divide(
        engine.weights,
        tau,
        out=np.zeros(len(home), dtype=float),
        where=tau > TAU_FLOOR,
    )
    home_gradient = engine.weights * (home_rate - home_goals) - scale * d_home
    away_gradient = engine.weights * (away_rate - away_goals) - scale * d_away
    attack = np.bincount(home, weights=home_gradient, minlength=n) + np.bincount(
        away, weights=away_gradient, minlength=n
    )
    defence = np.bincount(away, weights=home_gradient, minlength=n) + np.bincount(
        home, weights=away_gradient, minlength=n
    )
    return np.r_[attack, defence, np.dot(home_gradient, home_flag), -np.dot(scale, d_rho)]


def _bounds(engine):
    return [(-2.5, 2.5)] * (2 * engine.n_teams) + [(0, 2), (-2.5, 2.5)]


def _constraint(engine, params):
    return float(sum(params[: engine.n_teams]) - engine.n_teams)


def _constraint_jacobian(engine):
    return np.r_[np.ones(engine.n_teams), np.zeros(engine.n_teams + 2)]


def _valid_start(engine, params):
    params = np.asarray(params)
    limits = np.asarray(_bounds(engine))
    return (
        params.shape == (len(limits),)
        and np.isfinite(params).all()
        and (params >= limits[:, 0] - 1e-10).all()
        and (params <= limits[:, 1] + 1e-10).all()
    )


def _full_slsqp(engine):
    normal = _constraint_jacobian(engine)
    try:
        engine._fit(
            engine._loss_function,
            engine._params,
            [{"type": "eq", "fun": lambda x: _constraint(engine, x), "jac": lambda x: normal}],
            _bounds(engine),
            None,  # Upstream maxiter=1000, SciPy SLSQP default ftol=1e-6.
            lambda x: complete_gradient(engine, x),
        )
        if (
            not engine._res.success
            or not _valid_start(engine, engine._params)
            or abs(_constraint(engine, engine._params)) > 1e-6
        ):
            raise ValueError("联合模型最终SLSQP参数或约束无效")
        loss = float(engine._loss_function(engine._params))
        if not np.isfinite(loss) or not np.isclose(
            -engine.loglikelihood, loss, rtol=1e-12, atol=1e-8
        ):
            raise ValueError("联合模型最终SLSQP未得到有限的原始DC目标")
    except Exception:
        engine.fitted = False
        raise
    return loss


def _augmented_initialization(engine):
    params = engine._params.copy()
    normal = _constraint_jacobian(engine)
    multiplier, penalty = 0.0, 1.0 / engine.n_teams
    trace = []
    for outer in range(OUTER_STEPS):

        def objective(values, dual=multiplier, quadratic=penalty):
            residual = _constraint(engine, values)
            return engine._loss_function(values) + dual * residual + 0.5 * quadratic * residual**2

        def gradient(values, dual=multiplier, quadratic=penalty):
            residual = _constraint(engine, values)
            return complete_gradient(engine, values) + (dual + quadratic * residual) * normal

        result = minimize(
            objective,
            params,
            jac=gradient,
            bounds=_bounds(engine),
            method="L-BFGS-B",
            options={"maxiter": INNER_MAXITER, "ftol": 1e-12, "gtol": 1e-8, "maxls": 40},
        )
        if not _valid_start(engine, result.x) or not np.isfinite(result.fun):
            raise ValueError("联合模型初始化参数无效")
        params = result.x.copy()
        residual = _constraint(engine, params)
        trace.append(
            dict(
                outer=outer,
                penalty=penalty,
                iterations=int(result.nit),
                success=bool(result.success),
                loss=float(engine._loss_function(params)),
                attack_sum_residual=residual,
            )
        )
        LOG.debug("联合模型初始化外步 %d：迭代 %d，attack和残差 %.9g", outer, result.nit, residual)
        if abs(residual) < 1e-7:
            break
        multiplier += penalty * residual
        penalty *= 10
    return params, trace


def fit_pooled_engine(engine):
    """Fit a fresh multi-league engine; return deterministic solver provenance."""
    if engine.fitted:
        raise ValueError("联合模型须使用本折历史构造的新引擎")
    original = engine._params.copy()
    failure = None
    try:
        candidate, trace = _augmented_initialization(engine)
        if not _valid_start(engine, candidate):
            raise ValueError("联合模型初始化参数无效")
        if not np.isfinite(engine._loss_function(candidate)):
            raise ValueError("联合模型初始化目标无效")
        engine._params = candidate.copy()
    except Exception as exc:
        engine._params = original
        trace = []
        failure = str(exc)
        LOG.warning("联合模型初始化失败，回落原始冷起点：%s", exc)
    loss = _full_slsqp(engine)
    return dict(
        optimizer="SLSQP",
        gradient="full_dc_with_tau_floor",
        initialization=(
            "cold_start_after_initialization_failure"
            if failure is not None
            else "augmented_lagrangian_lbfgsb"
        ),
        initialization_failure=failure,
        initialization_steps=trace,
        outer_steps_limit=OUTER_STEPS,
        inner_iterations_limit=INNER_MAXITER,
        maxiter=1000,
        ftol=1e-6,
        iterations=int(engine._res.nit),
        success=bool(engine._res.success),
        loss=loss,
        attack_sum_residual=_constraint(engine, engine._params),
    )
