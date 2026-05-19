"""Speedrun segment model: likelihood, priors, log-posterior.

Parameters live in log-space for unconstrained optimization. The theta vector is:
    theta[0] = log(bpt)         best possible time (ms)
    theta[1] = log(slop_frac)   excess clean time as fraction of bpt
    theta[2] = log(slop_spread) std/mean of clean-time slop (CV)
    theta[3:3 + hazard.n_params] = hazard model's own log-space parameters

The hazard model is plugged in as a HazardModel instance (see hazard.py); this
module is agnostic to its form. To swap in a Beta mixture or other survival
model, implement a new HazardModel subclass and pass it in.
"""
import numpy as np
from scipy.stats import lognorm, norm
from scipy.integrate import simpson

import config


def unpack(theta, hazard):
    bpt = np.exp(theta[0])
    slop_frac = np.exp(theta[1])
    slop_spread = np.exp(theta[2])
    haz_params = hazard.unpack(theta[3:3 + hazard.n_params])
    return bpt, slop_frac, slop_spread, haz_params


def lognormal_params(slop_frac, slop_spread, bpt):
    """Match the moments of the slop term:
        E[slop]   = mean_slop = slop_frac * bpt
        Var[slop] = (slop_spread * mean_slop)^2
    For X ~ Lognormal(mu, sigma):  CV^2 = exp(sigma^2) - 1.
    """
    mean_slop = slop_frac * bpt
    sigma_log_sq = np.log(1.0 + slop_spread ** 2)
    mu_log = np.log(mean_slop) - 0.5 * sigma_log_sq
    return mu_log, np.sqrt(sigma_log_sq)


def slop_moments(mu_log, sigma_log):
    """Mean and std of the slop term (Lognormal in original units)."""
    sigma_log_sq = sigma_log ** 2
    mean = np.exp(mu_log + 0.5 * sigma_log_sq)
    std = mean * np.sqrt(np.exp(sigma_log_sq) - 1.0)
    return mean, std


def clean_dist_pdf(T, bpt, mu_log, sigma_log):
    """Density of T_clean = bpt + Lognormal(mu_log, sigma_log) at T.

    Works for both scalar and array T.
    """
    T = np.asarray(T, dtype=float)
    out = np.zeros_like(T)
    mask = T > bpt
    if np.any(mask):
        out[mask] = lognorm.pdf(T[mask] - bpt, s=sigma_log, scale=np.exp(mu_log))
    return out if out.ndim else float(out)


def log_lik_survived(T, bpt, mu_log, sigma_log, hazard, haz_params):
    """L_survived(T) = clean_dist(T) * surv_prob(1)."""
    cd = clean_dist_pdf(T, bpt, mu_log, sigma_log)
    sp = float(np.exp(-hazard.cum(1.0, haz_params)))
    if cd <= 0.0 or sp <= 0.0:
        return -np.inf
    return np.log(cd) + np.log(sp)


def log_lik_died(tau, bpt, mu_log, sigma_log, hazard, haz_params):
    """Marginal likelihood for a died attempt at in-attempt time tau.

        L_died(tau) = integral over T >= max(bpt, tau) of
                       clean_dist(T) * haz(tau/T) * surv_prob(tau/T) / T  dT

    Computed by composite Simpson's rule on a fixed grid. The upper integration
    limit is set at QUAD_UPPER_SIGMAS above bpt+mean_slop (essentially all of
    clean_dist's mass lies below this); the lower limit is max(bpt, tau) because
    T < bpt gives clean_dist == 0 and T < tau gives s > 1 (already finished).
    """
    if tau <= 0.0:
        return -np.inf
    mean_slop, std_slop = slop_moments(mu_log, sigma_log)
    T_max = bpt + mean_slop + config.QUAD_UPPER_SIGMAS * std_slop
    T_min = max(bpt, tau)
    if T_min >= T_max:
        return -np.inf

    T_grid = np.linspace(T_min, T_max, config.QUAD_POINTS)
    s_grid = tau / T_grid
    cd = clean_dist_pdf(T_grid, bpt, mu_log, sigma_log)
    h = hazard.h(s_grid, haz_params)
    sp = np.exp(-hazard.cum(s_grid, haz_params))
    integrand = cd * h * sp / T_grid

    val = simpson(integrand, x=T_grid)
    if val <= 0.0 or not np.isfinite(val):
        return -np.inf
    return float(np.log(val))


def log_likelihood(theta, data, hazard):
    bpt, slop_frac, slop_spread, haz_params = unpack(theta, hazard)
    mu_log, sigma_log = lognormal_params(slop_frac, slop_spread, bpt)
    total = 0.0
    for outcome, time_ms in data:
        if outcome == 'survived':
            total += log_lik_survived(time_ms, bpt, mu_log, sigma_log, hazard, haz_params)
        else:
            tau = time_ms - config.RESPAWN_MS
            total += log_lik_died(tau, bpt, mu_log, sigma_log, hazard, haz_params)
        if not np.isfinite(total):
            return -np.inf
    return total


def log_prior(theta, hazard):
    lp = 0.0
    lp += norm.logpdf(theta[0], config.PRIOR_LOG_BPT_MEAN, config.PRIOR_LOG_BPT_SD)
    lp += norm.logpdf(theta[1], config.PRIOR_LOG_SLOP_FRAC_MEAN, config.PRIOR_LOG_SLOP_FRAC_SD)
    lp += norm.logpdf(theta[2], config.PRIOR_LOG_SLOP_SPREAD_MEAN, config.PRIOR_LOG_SLOP_SPREAD_SD)
    lp += hazard.log_prior(theta[3:3 + hazard.n_params])
    return lp


def log_posterior(theta, data, hazard):
    lp = log_prior(theta, hazard)
    if not np.isfinite(lp):
        return -np.inf
    ll = log_likelihood(theta, data, hazard)
    return lp + ll


def expected_clean_time(bpt, mu_log, sigma_log):
    """E[T_clean] = bpt + E[slop]."""
    mean_slop, _ = slop_moments(mu_log, sigma_log)
    return bpt + mean_slop


def mean_s_given_died(hazard, haz_params, n_grid=200):
    """E[S_death | S_death <= 1].

        = integral_0^1 s * haz(s) * surv_prob(s) ds  /  (1 - surv_prob(1))
    """
    p_die = 1.0 - float(np.exp(-hazard.cum(1.0, haz_params)))
    if p_die <= 0.0:
        return 0.0
    s = np.linspace(0.0, 1.0, n_grid)
    density = hazard.h(s, haz_params) * np.exp(-hazard.cum(s, haz_params))
    return float(simpson(s * density, x=s) / p_die)


def m_clear(theta, hazard):
    """Expected wall-clock time to first survival from a fresh start.

        M_clear = (1/p - 1) * E[wall-clock | died] + E[T_clean]
                = (1-p)/p  * (E[S | died] * E[T_clean] + respawn) + E[T_clean]
    """
    bpt, sf, ssp, haz_params = unpack(theta, hazard)
    mu_log, sigma_log = lognormal_params(sf, ssp, bpt)
    p_surv = float(np.exp(-hazard.cum(1.0, haz_params)))
    e_clean = expected_clean_time(bpt, mu_log, sigma_log)
    if p_surv >= 1.0:
        return e_clean
    mean_s_died = mean_s_given_died(hazard, haz_params)
    e_died_time = mean_s_died * e_clean + config.RESPAWN_MS
    return (1.0 - p_surv) / p_surv * e_died_time + e_clean


def initial_theta(hazard):
    scalars = np.array([
        config.PRIOR_LOG_BPT_MEAN,
        config.PRIOR_LOG_SLOP_FRAC_MEAN,
        config.PRIOR_LOG_SLOP_SPREAD_MEAN,
    ])
    return np.concatenate([scalars, hazard.initial_theta_slice()])
