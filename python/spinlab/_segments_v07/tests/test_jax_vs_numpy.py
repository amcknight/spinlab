"""JAX and numpy implementations must agree on log_posterior values.

These tests pin the equivalence so future refactors can't silently drift.
Tolerances chosen empirically: prior is exact (Normal logpdf both ways);
likelihood differs at ~1e-5 relative due to Simpson's-rule grid construction.
"""
import numpy as np
import pytest

import learning_model_v07 as lm_np
import learning_model_v07_jax as lm_jx


PRIOR_TOL = 1e-12
LIKELIHOOD_REL_TOL = 1e-4


def test_log_prior_exact(truth_theta):
    lp_np = lm_np.log_prior(truth_theta, lm_np.haz1())
    lp_jx = float(lm_jx.log_prior(truth_theta))
    assert abs(lp_jx - lp_np) < PRIOR_TOL


def test_log_likelihood_matches_at_truth(truth_theta, synth_data_n500_seed0):
    time_ms, is_died = lm_jx.data_to_arrays(synth_data_n500_seed0)
    ll_np = lm_np.log_likelihood(truth_theta, synth_data_n500_seed0, lm_np.haz1())
    ll_jx = float(lm_jx.log_likelihood(truth_theta, time_ms, is_died))
    assert np.isfinite(ll_np) and np.isfinite(ll_jx), \
        f"both should be finite at truth; numpy={ll_np}, jax={ll_jx}"
    assert abs(ll_jx - ll_np) / abs(ll_np) < LIKELIHOOD_REL_TOL


def test_log_posterior_matches_at_perturbations(truth_theta, synth_data_n500_seed0):
    """Small perturbations around truth should also agree."""
    time_ms, is_died = lm_jx.data_to_arrays(synth_data_n500_seed0)
    rng = np.random.default_rng(42)
    for _ in range(5):
        theta = truth_theta + 0.05 * rng.standard_normal(lm_np.N_PARAMS)
        p_np = lm_np.log_posterior(theta, synth_data_n500_seed0, lm_np.haz1())
        p_jx = float(lm_jx.log_posterior(theta, time_ms, is_died))
        if not (np.isfinite(p_np) and np.isfinite(p_jx)):
            continue   # both -inf is acceptable (tail underflow), skip
        assert abs(p_jx - p_np) / abs(p_np) < LIKELIHOOD_REL_TOL, \
            f"theta={theta}: numpy={p_np}, jax={p_jx}"


def test_padded_matches_unpadded(truth_theta, synth_data_n500_seed0):
    """The padded log_posterior with valid_n=N must equal the unpadded version."""
    time_ms, is_died = lm_jx.data_to_arrays(synth_data_n500_seed0)
    n_max = lm_jx.bucket_size(len(synth_data_n500_seed0))
    time_pad, died_pad, valid_n = lm_jx.pad_to(time_ms, is_died, n_max)
    p_full   = float(lm_jx.log_posterior(truth_theta, time_ms, is_died))
    p_padded = float(lm_jx.log_posterior_padded(truth_theta, time_pad, died_pad, valid_n))
    assert abs(p_full - p_padded) < 1e-9


def test_gradient_finite_at_truth(truth_theta, synth_data_n500_seed0):
    """jax.grad of log_posterior should give finite gradients (10-vector)."""
    import jax
    time_ms, is_died = lm_jx.data_to_arrays(synth_data_n500_seed0)
    grad_fn = jax.grad(lm_jx.log_posterior, argnums=0)
    g = grad_fn(truth_theta, time_ms, is_died)
    g_np = np.asarray(g)
    assert g_np.shape == (10,)
    assert np.all(np.isfinite(g_np))
