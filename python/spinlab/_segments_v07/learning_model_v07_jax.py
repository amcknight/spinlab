"""JAX rewrite of learning_model_v07 + model for fast MAP and Laplace posteriors.

Mirrors learning_model_v07.py's API (log_likelihood, log_prior, log_posterior,
initial_theta, m_clear_at_n, sample_prior) but built on jax.numpy so the whole
pipeline is differentiable, JIT-compilable, and vmap-able.

V07-specific scope: hardcodes haz1 (K=1 piecewise constant) since that's the
committed hazard. Other hazard models would need their own JAX implementations.

The numpy original (learning_model_v07.py) is the reference; this file should
produce numerically equivalent log_posterior values at any theta. Validation
lives in tests/test_jax_vs_numpy.py.
"""
import jax
import jax.numpy as jnp
from jax.scipy.stats import norm as jnorm

import config


# Reuse layout + constants from the numpy version so we don't drift apart.
from learning_model_v07 import (
    N_STATIC_ASYMP, N_LEARNING, N_PARAMS,
    IDX_LOG_BPT, IDX_LOG_SF_INF, IDX_LOG_SSP_INF, IDX_LOG_ALPHA_INF,
    IDX_LOG_SF_1, IDX_LOG_SSP_1, IDX_LOG_ALPHA_1,
    IDX_LOG_HALFLIFE_SF, IDX_LOG_HALFLIFE_SSP, IDX_LOG_HALFLIFE_ALPHA,
    PRIOR_LOG_BPT_MEAN, PRIOR_LOG_BPT_SD,
    PRIOR_LOG_SF_INF_MEAN, PRIOR_LOG_SF_INF_SD,
    PRIOR_LOG_SSP_INF_MEAN, PRIOR_LOG_SSP_INF_SD,
    PRIOR_LOG_ALPHA_INF_MEAN, PRIOR_LOG_ALPHA_INF_SD,
    PRIOR_LOG_SF_1_MEAN, PRIOR_LOG_SF_1_SD,
    PRIOR_LOG_SSP_1_MEAN, PRIOR_LOG_SSP_1_SD,
    PRIOR_LOG_ALPHA_1_MEAN, PRIOR_LOG_ALPHA_1_SD,
    PRIOR_LOG_HALFLIFE_SF_MEAN,
    PRIOR_LOG_HALFLIFE_SSP_MEAN,
    PRIOR_LOG_HALFLIFE_ALPHA_MEAN,
    HALFLIFE_SIGMA_MIN, HALFLIFE_SIGMA_MAX,
)


# Pre-stack priors into jax arrays for vectorized scoring.
_PRIOR_ASYMP_MEAN = jnp.array([
    PRIOR_LOG_BPT_MEAN, PRIOR_LOG_SF_INF_MEAN,
    PRIOR_LOG_SSP_INF_MEAN, PRIOR_LOG_ALPHA_INF_MEAN,
])
_PRIOR_ASYMP_SD = jnp.array([
    PRIOR_LOG_BPT_SD, PRIOR_LOG_SF_INF_SD,
    PRIOR_LOG_SSP_INF_SD, PRIOR_LOG_ALPHA_INF_SD,
])
_PRIOR_ONE_MEAN = jnp.array([
    PRIOR_LOG_SF_1_MEAN, PRIOR_LOG_SSP_1_MEAN, PRIOR_LOG_ALPHA_1_MEAN,
])
_PRIOR_ONE_SD = jnp.array([
    PRIOR_LOG_SF_1_SD, PRIOR_LOG_SSP_1_SD, PRIOR_LOG_ALPHA_1_SD,
])
_PRIOR_HALFLIFE_MEAN = jnp.array([
    PRIOR_LOG_HALFLIFE_SF_MEAN, PRIOR_LOG_HALFLIFE_SSP_MEAN,
    PRIOR_LOG_HALFLIFE_ALPHA_MEAN,
])


_SMOOTH_ABS_EPS = 1e-2


def _smooth_abs(A, eps=_SMOOTH_ABS_EPS):
    """sqrt(A^2 + eps^2) - eps. Matches the numpy reference; see
    learning_model_v07._smooth_abs for rationale (Hessian autodiff bug at
    the |A| cusp)."""
    return jnp.sqrt(A * A + eps * eps) - eps


def halflife_sigma(A, sigma_max=HALFLIFE_SIGMA_MAX, sigma_min=HALFLIFE_SIGMA_MIN):
    """Joint-prior sigma on log(halflife) given amplitude A. JAX version.

    sigma_max can be overridden by an empirical-Bayes pool that has shrunk the
    across-segment SD on halflife. See learning_model_v07.halflife_sigma docs.
    """
    return sigma_min + (sigma_max - sigma_min) * jnp.tanh(_smooth_abs(A))


def learning_curve_at_n(theta, n):
    """Returns log_sf(n), log_ssp(n), log_alpha(n) at attempt n (scalar or array).

    Vectorized over n: returns shape (..., 3).
    """
    log_inf = theta[IDX_LOG_SF_INF:IDX_LOG_ALPHA_INF + 1]              # (3,)
    log_1   = theta[IDX_LOG_SF_1:IDX_LOG_ALPHA_1 + 1]                  # (3,)
    log_hl  = theta[IDX_LOG_HALFLIFE_SF:IDX_LOG_HALFLIFE_ALPHA + 1]    # (3,)
    halflife = jnp.exp(log_hl)                                          # (3,)
    n_arr = jnp.asarray(n, dtype=jnp.float64)
    decay = jnp.power(2.0, -(n_arr[..., None] - 1.0) / halflife)        # (..., 3)
    return log_inf + (log_1 - log_inf) * decay                          # (..., 3)


def _lognormal_logpdf_safe(x, mu, sigma):
    """log(LN_pdf(x)) = -log(x*sigma*sqrt(2pi)) - 0.5*((log x - mu)/sigma)^2.

    For x <= 0 returns -inf. mu, sigma can broadcast against x.
    """
    safe_x = jnp.where(x > 0, x, 1.0)
    log_pdf = (
        -jnp.log(safe_x) - jnp.log(sigma) - 0.5 * jnp.log(2.0 * jnp.pi)
        - 0.5 * ((jnp.log(safe_x) - mu) / sigma) ** 2
    )
    return jnp.where(x > 0, log_pdf, -jnp.inf)


# Simpson's rule weights for an odd number of points (composite rule).
def _simpson_weights(n_points):
    if n_points % 2 == 0:
        raise ValueError("Simpson's rule needs an odd number of points")
    w = jnp.ones(n_points)
    # interior: alternating 4, 2, 4, 2, ...
    w = w.at[1::2].set(4.0)
    w = w.at[2:-1:2].set(2.0)
    return w   # multiplied by dx/3 by the caller


# Use the same QUAD_POINTS as the numpy code so results match closely. Must be odd.
_QUAD_POINTS = config.QUAD_POINTS + 1 if config.QUAD_POINTS % 2 == 0 else config.QUAD_POINTS
_SIMPSON_W = _simpson_weights(_QUAD_POINTS)
_UNIT_GRID = jnp.linspace(0.0, 1.0, _QUAD_POINTS)


def _log_lik_died_vec(tau_obs, bpt, mu_log, sigma_log, alpha):
    """Vectorized log P(died at tau_obs).  All args are (N,) arrays except bpt (scalar).

    Replicates model.log_lik_died via Simpson's rule on a fixed grid per attempt.
    For haz1 (K=1 constant alpha on [0,1]):
        h(s)   = alpha
        H(s)   = alpha * s
        S(s)   = exp(-alpha * s)
    Integrand: clean_dist(T) * alpha * exp(-alpha * tau/T) / T
    """
    mean_slop = jnp.exp(mu_log + 0.5 * sigma_log ** 2)
    std_slop = mean_slop * jnp.sqrt(jnp.exp(sigma_log ** 2) - 1.0)
    T_max = bpt + mean_slop + config.QUAD_UPPER_SIGMAS * std_slop      # (N,)
    T_min = jnp.maximum(bpt, tau_obs)                                   # (N,)

    width = T_max - T_min                                               # (N,)
    T_grid = T_min[:, None] + width[:, None] * _UNIT_GRID[None, :]      # (N, Q)

    cd_log = _lognormal_logpdf_safe(
        T_grid - bpt, mu_log[:, None], sigma_log[:, None]
    )                                                                   # (N, Q)
    cd = jnp.exp(cd_log)

    s = tau_obs[:, None] / T_grid                                       # (N, Q)
    s_clip = jnp.clip(s, 0.0, 1.0)
    surv = jnp.exp(-alpha[:, None] * s_clip)                            # (N, Q)
    integrand = cd * alpha[:, None] * surv / T_grid                     # (N, Q)

    dx = width / (_QUAD_POINTS - 1)                                     # (N,)
    integral = jnp.sum(integrand * _SIMPSON_W[None, :], axis=1) * dx / 3.0
    valid = (T_min < T_max) & (integral > 0.0)
    return jnp.where(valid, jnp.log(jnp.where(integral > 0, integral, 1.0)), -jnp.inf)


def log_likelihood(theta, time_ms_arr, is_died_arr):
    """Vectorized over all attempts.

    time_ms_arr: shape (N,) observed time in ms.
    is_died_arr: shape (N,) boolean (or 0/1) -- True if died, False if survived.
    """
    N = time_ms_arr.shape[0]
    n_arr = jnp.arange(1, N + 1, dtype=jnp.float64)
    log_curves = learning_curve_at_n(theta, n_arr)                      # (N, 3)

    bpt = jnp.exp(theta[IDX_LOG_BPT])
    sf  = jnp.exp(log_curves[:, 0])
    ssp = jnp.exp(log_curves[:, 1])
    alpha = jnp.exp(log_curves[:, 2])

    mean_slop = sf * bpt                                                # (N,)
    sigma_log_sq = jnp.log(1.0 + ssp ** 2)
    mu_log = jnp.log(mean_slop) - 0.5 * sigma_log_sq                    # (N,)
    sigma_log = jnp.sqrt(sigma_log_sq)                                  # (N,)

    # Survived: log_pdf(T - bpt | LN(mu, sigma)) + log(surv_prob(1))
    surv_log = (
        _lognormal_logpdf_safe(time_ms_arr - bpt, mu_log, sigma_log)
        - alpha   # log(exp(-alpha)) = -alpha
    )

    # Died: log of integrated likelihood (Simpson's rule per attempt).
    tau_obs = time_ms_arr - config.RESPAWN_MS
    died_log = _log_lik_died_vec(tau_obs, bpt, mu_log, sigma_log, alpha)

    per_attempt = jnp.where(is_died_arr, died_log, surv_log)
    total = jnp.sum(per_attempt)
    # If any per_attempt is -inf, total goes -inf; that's correct behavior.
    return total


def log_prior(theta,
              halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
              halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
              halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None):
    """Joint prior. See learning_model_v07.log_prior for rationale on
    halflife_sigma(A) tightening when |A| is small.

    For each of {sf, ssp, alpha}, halflife_<x>_pop_{mean,sigma} are optional
    empirical-Bayes pool overrides; None = use module defaults.
    """
    hl_sf_mean   = PRIOR_LOG_HALFLIFE_SF_MEAN    if halflife_sf_pop_mean    is None else halflife_sf_pop_mean
    hl_sf_sigma  = HALFLIFE_SIGMA_MAX            if halflife_sf_pop_sigma   is None else halflife_sf_pop_sigma
    hl_ssp_mean  = PRIOR_LOG_HALFLIFE_SSP_MEAN   if halflife_ssp_pop_mean   is None else halflife_ssp_pop_mean
    hl_ssp_sigma = HALFLIFE_SIGMA_MAX            if halflife_ssp_pop_sigma  is None else halflife_ssp_pop_sigma
    hl_a_mean    = PRIOR_LOG_HALFLIFE_ALPHA_MEAN if halflife_alpha_pop_mean is None else halflife_alpha_pop_mean
    hl_a_sigma   = HALFLIFE_SIGMA_MAX            if halflife_alpha_pop_sigma is None else halflife_alpha_pop_sigma

    asymp = theta[:N_STATIC_ASYMP]
    ones = jnp.array([theta[IDX_LOG_SF_1], theta[IDX_LOG_SSP_1], theta[IDX_LOG_ALPHA_1]])
    A = ones - jnp.array([theta[IDX_LOG_SF_INF],
                          theta[IDX_LOG_SSP_INF],
                          theta[IDX_LOG_ALPHA_INF]])

    lp_asymp = jnp.sum(jnorm.logpdf(asymp, _PRIOR_ASYMP_MEAN, _PRIOR_ASYMP_SD))
    lp_one   = jnp.sum(jnorm.logpdf(ones,  _PRIOR_ONE_MEAN,   _PRIOR_ONE_SD))

    lp_hl_sf  = jnorm.logpdf(theta[IDX_LOG_HALFLIFE_SF],    hl_sf_mean,  halflife_sigma(A[0], sigma_max=hl_sf_sigma))
    lp_hl_ssp = jnorm.logpdf(theta[IDX_LOG_HALFLIFE_SSP],   hl_ssp_mean, halflife_sigma(A[1], sigma_max=hl_ssp_sigma))
    lp_hl_a   = jnorm.logpdf(theta[IDX_LOG_HALFLIFE_ALPHA], hl_a_mean,   halflife_sigma(A[2], sigma_max=hl_a_sigma))

    return lp_asymp + lp_one + lp_hl_sf + lp_hl_ssp + lp_hl_a


def log_posterior(theta, time_ms_arr, is_died_arr,
                  halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
                  halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
                  halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None):
    return (log_prior(theta,
                      halflife_sf_pop_mean=halflife_sf_pop_mean,
                      halflife_sf_pop_sigma=halflife_sf_pop_sigma,
                      halflife_ssp_pop_mean=halflife_ssp_pop_mean,
                      halflife_ssp_pop_sigma=halflife_ssp_pop_sigma,
                      halflife_alpha_pop_mean=halflife_alpha_pop_mean,
                      halflife_alpha_pop_sigma=halflife_alpha_pop_sigma)
            + log_likelihood(theta, time_ms_arr, is_died_arr))


def initial_theta():
    """All latents at prior means / medians. Same as the numpy version."""
    out = jnp.array([
        PRIOR_LOG_BPT_MEAN,
        PRIOR_LOG_SF_INF_MEAN,
        PRIOR_LOG_SSP_INF_MEAN,
        PRIOR_LOG_ALPHA_INF_MEAN,
        PRIOR_LOG_SF_1_MEAN,
        PRIOR_LOG_SSP_1_MEAN,
        PRIOR_LOG_ALPHA_1_MEAN,
        PRIOR_LOG_HALFLIFE_SF_MEAN,
        PRIOR_LOG_HALFLIFE_SSP_MEAN,
        PRIOR_LOG_HALFLIFE_ALPHA_MEAN,
    ])
    return out


def data_to_arrays(data):
    """Convert list of ('survived'|'died', time_ms) tuples to (time_ms, is_died) arrays."""
    import numpy as _np
    times = _np.asarray([t for _, t in data], dtype=_np.float64)
    died = _np.asarray([o == 'died' for o, _ in data], dtype=bool)
    return jnp.asarray(times), jnp.asarray(died)


# ---------------------------------------------------------------------------
# Padded / masked variant for streaming fits.
#
# Problem: JAX JIT-compiles per unique input SHAPE. In a streaming loop where
# the attempt count grows one at a time, every new N triggers a recompile,
# and compilation dominates per-call latency.
#
# Fix: pad input arrays to a bucketed max size and pass `valid_n`. The masked
# log_likelihood ignores padding. One JIT per bucket size; subsequent calls
# within the same bucket are cache hits.
# ---------------------------------------------------------------------------


def log_likelihood_padded(theta, time_ms_padded, is_died_padded, valid_n):
    """Like log_likelihood, but evaluates only the first valid_n entries.

    time_ms_padded, is_died_padded: shape (N_max,). valid_n: scalar int <= N_max.
    Padded slots can be any value -- the mask zeros their contribution.
    """
    N_max = time_ms_padded.shape[0]
    n_arr = jnp.arange(1, N_max + 1, dtype=jnp.float64)
    mask = n_arr <= valid_n
    log_curves = learning_curve_at_n(theta, n_arr)                      # (N_max, 3)

    bpt = jnp.exp(theta[IDX_LOG_BPT])
    sf  = jnp.exp(log_curves[:, 0])
    ssp = jnp.exp(log_curves[:, 1])
    alpha = jnp.exp(log_curves[:, 2])

    mean_slop = sf * bpt
    sigma_log_sq = jnp.log(1.0 + ssp ** 2)
    mu_log = jnp.log(mean_slop) - 0.5 * sigma_log_sq
    sigma_log = jnp.sqrt(sigma_log_sq)

    surv_log = (
        _lognormal_logpdf_safe(time_ms_padded - bpt, mu_log, sigma_log)
        - alpha
    )

    tau_obs = time_ms_padded - config.RESPAWN_MS
    died_log = _log_lik_died_vec(tau_obs, bpt, mu_log, sigma_log, alpha)

    per_attempt = jnp.where(is_died_padded, died_log, surv_log)
    per_attempt = jnp.where(mask, per_attempt, 0.0)
    return jnp.sum(per_attempt)


def log_posterior_padded(theta, time_ms_padded, is_died_padded, valid_n,
                         halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
                         halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
                         halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None):
    return (log_prior(theta,
                      halflife_sf_pop_mean=halflife_sf_pop_mean,
                      halflife_sf_pop_sigma=halflife_sf_pop_sigma,
                      halflife_ssp_pop_mean=halflife_ssp_pop_mean,
                      halflife_ssp_pop_sigma=halflife_ssp_pop_sigma,
                      halflife_alpha_pop_mean=halflife_alpha_pop_mean,
                      halflife_alpha_pop_sigma=halflife_alpha_pop_sigma)
            + log_likelihood_padded(theta, time_ms_padded, is_died_padded, valid_n))


def bucket_size(n, base=64):
    """Round n up to the next multiple of `base`. Keeps the JIT cache small."""
    return ((int(n) - 1) // base + 1) * base


def pad_to(time_ms, is_died, n_max):
    """Pad to length n_max. Padding values are inert (mask zeros them)."""
    import numpy as _np
    n = len(time_ms)
    if n_max < n:
        raise ValueError(f"n_max={n_max} < n={n}")
    if n_max == n:
        return jnp.asarray(time_ms), jnp.asarray(is_died), n
    times = _np.concatenate([_np.asarray(time_ms), _np.full(n_max - n, 30000.0)])
    died  = _np.concatenate([_np.asarray(is_died), _np.zeros(n_max - n, dtype=bool)])
    return jnp.asarray(times), jnp.asarray(died), n
