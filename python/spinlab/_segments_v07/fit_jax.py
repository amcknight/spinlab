"""haz1 fit shim: data-driven warm init + thin wrappers over fit_core.

The fit machinery (MAP via L-BFGS-B with JIT'd value/grad, Laplace
posterior, JIT prewarm, sample_laplace) lives in fit_core.py. This file
binds the haz1 model module (learning_model_v07_jax) into that pipeline
and provides the model-specific warm_init.

Public API (preserved from the pre-refactor version):
    warm_init_from_data(time_ms, is_died) -> theta_init
    prewarm_buckets(buckets=...)
    find_map(time_ms, is_died, theta_init=None, ...)
    laplace_posterior(theta_map, time_ms, is_died, ...)
    sample_laplace(theta_map, cov, n_samples, seed)
"""
import numpy as np

import learning_model_v07_jax as lm
import fit_core


# Re-export sample_laplace unchanged (model-agnostic).
sample_laplace = fit_core.sample_laplace


# ---------------------------------------------------------------------------
# Warm start (haz1-specific: estimates the 10 V07 latents from data windows)
# ---------------------------------------------------------------------------


def warm_init_from_data(time_ms, is_died, frac_window=0.25):
    """Heuristic theta_init from observed data. Use as the cold-start MAP init.

    Estimates per-curve asymptote and first-attempt values from late vs early
    attempt windows. Halflives stay at prior medians (the windowed data has
    no curvature signal to estimate them, and bad halflife guesses send fits
    sideways).

    Returns a length-10 numpy theta in the V07 layout.
    """
    time_ms = np.asarray(time_ms, dtype=np.float64)
    is_died = np.asarray(is_died, dtype=bool)
    N = len(time_ms)
    if N < 4:
        return np.asarray(lm.initial_theta(), dtype=np.float64)

    # bpt: small safety margin below the fastest survived time.
    survived_T = time_ms[~is_died]
    if len(survived_T) > 0:
        bpt_est = max(float(np.min(survived_T)) - 500.0, 1000.0)
    else:
        bpt_est = float(np.exp(lm.PRIOR_LOG_BPT_MEAN))

    w = max(1, int(N * frac_window))
    early, late = slice(0, w), slice(N - w, N)

    def _slop_stats(window):
        survived_in_window = time_ms[window][~is_died[window]]
        slop = survived_in_window - bpt_est
        slop = slop[slop > 0]
        if len(slop) < 2:
            return None, None
        mean_slop = float(np.mean(slop))
        std_slop  = float(np.std(slop))
        return mean_slop / bpt_est, (std_slop / mean_slop if mean_slop > 0 else 1.0)

    sf_e, ssp_e = _slop_stats(early)
    sf_l, ssp_l = _slop_stats(late)
    sf_inf  = sf_l  if sf_l  is not None else float(np.exp(lm.PRIOR_LOG_SF_INF_MEAN))
    sf_1    = sf_e  if sf_e  is not None else float(np.exp(lm.PRIOR_LOG_SF_1_MEAN))
    ssp_inf = ssp_l if ssp_l is not None else float(np.exp(lm.PRIOR_LOG_SSP_INF_MEAN))
    ssp_1   = ssp_e if ssp_e is not None else float(np.exp(lm.PRIOR_LOG_SSP_1_MEAN))

    def _alpha_from_window(window):
        died_in_window = is_died[window]
        p_die = float(died_in_window.mean()) if len(died_in_window) > 0 else 0.5
        p_die = min(max(p_die, 0.02), 0.98)
        return -np.log(1.0 - p_die)

    alpha_inf = _alpha_from_window(late)
    alpha_1   = _alpha_from_window(early)

    return np.array([
        np.log(bpt_est),
        np.log(max(sf_inf, 1e-4)),
        np.log(max(ssp_inf, 1e-3)),
        np.log(max(alpha_inf, 1e-3)),
        np.log(max(sf_1, 1e-4)),
        np.log(max(ssp_1, 1e-3)),
        np.log(max(alpha_1, 1e-3)),
        lm.PRIOR_LOG_HALFLIFE_SF_MEAN,
        lm.PRIOR_LOG_HALFLIFE_SSP_MEAN,
        lm.PRIOR_LOG_HALFLIFE_ALPHA_MEAN,
    ])


# ---------------------------------------------------------------------------
# Thin shims over fit_core (bind lm)
# ---------------------------------------------------------------------------


_PREWARMED_BUCKETS = set()


def prewarm_buckets(buckets=(64, 128, 192, 256, 320, 384, 448, 512, 768, 1024)):
    """One-time JIT compile per bucket size. ~200-400ms per bucket."""
    fit_core.prewarm(lm, buckets, _PREWARMED_BUCKETS)


def find_map(time_ms, is_died, theta_init=None,
             halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
             halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
             halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None,
             *, bucket_base=64, maxiter=200):
    if theta_init is None:
        theta_init = warm_init_from_data(time_ms, is_died)
    pool = fit_core.resolve_pool(
        lm, halflife_sf_pop_mean, halflife_sf_pop_sigma,
        halflife_ssp_pop_mean, halflife_ssp_pop_sigma,
        halflife_alpha_pop_mean, halflife_alpha_pop_sigma,
    )
    return fit_core.find_map(
        lm, time_ms, is_died, theta_init,
        pool=pool, bucket_base=bucket_base, maxiter=maxiter,
    )


def laplace_posterior(theta_map, time_ms, is_died,
                      halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
                      halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
                      halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None,
                      *, bucket_base=64, regularize=1e-6):
    pool = fit_core.resolve_pool(
        lm, halflife_sf_pop_mean, halflife_sf_pop_sigma,
        halflife_ssp_pop_mean, halflife_ssp_pop_sigma,
        halflife_alpha_pop_mean, halflife_alpha_pop_sigma,
    )
    return fit_core.laplace_posterior(
        lm, theta_map, time_ms, is_died,
        pool=pool, bucket_base=bucket_base, regularize=regularize,
    )


# Internal helpers kept for backward compat with diagnostic scripts in tmp/.
_bpt_upper = fit_core.bpt_upper
_hessian = lambda theta, time_pad, died_pad, valid_n, *pool_args: \
    fit_core._fit_fns_for(lm).hessian(theta, time_pad, died_pad, valid_n, *pool_args)
_resolve_pool = lambda *args, **kwargs: fit_core.resolve_pool(lm, *args, **kwargs)
