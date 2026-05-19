"""Empirical-Bayes halflife_sf pooling: does it recover the population mean,
and does it shrink a regime-1 segment toward that mean?

These are the two behaviors that justify HYPER-lite (see pgm_tricks.md and
external_docs/speed_and_techniques.md). If they don't hold, the EB code is
broken or the assumptions don't.
"""
import numpy as np
import pytest

import learning_model_v07 as lm
import fit_eb_pool as eb
from generate_synthetic_learning_v07 import (
    TRUTH_DEFAULTS, generate_multi_segment, generate,
)


@pytest.fixture(scope='module')
def pop_segments():
    """5 segments, halflife_sf truths drawn from Normal(log 50, 0.15) -- shifted
    from the prior median (log 28) so we can see the pool MOVE the prior.
    """
    return generate_multi_segment(
        n_segments=5, n_per_segment=300,
        true_pop_log_halflife_sf_mean=np.log(50.0),
        true_pop_log_halflife_sf_sigma=0.15,
        base_seed=1,
    )


def test_pool_recovers_population_mean(pop_segments):
    """After EB iteration, pool.mean should land near the true population
    log(50) ≈ 3.91, not the prior default log(28) ≈ 3.33.
    """
    pool, maps, history = eb.fit_eb_pool(pop_segments, max_iter=8)
    # Truth mean = log(50) ≈ 3.912
    assert 3.5 < pool.log_halflife_sf_mean < 4.2, (
        f"Pool mean {pool.log_halflife_sf_mean:.3f} (natural "
        f"{np.exp(pool.log_halflife_sf_mean):.1f}) is far from truth log(50)≈3.91"
    )
    # Sigma should be smallish (segments aren't wildly different)
    assert pool.log_halflife_sf_sigma < 0.5


def test_pool_converges_in_few_iters(pop_segments):
    """EB should reach tolerance within a handful of iterations."""
    pool, maps, history = eb.fit_eb_pool(pop_segments, max_iter=8, tol=1e-3)
    # Either converged before maxiter (history length <= maxiter) or final
    # delta tiny
    final_two = history[-2:]
    if len(final_two) == 2:
        delta_mean = abs(final_two[1].log_halflife_sf_mean
                         - final_two[0].log_halflife_sf_mean)
        assert delta_mean < 0.02, f"Final step still {delta_mean:.4f}"


def test_pool_shrinks_regime1_segment_toward_population_mean():
    """The killer use case: one regime-1 (no-learning) segment among many
    informative ones. Independent fit lands halflife near prior median (28).
    Pooled fit should pull it toward the population mean (~50).
    """
    # 4 informative segments with truth halflife=50, plus 1 regime-1 segment.
    informative = generate_multi_segment(
        n_segments=4, n_per_segment=300,
        true_pop_log_halflife_sf_mean=np.log(50.0),
        true_pop_log_halflife_sf_sigma=0.1,
        base_seed=2,
    )
    # Build the regime-1 segment manually: sf_1 = sf_inf (no sf learning).
    no_learn_truth = dict(TRUTH_DEFAULTS)
    no_learn_truth['sf_1'] = no_learn_truth['sf_inf']
    regime1 = {
        'data': generate(300, no_learn_truth, seed=999),
        'true_halflife_sf': float(no_learn_truth['sf_halflife']),
        'truth': no_learn_truth,
    }
    segments = informative + [regime1]

    # Baseline: independent fits
    indep_maps, _ = eb.fit_independent(segments)
    indep_hl_regime1 = float(np.exp(indep_maps[-1][lm.IDX_LOG_HALFLIFE_SF]))

    # Pooled fits
    pool, pooled_maps, _ = eb.fit_eb_pool(segments, max_iter=8)
    pooled_hl_regime1 = float(np.exp(pooled_maps[-1][lm.IDX_LOG_HALFLIFE_SF]))

    pop_target = float(np.exp(pool.log_halflife_sf_mean))

    # Independent regime-1 halflife should be near prior median (28).
    # Pooled regime-1 halflife should be near pop_target (~50).
    # The pooled value should be CLOSER to the pop_target than the independent
    # value is to the pop_target.
    indep_dist = abs(indep_hl_regime1 - pop_target)
    pooled_dist = abs(pooled_hl_regime1 - pop_target)
    assert pooled_dist < indep_dist, (
        f"Pool should pull regime-1 halflife toward population mean.\n"
        f"  population target: {pop_target:.1f}\n"
        f"  independent fit:   {indep_hl_regime1:.1f}  (distance {indep_dist:.1f})\n"
        f"  pooled fit:        {pooled_hl_regime1:.1f}  (distance {pooled_dist:.1f})"
    )


def _generate_population_on(halflife_key, true_pop_log_mean, true_pop_log_sigma,
                             n_segments=5, n_per_segment=300, base_seed=11):
    """Generate segments where a chosen halflife (sf, ssp, or alpha) varies
    per segment per the given log-Normal population; other halflives stay at
    TRUTH_DEFAULTS. Used to test pooling on ssp and alpha symmetrically.
    """
    rng = np.random.default_rng(base_seed)
    segments = []
    for s in range(n_segments):
        truth = dict(TRUTH_DEFAULTS)
        truth[halflife_key] = float(np.exp(rng.normal(true_pop_log_mean,
                                                      true_pop_log_sigma)))
        data = generate(n_per_segment, truth, seed=base_seed * 10_000 + s)
        segments.append({'data': data, 'truth': truth})
    return segments


@pytest.fixture(scope='module')
def pop_segments_ssp():
    return _generate_population_on('ssp_halflife',
                                    np.log(80.0), 0.15, base_seed=12)


@pytest.fixture(scope='module')
def pop_segments_alpha():
    return _generate_population_on('alpha_halflife',
                                    np.log(30.0), 0.15, base_seed=13)


def test_ssp_pool_recovers_population_mean(pop_segments_ssp):
    """Symmetric to test_pool_recovers_population_mean but on halflife_ssp.
    Truth pop log mean = log(80) ≈ 4.38; prior default for ssp is log(80*ln2) ≈ 4.0.
    The pool should land closer to the truth than the prior default.
    """
    pool, _, _ = eb.fit_eb_pool(pop_segments_ssp, max_iter=8)
    truth_log_mean = np.log(80.0)
    prior_default = float(lm.PRIOR_LOG_HALFLIFE_SSP_MEAN)
    # The pool should be at least as close to truth as the default would be.
    pool_to_truth = abs(pool.log_halflife_ssp_mean - truth_log_mean)
    default_to_truth = abs(prior_default - truth_log_mean)
    assert pool_to_truth < max(default_to_truth, 0.20), (
        f"ssp pool mean {pool.log_halflife_ssp_mean:.3f} (natural "
        f"{np.exp(pool.log_halflife_ssp_mean):.1f}) is farther from truth "
        f"log(80)≈{truth_log_mean:.3f} than the default {prior_default:.3f}"
    )
    assert pool.log_halflife_ssp_sigma < 0.5


def test_alpha_pool_recovers_population_mean(pop_segments_alpha):
    """Symmetric to test_pool_recovers_population_mean but on halflife_alpha.
    Truth pop log mean = log(30) ≈ 3.40.
    """
    pool, _, _ = eb.fit_eb_pool(pop_segments_alpha, max_iter=8)
    truth_log_mean = np.log(30.0)
    prior_default = float(lm.PRIOR_LOG_HALFLIFE_ALPHA_MEAN)
    pool_to_truth = abs(pool.log_halflife_alpha_mean - truth_log_mean)
    default_to_truth = abs(prior_default - truth_log_mean)
    assert pool_to_truth < max(default_to_truth, 0.20), (
        f"alpha pool mean {pool.log_halflife_alpha_mean:.3f} (natural "
        f"{np.exp(pool.log_halflife_alpha_mean):.1f}) is farther from truth "
        f"log(30)≈{truth_log_mean:.3f} than the default {prior_default:.3f}"
    )
    assert pool.log_halflife_alpha_sigma < 0.5


def test_ssp_pool_shrinks_regime1_segment():
    """Symmetric to the sf regime-1 shrinkage test, but for ssp.
    4 informative ssp-varying segments + 1 regime-1 (ssp_1 = ssp_inf) segment.
    The pooled ssp halflife on the regime-1 segment should land closer to
    the population mean than the independent fit does.
    """
    informative = _generate_population_on(
        'ssp_halflife', np.log(80.0), 0.1, n_segments=4, base_seed=21,
    )
    no_learn_truth = dict(TRUTH_DEFAULTS)
    no_learn_truth['ssp_1'] = no_learn_truth['ssp_inf']
    regime1 = {
        'data': generate(300, no_learn_truth, seed=99999),
        'truth': no_learn_truth,
    }
    segments = informative + [regime1]

    indep_maps, _ = eb.fit_independent(segments)
    pool, pooled_maps, _ = eb.fit_eb_pool(segments, max_iter=8)

    indep_hl = float(np.exp(indep_maps[-1][lm.IDX_LOG_HALFLIFE_SSP]))
    pooled_hl = float(np.exp(pooled_maps[-1][lm.IDX_LOG_HALFLIFE_SSP]))
    pop_target = float(np.exp(pool.log_halflife_ssp_mean))

    indep_dist = abs(indep_hl - pop_target)
    pooled_dist = abs(pooled_hl - pop_target)
    assert pooled_dist < indep_dist, (
        f"ssp pool should pull regime-1 toward population mean.\n"
        f"  pop target {pop_target:.1f}\n"
        f"  independent {indep_hl:.1f} (dist {indep_dist:.1f})\n"
        f"  pooled     {pooled_hl:.1f} (dist {pooled_dist:.1f})"
    )


def test_alpha_pool_shrinks_regime1_segment():
    """Symmetric: alpha curve has no learning, pool pulls toward pop mean."""
    informative = _generate_population_on(
        'alpha_halflife', np.log(30.0), 0.1, n_segments=4, base_seed=22,
    )
    no_learn_truth = dict(TRUTH_DEFAULTS)
    no_learn_truth['alpha_1'] = no_learn_truth['alpha_inf']
    regime1 = {
        'data': generate(300, no_learn_truth, seed=88888),
        'truth': no_learn_truth,
    }
    segments = informative + [regime1]

    indep_maps, _ = eb.fit_independent(segments)
    pool, pooled_maps, _ = eb.fit_eb_pool(segments, max_iter=8)

    indep_hl = float(np.exp(indep_maps[-1][lm.IDX_LOG_HALFLIFE_ALPHA]))
    pooled_hl = float(np.exp(pooled_maps[-1][lm.IDX_LOG_HALFLIFE_ALPHA]))
    pop_target = float(np.exp(pool.log_halflife_alpha_mean))

    indep_dist = abs(indep_hl - pop_target)
    pooled_dist = abs(pooled_hl - pop_target)
    assert pooled_dist < indep_dist, (
        f"alpha pool should pull regime-1 toward population mean.\n"
        f"  pop target {pop_target:.1f}\n"
        f"  independent {indep_hl:.1f} (dist {indep_dist:.1f})\n"
        f"  pooled     {pooled_hl:.1f} (dist {pooled_dist:.1f})"
    )


def test_all_three_pools_recover_shared_truth(pop_segments):
    """When only sf varies and ssp/alpha are at TRUTH_DEFAULTS for every
    segment, the pool should *recover the truth*, not stay at the prior.

    This used to assert "stays near default", but that was masking a bug
    in the iterative-sigma algorithm that collapsed sigma_pop and pinned
    means near the prior median. The one-step-sigma algorithm correctly
    drifts the mean to where the data says it should be — even when the
    truth happens to disagree with our prior median (the SSP prior median
    is log(28) but TRUTH_DEFAULTS['ssp_halflife'] is ~55).
    """
    pool, _, _ = eb.fit_eb_pool(pop_segments, max_iter=8)
    truth_ssp = np.log(TRUTH_DEFAULTS['ssp_halflife'])
    truth_alpha = np.log(TRUTH_DEFAULTS['alpha_halflife'])
    # Pool should be closer to truth than the prior default is to truth.
    ssp_to_truth = abs(pool.log_halflife_ssp_mean - truth_ssp)
    prior_to_truth = abs(float(lm.PRIOR_LOG_HALFLIFE_SSP_MEAN) - truth_ssp)
    assert ssp_to_truth < prior_to_truth, (
        f"ssp pool didn't move toward truth: pool={pool.log_halflife_ssp_mean:.3f}, "
        f"truth={truth_ssp:.3f}, default={float(lm.PRIOR_LOG_HALFLIFE_SSP_MEAN):.3f}"
    )
    alpha_to_truth = abs(pool.log_halflife_alpha_mean - truth_alpha)
    prior_alpha_to_truth = abs(float(lm.PRIOR_LOG_HALFLIFE_ALPHA_MEAN) - truth_alpha)
    # alpha truth IS approximately the prior median, so just check the pool
    # didn't wander.
    assert alpha_to_truth <= max(prior_alpha_to_truth + 0.05, 0.20)


def test_pool_with_one_segment_equals_independent_fit():
    """Degenerate case: 1 segment means EB has no extra info; the pool just
    becomes that segment's MAP. Result should match (approximately) the
    independent fit."""
    seg = generate_multi_segment(
        n_segments=1, n_per_segment=300,
        true_pop_log_halflife_sf_mean=np.log(50.0),
        true_pop_log_halflife_sf_sigma=0.1,
        base_seed=3,
    )
    indep_maps, _ = eb.fit_independent(seg)
    pool, pooled_maps, _ = eb.fit_eb_pool(seg, max_iter=8)
    # Single-segment EB pulls toward itself; should converge to roughly
    # the independent estimate.
    indep_hl = float(np.exp(indep_maps[0][lm.IDX_LOG_HALFLIFE_SF]))
    pooled_hl = float(np.exp(pooled_maps[0][lm.IDX_LOG_HALFLIFE_SF]))
    rel_diff = abs(pooled_hl - indep_hl) / max(indep_hl, 1e-6)
    assert rel_diff < 0.30, (
        f"Single-segment pool diverged from independent fit: "
        f"indep={indep_hl:.1f}, pooled={pooled_hl:.1f}, rel_diff={rel_diff:.2f}"
    )
