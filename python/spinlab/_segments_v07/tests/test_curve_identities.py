"""Mathematical identities of the V07 learning curve.

These are properties that MUST hold by construction:
  - At n=1, theta(1) == theta_1 (no decay).
  - As n -> inf, theta(n) -> theta_inf (asymptote).
  - At n=1+halflife, the remaining gap is exactly half of (theta_1 - theta_inf).
  - The curve is monotonic between theta_1 and theta_inf.
"""
import numpy as np
import pytest
import learning_model_v07 as lm


@pytest.fixture
def example_curve():
    """A non-trivial curve: theta_inf=0.05, theta_1=0.20, halflife=28."""
    return dict(log_theta_inf=np.log(0.05),
                log_theta_1=np.log(0.20),
                log_halflife=np.log(28.0))


def test_n1_returns_theta_1_exactly(example_curve):
    val = lm.learning_curve(n=1, **example_curve)
    assert np.isclose(val, example_curve['log_theta_1'])


def test_asymptote(example_curve):
    val = lm.learning_curve(n=100_000, **example_curve)
    assert np.isclose(val, example_curve['log_theta_inf'], atol=1e-9)


def test_halflife_semantics(example_curve):
    """At n=1+halflife, remaining gap should be exactly half the initial gap."""
    halflife = round(np.exp(example_curve['log_halflife']))
    n_hl = halflife + 1
    val = lm.learning_curve(n=n_hl, **example_curve)
    gap = val - example_curve['log_theta_inf']
    expected_gap = (example_curve['log_theta_1'] - example_curve['log_theta_inf']) * 0.5
    assert np.isclose(gap, expected_gap, rtol=1e-6)


def test_monotone_when_decreasing(example_curve):
    """theta_1 > theta_inf means the curve decreases monotonically toward theta_inf."""
    ns = np.array([1, 5, 20, 40, 100, 1000])
    vals = lm.learning_curve(n=ns, **example_curve)
    assert np.all(np.diff(vals) < 0), f"expected decreasing, got {vals}"


def test_monotone_when_increasing():
    """If log_1 < log_inf (anti-learning), curve increases monotonically."""
    vals = lm.learning_curve(
        log_theta_inf=np.log(0.20), log_theta_1=np.log(0.05),
        log_halflife=np.log(28.0), n=np.array([1, 5, 20, 40, 100, 1000]),
    )
    assert np.all(np.diff(vals) > 0)


def test_static_when_inf_equals_1():
    """log_1 == log_inf means a flat curve regardless of halflife."""
    same = np.log(0.10)
    for halflife in [1.0, 10.0, 1000.0]:
        vals = lm.learning_curve(
            log_theta_inf=same, log_theta_1=same,
            log_halflife=np.log(halflife), n=np.array([1, 5, 100, 10000]),
        )
        assert np.allclose(vals, same)
