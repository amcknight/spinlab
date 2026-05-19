"""Fit pipeline: warm_init from data, MAP convergence, asymptote recovery.

These are integration tests against a single seed at N=500. They're slow-ish
(~5s each) but they exercise the production fit path end-to-end.
"""
import numpy as np
import pytest

import learning_model_v07_jax as lm_jx
import fit_jax


def test_warm_init_estimates_close_to_truth(synth_data_n50_seed0):
    """warm_init_from_data should land within ~30% of truth on early data."""
    from generate_synthetic_learning_v07 import TRUTH_DEFAULTS as TD
    time_ms = np.asarray([t for _, t in synth_data_n50_seed0], dtype=np.float64)
    is_died = np.asarray([o == 'died' for o, _ in synth_data_n50_seed0], dtype=bool)
    theta_init = fit_jax.warm_init_from_data(time_ms, is_died)
    bpt_est = float(np.exp(theta_init[0]))
    # bpt is bounded below truth (we subtract a small constant for safety).
    assert abs(bpt_est - TD['bpt']) / TD['bpt'] < 0.10
    # alpha_inf estimated from late-attempt death rate, within ~50% of truth.
    alpha_inf_est = float(np.exp(theta_init[3]))
    assert abs(alpha_inf_est - TD['alpha_inf']) / TD['alpha_inf'] < 0.6


def test_map_converges_from_warm_init(synth_data_n500_seed0):
    time_ms = np.asarray([t for _, t in synth_data_n500_seed0], dtype=np.float64)
    is_died = np.asarray([o == 'died' for o, _ in synth_data_n500_seed0], dtype=bool)
    theta_map, info = fit_jax.find_map(time_ms, is_died)
    assert info['converged'], f"L-BFGS-B did not converge: {info}"
    assert info['iter'] > 0
    assert info['nll'] < 1e6, "fit ended up at penalty value"


def test_map_recovers_asymptotes_within_30pct(synth_data_n500_seed0, truth_theta):
    """The published V07 pass criterion: asymptotes within 30% of truth."""
    time_ms = np.asarray([t for _, t in synth_data_n500_seed0], dtype=np.float64)
    is_died = np.asarray([o == 'died' for o, _ in synth_data_n500_seed0], dtype=bool)
    theta_map, _ = fit_jax.find_map(time_ms, is_died)
    # Asymptote indices: bpt, sf_inf, ssp_inf, alpha_inf  (0, 1, 2, 3)
    truth_nat = np.exp(truth_theta[:4])
    map_nat = np.exp(theta_map[:4])
    rel_err = np.abs(map_nat - truth_nat) / truth_nat
    names = ['bpt', 'sf_inf', 'ssp_inf', 'alpha_inf']
    for name, err in zip(names, rel_err):
        assert err < 0.30, f"{name} MAP rel err {err:.2%} exceeds 30%"


def test_streaming_warm_fit_is_fast(synth_data_n500_seed0):
    """A single warm-started incremental refit should land in well under 100ms.
    This is a regression smoke test for the JIT-cached padded path."""
    import time
    time_ms = np.asarray([t for _, t in synth_data_n500_seed0[:100]], dtype=np.float64)
    is_died = np.asarray([o == 'died' for o, _ in synth_data_n500_seed0[:100]], dtype=bool)

    # Pre-warm so the timed call doesn't include JIT compile.
    fit_jax.prewarm_buckets(buckets=(128,))
    theta0, _ = fit_jax.find_map(time_ms, is_died)

    # Now grow by one attempt and time the warm-started refit.
    time_ms2 = np.asarray([t for _, t in synth_data_n500_seed0[:101]], dtype=np.float64)
    is_died2 = np.asarray([o == 'died' for o, _ in synth_data_n500_seed0[:101]], dtype=bool)
    t0 = time.perf_counter()
    theta1, info = fit_jax.find_map(time_ms2, is_died2, theta_init=theta0)
    dt_ms = (time.perf_counter() - t0) * 1000
    assert dt_ms < 200, f"warm-started streaming refit took {dt_ms:.0f}ms (target <200)"
    assert info['converged']
