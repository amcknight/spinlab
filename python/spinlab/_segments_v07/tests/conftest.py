"""Shared fixtures for the test suite."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest

# Enable JAX float64 globally for tests -- without this, log_posterior values
# diverge from the numpy reference at the ~1e-3 level.
import jax
jax.config.update("jax_enable_x64", True)

from generate_synthetic_learning_v07 import TRUTH_DEFAULTS, generate


@pytest.fixture(scope='session')
def truth_theta():
    """V07 truth values as a length-10 log-space theta vector."""
    t = TRUTH_DEFAULTS
    return np.array([
        np.log(t['bpt']),
        np.log(t['sf_inf']),
        np.log(t['ssp_inf']),
        np.log(t['alpha_inf']),
        np.log(t['sf_1']),
        np.log(t['ssp_1']),
        np.log(t['alpha_1']),
        np.log(t['sf_halflife']),
        np.log(t['ssp_halflife']),
        np.log(t['alpha_halflife']),
    ])


@pytest.fixture(scope='session')
def synth_data_n500_seed0():
    """Standard V07 synth: 500 attempts, seed 0. Truth as per TRUTH_DEFAULTS."""
    return generate(500, TRUTH_DEFAULTS, seed=0)


@pytest.fixture(scope='session')
def synth_data_n50_seed0(synth_data_n500_seed0):
    """First 50 attempts of the standard synth. Use for warm-start tests."""
    return synth_data_n500_seed0[:50]
