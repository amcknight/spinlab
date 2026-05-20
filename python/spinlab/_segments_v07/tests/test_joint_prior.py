"""Behavior of the joint prior P(halflife | A).

The joint prior tightens halflife's sigma as |A| -> 0, so a posterior fit on
no-learning data should put halflife at the prior median rather than letting
it wander. See learning_model_v07.halflife_sigma() and external_docs/pgm_tricks.md.
"""
import numpy as np
import pytest

import learning_model_v07 as lm
import learning_model_v07_jax as lm_jx


def test_halflife_sigma_at_zero_amplitude():
    """At A=0, sigma collapses to HALFLIFE_SIGMA_MIN."""
    assert np.isclose(lm.halflife_sigma(0.0), lm.HALFLIFE_SIGMA_MIN)


def test_halflife_sigma_at_large_amplitude():
    """At |A| -> inf, sigma approaches HALFLIFE_SIGMA_MAX."""
    assert np.isclose(lm.halflife_sigma(10.0), lm.HALFLIFE_SIGMA_MAX, rtol=1e-6)
    assert np.isclose(lm.halflife_sigma(-10.0), lm.HALFLIFE_SIGMA_MAX, rtol=1e-6)


def test_halflife_sigma_is_smooth_and_monotone():
    """tanh smoothing: sigma is monotone in |A|, continuous everywhere."""
    A_vals = np.linspace(-3.0, 3.0, 50)
    sigmas = np.array([lm.halflife_sigma(A) for A in A_vals])
    abs_A = np.abs(A_vals)
    # Sort by |A| and confirm sigma is non-decreasing in |A|
    order = np.argsort(abs_A)
    sigmas_sorted = sigmas[order]
    assert np.all(np.diff(sigmas_sorted) >= -1e-9)


def test_jax_and_numpy_halflife_sigma_agree():
    for A in [0.0, 0.3, 1.0, -0.5, 2.5]:
        np_val = lm.halflife_sigma(A)
        jx_val = float(lm_jx.halflife_sigma(A))
        assert np.isclose(np_val, jx_val)


def test_autodiff_hessian_matches_fd_at_zero_amplitude():
    """Regression: the |A| cusp at A=0 used to corrupt JAX autodiff
    Hessians, making Laplace bands fail PD on well-spec haz1 fits.
    Smooth-abs (sqrt(A^2+eps^2)-eps) fixes it. See pgm_tricks.md
    "Smooth-once isn't smooth-twice".

    Construct theta where A_ssp = 0 exactly (the worst case) and verify
    autodiff Hessian agrees with central finite differences along every
    coordinate axis. Failure here = a non-C^2 spot has crept into the
    posterior somewhere.
    """
    import jax
    import jax.numpy as jnp

    from generate_synthetic_learning_v07 import TRUTH_DEFAULTS, generate
    import learning_model_v07_jax as lm_jx

    data = generate(100, TRUTH_DEFAULTS, seed=0)
    time_ms, is_died = lm_jx.data_to_arrays(data)

    # theta with A_ssp = 0 (log_ssp_1 == log_ssp_inf)
    t = TRUTH_DEFAULTS
    theta = np.array([
        np.log(t['bpt']), np.log(t['sf_inf']), np.log(t['ssp_inf']),
        np.log(t['alpha_inf']), np.log(t['sf_1']),
        np.log(t['ssp_inf']),                      # log_ssp_1 := log_ssp_inf
        np.log(t['alpha_1']),
        np.log(t['sf_halflife']), np.log(t['ssp_halflife']),
        np.log(t['alpha_halflife']),
    ])

    def nlp(th):
        return float(-lm_jx.log_posterior(jnp.asarray(th), time_ms, is_died))

    H_ad = np.asarray(
        jax.hessian(lambda th: -lm_jx.log_posterior(th, time_ms, is_died))(
            jnp.asarray(theta)
        )
    )

    nlp_0 = nlp(theta)
    eps = 1e-3
    # Diagonal FD only — full FD Hessian is O(N^2) calls; diagonal catches
    # the bug because |A_ssp| sits along (log_ssp_inf, log_ssp_1) which are
    # axis-aligned coordinates.
    for i in range(len(theta)):
        e = np.zeros_like(theta)
        e[i] = 1.0
        nlp_plus = nlp(theta + eps * e)
        nlp_minus = nlp(theta - eps * e)
        fd_diag = (nlp_plus + nlp_minus - 2 * nlp_0) / eps**2
        ad_diag = H_ad[i, i]
        # Tolerance: 5% relative or 0.5 absolute. Large abs tolerance for
        # tiny FD values where rounding noise dominates.
        rel = abs(ad_diag - fd_diag) / max(abs(fd_diag), 1.0)
        abs_err = abs(ad_diag - fd_diag)
        assert rel < 0.05 or abs_err < 0.5, (
            f"autodiff vs FD Hessian disagreement at dim {i}: "
            f"AD={ad_diag:+.3f}, FD={fd_diag:+.3f} "
            f"(rel={rel:.3f}, abs={abs_err:.3f}). "
            f"This usually means a non-C^2 function (abs, max, sqrt-near-0, "
            f"sharp where) has been introduced into the model."
        )


def test_no_learning_posterior_collapses_halflife():
    """The big behavioral test: at A=0 with no-learning data, log_posterior on a
    halflife grid should be MUCH tighter than the broad-marginal prior would allow.
    """
    from generate_synthetic_learning_v07 import TRUTH_DEFAULTS, generate

    # Synth with sf_1 = sf_inf etc. (no learning).
    no_learn = dict(TRUTH_DEFAULTS)
    no_learn['sf_1'] = no_learn['sf_inf']
    no_learn['ssp_1'] = no_learn['ssp_inf']
    no_learn['alpha_1'] = no_learn['alpha_inf']
    data = generate(500, no_learn, seed=0)
    hazard = lm.haz1()

    # Theta at truth (A=0 for sf), vary halflife_sf.
    theta_base = np.array([
        np.log(no_learn['bpt']),
        np.log(no_learn['sf_inf']),
        np.log(no_learn['ssp_inf']),
        np.log(no_learn['alpha_inf']),
        np.log(no_learn['sf_1']),     # = log sf_inf -> A_sf = 0
        np.log(no_learn['ssp_1']),
        np.log(no_learn['alpha_1']),
        np.log(no_learn['sf_halflife']),
        np.log(no_learn['ssp_halflife']),
        np.log(no_learn['alpha_halflife']),
    ])

    hl_grid = np.exp(np.linspace(np.log(3), np.log(300), 11))
    posts = []
    for hl in hl_grid:
        th = theta_base.copy()
        th[lm.IDX_LOG_HALFLIFE_SF] = np.log(hl)
        posts.append(lm.log_posterior(th, data, hazard))
    posts = np.array(posts) - np.max(posts)

    # The "within 3 nats" zone should be at most 2 grid cells wide (i.e. the
    # joint prior keeps halflife near its median when A=0).
    within_3 = np.sum(posts > -3.0)
    assert within_3 <= 3, (
        f"Expected joint prior to collapse halflife near its median at A=0, "
        f"but {within_3} grid cells are within 3 nats of best fit. "
        f"Posts: {posts}"
    )

    # The best-fit halflife should be close to the prior median (28 attempts).
    best_hl = hl_grid[np.argmax(posts)]
    assert 20 <= best_hl <= 50, f"best halflife at A=0 should be near 28, got {best_hl}"
