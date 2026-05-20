"""Empirical-Bayes pooling for the three halflives across many segments.

The lightest principled HYPER step (see external_docs/speed_and_techniques.md).
Point-estimates the population (mean, sigma) for log(halflife_sf),
log(halflife_ssp), and log(halflife_alpha) symmetrically, then refits each
segment with those as its prior. Iterates until convergence.

Standard scaling pattern for production hierarchical models: per-segment fits
remain independent and fully parallel, conditional on a small shared block of
point-estimated hyperparameters. To upgrade to fully Bayesian later, swap
this loop for NUTS over the joint hyper + per-segment posteriors.

Identification budget: pooling 3 latents adds 6 hyperparameters (3 means +
3 sigmas) shared across S segments — negligible relative to 10·S per-segment
params, big payoff when any segments are regime-1 in any of the curves.

Public API:
    Pool: namedtuple holding the 3 pool (mean, sigma) pairs.
    fit_eb_pool(segments, ...)         iterate EB until convergence.
    fit_independent(segments, ...)     baseline: each segment fit alone.
"""
from typing import NamedTuple

import numpy as np

import learning_model_v07 as lm
import fit_jax


class Pool(NamedTuple):
    """Empirical-Bayes pool: per-segment priors on log(halflife_<x>) for
    each of x in {sf, ssp, alpha}. Field naming follows
    `log_halflife_<x>_{mean,sigma}` so consumers can stay explicit."""
    log_halflife_sf_mean:    float
    log_halflife_sf_sigma:   float
    log_halflife_ssp_mean:   float
    log_halflife_ssp_sigma:  float
    log_halflife_alpha_mean: float
    log_halflife_alpha_sigma: float

    @classmethod
    def from_defaults(cls):
        """Pool == module defaults (no pooling). Sigma defaults to the
        broadest joint-prior σ (HALFLIFE_SIGMA_MAX) so the unpooled prior
        is reproduced exactly."""
        return cls(
            float(lm.PRIOR_LOG_HALFLIFE_SF_MEAN),    float(lm.HALFLIFE_SIGMA_MAX),
            float(lm.PRIOR_LOG_HALFLIFE_SSP_MEAN),   float(lm.HALFLIFE_SIGMA_MAX),
            float(lm.PRIOR_LOG_HALFLIFE_ALPHA_MEAN), float(lm.HALFLIFE_SIGMA_MAX),
        )

    def as_kwargs(self):
        """Splat into find_map/laplace_posterior kwargs."""
        return dict(
            halflife_sf_pop_mean=self.log_halflife_sf_mean,
            halflife_sf_pop_sigma=self.log_halflife_sf_sigma,
            halflife_ssp_pop_mean=self.log_halflife_ssp_mean,
            halflife_ssp_pop_sigma=self.log_halflife_ssp_sigma,
            halflife_alpha_pop_mean=self.log_halflife_alpha_mean,
            halflife_alpha_pop_sigma=self.log_halflife_alpha_sigma,
        )


# Index map: (pool-mean field, pool-sigma field, theta index)
_HALFLIFE_FIELDS = [
    ('log_halflife_sf_mean',    'log_halflife_sf_sigma',    lm.IDX_LOG_HALFLIFE_SF),
    ('log_halflife_ssp_mean',   'log_halflife_ssp_sigma',   lm.IDX_LOG_HALFLIFE_SSP),
    ('log_halflife_alpha_mean', 'log_halflife_alpha_sigma', lm.IDX_LOG_HALFLIFE_ALPHA),
]


def _fit_one(seg, pool, theta_init=None):
    """Fit one segment under the given pool prior. Returns (theta_map, info)."""
    import learning_model_v07_jax as lm_jx
    time_ms, is_died = lm_jx.data_to_arrays(seg['data'])
    return fit_jax.find_map(
        np.asarray(time_ms), np.asarray(is_died),
        theta_init=theta_init, **pool.as_kwargs(),
    )


def fit_independent(segments):
    """Fit each segment with module-default priors (no pooling). Baseline."""
    pool = Pool.from_defaults()
    maps, infos = [], []
    for seg in segments:
        theta_map, info = _fit_one(seg, pool)
        maps.append(theta_map)
        infos.append(info)
    return maps, infos


def fit_eb_pool(segments, max_iter=10, tol=1e-3, verbose=False,
                sigma_floor=0.05):
    """EB pool over all three halflives. One-step sigma + iterated mean.

    Why one-step sigma: the data on a single segment typically can't
    distinguish per-segment halflife noise from population spread (the
    within-segment Laplace SD is comparable to cross-segment MAP spread
    for ssp and alpha). Iterating the sigma estimate is then unstable —
    each iteration shrinks the MAPs toward the current pop mean, which
    shrinks std(MAPs), which shrinks sigma_pop, in a collapse to the
    floor. See pgm_tricks.md "EB sigma collapse" and memory/eb-pool-budget.

    Robust approach (this implementation):
      1. Stage 1: fit every segment under the broad default prior. The
         cross-MAP std is the moment estimate of sigma_pop. Pool that
         sigma in (one-step, never updated).
      2. Stage 2: iterate the pool mean only. Each iteration refits each
         segment under the fixed sigma_pop and recomputes mean(MAPs).
         Converges in a few iters and doesn't collapse.

    Args:
        segments: list of {'data': [...]} dicts.
        max_iter: cap on stage-2 iterations.
        tol: convergence threshold for the mean updates.
        sigma_floor: lower bound on sigma_pop. Defensive against degenerate
            populations (1 segment, all identical halflives by chance).
        verbose: print per-iteration trace.

    Returns:
        (pool, maps, history) where:
            pool: final Pool (sigmas fixed after stage 1; means iterated)
            maps: list of per-segment MAP thetas under the final pool
            history: list of Pool values; index 0 = defaults, 1 = stage 1
                fit (sigmas now fixed, means initialized), 2+ = stage-2 iters
    """
    # Stage 1: unpooled fits to estimate sigma_pop per latent.
    default_pool = Pool.from_defaults()
    history = [default_pool]
    stage1_maps = []
    for seg in segments:
        theta_map, _ = _fit_one(seg, default_pool)
        stage1_maps.append(theta_map)

    sigma_fixed = {}
    initial_means = {}
    for mean_name, sigma_name, idx in _HALFLIFE_FIELDS:
        logs = np.array([t[idx] for t in stage1_maps])
        # Raw std on broad-prior MAPs. Slightly overestimates the true
        # population SD (includes within-segment noise) but doesn't
        # pathologically collapse like the iterated estimator does.
        sigma_fixed[sigma_name] = max(float(logs.std()), sigma_floor)
        initial_means[mean_name] = float(logs.mean())

    pool = Pool(**initial_means, **sigma_fixed)
    history.append(pool)
    if verbose:
        print(f"  stage 1 (sigma fixed): "
              f"sf=({pool.log_halflife_sf_mean:+.3f}, {pool.log_halflife_sf_sigma:.3f})  "
              f"ssp=({pool.log_halflife_ssp_mean:+.3f}, {pool.log_halflife_ssp_sigma:.3f})  "
              f"alpha=({pool.log_halflife_alpha_mean:+.3f}, {pool.log_halflife_alpha_sigma:.3f})")

    # Stage 2: iterate the mean given fixed sigma.
    prev_maps = stage1_maps
    maps = stage1_maps
    for it in range(max_iter):
        maps = []
        for s_idx, seg in enumerate(segments):
            theta_map, _ = _fit_one(seg, pool, theta_init=prev_maps[s_idx])
            maps.append(theta_map)
        prev_maps = maps

        new_means = {}
        for mean_name, sigma_name, idx in _HALFLIFE_FIELDS:
            logs = np.array([t[idx] for t in maps])
            new_means[mean_name] = float(logs.mean())

        new_pool = Pool(**new_means, **sigma_fixed)
        max_delta = max(abs(getattr(new_pool, f) - getattr(pool, f))
                        for f in new_pool._fields)
        history.append(new_pool)
        if verbose:
            print(f"  stage 2 iter {it+1}: max_delta={max_delta:.4f}  "
                  f"sf=({new_pool.log_halflife_sf_mean:+.3f}, {new_pool.log_halflife_sf_sigma:.3f})  "
                  f"ssp=({new_pool.log_halflife_ssp_mean:+.3f}, {new_pool.log_halflife_ssp_sigma:.3f})  "
                  f"alpha=({new_pool.log_halflife_alpha_mean:+.3f}, {new_pool.log_halflife_alpha_sigma:.3f})")
        pool = new_pool
        if max_delta < tol:
            break

    return pool, maps, history
