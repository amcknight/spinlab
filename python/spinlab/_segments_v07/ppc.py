"""Posterior predictive checks for the V07 segment model.

Given posterior samples of theta (length-10 for haz1, length-15 for beta2)
and the observed attempts, simulate replicated datasets from each sample and
compare a discrepancy statistic between observed and replicated. The Bayes
p-value answers "is the observed value surprising under what the fitted
model would generate?"

Used to:

  - sanity-check that own-model-on-own-truth lands p near 0.5 (no false alarm)
  - confirm that misspecified fits (e.g. haz1 on beta2-truth) get caught
    by a shape-sensitive statistic
  - on real data, surface modeling tension that argues for richer hazard
    (the moving-peaks question)

API:

    sims = posterior_predictive(theta_samples, n_attempts, model='haz1', seed=0)
        -> ndarray (S, N) time_ms, ndarray (S, N) bool is_died

    result = run_ppc(theta_samples, time_ms_obs, is_died_obs,
                      model='haz1', seed=0, stats=DEFAULT_STATS)
        -> dict[stat_name] = {'obs': float, 'sims': ndarray (S,),
                              'p_two_sided': float, 'p_one_sided': float}

The forward simulator is consistent-by-construction with
generate_synthetic_learning_v07.generate(): theta gets unpacked into a
`truth` dict and passed through `generate()` for haz1, or a vectorized
re-implementation for beta2 (Python loop over attempts was too slow at
S * N = 1M).
"""
from __future__ import annotations

import numpy as np

import config
from generate_synthetic_learning_v07 import TRUTH_DEFAULTS


# ---------------------------------------------------------------------------
# theta -> truth dict
# ---------------------------------------------------------------------------


def _curve_at_n(log_inf, log_one, halflife, n_arr):
    """Vectorized V07 learning-curve evaluation. log_inf, log_one, halflife
    shape (S,) or scalar; n_arr shape (N,). Returns (S, N)."""
    log_inf = np.atleast_1d(log_inf)[:, None]
    log_one = np.atleast_1d(log_one)[:, None]
    halflife = np.atleast_1d(halflife)[:, None]
    decay = 2.0 ** (-(n_arr[None, :] - 1.0) / halflife)
    return log_inf + (log_one - log_inf) * decay


def _theta_to_curves(theta_samples, n_attempts):
    """For each sample, compute sf(n), ssp(n), alpha(n) at n=1..n_attempts.

    theta_samples: (S, 10+) — first 10 entries follow the V07 layout.
    Returns (sf, ssp, alpha) each of shape (S, n_attempts) in natural units.
    """
    theta = np.atleast_2d(np.asarray(theta_samples, dtype=np.float64))
    S = theta.shape[0]
    n_arr = np.arange(1, n_attempts + 1, dtype=np.float64)

    log_sf  = _curve_at_n(theta[:, 1], theta[:, 4], np.exp(theta[:, 7]), n_arr)
    log_ssp = _curve_at_n(theta[:, 2], theta[:, 5], np.exp(theta[:, 8]), n_arr)
    log_a   = _curve_at_n(theta[:, 3], theta[:, 6], np.exp(theta[:, 9]), n_arr)
    return np.exp(log_sf), np.exp(log_ssp), np.exp(log_a)


# ---------------------------------------------------------------------------
# Forward simulation
# ---------------------------------------------------------------------------


def _simulate_haz1_vec(theta_samples, n_attempts, seed):
    """Vectorized forward simulation for haz1 over S posterior samples.

    Returns (time_ms, is_died) shape (S, n_attempts).
    """
    theta = np.atleast_2d(np.asarray(theta_samples, dtype=np.float64))
    S = theta.shape[0]
    N = int(n_attempts)
    rng = np.random.default_rng(seed)

    sf, ssp, alpha = _theta_to_curves(theta, N)            # (S, N)
    bpt = np.exp(theta[:, 0])[:, None]                     # (S, 1)

    mean_slop = sf * bpt
    sigma_log_sq = np.log1p(ssp ** 2)
    mu_log = np.log(mean_slop) - 0.5 * sigma_log_sq
    sigma_log = np.sqrt(sigma_log_sq)

    p_die = 1.0 - np.exp(-alpha)                           # (S, N)
    u_die = rng.random(size=(S, N))
    is_died = u_die < p_die                                # (S, N) bool

    # Clean time: T_clean = bpt + LN(mu_log, sigma_log)
    t_clean = bpt + rng.lognormal(mean=mu_log, sigma=sigma_log)

    # s_death | died for haz1: truncated exp(alpha) on [0, 1].
    # s = -log(1 - u * p_die) / alpha  (u ~ U(0, 1)).
    u_s = rng.random(size=(S, N))
    s_death = -np.log1p(-u_s * p_die) / np.maximum(alpha, 1e-300)
    s_death = np.clip(s_death, 0.0, 1.0)

    t_obs = np.where(
        is_died,
        s_death * t_clean + config.RESPAWN_MS,
        t_clean,
    )
    return np.rint(t_obs).astype(np.float64), is_died


def _simulate_beta2_vec(theta_samples, n_attempts, seed):
    """Vectorized forward simulation for beta2 (K=2 beta mixture).

    Mirrors generate_synthetic_learning_v07.generate(..., hazard_truth='beta2')
    but vectorized over S samples. Beta-shape params per sample come from the
    theta layout used by learning_model_v07_beta2_jax (indices 10..14):
        10: logit(mu_1)
        11: logit(mu_2)
        12: log(kappa_1)
        13: log(kappa_2)
        14: eta_1   (eta_2 := 0, w = softmax([eta_1, 0]))
    """
    theta = np.atleast_2d(np.asarray(theta_samples, dtype=np.float64))
    if theta.shape[1] < 15:
        raise ValueError(
            f"beta2 simulation needs 15-dim theta, got {theta.shape[1]}"
        )
    S = theta.shape[0]
    N = int(n_attempts)
    rng = np.random.default_rng(seed)

    sf, ssp, alpha = _theta_to_curves(theta, N)            # (S, N)
    bpt = np.exp(theta[:, 0])[:, None]                     # (S, 1)

    mean_slop = sf * bpt
    sigma_log_sq = np.log1p(ssp ** 2)
    mu_log = np.log(mean_slop) - 0.5 * sigma_log_sq
    sigma_log = np.sqrt(sigma_log_sq)

    p_die = 1.0 - np.exp(-alpha)
    u_die = rng.random(size=(S, N))
    is_died = u_die < p_die

    t_clean = bpt + rng.lognormal(mean=mu_log, sigma=sigma_log)

    # Per-sample mixture shape
    mu_k = 1.0 / (1.0 + np.exp(-theta[:, 10:12]))          # (S, 2)
    kappa_k = np.exp(theta[:, 12:14])                      # (S, 2)
    eta = np.stack([theta[:, 14], np.zeros(S)], axis=1)    # (S, 2)
    eta = eta - eta.max(axis=1, keepdims=True)
    ex = np.exp(eta)
    w = ex / ex.sum(axis=1, keepdims=True)                 # (S, 2)
    a = mu_k * kappa_k                                     # (S, 2)
    b = (1.0 - mu_k) * kappa_k                             # (S, 2)

    # Pick component per (sample, attempt) using w[s, 0] as threshold.
    u_mix = rng.random(size=(S, N))
    k_idx = (u_mix >= w[:, 0:1]).astype(np.int64)          # (S, N) in {0, 1}

    # Gather a, b per (sample, attempt).
    s_arange = np.arange(S)[:, None]                       # (S, 1)
    a_pa = a[s_arange, k_idx]                              # (S, N)
    b_pa = b[s_arange, k_idx]                              # (S, N)

    # np.random.beta supports broadcasting on shape parameters.
    s_death = rng.beta(a_pa, b_pa)                         # (S, N) in (0, 1)

    t_obs = np.where(
        is_died,
        s_death * t_clean + config.RESPAWN_MS,
        t_clean,
    )
    return np.rint(t_obs).astype(np.float64), is_died


def posterior_predictive(theta_samples, n_attempts, model='haz1', seed=0):
    """Forward simulate replicated datasets from posterior samples.

    Args:
        theta_samples: (S, 10) for haz1 or (S, 15) for beta2.
        n_attempts: int N. Each replicated dataset has N attempts.
        model: 'haz1' or 'beta2'.
        seed: rng seed.

    Returns:
        (time_ms, is_died), each shape (S, N). time_ms is rounded to integer
        ms (matches generate()'s output format).
    """
    if model == 'haz1':
        return _simulate_haz1_vec(theta_samples, n_attempts, seed)
    elif model == 'beta2':
        return _simulate_beta2_vec(theta_samples, n_attempts, seed)
    else:
        raise ValueError(f"model must be 'haz1' or 'beta2', got {model!r}")


# ---------------------------------------------------------------------------
# Discrepancy statistics
# ---------------------------------------------------------------------------
#
# Each stat takes (time_ms, is_died) and returns a scalar. For sample stacks
# we accept (S, N) inputs and return (S,).
#
# Choice notes (see external_docs/reports/2026-05-17_handoff_status.md):
#   - died_rate: basic — picks up p_die mismatch.
#   - died_tau_skew / died_tau_kurt: moments of (t_obs - respawn) on died
#     attempts. Since t_clean is shared, higher moments differ across haz1 vs
#     beta2 driven mostly by s_death shape.
#   - died_s_mode_count: bin s_proxy in [0, 1] and count peaks. Most direct
#     test for "is there structural bimodality the model can't fit?" Used as
#     the haz1-on-beta2-truth canary.


def _moments_atleast2d(time_ms, is_died, kind):
    """Per-row (or whole-array) skewness or excess kurtosis of (time_ms -
    respawn) on died attempts only. Returns 0 if fewer than 4 died entries.
    """
    time_ms = np.asarray(time_ms, dtype=np.float64)
    is_died = np.asarray(is_died, dtype=bool)
    tau = time_ms - config.RESPAWN_MS
    if time_ms.ndim == 1:
        d = tau[is_died]
        if len(d) < 4:
            return 0.0
        return _scalar_moment(d, kind)
    # 2D: per-row
    out = np.zeros(time_ms.shape[0])
    for s in range(time_ms.shape[0]):
        d = tau[s, is_died[s]]
        if len(d) >= 4:
            out[s] = _scalar_moment(d, kind)
    return out


def _scalar_moment(x, kind):
    m = x.mean()
    c = x - m
    var = float(np.mean(c ** 2))
    if var <= 0:
        return 0.0
    if kind == 'skew':
        return float(np.mean(c ** 3) / var ** 1.5)
    elif kind == 'kurt':       # excess kurtosis (normal -> 0)
        return float(np.mean(c ** 4) / var ** 2 - 3.0)
    else:
        raise ValueError(kind)


def stat_died_rate(time_ms, is_died):
    is_died = np.asarray(is_died, dtype=bool)
    if is_died.ndim == 1:
        return float(is_died.mean())
    return is_died.mean(axis=1)


def stat_died_tau_skew(time_ms, is_died):
    return _moments_atleast2d(time_ms, is_died, 'skew')


def stat_died_tau_kurt(time_ms, is_died):
    return _moments_atleast2d(time_ms, is_died, 'kurt')


def _middle_third_one(tau_died):
    """Fraction of died attempts with s_proxy in [1/3, 2/3], where s_proxy
    is tau / quantile_99(tau). For haz1 (monotone decreasing s_death
    density), most mass is at low s, so middle-third mass is small. For
    beta2 with mu=[0.35, 0.70], one mode lands in the middle third and one
    is at its upper edge, so middle-third mass is much higher.
    """
    if len(tau_died) < 10:
        return 0.0
    upper = np.quantile(tau_died, 0.99)
    if upper <= 0:
        return 0.0
    x = tau_died / upper
    return float(np.mean((x >= 1.0 / 3.0) & (x <= 2.0 / 3.0)))


def stat_died_s_middle_third(time_ms, is_died):
    """Middle-third mass of normalized died-tau distribution. Discriminates
    haz1 (low middle mass) vs beta2 (high middle mass)."""
    time_ms = np.asarray(time_ms, dtype=np.float64)
    is_died = np.asarray(is_died, dtype=bool)
    tau = time_ms - config.RESPAWN_MS
    if time_ms.ndim == 1:
        return _middle_third_one(tau[is_died])
    out = np.zeros(time_ms.shape[0])
    for s in range(time_ms.shape[0]):
        out[s] = _middle_third_one(tau[s, is_died[s]])
    return out


# ---------------------------------------------------------------------------
# Within-run tertile stats — for catching non-stationary hazard shape.
#
# `stat_died_s_middle_third` aggregates over the whole run. If the hazard
# shape drifts within a single run (one of the three phenomena in
# external_docs/nonstationary_shape.md), whole-run mid_third averages the
# drift away — observed mid_third matches model mid_third on average even
# though neither captures what's happening per-tertile.
#
# These stats bin deaths by attempt position (early/late thirds) and
# return either the per-bin mid_third or its late-minus-early delta. The
# delta is the "shape-drift detector" — ~0 for static truth, non-zero for
# drift truth.
#
# Why tertiles and not halves: drift is often centered (phenomenon 2's
# swap typically lands mid-run), so half-vs-half blurs the transition.
# Comparing first-third to last-third gives a cleaner signal at the cost
# of dropping the middle third's data from the delta.
# ---------------------------------------------------------------------------


def _tertile_slices(n_attempts):
    """Index ranges for early third and late third. The middle third is
    deliberately excluded from the delta to maximize separation."""
    third = n_attempts // 3
    early_hi = third
    late_lo = n_attempts - third
    return early_hi, late_lo


def _middle_third_tertile_one(tau, died_mask, kind):
    """One-dataset implementation. kind in {'early','late','delta'}."""
    N = len(tau)
    if N < 9:
        return 0.0
    early_hi, late_lo = _tertile_slices(N)
    early = tau[:early_hi][died_mask[:early_hi]]
    late  = tau[late_lo:][died_mask[late_lo:]]
    mt_early = _middle_third_one(early)
    mt_late  = _middle_third_one(late)
    if kind == 'early':
        return mt_early
    if kind == 'late':
        return mt_late
    if kind == 'delta':
        return mt_late - mt_early
    raise ValueError(kind)


def _stat_mid_third_tertile(time_ms, is_died, kind):
    time_ms = np.asarray(time_ms, dtype=np.float64)
    is_died = np.asarray(is_died, dtype=bool)
    tau = time_ms - config.RESPAWN_MS
    if time_ms.ndim == 1:
        return _middle_third_tertile_one(tau, is_died, kind)
    out = np.zeros(time_ms.shape[0])
    for s in range(time_ms.shape[0]):
        out[s] = _middle_third_tertile_one(tau[s], is_died[s], kind)
    return out


def stat_died_s_mid_third_early(time_ms, is_died):
    """Mid-third mass computed on deaths in the first third of attempts."""
    return _stat_mid_third_tertile(time_ms, is_died, 'early')


def stat_died_s_mid_third_late(time_ms, is_died):
    """Mid-third mass computed on deaths in the last third of attempts."""
    return _stat_mid_third_tertile(time_ms, is_died, 'late')


def stat_died_s_mid_third_delta(time_ms, is_died):
    """Late-third mid_third mass minus early-third mid_third mass.

    Detector for within-run hazard-shape drift (the three phenomena in
    nonstationary_shape.md). For static-shape truth, ~0 in expectation;
    for drift truth, non-zero with sign depending on which way the peak
    moved.
    """
    return _stat_mid_third_tertile(time_ms, is_died, 'delta')


DEFAULT_STATS = {
    'died_rate':         stat_died_rate,
    'died_tau_skew':     stat_died_tau_skew,
    'died_tau_kurt':     stat_died_tau_kurt,
    'died_s_mid_third':  stat_died_s_middle_third,
}


# v2-scope stats: detectors for within-run hazard-shape drift. Not in
# DEFAULT_STATS — v1 ships with the static-shape assumption, so firing
# these by default would surface noise the v1 model can't act on. They
# stay defined here so the v2 follow-on (moving-peaks per
# external_docs/nonstationary_shape.md) can opt in via stats=V2_WITHIN_RUN_STATS
# or by extending DEFAULT_STATS.
V2_WITHIN_RUN_STATS = {
    'died_s_mid_third_early':    stat_died_s_mid_third_early,
    'died_s_mid_third_late':     stat_died_s_mid_third_late,
    'died_s_mid_third_delta':    stat_died_s_mid_third_delta,
}


# ---------------------------------------------------------------------------
# Bayes p-values
# ---------------------------------------------------------------------------


def bayes_pvalue(t_obs, t_sims):
    """Return (p_one_sided, p_two_sided).

    Uses the mid-p convention: P(T_rep > T_obs) + 0.5 * P(T_rep == T_obs).
    For continuous-valued statistics this equals the classical
    mean(T_sim >= T_obs); for discrete or coarse stats it avoids the
    pathology where many sims hit T_obs exactly and the classical p
    collapses to 0 or 1.

    Near 0.5 = model can reproduce the observed value. Near 0 or 1 = tension.
    """
    sims = np.asarray(t_sims, dtype=np.float64)
    if len(sims) == 0:
        return float('nan'), float('nan')
    p_one = float(np.mean(sims > t_obs) + 0.5 * np.mean(sims == t_obs))
    p_two = 2.0 * min(p_one, 1.0 - p_one)
    return p_one, p_two


def run_ppc(theta_samples, time_ms_obs, is_died_obs,
            model='haz1', stats=None, seed=0, n_attempts=None):
    """Top-level driver.

    Args:
        theta_samples: (S, 10) for haz1 or (S, 15) for beta2 posterior samples.
        time_ms_obs, is_died_obs: observed data, length-N each.
        model: 'haz1' or 'beta2'.
        stats: dict[name -> callable(time_ms, is_died) -> scalar]. Defaults
            to DEFAULT_STATS.
        seed: rng seed for the forward sim.
        n_attempts: number of attempts per replicated dataset. Defaults to
            len(time_ms_obs) — the standard PPC choice.

    Returns:
        dict[stat_name] = {
            'obs': scalar T(y_obs),
            'sims': ndarray (S,) of T(y_rep),
            'p_one_sided': float,
            'p_two_sided': float,
        }
    """
    if stats is None:
        stats = DEFAULT_STATS
    time_ms_obs = np.asarray(time_ms_obs, dtype=np.float64)
    is_died_obs = np.asarray(is_died_obs, dtype=bool)
    if n_attempts is None:
        n_attempts = len(time_ms_obs)

    sim_t, sim_d = posterior_predictive(theta_samples, n_attempts,
                                        model=model, seed=seed)

    out = {}
    for name, fn in stats.items():
        obs_val = float(fn(time_ms_obs, is_died_obs))
        sim_vals = np.asarray(fn(sim_t, sim_d), dtype=np.float64)
        p1, p2 = bayes_pvalue(obs_val, sim_vals)
        out[name] = {
            'obs': obs_val,
            'sims': sim_vals,
            'p_one_sided': p1,
            'p_two_sided': p2,
        }
    return out
