"""Shared fitting machinery, parametrized over the model module.

`fit_jax.py` and `fit_jax_beta2.py` are now thin shims that bind a specific
learning_model module (haz1 or beta2) into the generic pipeline here. Adding
a third hazard model is a ~30-line file: implement `log_posterior_padded` +
`bucket_size` + `pad_to` + `initial_theta` + `PRIOR_LOG_HALFLIFE_*_MEAN` and
`HALFLIFE_SIGMA_MAX` constants, then wire up warm_init + find_map shims.

The JIT functions are closed over a specific model module and cached, so
each (model, bucket size) combo compiles once and is reused. That keeps
the JIT cache tight without paying recompile cost on each call.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

import jax
jax.config.update("jax_enable_x64", True)
import jax.numpy as jnp
import numpy as np
from scipy.optimize import minimize

import config


# Penalty for non-finite log_posterior; lets L-BFGS-B's line search detect
# the cliff and backtrack instead of failing on inf/NaN.
_NLL_PENALTY = 1e10


@dataclass
class FitFns:
    nlp: Callable
    value_and_grad: Callable
    hessian: Callable


@lru_cache(maxsize=8)
def _fit_fns_for(lm):
    """JIT-compile and cache the (nlp, value_and_grad, hessian) trio for a
    specific model module. lru_cache keys on the module identity, so each
    model gets one entry."""

    @jax.jit
    def _nlp(theta, time_pad, died_pad, valid_n,
             hl_sf_m, hl_sf_s, hl_ssp_m, hl_ssp_s, hl_a_m, hl_a_s):
        lp = lm.log_posterior_padded(
            theta, time_pad, died_pad, valid_n,
            halflife_sf_pop_mean=hl_sf_m,    halflife_sf_pop_sigma=hl_sf_s,
            halflife_ssp_pop_mean=hl_ssp_m,  halflife_ssp_pop_sigma=hl_ssp_s,
            halflife_alpha_pop_mean=hl_a_m,  halflife_alpha_pop_sigma=hl_a_s,
        )
        return jnp.where(jnp.isfinite(lp), -lp, _NLL_PENALTY)

    @jax.jit
    def _vg(theta, time_pad, died_pad, valid_n,
            hl_sf_m, hl_sf_s, hl_ssp_m, hl_ssp_s, hl_a_m, hl_a_s):
        return jax.value_and_grad(_nlp, argnums=0)(
            theta, time_pad, died_pad, valid_n,
            hl_sf_m, hl_sf_s, hl_ssp_m, hl_ssp_s, hl_a_m, hl_a_s)

    @jax.jit
    def _hess(theta, time_pad, died_pad, valid_n,
              hl_sf_m, hl_sf_s, hl_ssp_m, hl_ssp_s, hl_a_m, hl_a_s):
        return jax.hessian(_nlp, argnums=0)(
            theta, time_pad, died_pad, valid_n,
            hl_sf_m, hl_sf_s, hl_ssp_m, hl_ssp_s, hl_a_m, hl_a_s)

    return FitFns(_nlp, _vg, _hess)


def resolve_pool(lm, sf_m=None, sf_s=None, ssp_m=None, ssp_s=None,
                 a_m=None, a_s=None):
    """Resolve six pool args to floats, falling back to lm's module defaults.
    Returns tuple in canonical (sf_m, sf_s, ssp_m, ssp_s, a_m, a_s) order."""
    def _r(val, default):
        return float(default if val is None else val)
    return (
        _r(sf_m,  lm.PRIOR_LOG_HALFLIFE_SF_MEAN),
        _r(sf_s,  lm.HALFLIFE_SIGMA_MAX),
        _r(ssp_m, lm.PRIOR_LOG_HALFLIFE_SSP_MEAN),
        _r(ssp_s, lm.HALFLIFE_SIGMA_MAX),
        _r(a_m,   lm.PRIOR_LOG_HALFLIFE_ALPHA_MEAN),
        _r(a_s,   lm.HALFLIFE_SIGMA_MAX),
    )


def bpt_upper(time_ms, is_died):
    """Hard upper bound on log(bpt) from min observed survived time."""
    survived = np.asarray(time_ms)[~np.asarray(is_died).astype(bool)]
    if len(survived) == 0:
        return np.inf
    return float(np.log(np.min(survived)) - config.BPT_UPPER_BOUND_LOG_BACKOFF)


def prewarm(lm, buckets, _cache):
    """JIT-compile the nlp + grad + hessian paths for each bucket size
    against `lm`. `_cache` is a module-level set of already-warmed sizes
    (kept per-model in the shim modules).

    The Hessian is prewarmed too because `laplace_posterior` needs it, and
    the v1 serializer pipeline (find_map → laplace_posterior → samples)
    would otherwise spike at every bucket crossing during streaming."""
    fns = _fit_fns_for(lm)
    theta_dummy = jnp.asarray(lm.initial_theta())
    pool = tuple(jnp.asarray(v) for v in resolve_pool(lm))
    for n_max in buckets:
        if n_max in _cache:
            continue
        t_dummy = jnp.full((n_max,), 30000.0, dtype=jnp.float64)
        d_dummy = jnp.zeros(n_max, dtype=bool)
        _ = float(fns.nlp(theta_dummy, t_dummy, d_dummy, 1, *pool))
        _ = fns.value_and_grad(theta_dummy, t_dummy, d_dummy, 1, *pool)
        _ = fns.hessian(theta_dummy, t_dummy, d_dummy, 1, *pool)
        _cache.add(n_max)


def find_map(lm, time_ms, is_died, theta_init, *, pool=None,
             bucket_base=64, maxiter=200):
    """Generic L-BFGS-B MAP fit against the supplied model module.

    Args:
        lm: a learning_model module (must expose log_posterior_padded,
            bucket_size, pad_to, initial_theta, PRIOR_LOG_HALFLIFE_*_MEAN,
            HALFLIFE_SIGMA_MAX).
        time_ms, is_died: numpy or jax arrays, length N.
        theta_init: starting theta (length lm-dependent).
        pool: length-6 tuple from resolve_pool(), or None to use defaults.
        bucket_base: pad N up to multiples of this for tight JIT cache.
        maxiter: L-BFGS-B iteration cap.

    Returns: (theta_map_np, info_dict).
    """
    fns = _fit_fns_for(lm)
    theta_init = np.asarray(theta_init, dtype=np.float64).copy()

    n_max = lm.bucket_size(len(time_ms), base=bucket_base)
    time_pad, died_pad, valid_n = lm.pad_to(time_ms, is_died, n_max)

    upper = bpt_upper(time_ms, is_died)
    if theta_init[0] >= upper:
        theta_init[0] = upper - 0.1
    bounds = [(None, upper)] + [(None, None)] * (len(theta_init) - 1)

    if pool is None:
        pool = resolve_pool(lm)
    pool_j = tuple(jnp.asarray(v) for v in pool)

    def fg(theta_np):
        v, g = fns.value_and_grad(jnp.asarray(theta_np), time_pad, died_pad,
                                   valid_n, *pool_j)
        return float(v), np.asarray(g, dtype=np.float64)

    result = minimize(
        fg, theta_init, jac=True, method='L-BFGS-B',
        bounds=bounds,
        options={'maxiter': maxiter, 'ftol': 1e-9, 'gtol': 1e-6},
    )
    # Detect the soft-penalty plateau: the JIT'd _nlp returns _NLL_PENALTY
    # when raw log_posterior is -inf. If the optimizer settled there,
    # scipy.success can be True even though the actual posterior is -inf
    # at the returned theta — and downstream NUTS init will fail. Reflect
    # that in `converged`.
    nll = float(result.fun)
    raw_lp_finite = nll < _NLL_PENALTY - 1.0
    info = {
        'iter': int(result.nit),
        'nll': nll,
        'raw_lp_finite': raw_lp_finite,
        # converged means BOTH scipy succeeded AND raw lp is finite. Soft-
        # penalty plateaus produce scipy.success=True with raw_lp=-inf;
        # rejecting those keeps the contract honest for downstream NUTS.
        'converged': bool(result.success) and raw_lp_finite,
        'scipy_success': bool(result.success),
        'bucket_n': n_max,
        'pool_sf_mean':    pool[0], 'pool_sf_sigma':    pool[1],
        'pool_ssp_mean':   pool[2], 'pool_ssp_sigma':   pool[3],
        'pool_alpha_mean': pool[4], 'pool_alpha_sigma': pool[5],
    }
    return result.x, info


def laplace_posterior(lm, theta_map, time_ms, is_died, *, pool=None,
                      bucket_base=64, regularize=1e-6):
    """Posterior covariance from -log_posterior Hessian at MAP.

    Pass the same pool you passed to find_map. Returns (cov, ok). ok=False
    if H was not PD; cov is then a Tikhonov-regularized pseudoinverse and
    the bands are approximate (use NUTS instead — see fit_nuts).
    """
    fns = _fit_fns_for(lm)
    n_max = lm.bucket_size(len(time_ms), base=bucket_base)
    time_pad, died_pad, valid_n = lm.pad_to(time_ms, is_died, n_max)

    if pool is None:
        pool = resolve_pool(lm)
    pool_j = tuple(jnp.asarray(v) for v in pool)

    H = np.asarray(fns.hessian(jnp.asarray(theta_map), time_pad, died_pad,
                                valid_n, *pool_j))
    if not np.all(np.isfinite(H)):
        return None, False
    try:
        eigvals = np.linalg.eigvalsh(H)
    except np.linalg.LinAlgError:
        return None, False
    ok = bool(np.min(eigvals) > 0)
    if not ok:
        H = H + (abs(np.min(eigvals)) + regularize) * np.eye(H.shape[0])
    try:
        cov = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        cov = np.linalg.pinv(H)
        ok = False
    return cov, ok


def sample_laplace(theta_map, cov, n_samples=2000, seed=0):
    """Draw samples from N(theta_map, cov). Model-agnostic — works for any
    dimension. Returns numpy (n_samples, len(theta_map))."""
    rng = np.random.default_rng(seed)
    return rng.multivariate_normal(np.asarray(theta_map), cov, size=n_samples)
