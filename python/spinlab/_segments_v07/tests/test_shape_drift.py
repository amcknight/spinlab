"""Tests for the non-stationary shape builders.

The builders implement the three phenomena from
external_docs/nonstationary_shape.md as `shape_at(n) -> (mu, kappa, w)`
callables passed into `generate(..., shape_at=...)`. These tests pin:

  1. Each builder's per-attempt shape trajectory matches the design intent.
  2. Weights stay on the simplex at every n.
  3. The default `shape_at=None` path is byte-identical to passing an
     explicit `static_shape(truth['mu','kappa','w'])` — backward compat.
  4. Generated data under drift is structurally different (early-third vs
     late-third s_death distributions diverge) — without this, the
     downstream tertile-PPC stat has nothing to detect.
"""
import numpy as np
import pytest

from generate_synthetic_learning_v07 import (
    TRUTH_DEFAULTS, generate,
    static_shape, weight_drift_shape, strat_swap_shape, strat_change_shape,
)
import config


# ---------------------------------------------------------------------------
# static_shape
# ---------------------------------------------------------------------------


def test_static_shape_invariant_over_attempts():
    s = static_shape([0.35, 0.70], [15.0, 10.0], [0.60, 0.40])
    mu_1, kappa_1, w_1 = s(1)
    for n in [1, 50, 500, 10_000]:
        mu, kappa, w = s(n)
        assert np.array_equal(mu, mu_1)
        assert np.array_equal(kappa, kappa_1)
        assert np.array_equal(w, w_1)


def test_static_shape_rejects_invalid_weights():
    with pytest.raises(ValueError, match="weights"):
        static_shape([0.35, 0.70], [15.0, 10.0], [0.30, 0.30])


# ---------------------------------------------------------------------------
# weight_drift_shape — phenomenon (1)
# ---------------------------------------------------------------------------


def test_weight_drift_endpoints():
    s = weight_drift_shape([0.35, 0.70], [15.0, 10.0],
                           w_init=[0.8, 0.2], w_final=[0.2, 0.8],
                           halflife=50)
    _, _, w1 = s(1)
    assert np.allclose(w1, [0.8, 0.2])
    _, _, w_late = s(10_000)
    # Asymptotic — within 1e-6 of w_final after many halflives.
    assert np.allclose(w_late, [0.2, 0.8], atol=1e-6)


def test_weight_drift_halflife_semantic():
    """At n = halflife + 1, progress should equal 1/2."""
    s = weight_drift_shape([0.35, 0.70], [15.0, 10.0],
                           w_init=[0.8, 0.2], w_final=[0.2, 0.8],
                           halflife=50)
    _, _, w_hl = s(51)
    # progress = 1 - 2^(-50/50) = 1 - 1/2 = 1/2
    expected = np.array([0.8, 0.2]) + 0.5 * (np.array([0.2, 0.8]) - np.array([0.8, 0.2]))
    assert np.allclose(w_hl, expected)


def test_weight_drift_monotone_in_n():
    """For the standard endpoint configuration, weight on component 0 should
    decrease monotonically with n."""
    s = weight_drift_shape([0.35, 0.70], [15.0, 10.0],
                           w_init=[0.8, 0.2], w_final=[0.2, 0.8],
                           halflife=50)
    w0s = [s(n)[2][0] for n in range(1, 300)]
    diffs = np.diff(w0s)
    assert (diffs <= 0).all(), "w[0] should be non-increasing"


def test_weight_drift_stays_on_simplex():
    s = weight_drift_shape([0.35, 0.70], [15.0, 10.0],
                           w_init=[0.8, 0.2], w_final=[0.2, 0.8],
                           halflife=50)
    for n in [1, 5, 25, 50, 100, 500, 1_000]:
        _, _, w = s(n)
        assert np.isclose(w.sum(), 1.0), f"weights don't sum to 1 at n={n}: {w}"
        assert (w >= 0).all(), f"negative weight at n={n}: {w}"


def test_weight_drift_validates_halflife():
    with pytest.raises(ValueError, match="halflife"):
        weight_drift_shape([0.35, 0.70], [15.0, 10.0],
                           [0.8, 0.2], [0.2, 0.8], halflife=0)


# ---------------------------------------------------------------------------
# strat_swap_shape — phenomenon (2)
# ---------------------------------------------------------------------------


def test_strat_swap_step_function():
    s = strat_swap_shape([0.35, 0.70], [15.0, 10.0],
                         w_before=[0.6, 0.4], w_after=[0.2, 0.8],
                         n_swap=100, ramp_width=0)
    _, _, w_pre = s(99)
    assert np.allclose(w_pre, [0.6, 0.4])
    _, _, w_post = s(100)
    assert np.allclose(w_post, [0.2, 0.8])
    _, _, w_far_post = s(500)
    assert np.allclose(w_far_post, [0.2, 0.8])


def test_strat_swap_smooth_ramp_centered():
    """At n = n_swap with smooth ramp, weight should sit at the midpoint."""
    s = strat_swap_shape([0.35, 0.70], [15.0, 10.0],
                         w_before=[0.6, 0.4], w_after=[0.2, 0.8],
                         n_swap=100, ramp_width=3)
    _, _, w_mid = s(100)
    expected = 0.5 * (np.array([0.6, 0.4]) + np.array([0.2, 0.8]))
    assert np.allclose(w_mid, expected)


def test_strat_swap_smooth_endpoints_match_step():
    """Far from n_swap, smooth and step should agree."""
    s_step = strat_swap_shape([0.35, 0.70], [15.0, 10.0],
                              [0.6, 0.4], [0.2, 0.8], n_swap=100, ramp_width=0)
    s_smooth = strat_swap_shape([0.35, 0.70], [15.0, 10.0],
                                [0.6, 0.4], [0.2, 0.8], n_swap=100, ramp_width=3)
    for n in [1, 50, 80, 130, 200, 500]:
        _, _, w_s = s_step(n)
        _, _, w_sm = s_smooth(n)
        assert np.allclose(w_s, w_sm, atol=0.01), (
            f"smooth vs step differ at n={n}: {w_sm} vs {w_s}"
        )


def test_strat_swap_validates_ramp_width():
    with pytest.raises(ValueError, match="ramp_width"):
        strat_swap_shape([0.35, 0.70], [15.0, 10.0],
                         [0.6, 0.4], [0.2, 0.8], n_swap=100, ramp_width=-1)


# ---------------------------------------------------------------------------
# strat_change_shape — phenomenon (3)
# ---------------------------------------------------------------------------


def test_strat_change_step():
    s = strat_change_shape(
        mu_before=[0.5], kappa_before=[10.0], w_before=[1.0],
        mu_after=[0.3, 0.7], kappa_after=[15.0, 10.0], w_after=[0.5, 0.5],
        n_change=100,
    )
    mu_pre, kappa_pre, w_pre = s(99)
    assert mu_pre.tolist() == [0.5]
    assert kappa_pre.tolist() == [10.0]
    assert w_pre.tolist() == [1.0]

    mu_post, kappa_post, w_post = s(100)
    assert mu_post.tolist() == [0.3, 0.7]
    assert kappa_post.tolist() == [15.0, 10.0]
    assert w_post.tolist() == [0.5, 0.5]


def test_strat_change_different_K_works():
    """The pre- and post-change can have different K (peak count)."""
    s = strat_change_shape(
        mu_before=[0.5], kappa_before=[10.0], w_before=[1.0],
        mu_after=[0.3, 0.7], kappa_after=[15.0, 10.0], w_after=[0.5, 0.5],
        n_change=100,
    )
    truth = dict(TRUTH_DEFAULTS)
    rows = generate(200, truth, seed=0, hazard_truth='beta2', shape_at=s)
    assert len(rows) == 200


# ---------------------------------------------------------------------------
# generate() with shape_at
# ---------------------------------------------------------------------------


def test_generate_backward_compat_default_path():
    """shape_at=None must produce byte-identical output to explicit static_shape."""
    truth = dict(TRUTH_DEFAULTS)
    rows_default = generate(500, truth, seed=0, hazard_truth='beta2')
    rows_static = generate(
        500, truth, seed=0, hazard_truth='beta2',
        shape_at=static_shape(truth['mu'], truth['kappa'], truth['w']),
    )
    assert rows_default == rows_static


def test_generate_haz1_ignores_shape_at():
    """shape_at is for the beta2 path; haz1 has no shape to drift."""
    truth = dict(TRUTH_DEFAULTS)
    drift = weight_drift_shape(truth['mu'], truth['kappa'],
                               truth['w'], [0.2, 0.8], halflife=50)
    rows_no = generate(200, truth, seed=0, hazard_truth='haz1')
    rows_with = generate(200, truth, seed=0, hazard_truth='haz1', shape_at=drift)
    assert rows_no == rows_with


def test_drift_data_has_within_segment_signature():
    """The whole point of the builders: data generated under drift should
    look statistically different early vs late, while static data should not.

    This is what the downstream tertile-PPC stat will detect. If this test
    fails, the synth isn't actually producing peak drift in the data."""
    truth = dict(TRUTH_DEFAULTS)
    # Force decent death rate so we have enough deaths to compare halves.
    truth['alpha_inf'] = 0.7
    truth['alpha_1'] = 1.5

    # Strong drift: peak shifts from low-s to high-s over the run.
    drift = strat_swap_shape(
        mu=[0.20, 0.80], kappa=[20.0, 20.0],
        w_before=[0.9, 0.1], w_after=[0.1, 0.9],
        n_swap=250, ramp_width=5,
    )

    N = 500
    rows_static = generate(N, truth, seed=7, hazard_truth='beta2')
    rows_drift  = generate(N, truth, seed=7, hazard_truth='beta2', shape_at=drift)

    def s_death_estimates(rows):
        """Approximate s_death by τ/median_T_clean per attempt."""
        survived = np.array([t for o, t in rows if o == 'survived'],
                            dtype=float)
        if len(survived) < 10:
            return None, None
        median_t = float(np.median(survived))
        died_idxs, s_vals = [], []
        for i, (o, t) in enumerate(rows):
            if o == 'died':
                tau = t - config.RESPAWN_MS
                if tau > 0:
                    died_idxs.append(i)
                    s_vals.append(min(max(tau / median_t, 0.0), 1.0))
        return np.array(died_idxs), np.array(s_vals)

    idx_st, s_st = s_death_estimates(rows_static)
    idx_dr, s_dr = s_death_estimates(rows_drift)
    assert s_st is not None and s_dr is not None

    # Compare mean s_death early-half vs late-half. Static should be ~equal;
    # drift should show a clear shift.
    def mean_halves(idxs, s, n):
        mid = n // 2
        early = s[idxs < mid]
        late = s[idxs >= mid]
        return float(early.mean()), float(late.mean())

    em_st, lm_st = mean_halves(idx_st, s_st, N)
    em_dr, lm_dr = mean_halves(idx_dr, s_dr, N)

    # Static: both halves should be similar (within 0.1 in mean s_death).
    # Generous gate — noise at N=500 with ~150 deaths is not tiny.
    assert abs(em_st - lm_st) < 0.1, (
        f"static data shows spurious within-run drift: early={em_st:.3f}, late={lm_st:.3f}"
    )
    # Drift: late should be substantially higher (peak moved from 0.2 to 0.8).
    assert lm_dr - em_dr > 0.2, (
        f"drift data does not show late > early shift as designed: "
        f"early={em_dr:.3f}, late={lm_dr:.3f}"
    )
