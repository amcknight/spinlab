"""Plan V07 reparameterization of the learning-curve model.

Replaces (theta_inf, Delta, tau) with (theta_inf, theta_1, halflife). theta_1
is the value on the *first observed attempt* (n=1), not a fictitious n=0
extrapolation. halflife is the number of attempts needed to close half of the
remaining gap between theta_1 and the asymptote -- a more intuitive timescale
than the 1/e-time (tau) the v0.6 model used.

    log theta(n) = log theta_inf + (log theta_1 - log theta_inf) * 2^(-(n-1)/halflife)

At n=1:           log theta(1) = log theta_inf + (log theta_1 - log theta_inf) * 1 = log theta_1.
At n=1+halflife:  the remaining gap is exactly half of (log theta_1 - log theta_inf).
As n -> inf:      log theta(n) -> log theta_inf.

The curve form is equivalent to the 1/e-time form via halflife = tau * ln(2),
so any tau-parameterized truth from v0.6 produces byte-identical synth at the
same seed -- this is purely a rename + base-change.

theta layout (length 10):
    theta[0] = log(bpt)              -- static
    theta[1] = log(sf_inf)           -- asymptote
    theta[2] = log(ssp_inf)          -- asymptote
    theta[3] = log(alpha_inf)        -- asymptote (single haz1 param)
    theta[4] = log(sf_1)             -- value at first attempt
    theta[5] = log(ssp_1)
    theta[6] = log(alpha_1)
    theta[7] = log(halflife_sf)      -- attempts to close half the gap
    theta[8] = log(halflife_ssp)
    theta[9] = log(halflife_alpha)

Prior tightening from v0.6: sigma=0.5 (between-segment factor ~1.6x).
Pre-structured to promote to per-segment hyperpriors in Plan HYPER.

Joint prior on (A, halflife):
    A = log(theta_1) - log(theta_inf)   (the learning amplitude)

  Independent priors on A and halflife leave halflife unidentified when |A| is
  small -- if the curve doesn't actually decay, the data carries zero info
  about how fast it would decay. The MAP then wanders along the (A, halflife)
  ridge somewhat arbitrarily.

  We address this with a joint prior: halflife's prior SIGMA tightens when |A|
  is small, so when the data is silent on halflife, the posterior collapses to
  the prior median rather than wandering. See halflife_sigma() for the
  smoothing function.
"""
import numpy as np
from scipy.stats import norm

import config
import model
from hazard import PiecewiseConstantHazard


N_STATIC_ASYMP = 4    # log(bpt) + 3 asymptotes
N_LEARNING = 3        # sf, ssp, alpha all learn
N_PARAMS = N_STATIC_ASYMP + 2 * N_LEARNING   # 10


IDX_LOG_BPT            = 0
IDX_LOG_SF_INF         = 1
IDX_LOG_SSP_INF        = 2
IDX_LOG_ALPHA_INF      = 3
IDX_LOG_SF_1           = 4
IDX_LOG_SSP_1          = 5
IDX_LOG_ALPHA_1        = 6
IDX_LOG_HALFLIFE_SF    = 7
IDX_LOG_HALFLIFE_SSP   = 8
IDX_LOG_HALFLIFE_ALPHA = 9


# ---------------------------------------------------------------------------
# Priors.  Tightened from v0.6; sigma=0.5 ~ between-segment factor 1.6x.
# Halflife medians are picked in natural attempts. Equivalent v0.6 tau values
# would be halflife / ln(2), i.e. 28 attempts <-> tau 40, 14 attempts <-> tau 20.
# ---------------------------------------------------------------------------
PRIOR_LOG_BPT_MEAN,            PRIOR_LOG_BPT_SD            = np.log(25000.0), 0.5
PRIOR_LOG_SF_INF_MEAN,         PRIOR_LOG_SF_INF_SD         = np.log(0.05),    0.5
PRIOR_LOG_SSP_INF_MEAN,        PRIOR_LOG_SSP_INF_SD        = np.log(0.2),     0.5
PRIOR_LOG_ALPHA_INF_MEAN,      PRIOR_LOG_ALPHA_INF_SD      = np.log(0.3),     0.5
PRIOR_LOG_SF_1_MEAN,           PRIOR_LOG_SF_1_SD           = np.log(0.2),     0.5
PRIOR_LOG_SSP_1_MEAN,          PRIOR_LOG_SSP_1_SD          = np.log(0.4),     0.5
PRIOR_LOG_ALPHA_1_MEAN,        PRIOR_LOG_ALPHA_1_SD        = np.log(1.0),     0.5
PRIOR_LOG_HALFLIFE_SF_MEAN                                 = np.log(28.0)
PRIOR_LOG_HALFLIFE_SSP_MEAN                                = np.log(28.0)
PRIOR_LOG_HALFLIFE_ALPHA_MEAN                              = np.log(14.0)
# Note: the synth TRUTH_DEFAULTS use ssp_halflife=55, not 28. Mismatch is
# intentional: the data is fake, the broad prior + EB pool should learn the
# offset from per-segment data. Don't move the prior to match the synth.

# Joint prior on (A, halflife): sigma_halflife is a function of |A|.
# ---------------------------------------------------------------------------
# This is a META-MODELING CHOICE, not a structural model claim. The generative
# story says nothing about A and halflife being causally linked. We're encoding
# our handling of weak identification: "when the data can't pin down halflife
# (which happens when |A| is small), default to the prior median confidently
# rather than letting MAP wander."  In Bayesian terms, P(halflife | A) tightens
# as |A| -> 0, so the conditional posterior matches the prior in that regime.
#
# tanh(|A|) smoothly maps [0, inf) -> [0, 1):
#   |A| = 0   -> sigma = HALFLIFE_SIGMA_MIN   (very tight; halflife collapses to prior)
#   |A| = 1   -> sigma ~ 0.39                 (mostly broad; data dominates)
#   |A| -> inf-> sigma = HALFLIFE_SIGMA_MAX   (full broad; full data trust)
# Smooth, differentiable, asymptotic -- no NM-hostile discontinuities.
HALFLIFE_SIGMA_MIN = 0.05
HALFLIFE_SIGMA_MAX = 0.50


_SMOOTH_ABS_EPS = 1e-2


def _smooth_abs(A, eps=_SMOOTH_ABS_EPS):
    """Twice-differentiable approximation to |A|. Equals 0 at A=0 exactly
    (so halflife_sigma(0) == sigma_min on the nose) and approaches |A| for
    |A| >> eps. The eps-scale smoothing kills the JAX-autodiff Hessian bug
    that |A| produces at A=0: the MAP can land at A=0 (no-learning regime),
    and there autodiff'd d²/dA² |A| is wrong (formally a Dirac). With this
    smoothing the second derivative is 1/eps at A=0 — large but finite,
    matching the true cusp-limit prior."""
    return np.sqrt(A * A + eps * eps) - eps


def halflife_sigma(A, sigma_max=HALFLIFE_SIGMA_MAX, sigma_min=HALFLIFE_SIGMA_MIN):
    """Conditional prior SD on log(halflife) given amplitude A.

    A may be scalar or array. Returns same shape as A.

    sigma_max defaults to the module constant but can be overridden -- e.g. by
    an empirical-Bayes pool that has narrowed the across-segment SD on halflife.
    Smaller sigma_max means a tighter prior at large |A| too, which is what
    pooling buys you.
    """
    return sigma_min + (sigma_max - sigma_min) * np.tanh(_smooth_abs(A))


def haz1():
    """Single-bin piecewise-constant hazard. v1 commitment, see learning_curves.md."""
    return PiecewiseConstantHazard(K=1, bin_edges=[0.0, 1.0])


def learning_curve(log_theta_inf, log_theta_1, log_halflife, n):
    """log theta(n) = log theta_inf + (log theta_1 - log theta_inf) * 2^(-(n-1)/halflife).

    n is 1-indexed (matching how attempts are recorded). At n=1 the curve
    evaluates exactly to log_theta_1. At n=1+halflife the remaining gap is
    halved. Vectorized over n.
    """
    halflife = np.exp(log_halflife)
    n_arr = np.asarray(n, dtype=float)
    return log_theta_inf + (log_theta_1 - log_theta_inf) * 2.0 ** (-(n_arr - 1.0) / halflife)


def static_theta_at_n(theta, n):
    """Effective static-model theta at attempt n (1-indexed).

    Returns [log_bpt, log_sf_n, log_ssp_n, log_alpha_n] in the layout
    model.unpack expects for haz1.
    """
    log_inf = theta[IDX_LOG_SF_INF:IDX_LOG_ALPHA_INF + 1]
    log_1   = theta[IDX_LOG_SF_1:IDX_LOG_ALPHA_1 + 1]
    log_hl  = theta[IDX_LOG_HALFLIFE_SF:IDX_LOG_HALFLIFE_ALPHA + 1]
    halflife = np.exp(log_hl)
    decay = 2.0 ** (-(float(n) - 1.0) / halflife)
    log_at_n = log_inf + (log_1 - log_inf) * decay
    out = np.empty(N_STATIC_ASYMP)
    out[0] = theta[IDX_LOG_BPT]
    out[1:] = log_at_n
    return out


def log_likelihood(theta, data, hazard):
    """Sum of per-attempt log-likelihoods, substituting theta(n) at each attempt."""
    total = 0.0
    for n, (outcome, time_ms) in enumerate(data, start=1):
        theta_n = static_theta_at_n(theta, n)
        bpt, sf, ssp, haz_params = model.unpack(theta_n, hazard)
        mu_log, sigma_log = model.lognormal_params(sf, ssp, bpt)
        if outcome == 'survived':
            total += model.log_lik_survived(time_ms, bpt, mu_log, sigma_log,
                                            hazard, haz_params)
        else:
            tau_obs = time_ms - config.RESPAWN_MS
            total += model.log_lik_died(tau_obs, bpt, mu_log, sigma_log,
                                        hazard, haz_params)
        if not np.isfinite(total):
            return -np.inf
    return total


def log_prior(theta, hazard,
              halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
              halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
              halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None):
    """V07 owns all priors -- does not delegate to hazard.log_prior.

    Halflife priors are joint with their respective A = log(theta_1) - log(theta_inf).
    See halflife_sigma() and the module docstring for rationale.

    Pool override (empirical Bayes):
        For each of {sf, ssp, alpha}, halflife_<x>_pop_mean and
        halflife_<x>_pop_sigma override the per-segment prior on
        log(halflife_<x>) when both are provided. These come from a
        multi-segment EB fit. Pooling all three is symmetric machinery.
    """
    hl_sf_mean   = (halflife_sf_pop_mean    if halflife_sf_pop_mean    is not None else PRIOR_LOG_HALFLIFE_SF_MEAN)
    hl_sf_sigma  = (halflife_sf_pop_sigma   if halflife_sf_pop_sigma   is not None else HALFLIFE_SIGMA_MAX)
    hl_ssp_mean  = (halflife_ssp_pop_mean   if halflife_ssp_pop_mean   is not None else PRIOR_LOG_HALFLIFE_SSP_MEAN)
    hl_ssp_sigma = (halflife_ssp_pop_sigma  if halflife_ssp_pop_sigma  is not None else HALFLIFE_SIGMA_MAX)
    hl_a_mean    = (halflife_alpha_pop_mean if halflife_alpha_pop_mean is not None else PRIOR_LOG_HALFLIFE_ALPHA_MEAN)
    hl_a_sigma   = (halflife_alpha_pop_sigma if halflife_alpha_pop_sigma is not None else HALFLIFE_SIGMA_MAX)

    lp = 0.0
    lp += norm.logpdf(theta[IDX_LOG_BPT],        PRIOR_LOG_BPT_MEAN,       PRIOR_LOG_BPT_SD)
    lp += norm.logpdf(theta[IDX_LOG_SF_INF],     PRIOR_LOG_SF_INF_MEAN,    PRIOR_LOG_SF_INF_SD)
    lp += norm.logpdf(theta[IDX_LOG_SSP_INF],    PRIOR_LOG_SSP_INF_MEAN,   PRIOR_LOG_SSP_INF_SD)
    lp += norm.logpdf(theta[IDX_LOG_ALPHA_INF],  PRIOR_LOG_ALPHA_INF_MEAN, PRIOR_LOG_ALPHA_INF_SD)
    lp += norm.logpdf(theta[IDX_LOG_SF_1],       PRIOR_LOG_SF_1_MEAN,      PRIOR_LOG_SF_1_SD)
    lp += norm.logpdf(theta[IDX_LOG_SSP_1],      PRIOR_LOG_SSP_1_MEAN,     PRIOR_LOG_SSP_1_SD)
    lp += norm.logpdf(theta[IDX_LOG_ALPHA_1],    PRIOR_LOG_ALPHA_1_MEAN,   PRIOR_LOG_ALPHA_1_SD)

    A_sf  = theta[IDX_LOG_SF_1]    - theta[IDX_LOG_SF_INF]
    A_ssp = theta[IDX_LOG_SSP_1]   - theta[IDX_LOG_SSP_INF]
    A_a   = theta[IDX_LOG_ALPHA_1] - theta[IDX_LOG_ALPHA_INF]
    lp += norm.logpdf(theta[IDX_LOG_HALFLIFE_SF],    hl_sf_mean,  halflife_sigma(A_sf,  sigma_max=hl_sf_sigma))
    lp += norm.logpdf(theta[IDX_LOG_HALFLIFE_SSP],   hl_ssp_mean, halflife_sigma(A_ssp, sigma_max=hl_ssp_sigma))
    lp += norm.logpdf(theta[IDX_LOG_HALFLIFE_ALPHA], hl_a_mean,   halflife_sigma(A_a,   sigma_max=hl_a_sigma))
    return float(lp)


def log_posterior(theta, data, hazard,
                  halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
                  halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
                  halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None):
    lp = log_prior(theta, hazard,
                   halflife_sf_pop_mean=halflife_sf_pop_mean,
                   halflife_sf_pop_sigma=halflife_sf_pop_sigma,
                   halflife_ssp_pop_mean=halflife_ssp_pop_mean,
                   halflife_ssp_pop_sigma=halflife_ssp_pop_sigma,
                   halflife_alpha_pop_mean=halflife_alpha_pop_mean,
                   halflife_alpha_pop_sigma=halflife_alpha_pop_sigma)
    if not np.isfinite(lp):
        return -np.inf
    ll = log_likelihood(theta, data, hazard)
    return lp + ll


def sample_prior(n_samples, rng=None):
    """Draw n_samples from the joint prior. Returns shape (n_samples, N_PARAMS).

    Asymptotes and theta_1 latents are independent. Halflife latents are sampled
    conditional on their A = log(theta_1) - log(theta_inf), with sigma per
    halflife_sigma(). Use this for prior-predictive checks.
    """
    if rng is None:
        rng = np.random.default_rng()
    out = np.empty((n_samples, N_PARAMS))
    out[:, IDX_LOG_BPT]       = rng.normal(PRIOR_LOG_BPT_MEAN,       PRIOR_LOG_BPT_SD,       n_samples)
    out[:, IDX_LOG_SF_INF]    = rng.normal(PRIOR_LOG_SF_INF_MEAN,    PRIOR_LOG_SF_INF_SD,    n_samples)
    out[:, IDX_LOG_SSP_INF]   = rng.normal(PRIOR_LOG_SSP_INF_MEAN,   PRIOR_LOG_SSP_INF_SD,   n_samples)
    out[:, IDX_LOG_ALPHA_INF] = rng.normal(PRIOR_LOG_ALPHA_INF_MEAN, PRIOR_LOG_ALPHA_INF_SD, n_samples)
    out[:, IDX_LOG_SF_1]      = rng.normal(PRIOR_LOG_SF_1_MEAN,      PRIOR_LOG_SF_1_SD,      n_samples)
    out[:, IDX_LOG_SSP_1]     = rng.normal(PRIOR_LOG_SSP_1_MEAN,     PRIOR_LOG_SSP_1_SD,     n_samples)
    out[:, IDX_LOG_ALPHA_1]   = rng.normal(PRIOR_LOG_ALPHA_1_MEAN,   PRIOR_LOG_ALPHA_1_SD,   n_samples)

    A_sf  = out[:, IDX_LOG_SF_1]    - out[:, IDX_LOG_SF_INF]
    A_ssp = out[:, IDX_LOG_SSP_1]   - out[:, IDX_LOG_SSP_INF]
    A_a   = out[:, IDX_LOG_ALPHA_1] - out[:, IDX_LOG_ALPHA_INF]
    out[:, IDX_LOG_HALFLIFE_SF]    = rng.normal(PRIOR_LOG_HALFLIFE_SF_MEAN,    halflife_sigma(A_sf))
    out[:, IDX_LOG_HALFLIFE_SSP]   = rng.normal(PRIOR_LOG_HALFLIFE_SSP_MEAN,   halflife_sigma(A_ssp))
    out[:, IDX_LOG_HALFLIFE_ALPHA] = rng.normal(PRIOR_LOG_HALFLIFE_ALPHA_MEAN, halflife_sigma(A_a))
    return out


def initial_theta(hazard):
    """All latents at their prior means (halflives at their MEDIAN, not joint
    sigma -- joint sigma only governs prior strength, not the prior center).

    Crucial: theta_1 starts at its OWN prior mean, NOT at theta_inf. A flat-
    curve init (theta_1 = theta_inf) is degenerate for Nelder-Mead at d=10:
    when the curve is flat, halflife drops out of the likelihood entirely, so
    3 of the 10 axes have zero gradient and the simplex has no direction to
    move on them. The prior on theta_1 (centered apart from theta_inf with
    sigma=0.5) carries the "learning happens" information; initializing there
    keeps halflife identified at the starting point.
    """
    out = np.empty(N_PARAMS)
    out[IDX_LOG_BPT]            = PRIOR_LOG_BPT_MEAN
    out[IDX_LOG_SF_INF]         = PRIOR_LOG_SF_INF_MEAN
    out[IDX_LOG_SSP_INF]        = PRIOR_LOG_SSP_INF_MEAN
    out[IDX_LOG_ALPHA_INF]      = PRIOR_LOG_ALPHA_INF_MEAN
    out[IDX_LOG_SF_1]           = PRIOR_LOG_SF_1_MEAN
    out[IDX_LOG_SSP_1]          = PRIOR_LOG_SSP_1_MEAN
    out[IDX_LOG_ALPHA_1]        = PRIOR_LOG_ALPHA_1_MEAN
    out[IDX_LOG_HALFLIFE_SF]    = PRIOR_LOG_HALFLIFE_SF_MEAN
    out[IDX_LOG_HALFLIFE_SSP]   = PRIOR_LOG_HALFLIFE_SSP_MEAN
    out[IDX_LOG_HALFLIFE_ALPHA] = PRIOR_LOG_HALFLIFE_ALPHA_MEAN
    return out


def m_clear_at_n(theta, hazard, n):
    """M_clear evaluated using theta(n). Useful for plotting M_clear evolution."""
    return model.m_clear(static_theta_at_n(theta, n), hazard)
