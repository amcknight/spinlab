"""Warm-started MAP optimization for streaming inference.

Nelder-Mead (gradient-free, accepts bounds). At low dim with finicky integrand-
based likelihoods, this is much more robust than L-BFGS-B, which fails when its
numerical gradient hits regions where the likelihood is -inf.
"""
import numpy as np
from scipy.optimize import minimize

import config
from model import log_posterior, initial_theta


def _nll(theta, data, hazard):
    lp = log_posterior(theta, data, hazard)
    if not np.isfinite(lp):
        return config.NLL_PENALTY
    return -lp


def _bounds(data, n_params):
    """Bounds in log-space. Only bpt has a hard upper bound, from observed data."""
    survived = [t for o, t in data if o == 'survived']
    log_bpt_upper = (np.log(min(survived)) - config.BPT_UPPER_BOUND_LOG_BACKOFF
                     if survived else np.inf)
    return [(-np.inf, log_bpt_upper)] + [(-np.inf, np.inf)] * (n_params - 1)


def _initial_simplex(theta_init):
    """Explicit simplex with fixed per-axis delta in log-space.

    scipy's default builds the simplex from |theta_init|, which collapses when
    log-parameters are near zero. We use a fixed delta so each vertex is a
    meaningful step from theta_init.
    """
    d = len(theta_init)
    simplex = np.tile(theta_init, (d + 1, 1))
    for i in range(d):
        simplex[i + 1, i] += config.NM_SIMPLEX_DELTA
    return simplex


def finite_diff_hessian(f, theta, eps):
    """Symmetric finite-difference Hessian of scalar f at theta."""
    d = len(theta)
    H = np.zeros((d, d))
    f0 = f(theta)
    for i in range(d):
        ti_p = theta.copy(); ti_p[i] += eps
        ti_m = theta.copy(); ti_m[i] -= eps
        H[i, i] = (f(ti_p) - 2.0 * f0 + f(ti_m)) / (eps ** 2)
        for j in range(i + 1, d):
            tpp = theta.copy(); tpp[i] += eps; tpp[j] += eps
            tmm = theta.copy(); tmm[i] -= eps; tmm[j] -= eps
            tpm = theta.copy(); tpm[i] += eps; tpm[j] -= eps
            tmp = theta.copy(); tmp[i] -= eps; tmp[j] += eps
            H[i, j] = (f(tpp) - f(tpm) - f(tmp) + f(tmm)) / (4.0 * eps ** 2)
            H[j, i] = H[i, j]
    return H


def laplace_cov(theta_map, data, hazard):
    """Posterior covariance (in log-space) from Hessian of -log_posterior at MAP.

    The function passed to finite-diff is the penalized NLL (_nll), not raw
    -log_posterior, so perturbations that cross into -inf regions return a
    large finite value rather than poisoning the Hessian with NaN.

    Returns (cov, ok) where ok=False if the Hessian was non-positive-definite
    even after symmetric regularization; the returned cov is then a pseudo-
    inverse and bands derived from it should be treated as approximate.
    """
    H = finite_diff_hessian(
        lambda t: _nll(t, data, hazard),
        theta_map, eps=config.HESSIAN_FD_EPS,
    )
    if not np.all(np.isfinite(H)):
        return None, False
    try:
        eigvals = np.linalg.eigvalsh(H)
    except np.linalg.LinAlgError:
        return None, False
    ok = bool(np.min(eigvals) > 0)
    if not ok:
        H = H + (abs(np.min(eigvals)) + 1e-6) * np.eye(len(theta_map))
    try:
        cov = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        cov = np.linalg.pinv(H)
        ok = False
    return cov, ok


def log_evidence_laplace(theta_map, cov, data, hazard):
    """Laplace approximation to the log marginal likelihood log p(data | model):

        log p(data | M) ≈ log p(data, theta_MAP)  +  (d/2) log(2 pi)  +  (1/2) log|Σ|
                       =  log_posterior(theta_MAP) + (d/2) log(2 pi)  +  (1/2) log|Σ|

    The volume term (1/2) log|Σ| is what implements the Occam penalty: more
    parameters mean a higher-dimensional posterior, and unless the data
    sharply constrains them, |Σ| grows and the evidence drops.
    """
    if cov is None:
        return float('-inf')
    d = len(theta_map)
    log_post = log_posterior(theta_map, data, hazard)
    if not np.isfinite(log_post):
        return float('-inf')
    sign, log_det_cov = np.linalg.slogdet(cov)
    if sign <= 0 or not np.isfinite(log_det_cov):
        return float('-inf')
    return log_post + 0.5 * d * np.log(2.0 * np.pi) + 0.5 * log_det_cov


def sample_posterior(theta_map, cov, n_samples, rng=None):
    if rng is None:
        rng = np.random.default_rng(0)
    return rng.multivariate_normal(theta_map, cov, size=n_samples)


def find_map(data, hazard, theta_init=None):
    if theta_init is None:
        theta_init = initial_theta(hazard)
    theta_init = theta_init.copy()

    bounds = _bounds(data, len(theta_init))

    # Clamp warm-started theta_init inside the bpt bound. If the previous MAP
    # had bpt above the new min-survived (because a faster survived attempt
    # just arrived), pull it back to the new admissible region.
    bpt_upper = bounds[0][1]
    if theta_init[0] >= bpt_upper:
        theta_init[0] = bpt_upper - config.NM_SIMPLEX_DELTA

    result = minimize(
        _nll,
        theta_init,
        args=(data, hazard),
        method='Nelder-Mead',
        bounds=bounds,
        options={
            'xatol': config.NM_XATOL,
            'fatol': config.NM_FATOL,
            'maxiter': config.NM_MAXITER,
            'adaptive': True,
            'initial_simplex': _initial_simplex(theta_init),
        },
    )
    return result.x, result
