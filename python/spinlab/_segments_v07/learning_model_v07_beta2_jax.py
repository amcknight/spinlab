"""V07 + Beta-K=2 mixture hazard, JAX implementation.

V2-SCOPE — PARKED. v1 ships haz1 only; this module stays in the repo for
v2 iteration inside SpinLab. Do not extend in v1. See /V1_ESSENCE.md.

Extension of learning_model_v07_jax (haz1, K=1 constant hazard) to a K=2 beta
mixture hazard. 15 params total: 10 from V07 base + 5 static beta-shape.

Theta layout (length 15):
    indices  0..9 : same as learning_model_v07_jax (bpt, sf_inf, ssp_inf,
                    alpha_inf, sf_1, ssp_1, alpha_1, halflife_sf,
                    halflife_ssp, halflife_alpha)
    index   10    : logit(mu_1)        first beta component mean (in 0..1)
    index   11    : logit(mu_2)        second beta component mean
    index   12    : log(kappa_1)       first beta concentration
    index   13    : log(kappa_2)       second beta concentration
    index   14    : eta_1              softmax logit for w_1; eta_2 := 0

Survival math (smooth cumulative hazard form):

    g(s)    = w_1 · Beta_pdf(s | a_1, b_1) + w_2 · Beta_pdf(s | a_2, b_2)
    G(s)    = w_1 · I_s(a_1, b_1)         + w_2 · I_s(a_2, b_2)
    a_k     = mu_k · kappa_k,  b_k = (1 - mu_k) · kappa_k

    f(s|n)  = alpha(n) · g(s) · exp(-alpha(n) · G(s))
    F(s|n)  = 1 - exp(-alpha(n) · G(s))
    S(s|n)  = exp(-alpha(n) · G(s))
    h(s|n)  = alpha(n) · g(s)

This is the "smooth cumulative hazard" formulation: alpha(n) means total
hazard mass over an attempt (same as haz1); g(s) just redistributes that
mass over [0, 1]. Reduces exactly to haz1 when g(s) = 1.

Alternative considered: the "scaled CDF" form of beta_mixture_decisions.md
where alpha = P(die) directly. Rejected for this comparison work because it
makes alpha mean a different thing across models, confounding any apples-to-
apples readings on alpha-learning recovery. The scaled-CDF form remains the
spec lock-in for downstream work.

Label-switching: w_1·Beta(mu_1) + w_2·Beta(mu_2) is symmetric under
(1)<->(2) swap. The 2! posterior modes are not addressed in the
parameterization itself; warm-init breaks symmetry (mu_1 ~ 0.3, mu_2 ~ 0.7)
so MAP picks one side consistently. Posterior summaries (Laplace samples,
NUTS) need an order-constraint or label-swap canonicalization to be
meaningful -- documented caveat.
"""
import jax
import jax.numpy as jnp
from jax.scipy.stats import norm as jnorm
from jax.scipy.stats import beta as jbeta

import config


# Fine grid for G(s) cumulative-trapezoidal integration. Used in place of
# jax.scipy.special.betainc, which JAX cannot differentiate w.r.t. its
# shape parameters (a, b). We integrate the differentiable jbeta.pdf instead.
# n=257 gives ds ~= 0.004, which is ~30x finer than the typical beta peak
# half-width at kappa=15.
_G_GRID_N = 257
_G_GRID_S = jnp.linspace(1e-9, 1.0 - 1e-9, _G_GRID_N)
_G_GRID_DS = (_G_GRID_S[1] - _G_GRID_S[0])


# Reuse layout + constants from V07 haz1.
from learning_model_v07 import (
    N_STATIC_ASYMP, N_LEARNING,
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

# Beta-shape param indices (extending V07).
IDX_LOGIT_MU_1   = 10
IDX_LOGIT_MU_2   = 11
IDX_LOG_KAPPA_1  = 12
IDX_LOG_KAPPA_2  = 13
IDX_ETA_1        = 14

N_BETA_SHAPE = 5
N_PARAMS_BETA2 = 10 + N_BETA_SHAPE   # 15

# Shape-param priors. Mirror BetaMixtureHazard in hazard.py.
PRIOR_LOGIT_MU_MEAN   = 0.0          # mu_k median 0.5
PRIOR_LOGIT_MU_SD     = 1.5          # 68% in (~0.18, ~0.82)
PRIOR_LOG_KAPPA_MEAN  = jnp.log(15.0)
PRIOR_LOG_KAPPA_SD    = 1.0
PRIOR_ETA_MEAN        = 0.0
PRIOR_ETA_SD          = 1.5


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
    """sqrt(A^2 + eps^2) - eps. See learning_model_v07_jax._smooth_abs."""
    return jnp.sqrt(A * A + eps * eps) - eps


def halflife_sigma(A, sigma_max=HALFLIFE_SIGMA_MAX, sigma_min=HALFLIFE_SIGMA_MIN):
    """Joint-prior sigma on log(halflife) given amplitude A. JAX version.

    Same as learning_model_v07_jax.halflife_sigma.
    """
    return sigma_min + (sigma_max - sigma_min) * jnp.tanh(_smooth_abs(A))


def learning_curve_at_n(theta, n):
    """Returns log_sf(n), log_ssp(n), log_alpha(n) at attempt n.

    Shape: (..., 3). Indexes only into the first 10 entries of theta.
    """
    log_inf = theta[IDX_LOG_SF_INF:IDX_LOG_ALPHA_INF + 1]
    log_1   = theta[IDX_LOG_SF_1:IDX_LOG_ALPHA_1 + 1]
    log_hl  = theta[IDX_LOG_HALFLIFE_SF:IDX_LOG_HALFLIFE_ALPHA + 1]
    halflife = jnp.exp(log_hl)
    n_arr = jnp.asarray(n, dtype=jnp.float64)
    decay = jnp.power(2.0, -(n_arr[..., None] - 1.0) / halflife)
    return log_inf + (log_1 - log_inf) * decay


def _sigmoid(x):
    return 0.5 * (1.0 + jnp.tanh(0.5 * x))


def _unpack_beta_shape(theta):
    """theta[10:15] -> (mu, kappa, w) each shape (2,)."""
    mu_1 = _sigmoid(theta[IDX_LOGIT_MU_1])
    mu_2 = _sigmoid(theta[IDX_LOGIT_MU_2])
    kappa_1 = jnp.exp(theta[IDX_LOG_KAPPA_1])
    kappa_2 = jnp.exp(theta[IDX_LOG_KAPPA_2])
    eta = jnp.array([theta[IDX_ETA_1], 0.0])
    eta = eta - eta.max()
    ex = jnp.exp(eta)
    w = ex / ex.sum()
    mu = jnp.array([mu_1, mu_2])
    kappa = jnp.array([kappa_1, kappa_2])
    return mu, kappa, w


def _build_gG_lookup(mu, kappa, w):
    """Precompute g(s) and G(s) on _G_GRID_S for the current shape params.

    Uses cumulative trapezoidal integration of jbeta.pdf (fully differentiable
    in JAX, unlike betainc which has no grads w.r.t. a, b). Returns:

        g_fine[i] = g(_G_GRID_S[i])           shape (_G_GRID_N,)
        G_fine[i] = integral_0^{_G_GRID_S[i]} g(t) dt    shape (_G_GRID_N,)

    G_fine is normalized so G_fine[-1] = 1, absorbing the small clip/grid
    error so the survival math S(1) = exp(-alpha) is exact.
    """
    a = mu * kappa                # shape (2,)
    b = (1.0 - mu) * kappa        # shape (2,)
    pdf_k = jbeta.pdf(_G_GRID_S[:, None], a, b)   # (N_grid, 2)
    g_fine = jnp.sum(w * pdf_k, axis=-1)           # (N_grid,)

    # Cumulative trapezoidal rule. trap[0]=0, trap[i]=sum_{j<i} 0.5*(g[j]+g[j+1])*ds.
    increments = 0.5 * (g_fine[:-1] + g_fine[1:]) * _G_GRID_DS
    cum_trap = jnp.concatenate([jnp.zeros(1), jnp.cumsum(increments)])

    # Normalize so G_fine[-1] = 1 exactly.
    G_at_1 = cum_trap[-1]
    G_fine = cum_trap / G_at_1
    g_fine_norm = g_fine / G_at_1
    return g_fine_norm, G_fine


def _eval_gG(s, g_fine, G_fine):
    """Look up g(s) and G(s) by linear interpolation on _G_GRID_S."""
    s_c = jnp.clip(s, 0.0, 1.0)
    g = jnp.interp(s_c, _G_GRID_S, g_fine)
    G = jnp.interp(s_c, _G_GRID_S, G_fine)
    return g, G


def _lognormal_logpdf_safe(x, mu, sigma):
    safe_x = jnp.where(x > 0, x, 1.0)
    log_pdf = (
        -jnp.log(safe_x) - jnp.log(sigma) - 0.5 * jnp.log(2.0 * jnp.pi)
        - 0.5 * ((jnp.log(safe_x) - mu) / sigma) ** 2
    )
    return jnp.where(x > 0, log_pdf, -jnp.inf)


def _simpson_weights(n_points):
    if n_points % 2 == 0:
        raise ValueError("Simpson's rule needs an odd number of points")
    w = jnp.ones(n_points)
    w = w.at[1::2].set(4.0)
    w = w.at[2:-1:2].set(2.0)
    return w


_QUAD_POINTS = config.QUAD_POINTS + 1 if config.QUAD_POINTS % 2 == 0 else config.QUAD_POINTS
_SIMPSON_W = _simpson_weights(_QUAD_POINTS)
_UNIT_GRID = jnp.linspace(0.0, 1.0, _QUAD_POINTS)


def _log_lik_died_vec(tau_obs, bpt, mu_log, sigma_log, alpha,
                       g_fine, G_fine):
    """Vectorized log P(died at tau_obs).

    Integrand: clean_dist(T) * alpha * g(tau/T) * exp(-alpha * G(tau/T)) / T

    Caller pre-builds the (g_fine, G_fine) lookup tables on _G_GRID_S so the
    cumulative-trapezoid cost is amortized over all N attempts.
    """
    mean_slop = jnp.exp(mu_log + 0.5 * sigma_log ** 2)
    std_slop = mean_slop * jnp.sqrt(jnp.exp(sigma_log ** 2) - 1.0)
    T_max = bpt + mean_slop + config.QUAD_UPPER_SIGMAS * std_slop
    T_min = jnp.maximum(bpt, tau_obs)
    width = T_max - T_min
    T_grid = T_min[:, None] + width[:, None] * _UNIT_GRID[None, :]    # (N, Q)

    cd_log = _lognormal_logpdf_safe(
        T_grid - bpt, mu_log[:, None], sigma_log[:, None]
    )
    cd = jnp.exp(cd_log)

    s = tau_obs[:, None] / T_grid
    s_flat = jnp.clip(s, 0.0, 1.0).reshape(-1)
    g_flat = jnp.interp(s_flat, _G_GRID_S, g_fine)
    G_flat = jnp.interp(s_flat, _G_GRID_S, G_fine)
    g_vals = g_flat.reshape(s.shape)
    G_vals = G_flat.reshape(s.shape)

    surv = jnp.exp(-alpha[:, None] * G_vals)
    integrand = cd * alpha[:, None] * g_vals * surv / T_grid

    dx = width / (_QUAD_POINTS - 1)
    integral = jnp.sum(integrand * _SIMPSON_W[None, :], axis=1) * dx / 3.0
    valid = (T_min < T_max) & (integral > 0.0)
    return jnp.where(valid, jnp.log(jnp.where(integral > 0, integral, 1.0)), -jnp.inf)


def log_likelihood(theta, time_ms_arr, is_died_arr):
    """Vectorized over all attempts."""
    N = time_ms_arr.shape[0]
    n_arr = jnp.arange(1, N + 1, dtype=jnp.float64)
    log_curves = learning_curve_at_n(theta, n_arr)                   # (N, 3)

    bpt = jnp.exp(theta[IDX_LOG_BPT])
    sf  = jnp.exp(log_curves[:, 0])
    ssp = jnp.exp(log_curves[:, 1])
    alpha = jnp.exp(log_curves[:, 2])

    mean_slop = sf * bpt
    sigma_log_sq = jnp.log(1.0 + ssp ** 2)
    mu_log = jnp.log(mean_slop) - 0.5 * sigma_log_sq
    sigma_log = jnp.sqrt(sigma_log_sq)

    mu_sh, kappa_sh, w_sh = _unpack_beta_shape(theta)
    g_fine, G_fine = _build_gG_lookup(mu_sh, kappa_sh, w_sh)

    # Survived: log P(survive) = -alpha(n) * G(1) = -alpha(n) (G_fine[-1]=1).
    surv_log = (
        _lognormal_logpdf_safe(time_ms_arr - bpt, mu_log, sigma_log)
        - alpha
    )

    tau_obs = time_ms_arr - config.RESPAWN_MS
    died_log = _log_lik_died_vec(tau_obs, bpt, mu_log, sigma_log, alpha,
                                  g_fine, G_fine)

    per_attempt = jnp.where(is_died_arr, died_log, surv_log)
    return jnp.sum(per_attempt)


def log_prior(theta,
              halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
              halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
              halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None):
    """Joint prior over 15 params.

    Shape-param priors are independent Normal in unconstrained space:
        logit(mu_k) ~ N(0, 1.5)
        log(kappa_k) ~ N(log 15, 1.0)
        eta_1 ~ N(0, 1.5)

    For each of {sf, ssp, alpha}, halflife_<x>_pop_{mean,sigma} are
    optional EB pool overrides; None = use module defaults.
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

    # Beta-shape priors (5 latents).
    lp_mu1   = jnorm.logpdf(theta[IDX_LOGIT_MU_1],  PRIOR_LOGIT_MU_MEAN, PRIOR_LOGIT_MU_SD)
    lp_mu2   = jnorm.logpdf(theta[IDX_LOGIT_MU_2],  PRIOR_LOGIT_MU_MEAN, PRIOR_LOGIT_MU_SD)
    lp_kap1  = jnorm.logpdf(theta[IDX_LOG_KAPPA_1], PRIOR_LOG_KAPPA_MEAN, PRIOR_LOG_KAPPA_SD)
    lp_kap2  = jnorm.logpdf(theta[IDX_LOG_KAPPA_2], PRIOR_LOG_KAPPA_MEAN, PRIOR_LOG_KAPPA_SD)
    lp_eta1  = jnorm.logpdf(theta[IDX_ETA_1],       PRIOR_ETA_MEAN,       PRIOR_ETA_SD)

    return (lp_asymp + lp_one + lp_hl_sf + lp_hl_ssp + lp_hl_a
            + lp_mu1 + lp_mu2 + lp_kap1 + lp_kap2 + lp_eta1)


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
    """Length-15 init at prior means (modulo symmetry break on mu_1, mu_2).

    mu_1 inits at logit(0.3) and mu_2 at logit(0.7) to break label-switching
    symmetry. All other latents at their prior means/medians.
    """
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
        jnp.log(0.3 / 0.7),    # logit(0.3) ~ -0.847
        jnp.log(0.7 / 0.3),    # logit(0.7) ~ +0.847
        PRIOR_LOG_KAPPA_MEAN,
        PRIOR_LOG_KAPPA_MEAN,
        PRIOR_ETA_MEAN,
    ])
    return out


def data_to_arrays(data):
    """Convert list of ('survived'|'died', time_ms) to (time_ms, is_died) arrays."""
    import numpy as _np
    times = _np.asarray([t for _, t in data], dtype=_np.float64)
    died = _np.asarray([o == 'died' for o, _ in data], dtype=bool)
    return jnp.asarray(times), jnp.asarray(died)


# ---------------------------------------------------------------------------
# Padded variant for streaming fits. Same pattern as haz1.
# ---------------------------------------------------------------------------


def log_likelihood_padded(theta, time_ms_padded, is_died_padded, valid_n):
    N_max = time_ms_padded.shape[0]
    n_arr = jnp.arange(1, N_max + 1, dtype=jnp.float64)
    mask = n_arr <= valid_n
    log_curves = learning_curve_at_n(theta, n_arr)

    bpt = jnp.exp(theta[IDX_LOG_BPT])
    sf  = jnp.exp(log_curves[:, 0])
    ssp = jnp.exp(log_curves[:, 1])
    alpha = jnp.exp(log_curves[:, 2])

    mean_slop = sf * bpt
    sigma_log_sq = jnp.log(1.0 + ssp ** 2)
    mu_log = jnp.log(mean_slop) - 0.5 * sigma_log_sq
    sigma_log = jnp.sqrt(sigma_log_sq)

    mu_sh, kappa_sh, w_sh = _unpack_beta_shape(theta)
    g_fine, G_fine = _build_gG_lookup(mu_sh, kappa_sh, w_sh)

    surv_log = (
        _lognormal_logpdf_safe(time_ms_padded - bpt, mu_log, sigma_log)
        - alpha
    )

    tau_obs = time_ms_padded - config.RESPAWN_MS
    died_log = _log_lik_died_vec(tau_obs, bpt, mu_log, sigma_log, alpha,
                                  g_fine, G_fine)

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
    return ((int(n) - 1) // base + 1) * base


def pad_to(time_ms, is_died, n_max):
    import numpy as _np
    n = len(time_ms)
    if n_max < n:
        raise ValueError(f"n_max={n_max} < n={n}")
    if n_max == n:
        return jnp.asarray(time_ms), jnp.asarray(is_died), n
    times = _np.concatenate([_np.asarray(time_ms), _np.full(n_max - n, 30000.0)])
    died  = _np.concatenate([_np.asarray(is_died), _np.zeros(n_max - n, dtype=bool)])
    return jnp.asarray(times), jnp.asarray(died), n
