"""NUTS posterior via NumPyro, wrapped around our existing JAX log_posterior.

Trick: our V07 `log_posterior(theta, time_ms, is_died)` already includes the
prior, so we can't let NumPyro impose its own prior on theta. We declare
theta with `dist.ImproperUniform` (zero log-density contribution) and inject
the full `log_posterior` value via `numpyro.factor`. NumPyro's NUTS sees the
right target log density and JAX autodiff supplies the gradient.

Public API:

    out = run_nuts(time_ms, is_died, model='haz1',
                   theta_init=<MAP>,
                   num_warmup=500, num_samples=1000, num_chains=2, seed=0)

    out['samples']: (num_samples * num_chains, n_params)
    out['samples_per_chain']: (num_chains, num_samples, n_params)
    out['rhat']:    (n_params,) Gelman-Rubin per dim   (NaN if num_chains == 1)
    out['ess']:     (n_params,) effective sample size  per dim
    out['wall_time_s']: float
    out['accept_prob']: float -- mean leaf acceptance over the run

Initialization: pass `theta_init` from `fit_jax.find_map(...)` to warm-start
the chain. NUTS warmup tunes mass matrix + step size; starting near the MAP
shortens warmup substantially.

Use cases (per memory/plan_forward_sequence):
  - Fallback when Laplace fails PD (the ridge case).
  - Honest band cross-check for PD cases.
  - Reusable building block for future hierarchical (NumPyro plate) work.
"""
from __future__ import annotations

import time

import jax
jax.config.update("jax_enable_x64", True)
import jax.numpy as jnp
import numpy as np
import numpyro
from numpyro.diagnostics import summary
import numpyro.distributions as dist
from numpyro.infer import MCMC, NUTS
from numpyro.infer.util import init_to_value

import learning_model_v07_jax as lm1
import learning_model_v07_beta2_jax as lm2
import fit_jax
import fit_jax_beta2


def _resolve_model(model):
    if model == 'haz1':
        return lm1, 10
    if model == 'beta2':
        return lm2, 15
    raise ValueError(f"model must be 'haz1' or 'beta2', got {model!r}")


def _make_numpyro_model(lm, time_ms, is_died, n_params, pool):
    """Closure: declare theta via ImproperUniform and add our log_posterior
    as a factor. The factor pattern is the canonical way to plug a custom
    differentiable density into NumPyro.

    pool is a length-6 tuple (sf_mean, sf_sigma, ssp_mean, ssp_sigma,
    alpha_mean, alpha_sigma) of pool overrides."""
    sf_m, sf_s, ssp_m, ssp_s, a_m, a_s = pool
    def model():
        theta = numpyro.sample(
            'theta',
            dist.ImproperUniform(
                support=dist.constraints.real_vector,
                batch_shape=(),
                event_shape=(n_params,),
            ),
        )
        lp = lm.log_posterior(
            theta, time_ms, is_died,
            halflife_sf_pop_mean=sf_m,     halflife_sf_pop_sigma=sf_s,
            halflife_ssp_pop_mean=ssp_m,   halflife_ssp_pop_sigma=ssp_s,
            halflife_alpha_pop_mean=a_m,   halflife_alpha_pop_sigma=a_s,
        )
        numpyro.factor('log_post', lp)
    return model


def run_nuts(time_ms, is_died, model='haz1', theta_init=None,
             num_warmup=500, num_samples=1000, num_chains=2, seed=0,
             halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
             halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
             halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None,
             target_accept=0.85, progress=False):
    """Run NUTS on our log_posterior.

    Args:
        time_ms, is_died: observed data, length N each.
        model: 'haz1' or 'beta2'.
        theta_init: starting theta for NUTS. If None, uses lm.initial_theta().
            Pass the MAP estimate for fast warmup.
        num_warmup, num_samples: NUTS budgets per chain.
        num_chains: number of parallel chains. R-hat is only meaningful when
            num_chains >= 2. Default 2.
        seed: PRNG seed.
        halflife_sf_pop_mean, halflife_sf_pop_sigma: EB pool overrides.
        target_accept: NUTS target acceptance probability.
        progress: show NumPyro's progress bar (per-chain).

    Returns dict (see module docstring).
    """
    lm, n_params = _resolve_model(model)

    time_ms_j = jnp.asarray(time_ms, dtype=jnp.float64)
    is_died_j = jnp.asarray(is_died, dtype=bool)

    if theta_init is None:
        theta_init = np.asarray(lm.initial_theta(), dtype=np.float64)
    theta_init = np.asarray(theta_init, dtype=np.float64)
    if theta_init.shape != (n_params,):
        raise ValueError(
            f"theta_init shape {theta_init.shape} != ({n_params},)"
        )

    def _resolve(val, default):
        return float(default if val is None else val)
    pool = (
        _resolve(halflife_sf_pop_mean,    lm.PRIOR_LOG_HALFLIFE_SF_MEAN),
        _resolve(halflife_sf_pop_sigma,   lm.HALFLIFE_SIGMA_MAX),
        _resolve(halflife_ssp_pop_mean,   lm.PRIOR_LOG_HALFLIFE_SSP_MEAN),
        _resolve(halflife_ssp_pop_sigma,  lm.HALFLIFE_SIGMA_MAX),
        _resolve(halflife_alpha_pop_mean, lm.PRIOR_LOG_HALFLIFE_ALPHA_MEAN),
        _resolve(halflife_alpha_pop_sigma, lm.HALFLIFE_SIGMA_MAX),
    )

    nuts_model = _make_numpyro_model(lm, time_ms_j, is_died_j, n_params, pool)

    kernel = NUTS(
        nuts_model,
        target_accept_prob=target_accept,
        init_strategy=init_to_value(values={'theta': jnp.asarray(theta_init)}),
    )
    # chain_method='sequential' is the only option that works on CPU
    # without multiprocessing complications on Windows.
    mcmc = MCMC(
        kernel,
        num_warmup=num_warmup,
        num_samples=num_samples,
        num_chains=num_chains,
        chain_method='sequential',
        progress_bar=progress,
    )

    t0 = time.time()
    mcmc.run(jax.random.PRNGKey(seed), extra_fields=('accept_prob',))
    wall = time.time() - t0

    samples_dict = mcmc.get_samples(group_by_chain=True)
    samples_chain = np.asarray(samples_dict['theta'])      # (C, S, P)
    samples_flat = samples_chain.reshape(-1, samples_chain.shape[-1])

    # Diagnostics: ESS and R-hat per dim. With 1 chain R-hat is meaningless.
    s = summary({'theta': samples_chain}, prob=0.9, group_by_chain=True)['theta']
    ess = np.asarray(s['n_eff'])
    rhat = (np.asarray(s['r_hat']) if num_chains >= 2
            else np.full(n_params, np.nan))

    # Mean acceptance probability across all draws (post-warmup).
    extras = mcmc.get_extra_fields(group_by_chain=True)
    accept_prob = (float(np.asarray(extras['accept_prob']).mean())
                   if 'accept_prob' in extras else float('nan'))

    return {
        'samples': samples_flat,
        'samples_per_chain': samples_chain,
        'rhat': rhat,
        'ess': ess,
        'wall_time_s': wall,
        'accept_prob': accept_prob,
        'theta_init': theta_init,
        'num_warmup': num_warmup,
        'num_samples': num_samples,
        'num_chains': num_chains,
    }


# ---------------------------------------------------------------------------
# Auto-fallback: Laplace where it's PD, NUTS where it isn't.
# ---------------------------------------------------------------------------


def posterior_samples(time_ms, is_died, theta_map, model='haz1',
                      n_samples=2000, seed=0,
                      halflife_sf_pop_mean=None, halflife_sf_pop_sigma=None,
                      halflife_ssp_pop_mean=None, halflife_ssp_pop_sigma=None,
                      halflife_alpha_pop_mean=None, halflife_alpha_pop_sigma=None,
                      nuts_warmup=300, nuts_samples=None, nuts_chains=2):
    """Posterior samples around theta_map; Laplace if PD, NUTS otherwise.

    The headline reason NUTS is in the stack (per plan_forward_sequence):
    when Laplace's Hessian goes non-PD (the ridge case — common at low N
    or on no-shape data for beta2), Laplace bands are inflated by Tikhonov
    regularization and not honest. NUTS doesn't care about the Hessian
    eigenstructure and gives the right posterior.

    Args:
        time_ms, is_died: observed data.
        theta_map: MAP estimate (from fit_jax.find_map or fit_jax_beta2).
        model: 'haz1' or 'beta2'.
        n_samples: how many samples to return total.
        seed: rng seed used by both paths.
        halflife_sf_pop_mean, halflife_sf_pop_sigma: pool overrides; pass the
            same values you passed to find_map.
        nuts_warmup, nuts_samples, nuts_chains: NUTS budgets used when the
            fallback fires. nuts_samples defaults to ceil(n_samples / nuts_chains).

    Returns dict:
        'samples': (n_samples, n_params)
        'source': 'laplace' or 'nuts'
        'laplace_pd': bool (whether Laplace was PD; False ⇒ fell back to NUTS)
        Plus NUTS diagnostics ('rhat', 'ess', etc.) when source == 'nuts'.
    """
    f = fit_jax if model == 'haz1' else fit_jax_beta2

    pool_kw = dict(
        halflife_sf_pop_mean=halflife_sf_pop_mean,
        halflife_sf_pop_sigma=halflife_sf_pop_sigma,
        halflife_ssp_pop_mean=halflife_ssp_pop_mean,
        halflife_ssp_pop_sigma=halflife_ssp_pop_sigma,
        halflife_alpha_pop_mean=halflife_alpha_pop_mean,
        halflife_alpha_pop_sigma=halflife_alpha_pop_sigma,
    )
    cov, ok = f.laplace_posterior(theta_map, time_ms, is_died, **pool_kw)

    if ok and cov is not None:
        samples = f.sample_laplace(theta_map, cov, n_samples=n_samples, seed=seed)
        return {
            'samples': np.asarray(samples),
            'source': 'laplace',
            'laplace_pd': True,
        }

    # PD failed (or Hessian non-finite) — fall back to NUTS.
    if nuts_samples is None:
        nuts_samples = (n_samples + nuts_chains - 1) // nuts_chains
    out = run_nuts(
        time_ms, is_died, model=model, theta_init=theta_map,
        num_warmup=nuts_warmup, num_samples=nuts_samples,
        num_chains=nuts_chains, seed=seed, **pool_kw,
    )
    # Trim or repeat to land exactly n_samples.
    s = out['samples']
    if s.shape[0] >= n_samples:
        s = s[:n_samples]
    return {
        **out,
        'samples': s,
        'source': 'nuts',
        'laplace_pd': False,
    }
