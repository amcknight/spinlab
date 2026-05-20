"""beta2 fit shim. Parallel to fit_jax.py but binds learning_model_v07_beta2_jax.

V2-SCOPE — PARKED. v1 ships haz1 only. See /V1_ESSENCE.md.

Same machinery via fit_core.py; only the warm_init differs (15-param theta
with broken shape-param symmetry).
"""
import numpy as np

import learning_model_v07_beta2_jax as lm2
import fit_core


sample_laplace = fit_core.sample_laplace


def warm_init_from_data(time_ms, is_died, frac_window=0.25):
    """Length-15 theta_init. First 10 entries mirror fit_jax.warm_init_from_data;
    the 5 shape params start at the symmetry-broken init (mu_1=0.3, mu_2=0.7,
    kappa at prior median, eta_1=0). Data-driven shape-param init isn't trivial
    -- you'd need to estimate s_death per attempt, which requires bpt and the
    slop curve already. Prior-mean shape init lets L-BFGS take care of it.
    """
    time_ms = np.asarray(time_ms, dtype=np.float64)
    is_died = np.asarray(is_died, dtype=bool)
    N = len(time_ms)
    if N < 4:
        return np.asarray(lm2.initial_theta(), dtype=np.float64)

    survived_T = time_ms[~is_died]
    if len(survived_T) > 0:
        bpt_est = max(float(np.min(survived_T)) - 500.0, 1000.0)
    else:
        bpt_est = float(np.exp(lm2.PRIOR_LOG_BPT_MEAN))

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
    sf_inf  = sf_l  if sf_l  is not None else float(np.exp(lm2.PRIOR_LOG_SF_INF_MEAN))
    sf_1    = sf_e  if sf_e  is not None else float(np.exp(lm2.PRIOR_LOG_SF_1_MEAN))
    ssp_inf = ssp_l if ssp_l is not None else float(np.exp(lm2.PRIOR_LOG_SSP_INF_MEAN))
    ssp_1   = ssp_e if ssp_e is not None else float(np.exp(lm2.PRIOR_LOG_SSP_1_MEAN))

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
        float(lm2.PRIOR_LOG_HALFLIFE_SF_MEAN),
        float(lm2.PRIOR_LOG_HALFLIFE_SSP_MEAN),
        float(lm2.PRIOR_LOG_HALFLIFE_ALPHA_MEAN),
        np.log(0.3 / 0.7),       # logit(mu_1) = logit(0.3)  -- breaks label sym
        np.log(0.7 / 0.3),       # logit(mu_2) = logit(0.7)
        float(lm2.PRIOR_LOG_KAPPA_MEAN),
        float(lm2.PRIOR_LOG_KAPPA_MEAN),
        float(lm2.PRIOR_ETA_MEAN),
    ])


_PREWARMED_BUCKETS = set()


def prewarm_buckets(buckets=(64, 128, 192, 256, 320, 384, 448, 512, 768, 1024)):
    fit_core.prewarm(lm2, buckets, _PREWARMED_BUCKETS)


def find_map(time_ms, is_died, theta_init=None,
             halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
             halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
             halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None,
             *, bucket_base=64, maxiter=200):
    if theta_init is None:
        theta_init = warm_init_from_data(time_ms, is_died)
    pool = fit_core.resolve_pool(
        lm2, halflife_sf_pop_mean, halflife_sf_pop_sigma,
        halflife_ssp_pop_mean, halflife_ssp_pop_sigma,
        halflife_alpha_pop_mean, halflife_alpha_pop_sigma,
    )
    return fit_core.find_map(
        lm2, time_ms, is_died, theta_init,
        pool=pool, bucket_base=bucket_base, maxiter=maxiter,
    )


def laplace_posterior(theta_map, time_ms, is_died,
                      halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
                      halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
                      halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None,
                      *, bucket_base=64, regularize=1e-6):
    pool = fit_core.resolve_pool(
        lm2, halflife_sf_pop_mean, halflife_sf_pop_sigma,
        halflife_ssp_pop_mean, halflife_ssp_pop_sigma,
        halflife_alpha_pop_mean, halflife_alpha_pop_sigma,
    )
    return fit_core.laplace_posterior(
        lm2, theta_map, time_ms, is_died,
        pool=pool, bucket_base=bucket_base, regularize=regularize,
    )
