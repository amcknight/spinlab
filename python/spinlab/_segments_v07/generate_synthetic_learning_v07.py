"""Synthetic attempts with per-attempt learning curves -- V07 parameterization.

V07 form: log theta(n) = log theta_inf + (log theta_1 - log theta_inf) * 2^(-(n-1)/halflife)

Defaults are converted from the v0.6 (Delta, tau) numbers so that fits to a
previous (v0.6 or v07-tau) synth file generated at the same seed produce
identical TSV bytes. The reparameterization is just rename + change-of-base:
halflife = tau * ln(2), with 2^(-(n-1)/halflife) == exp(-(n-1)/tau).

Beta-mixture death timing stays static (misspec w.r.t. haz1, as before).

Usage:
    python generate_synthetic_learning_v07.py --n 500 --seed 0
    python generate_synthetic_learning_v07.py --n 500 --seed 1 --out custom.tsv
"""
import argparse
import csv
import json
import os

import numpy as np

import config


def _v07_first_from_v06(theta_inf, delta_v06, tau_v06):
    """Compute theta_1 such that V07 curve == v0.6 curve at every n>=1.

    v0.6: log theta(n) = log theta_inf + delta * exp(-n/tau)
    V07:  log theta(n) = log theta_inf + (log theta_1 - log theta_inf) * 2^(-(n-1)/halflife)

    Equivalence at every n>=1 requires halflife = tau * ln(2) and
    log theta_1 - log theta_inf = delta * exp(-1/tau).
    """
    return float(theta_inf * np.exp(delta_v06 * np.exp(-1.0 / tau_v06)))


# V07 truth defaults, derived from v0.6 numbers in external_docs/learning_curves.md
# "Phase 2" so generated data matches v0.6 at every observed n given the same seed.
TRUTH_DEFAULTS = dict(
    bpt=30000.0,
    sf_inf=0.05,
    sf_1=_v07_first_from_v06(0.05, 1.4, 40.0),       # ~0.196
    sf_halflife=40.0 * np.log(2.0),                  # ~27.7 attempts (= old tau 40)
    ssp_inf=0.2,
    ssp_1=_v07_first_from_v06(0.2, 1.0, 80.0),       # ~0.537
    ssp_halflife=80.0 * np.log(2.0),                 # ~55.5 attempts (= old tau 80)
    alpha_inf=0.3,
    alpha_1=_v07_first_from_v06(0.3, 1.4, 20.0),     # ~1.136
    alpha_halflife=20.0 * np.log(2.0),               # ~13.9 attempts (= old tau 20)
    # Beta-mixture for within-attempt death timing (static, not learning).
    mu=[0.35, 0.70],
    kappa=[15.0, 10.0],
    w=[0.60, 0.40],
    respawn_ms=config.RESPAWN_MS,
)


def curve(log_theta_inf, log_theta_1, halflife, n):
    """log theta(n) = log theta_inf + (log theta_1 - log theta_inf) * 2^(-(n-1)/halflife).

    n is 1-indexed. Vectorized over n.
    """
    n = np.asarray(n, dtype=float)
    return log_theta_inf + (log_theta_1 - log_theta_inf) * 2.0 ** (-(n - 1.0) / halflife)


def lognormal_slop_params(slop_frac, slop_spread, bpt):
    mean_slop = slop_frac * bpt
    sigma_log_sq = np.log(1.0 + slop_spread ** 2)
    mu_log = np.log(mean_slop) - 0.5 * sigma_log_sq
    return mu_log, np.sqrt(sigma_log_sq)


def generate(n_attempts, truth, seed=0, hazard_truth='beta2', shape_at=None):
    """Generate n_attempts synthetic attempts under V07 learning curves.

    hazard_truth selects the conditional distribution of s_death | died:
      'beta2' (default): mixture of K Betas with truth['mu'], ['kappa'], ['w'].
                         Matches the existing TRUTH_DEFAULTS shape.
      'haz1':            truncated exponential on [0, 1] with rate alpha(n).
                         The conditional density implied by constant hazard
                         h(s) = alpha. Used to generate haz1-truth synth for
                         the haz1-vs-beta2 model comparison.

    shape_at: optional callable n -> (mu, kappa, w) returning per-attempt
        beta-mixture shape. Used to exercise the non-stationary phenomena
        from external_docs/nonstationary_shape.md. Ignored when
        hazard_truth='haz1'. If None and hazard_truth='beta2', falls back
        to static shape from truth['mu','kappa','w']. Builders for the 3
        documented phenomena live in this module:
        `static_shape`, `weight_drift_shape`, `strat_swap_shape`,
        `strat_change_shape`.

    P(die per attempt) = 1 - exp(-alpha(n)) in both cases -- the two truths
    differ only in *where* deaths land within [0, 1], not how often.
    """
    if hazard_truth not in ('beta2', 'haz1'):
        raise ValueError(f"hazard_truth must be 'beta2' or 'haz1', got {hazard_truth!r}")

    rng = np.random.default_rng(seed)

    log_sf_inf, log_sf_1   = np.log(truth['sf_inf']),    np.log(truth['sf_1'])
    log_ssp_inf, log_ssp_1 = np.log(truth['ssp_inf']),   np.log(truth['ssp_1'])
    log_a_inf, log_a_1     = np.log(truth['alpha_inf']), np.log(truth['alpha_1'])

    if hazard_truth == 'beta2' and shape_at is None:
        shape_at = static_shape(truth['mu'], truth['kappa'], truth['w'])

    bpt = truth['bpt']
    rows = []
    for n in range(1, n_attempts + 1):
        sf_n    = float(np.exp(curve(log_sf_inf,  log_sf_1,  truth['sf_halflife'],    n)))
        ssp_n   = float(np.exp(curve(log_ssp_inf, log_ssp_1, truth['ssp_halflife'],   n)))
        alpha_n = float(np.exp(curve(log_a_inf,   log_a_1,   truth['alpha_halflife'], n)))
        p_die_n = 1.0 - np.exp(-alpha_n)

        mu_log, sigma_log = lognormal_slop_params(sf_n, ssp_n, bpt)
        t_clean = bpt + rng.lognormal(mu_log, sigma_log)
        if rng.random() < p_die_n:
            if hazard_truth == 'beta2':
                mu_n, kappa_n, w_n = shape_at(n)
                k = rng.choice(len(mu_n), p=w_n)
                a_k = mu_n[k] * kappa_n[k]
                b_k = (1.0 - mu_n[k]) * kappa_n[k]
                s_death = float(rng.beta(a_k, b_k))
            else:  # 'haz1' — truncated exponential with rate alpha_n on [0, 1]
                # F(s) = (1 - exp(-alpha s)) / (1 - exp(-alpha))
                # F^-1(u) = -log(1 - u (1 - exp(-alpha))) / alpha
                u = rng.random()
                s_death = -np.log1p(-u * p_die_n) / alpha_n
                s_death = float(min(max(s_death, 0.0), 1.0))
            t_obs = s_death * t_clean + truth['respawn_ms']
            rows.append(('died', round(float(t_obs))))
        else:
            rows.append(('survived', round(float(t_clean))))
    return rows


# ---------------------------------------------------------------------------
# Non-stationary shape builders.
#
# Implement the three phenomena from external_docs/nonstationary_shape.md as
# `shape_at(n) -> (mu, kappa, w)` callables. Pass any of these to
# `generate(..., shape_at=...)` to exercise drift truth. Built so:
#
#   - whole-run PPC mid_third averages out within-segment drift and won't
#     fire reliably (the moving-peaks deferral assumes detection requires
#     drift-aware stats, not static stats);
#   - tertile-binned / split-half PPC stats DO fire, giving us per-phenomenon
#     detection truth to validate proposed within-run stats against.
#
# kappa stays static throughout — matches the design proposal's "frozen
# kappa" stance. The three phenomena differ only in their (mu, w) trajectories.
# ---------------------------------------------------------------------------


def _as_arrays(mu, kappa, w):
    mu = np.asarray(mu, dtype=float)
    kappa = np.asarray(kappa, dtype=float)
    w = np.asarray(w, dtype=float)
    if not np.isclose(w.sum(), 1.0):
        raise ValueError(f"weights must sum to 1, got {w}")
    return mu, kappa, w


def static_shape(mu, kappa, w):
    """No drift — returns the same (mu, kappa, w) at every attempt.

    Equivalent to the existing beta2 truth behavior; this is the fallback
    builder when no `shape_at` is passed.
    """
    mu, kappa, w = _as_arrays(mu, kappa, w)
    def shape_at(n):
        return mu, kappa, w
    return shape_at


def weight_drift_shape(mu, kappa, w_init, w_final, halflife):
    """Phenomenon (1): thinky → muscle memory.

    Weights drift smoothly from w_init to w_final via the same halflife
    parameterization as the V07 learning curves: at attempt n,
        w(n) = w_init + (1 - 2^(-(n-1)/halflife)) * (w_final - w_init).

    Use case: at n=1 the "decision-point" component carries most weight;
    by late attempts it has shifted to the "execution" component. mu and
    kappa stay fixed (peak *locations* don't move, only their proportion).
    """
    mu, kappa, _ = _as_arrays(mu, kappa, w_init)
    w_i = np.asarray(w_init, dtype=float)
    w_f = np.asarray(w_final, dtype=float)
    if not np.isclose(w_f.sum(), 1.0):
        raise ValueError(f"w_final must sum to 1, got {w_f}")
    if halflife <= 0:
        raise ValueError(f"halflife must be > 0, got {halflife}")
    def shape_at(n):
        progress = 1.0 - 2.0 ** (-(n - 1.0) / halflife)
        w = w_i + progress * (w_f - w_i)
        # Re-normalize defensively: linear interp of simplex stays on simplex
        # for w_i,w_f on simplex, but rounding can drift by ~1e-16.
        w = w / w.sum()
        return mu, kappa, w
    return shape_at


def strat_swap_shape(mu, kappa, w_before, w_after, n_swap, ramp_width=3):
    """Phenomenon (2): section strat change.

    Localized weight swap centered at attempt n_swap. ramp_width controls
    the transition width: 0 = step function; >0 = smooth tanh over ±ramp_width
    attempts. Defaults to 3 (smooth) since real strat adoption is rarely
    instantaneous.

    Mechanism: one peak's weight drops, another's rises. mu and kappa fixed.
    """
    mu, kappa, _ = _as_arrays(mu, kappa, w_before)
    w_b = np.asarray(w_before, dtype=float)
    w_a = np.asarray(w_after, dtype=float)
    if not np.isclose(w_a.sum(), 1.0):
        raise ValueError(f"w_after must sum to 1, got {w_a}")
    if ramp_width < 0:
        raise ValueError(f"ramp_width must be >= 0, got {ramp_width}")
    def shape_at(n):
        if ramp_width == 0:
            return mu, kappa, (w_a if n >= n_swap else w_b)
        t = (n - n_swap) / float(ramp_width)
        alpha = 0.5 * (1.0 + np.tanh(t))
        w = (1.0 - alpha) * w_b + alpha * w_a
        w = w / w.sum()
        return mu, kappa, w
    return shape_at


def strat_change_shape(mu_before, kappa_before, w_before,
                       mu_after, kappa_after, w_after, n_change):
    """Phenomenon (3): complete strat change.

    All three of (mu, kappa, w) jump at attempt n_change. Step function
    (no smooth ramp: a "complete strat change" is by definition an event,
    not a gradual transition). Use the manual `σ_jump` flag pattern from
    nonstationary_shape.md: model is told the break point explicitly.

    The pre- and post-change shapes may have different K (different number
    of peaks). The generator handles that since weights are length-K and
    K can differ between the two configs.
    """
    mu_b, kappa_b, w_b = _as_arrays(mu_before, kappa_before, w_before)
    mu_a, kappa_a, w_a = _as_arrays(mu_after, kappa_after, w_after)
    def shape_at(n):
        if n >= n_change:
            return mu_a, kappa_a, w_a
        return mu_b, kappa_b, w_b
    return shape_at


def generate_multi_segment(n_segments, n_per_segment, true_pop_log_halflife_sf_mean,
                            true_pop_log_halflife_sf_sigma, base_seed=0,
                            base_truth=None):
    """Generate `n_segments` segments. Each segment's true halflife_sf is drawn
    from Normal(true_pop_..., true_pop_..._sigma) in log-space; all other truth
    values come from base_truth (defaults to TRUTH_DEFAULTS).

    Returns a list of dicts: {data, true_halflife_sf, truth}.

    Used by fit_eb_pool tests and demos to construct a multi-segment population
    where halflife_sf actually varies in a known way -- so we can check that
    pooling recovers the population (mean, sigma).
    """
    base_truth = dict(base_truth or TRUTH_DEFAULTS)
    rng = np.random.default_rng(base_seed)
    segments = []
    for s in range(n_segments):
        # Draw THIS segment's true halflife_sf from the population.
        log_hl = rng.normal(true_pop_log_halflife_sf_mean,
                            true_pop_log_halflife_sf_sigma)
        truth = dict(base_truth)
        truth['sf_halflife'] = float(np.exp(log_hl))
        data = generate(n_per_segment, truth, seed=base_seed * 10_000 + s)
        segments.append({
            'data': data,
            'true_halflife_sf': float(np.exp(log_hl)),
            'truth': truth,
        })
    return segments


def generate_multi_segment_varied(n_segments, n_per_segment, base_seed=0,
                                   base_truth=None, hazard_truth='beta2',
                                   sigma_per_param=0.4):
    """Multi-segment synth with ALL headline latents varied per segment.

    Unlike `generate_multi_segment` which only varies `halflife_sf`, this
    perturbs every learning latent (bpt, sf_inf, ssp_inf, alpha_inf, sf_1,
    ssp_1, alpha_1, halflife_sf, halflife_ssp, halflife_alpha) per segment
    by a log-Normal jitter with `sigma_per_param`.

    Useful for realistic batch-fit benchmarks where each segment is a
    distinct fit (not identical-up-to-data-noise).

    Returns same shape as generate_multi_segment: list of {data, truth}.
    Note: 'true_halflife_sf' included for compat with EB-pool drivers.
    """
    base_truth = dict(base_truth or TRUTH_DEFAULTS)
    rng = np.random.default_rng(base_seed)
    varied_keys = [
        'bpt', 'sf_inf', 'ssp_inf', 'alpha_inf',
        'sf_1', 'ssp_1', 'alpha_1',
        'sf_halflife', 'ssp_halflife', 'alpha_halflife',
    ]
    segments = []
    for s in range(n_segments):
        truth = dict(base_truth)
        for k in varied_keys:
            base = base_truth[k]
            log_jitter = rng.normal(0.0, sigma_per_param)
            truth[k] = float(base * np.exp(log_jitter))
        data = generate(n_per_segment, truth, seed=base_seed * 10_000 + s,
                        hazard_truth=hazard_truth)
        segments.append({
            'data': data,
            'true_halflife_sf': truth['sf_halflife'],
            'truth': truth,
        })
    return segments


def write_outputs(rows, truth, n_attempts, seed, out_path):
    with open(out_path, 'w', newline='') as f:
        writer = csv.writer(f, delimiter='\t')
        writer.writerow(['outcome', 'time_ms'])
        writer.writerows(rows)

    truth_path = os.path.splitext(out_path)[0] + '.truth.json'
    truth_dump = dict(truth)
    truth_dump['n_attempts'] = n_attempts
    truth_dump['seed'] = seed
    truth_dump['parameterization'] = 'v07-halflife'
    with open(truth_path, 'w') as f:
        json.dump(truth_dump, f, indent=2)
    return truth_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=500)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--out', default='test_synth_learning_v07.tsv')
    for key, default in TRUTH_DEFAULTS.items():
        if isinstance(default, list):
            ap.add_argument(f'--{key}', type=float, nargs='+', default=default)
        else:
            ap.add_argument(f'--{key}', type=float, default=default)
    args = ap.parse_args()

    truth = {k: getattr(args, k) for k in TRUTH_DEFAULTS}
    rows = generate(args.n, truth, seed=args.seed)
    truth_path = write_outputs(rows, truth, args.n, args.seed, args.out)

    n_died = sum(1 for o, _ in rows if o == 'died')
    print(f"Wrote {len(rows)} attempts ({n_died} died, "
          f"{len(rows) - n_died} survived) -> {args.out}")
    print(f"Truth -> {truth_path}\n")

    ns = sorted({1, max(1, args.n // 4), max(1, args.n // 2), args.n})
    print(f"  {'n':>4}  {'sf(n)':>8}  {'ssp(n)':>8}  {'alpha':>8}  {'P(die)':>8}")
    for n in ns:
        sf = float(np.exp(curve(np.log(truth['sf_inf']),
                                np.log(truth['sf_1']), truth['sf_halflife'], n)))
        ssp = float(np.exp(curve(np.log(truth['ssp_inf']),
                                 np.log(truth['ssp_1']), truth['ssp_halflife'], n)))
        ah = float(np.exp(curve(np.log(truth['alpha_inf']),
                                np.log(truth['alpha_1']), truth['alpha_halflife'], n)))
        p_die = 1.0 - np.exp(-ah)
        print(f"  {n:>4}  {sf:>8.4f}  {ssp:>8.4f}  {ah:>8.4f}  {p_die:>8.4f}")


if __name__ == '__main__':
    main()
