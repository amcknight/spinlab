"""NUTS wrapper tests.

Each NUTS test pays a one-time JIT/compile cost of ~25s. We use small
warmup/sample budgets and one or two synth setups to stay reasonable.
The Laplace cross-check is the load-bearing test: NUTS and Laplace
posterior SDs must agree per-dim on a well-identified problem.
"""
import numpy as np
import pytest

from generate_synthetic_learning_v07 import TRUTH_DEFAULTS, generate
import learning_model_v07_jax as lm1
import fit_jax
import fit_nuts


# Module-scoped fixtures so the JIT cache and one NUTS run are amortized
# across the assertions below.


@pytest.fixture(scope='module')
def synth_data_n300():
    return generate(300, TRUTH_DEFAULTS, seed=0, hazard_truth='haz1')


@pytest.fixture(scope='module')
def nuts_and_laplace(synth_data_n300):
    """One MAP, one Laplace, one NUTS run — reused across the SD-agreement
    and band-shape tests."""
    time_ms, is_died = lm1.data_to_arrays(synth_data_n300)
    theta_map, info = fit_jax.find_map(time_ms, is_died)
    cov, ok = fit_jax.laplace_posterior(theta_map, time_ms, is_died)
    out = fit_nuts.run_nuts(
        time_ms, is_died, model='haz1', theta_init=theta_map,
        num_warmup=300, num_samples=400, num_chains=2, seed=0,
    )
    return {
        'theta_map': np.asarray(theta_map),
        'cov': cov,
        'laplace_pd': ok,
        'nuts': out,
        'time_ms': np.asarray(time_ms),
        'is_died': np.asarray(is_died),
    }


# ---------------------------------------------------------------------------
# Shape and basic contract
# ---------------------------------------------------------------------------


def test_nuts_samples_shape(nuts_and_laplace):
    out = nuts_and_laplace['nuts']
    # haz1: 10 params, 2 chains x 400 samples = 800 flat draws
    assert out['samples'].shape == (800, 10)
    assert out['samples_per_chain'].shape == (2, 400, 10)
    assert out['ess'].shape == (10,)
    assert out['rhat'].shape == (10,)


def test_nuts_rejects_wrong_theta_init_shape():
    time_ms = np.array([10000.0, 12000.0, 11000.0, 9000.0])
    is_died = np.array([False, False, True, False])
    bad_init = np.zeros(7)
    with pytest.raises(ValueError, match='theta_init shape'):
        fit_nuts.run_nuts(time_ms, is_died, theta_init=bad_init,
                          num_warmup=50, num_samples=50)


# ---------------------------------------------------------------------------
# Convergence diagnostics
# ---------------------------------------------------------------------------


def test_nuts_converges(nuts_and_laplace):
    """R-hat < 1.05 on every dim, ESS > 50."""
    out = nuts_and_laplace['nuts']
    assert (out['rhat'] < 1.05).all(), f"rhat values {out['rhat']}"
    assert (out['ess'] > 50).all(), f"ess values {out['ess']}"


def test_nuts_accept_prob_reasonable(nuts_and_laplace):
    """Mean acceptance prob is reported and within (0, 1)."""
    out = nuts_and_laplace['nuts']
    assert 0.0 < out['accept_prob'] < 1.0


# ---------------------------------------------------------------------------
# Recovery + Laplace cross-check (the headline use case)
# ---------------------------------------------------------------------------


def test_nuts_posterior_mean_near_map(nuts_and_laplace):
    """On a well-identified N=300 problem the posterior should be roughly
    Gaussian and the NUTS posterior mean should sit within ~1 NUTS SD of
    the MAP on every dim. (Skew can shift the mean off the MAP, but not
    by more than the posterior width on identified dims.)"""
    out = nuts_and_laplace['nuts']
    map_theta = nuts_and_laplace['theta_map']
    nuts_mean = out['samples'].mean(axis=0)
    nuts_sd = out['samples'].std(axis=0)
    z = (nuts_mean - map_theta) / nuts_sd
    assert (np.abs(z) < 1.5).all(), (
        f"NUTS mean shifted > 1.5 SD from MAP on some dim: z={z}"
    )


def test_nuts_sds_agree_with_laplace(nuts_and_laplace):
    """Laplace SD vs NUTS SD per dim on the PD case should agree within
    ~50%. Tolerance is wide because (a) NUTS at S=800 has ~3% MC error on
    SD itself and (b) Laplace ignores posterior asymmetry. This catches
    big regressions (mis-scaled prior, wrong likelihood)."""
    assert nuts_and_laplace['laplace_pd'], (
        "N=300 well-spec haz1 fit should be Laplace PD; if this fails the "
        "PD assumption underlying the cross-check is wrong"
    )
    nuts_sd = nuts_and_laplace['nuts']['samples'].std(axis=0)
    lap_sd = np.sqrt(np.diag(nuts_and_laplace['cov']))
    rel = nuts_sd / lap_sd
    assert ((rel > 0.5) & (rel < 2.0)).all(), (
        f"NUTS / Laplace SD ratio out of [0.5, 2] on some dim: rel={rel}, "
        f"nuts_sd={nuts_sd}, lap_sd={lap_sd}"
    )


def test_posterior_samples_uses_laplace_when_pd(synth_data_n300):
    """In the PD case, the auto-fallback returns Laplace samples (cheap)."""
    time_ms, is_died = lm1.data_to_arrays(synth_data_n300)
    theta_map, _ = fit_jax.find_map(time_ms, is_died)
    out = fit_nuts.posterior_samples(
        time_ms, is_died, theta_map, model='haz1', n_samples=500, seed=0,
    )
    assert out['source'] == 'laplace'
    assert out['laplace_pd']
    assert out['samples'].shape == (500, 10)
