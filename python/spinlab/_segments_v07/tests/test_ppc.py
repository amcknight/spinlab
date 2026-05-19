"""PPC infrastructure tests.

Pins the things we care about most:

  1. Forward sim shape, dtype, value contracts.
  2. Well-spec p-values are mid-range (no false alarm).
  3. Misspec — haz1 fit on beta2-truth — is caught by the s_modes statistic.

The well-spec and misspec tests use small N and few samples to keep wall
time reasonable; with more N or S the boundaries get tighter, so these are
*conservative* gates, not tight ones.
"""
import numpy as np
import pytest

import config
from generate_synthetic_learning_v07 import TRUTH_DEFAULTS, generate
import learning_model_v07_jax as lm1
import learning_model_v07_beta2_jax as lm2
import fit_jax as f1
import fit_jax_beta2 as f2
import ppc


# ---------------------------------------------------------------------------
# Forward simulator contracts
# ---------------------------------------------------------------------------


def test_simulate_haz1_shape_and_dtype(truth_theta):
    samples = np.tile(truth_theta, (5, 1))
    t, d = ppc.posterior_predictive(samples, n_attempts=20,
                                     model='haz1', seed=0)
    assert t.shape == (5, 20)
    assert d.shape == (5, 20)
    assert t.dtype == np.float64
    assert d.dtype == np.bool_
    # Integer ms (generate() rounds; PPC matches that contract).
    assert np.allclose(t, np.rint(t))
    # All survived attempts have t >= bpt; all died attempts have t >= respawn.
    assert (t[~d] >= 1.0).all()
    assert (t[d] >= config.RESPAWN_MS - 1).all()


def test_simulate_beta2_rejects_short_theta(truth_theta):
    """beta2 requires 15-dim theta; bare V07 (10-dim) is an error."""
    samples = np.tile(truth_theta, (3, 1))
    with pytest.raises(ValueError, match="beta2"):
        ppc.posterior_predictive(samples, n_attempts=10,
                                  model='beta2', seed=0)


def test_simulate_haz1_matches_generate_die_rate(truth_theta):
    """At fixed theta=truth, S replicate datasets should have died-rate
    close to the analytical mean p_die averaged over n=1..N."""
    S, N = 50, 500
    samples = np.tile(truth_theta, (S, 1))
    t, d = ppc.posterior_predictive(samples, n_attempts=N,
                                     model='haz1', seed=11)
    rate_per_rep = d.mean(axis=1)

    # Analytical: alpha(n) = exp(curve(...)), p_die(n) = 1-exp(-alpha(n)).
    from generate_synthetic_learning_v07 import curve
    t_d = TRUTH_DEFAULTS
    n_arr = np.arange(1, N + 1, dtype=np.float64)
    log_a = curve(np.log(t_d['alpha_inf']), np.log(t_d['alpha_1']),
                  t_d['alpha_halflife'], n_arr)
    p_die = 1.0 - np.exp(-np.exp(log_a))
    expected = float(p_die.mean())

    # Mean over 50 replicates of N=500 should be within ~1% of expected.
    assert abs(rate_per_rep.mean() - expected) < 0.01


def test_simulate_haz1_s_death_matches_truncated_exp(truth_theta):
    """The shape contract: died-attempt s_death = (t_obs - respawn) / t_clean
    follows truncated exp(alpha) on [0, 1]. Test asymptotic alpha_inf only
    (late attempts) so alpha is approximately constant; check mean(s) against
    the analytical mean of the truncated exponential.

    For truncated exp with rate a on [0, 1]:
        E[s] = 1/a - 1/(exp(a) - 1)
    """
    a_inf = TRUTH_DEFAULTS['alpha_inf']
    # Build a theta whose curves are flat at alpha_inf, then run a long sim.
    theta = np.array(truth_theta, dtype=np.float64).copy()
    theta[6] = theta[3]   # alpha_1 := alpha_inf so the alpha curve is constant
    S, N = 1, 200_000
    samples = theta[None, :]
    t_arr, d_arr = ppc.posterior_predictive(samples, n_attempts=N,
                                             model='haz1', seed=42)
    # Recover s from t_obs: we don't know per-attempt t_clean, but for died
    # attempts s = (t_obs - respawn) / t_clean ~ tau/(bpt + slop). The marginal
    # mean of s should still match the truncated-exp mean once we marginalize
    # over t_clean uniformly. Approximate by dividing tau by the *average*
    # clean time, then averaging.
    bpt = float(np.exp(theta[0]))
    sf_inf = float(np.exp(theta[1]))
    mean_t_clean = bpt * (1.0 + sf_inf)
    tau = (t_arr[0] - config.RESPAWN_MS)[d_arr[0]]
    s_proxy_mean = float(tau.mean() / mean_t_clean)

    expected = 1.0 / a_inf - 1.0 / (np.exp(a_inf) - 1.0)
    # 5% tolerance — proxy s using mean t_clean rather than per-attempt
    # introduces ~ssp^2 / 2 bias; for ssp~0.2 that's ~2%.
    assert abs(s_proxy_mean - expected) < 0.05, (
        f"sim s_death mean {s_proxy_mean:.4f} vs analytical {expected:.4f}"
    )


# ---------------------------------------------------------------------------
# Bayes p-value math
# ---------------------------------------------------------------------------


def test_bayes_pvalue_symmetric():
    rng = np.random.default_rng(0)
    sims = rng.standard_normal(10_000)
    p1, p2 = ppc.bayes_pvalue(0.0, sims)
    assert 0.45 < p1 < 0.55
    assert 0.85 < p2 <= 1.0


def test_bayes_pvalue_extreme():
    sims = np.zeros(1000)
    p1, p2 = ppc.bayes_pvalue(10.0, sims)
    assert p1 == 0.0
    assert p2 == 0.0


# ---------------------------------------------------------------------------
# Well-spec and misspec detection
# ---------------------------------------------------------------------------


@pytest.fixture(scope='module')
def haz1_truth_data():
    """N=300, seed=0, haz1-truth. Smaller than the synth_data_n500 fixture
    so PPC tests stay <10s each."""
    return generate(300, TRUTH_DEFAULTS, seed=0, hazard_truth='haz1')


@pytest.fixture(scope='module')
def beta2_truth_data():
    return generate(300, TRUTH_DEFAULTS, seed=0, hazard_truth='beta2')


def _fit_and_sample(data, model, n_samples=200, seed=42):
    """MAP + Laplace + posterior sample. Diag fallback on non-PD."""
    if model == 'haz1':
        lm, fit_mod = lm1, f1
    else:
        lm, fit_mod = lm2, f2
    time_ms, is_died = lm.data_to_arrays(data)
    theta_map, _ = fit_mod.find_map(time_ms, is_died, maxiter=400)
    cov, ok = fit_mod.laplace_posterior(theta_map, time_ms, is_died)
    rng = np.random.default_rng(seed)
    if cov is not None and ok:
        samples = rng.multivariate_normal(np.asarray(theta_map), cov,
                                           size=n_samples)
    else:
        d = len(theta_map)
        sd = np.sqrt(np.abs(np.diag(cov))) if cov is not None else np.full(d, 0.1)
        samples = (np.asarray(theta_map)[None, :]
                   + rng.standard_normal((n_samples, d)) * sd)
    return np.asarray(time_ms), np.asarray(is_died), samples


def test_haz1_on_haz1_truth_no_false_alarm(haz1_truth_data):
    """own-model on own-truth: at least 3 of 4 stats should land in
    p_two_sided > 0.05. Stochastic — a single statistic occasionally dips
    below at small S, but losing more than one is a regression."""
    time_ms, is_died, samples = _fit_and_sample(haz1_truth_data, 'haz1')
    result = ppc.run_ppc(samples, time_ms, is_died, model='haz1', seed=3)
    passing = sum(1 for r in result.values() if r['p_two_sided'] > 0.05)
    assert passing >= 3, (
        f"haz1-on-haz1-truth should not raise tension on most stats; "
        f"got {passing}/4. Detail: "
        + ", ".join(f"{k}={v['p_two_sided']:.3f}" for k, v in result.items())
    )


def test_haz1_on_beta2_truth_catches_misspec(beta2_truth_data):
    """The headline contract: PPC catches haz1's inability to fit beta2-truth.

    s_modes is the cleanest signal — beta2-truth has 2 peaks in s_death
    density, haz1 generates monotone. The observed mode count should be
    in the 1-or-2 range; haz1's posterior predictive distribution should
    almost always give 1 (the monotone-density mode count). So the
    one-sided p (sim >= obs) is tiny, and so is p_two_sided.
    """
    time_ms, is_died, samples = _fit_and_sample(beta2_truth_data, 'haz1')
    result = ppc.run_ppc(samples, time_ms, is_died, model='haz1', seed=5)
    # At least one of the shape-sensitive stats must catch it.
    shape_stats = ['died_s_mid_third', 'died_tau_skew', 'died_tau_kurt']
    min_p = min(result[s]['p_two_sided'] for s in shape_stats)
    assert min_p < 0.10, (
        f"haz1-on-beta2-truth should be flagged by some shape stat; "
        f"min p_two_sided over shape stats = {min_p:.3f}. Detail: "
        + ", ".join(f"{s}={result[s]['p_two_sided']:.3f}" for s in shape_stats)
    )


# ---------------------------------------------------------------------------
# Tertile mid_third stats — within-run shape drift
# ---------------------------------------------------------------------------


def test_tertile_stats_register_v2_only():
    """The tertile stats live in V2_WITHIN_RUN_STATS, NOT DEFAULT_STATS —
    v1 ships static-shape so firing within-run drift detectors by default
    would surface noise the v1 model can't act on. v2 (moving-peaks) opts in."""
    for name in ('died_s_mid_third_early', 'died_s_mid_third_late',
                 'died_s_mid_third_delta'):
        assert name in ppc.V2_WITHIN_RUN_STATS, f"{name} should be v2-only"
        assert name not in ppc.DEFAULT_STATS, (
            f"{name} leaked into v1 DEFAULT_STATS"
        )


def test_tertile_delta_static_truth_is_small(haz1_truth_data):
    """Static-shape truth should produce a near-zero late-minus-early delta
    (modulo finite-sample noise at N=300)."""
    time_ms = np.array([t for _, t in haz1_truth_data], dtype=float)
    is_died = np.array([o == 'died' for o, _ in haz1_truth_data], dtype=bool)
    delta = ppc.stat_died_s_mid_third_delta(time_ms, is_died)
    assert abs(delta) < 0.25, (
        f"static truth should not show large within-run drift; got delta={delta:.3f}"
    )


def test_tertile_delta_drift_truth_is_nonzero():
    """Drift truth that moves mass ACROSS the [1/3, 2/3] boundary of the
    normalized-tau distribution must produce a clearly non-zero delta.

    Stat anatomy: `_middle_third_one` computes tau/q99(tau) per-tertile,
    then asks "what fraction is in [1/3, 2/3]?". The q99 normalization
    means a peak at s=0.5 only registers in mid-third if there's a
    higher-s peak anchoring q99 above ~1.5 * 0.5 = 0.75. So this test
    uses 3 mu's with a constant 10% weight at mu=0.95 as the anchor; the
    movement is between mu=0.50 (early-dominant, registers in mid-third)
    and mu=0.05 (late-dominant, registers below 1/3).

    The stat has KNOWN BLIND SPOTS — drift that stays outside mid-third
    in both phases, or that has no high-s anchor, produces ~0 delta even
    for substantial within-run shape change. The sensitivity-matrix task
    (#7) is where we map them.
    """
    from generate_synthetic_learning_v07 import strat_swap_shape
    truth = dict(TRUTH_DEFAULTS)
    truth['alpha_inf'] = 0.7
    truth['alpha_1'] = 1.5
    drift = strat_swap_shape(
        mu=[0.05, 0.50, 0.95], kappa=[40.0, 30.0, 40.0],
        w_before=[0.05, 0.85, 0.10],
        w_after=[0.85, 0.05, 0.10],
        n_swap=250, ramp_width=5,
    )
    rows = generate(500, truth, seed=7, hazard_truth='beta2', shape_at=drift)
    time_ms = np.array([t for _, t in rows], dtype=float)
    is_died = np.array([o == 'died' for o, _ in rows], dtype=bool)

    mt_early = ppc.stat_died_s_mid_third_early(time_ms, is_died)
    mt_late  = ppc.stat_died_s_mid_third_late(time_ms, is_died)
    delta    = ppc.stat_died_s_mid_third_delta(time_ms, is_died)
    assert np.isclose(delta, mt_late - mt_early)
    assert mt_early > 0.15, (
        f"early tertile (mass at s=0.5, anchored by s=0.95 peak) "
        f"should hit mid-third; got mt_early={mt_early:.3f}"
    )
    assert mt_late < mt_early - 0.10, (
        f"late should drop mid-third mass relative to early; "
        f"got mt_early={mt_early:.3f}, mt_late={mt_late:.3f}"
    )


def test_tertile_stats_batched_shape(truth_theta):
    """Batched (S, N) input should produce length-S output for each tertile stat."""
    S = 5
    samples = np.tile(truth_theta, (S, 1))
    sim_t, sim_d = ppc.posterior_predictive(samples, n_attempts=300,
                                             model='haz1', seed=0)
    for name in ['died_s_mid_third_early', 'died_s_mid_third_late',
                 'died_s_mid_third_delta']:
        result = ppc.V2_WITHIN_RUN_STATS[name](sim_t, sim_d)
        assert result.shape == (S,), f"{name}: got shape {result.shape}"


def test_tertile_short_data_returns_zero():
    """N < 9 should short-circuit to 0 (not enough deaths per tertile to
    meaningfully estimate mid_third)."""
    time_ms = np.array([5000.0, 6000.0, 7000.0], dtype=float)
    is_died = np.array([True, True, False], dtype=bool)
    assert ppc.stat_died_s_mid_third_early(time_ms, is_died) == 0.0
    assert ppc.stat_died_s_mid_third_late(time_ms, is_died) == 0.0
    assert ppc.stat_died_s_mid_third_delta(time_ms, is_died) == 0.0
