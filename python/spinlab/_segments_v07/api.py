"""v1 API serializer: turns fit results into the JSON payload shape that
`external_docs/api_contract.md` specifies. SpinLab calls these three
entry points; everything downstream is internal.

The serializer is intentionally thin — it composes existing modules
(`fit_jax`, `fit_nuts`, `fit_eb_pool`, `ppc`, `learning_model_v07`) and
shapes their outputs into the contract envelope. No new math.

Public API:
    fit_segment(attempts, segment_id=None, ...) -> dict     # cold fit
    refit_segment(attempts, prev_result=None, ...) -> dict  # streaming
    fit_pool(segments) -> dict                              # EB across many

Each returns a dict in the `segments-v1` schema. Use `json.dumps` to put
it on the wire.
"""
from __future__ import annotations

import time

import numpy as np

import config
import fit_jax
import fit_nuts
import fit_eb_pool
import learning_model_v07 as lm_np
import ppc as ppc_mod


SCHEMA = 'segments-v1'

# Threshold for PPC tension. p_two_sided below this fires the
# ppc_tension_<stat> caveat and the aggregate status.ppc_tension flag.
# Matches V1_ESSENCE caveat #5.
PPC_TENSION_THRESHOLD = 0.01

# Below this N, Laplace bands are typically Hessian-ridge-y; the
# `low_n` caveat tells the UI to widen heuristically or expect NUTS.
LOW_N_THRESHOLD = 22

# Latent names (parallel to lm_np.IDX_LOG_*). Order matters — must
# match the V07 theta layout.
_LATENT_LOG_NAMES = [
    'log_bpt',
    'log_sf_inf',  'log_ssp_inf',  'log_alpha_inf',
    'log_sf_1',    'log_ssp_1',    'log_alpha_1',
    'log_hl_sf',   'log_hl_ssp',   'log_hl_alpha',
]
_LATENT_NATURAL_NAMES = [
    'bpt_ms',
    'sf_inf',      'ssp_inf',      'alpha_inf',
    'sf_1',        'ssp_1',        'alpha_1',
    'halflife_sf', 'halflife_ssp', 'halflife_alpha',
]

# Index of log_hl_ssp in the 10-vector — used for band-suppression.
_IDX_LOG_HL_SSP = _LATENT_LOG_NAMES.index('log_hl_ssp')


# ---------------------------------------------------------------------------
# Input adapters
# ---------------------------------------------------------------------------


def _attempts_to_arrays(attempts):
    """Accept either tuples ('survived'|'died', time_ms) or dicts
    {"outcome": ..., "time_ms": ...} from SpinLab's schema. Returns
    (time_ms, is_died) as numpy float64 / bool arrays."""
    n = len(attempts)
    times = np.empty(n, dtype=np.float64)
    died = np.empty(n, dtype=bool)
    for i, row in enumerate(attempts):
        if isinstance(row, dict):
            outcome = row['outcome']
            t = row['time_ms']
        else:
            outcome, t = row
        times[i] = float(t)
        died[i] = (outcome == 'died')
    return times, died


def _prev_theta_from_result(prev_result):
    """Extract a warm-start theta from a previous payload, or None."""
    if prev_result is None:
        return None
    try:
        log_theta = prev_result['result']['map']['log_theta']
    except (KeyError, TypeError):
        return None
    return np.asarray(log_theta, dtype=np.float64)


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------


def _build_map(theta_log):
    natural_vals = np.exp(np.asarray(theta_log, dtype=np.float64))
    return {
        'log_theta': [float(v) for v in theta_log],
        'natural':   {k: float(v) for k, v in zip(_LATENT_NATURAL_NAMES, natural_vals)},
    }


def _build_bands(samples, pool_active):
    """Per-latent 5/50/95 log-space percentiles. Returns dict keyed by
    `_LATENT_LOG_NAMES`. `log_hl_ssp` is set to None when pool_active
    (V1_ESSENCE caveat #1)."""
    samples = np.asarray(samples, dtype=np.float64)
    pcts = np.percentile(samples, [5, 50, 95], axis=0)   # (3, 10)
    out = {}
    for i, name in enumerate(_LATENT_LOG_NAMES):
        if pool_active and i == _IDX_LOG_HL_SSP:
            out[name] = None
        else:
            out[name] = {
                'p5':  float(pcts[0, i]),
                'p50': float(pcts[1, i]),
                'p95': float(pcts[2, i]),
            }
    return out


def _m_clear_samples_haz1(samples, n_next):
    """Vectorized M_clear over posterior samples at the given attempt n,
    closed-form for haz1.

    For constant hazard α on [0, 1]:
        p_surv = exp(-α)
        E[s | died] = (1 - (1+α)·exp(-α)) / (α · (1 - exp(-α)))
        E[T_clean] = bpt + sf(n)·bpt = bpt·(1+sf(n))
        E[wall | died] = E[s|died] · E[T_clean] + RESPAWN_MS
        M_clear = ((1-p_surv)/p_surv) · E[wall|died] + E[T_clean]

    The numpy reference (`model.m_clear`) does this via Simpson quadrature
    with n_grid=200 — fine for offline plots but ~12 ms per call at S=200,
    which dominates the live streaming budget. Closed form is ~0.1 ms.
    Tests pin the two to ~1e-8 agreement.
    """
    th = np.asarray(samples, dtype=np.float64)        # (S, 10)
    bpt = np.exp(th[:, lm_np.IDX_LOG_BPT])

    # Per-sample sf(n), alpha(n) via the V07 learning curve.
    def _at_n(idx_inf, idx_1, idx_hl):
        log_inf = th[:, idx_inf]
        log_1   = th[:, idx_1]
        hl      = np.exp(th[:, idx_hl])
        decay   = 2.0 ** (-(float(n_next) - 1.0) / hl)
        return np.exp(log_inf + (log_1 - log_inf) * decay)

    sf_n    = _at_n(lm_np.IDX_LOG_SF_INF,    lm_np.IDX_LOG_SF_1,    lm_np.IDX_LOG_HALFLIFE_SF)
    alpha_n = _at_n(lm_np.IDX_LOG_ALPHA_INF, lm_np.IDX_LOG_ALPHA_1, lm_np.IDX_LOG_HALFLIFE_ALPHA)

    p_surv = np.exp(-alpha_n)
    e_clean = bpt * (1.0 + sf_n)

    # E[s | died] for haz1. Safe at small α via expansion: 1/(α(1-e^-α)) → 1/α²
    # but we don't hit it in practice (alpha bounded away from 0 by the prior).
    one_minus_p_surv = 1.0 - p_surv
    mean_s_died = np.where(
        alpha_n > 1e-8,
        (1.0 - (1.0 + alpha_n) * p_surv) / (alpha_n * one_minus_p_surv),
        0.5,   # limit as α → 0: uniform death location on [0, 1]
    )

    e_died_wall = mean_s_died * e_clean + config.RESPAWN_MS
    # Survival certainty short-circuit: if p_surv → 1, M_clear → e_clean.
    return np.where(
        p_surv >= 1.0,
        e_clean,
        (one_minus_p_surv / np.maximum(p_surv, 1e-300)) * e_died_wall + e_clean,
    )


def _death_rate_next(theta_log, n_next):
    """1 - exp(-alpha(n_next)). Closed-form from the learning curve."""
    log_alpha_inf = theta_log[lm_np.IDX_LOG_ALPHA_INF]
    log_alpha_1   = theta_log[lm_np.IDX_LOG_ALPHA_1]
    log_hl_alpha  = theta_log[lm_np.IDX_LOG_HALFLIFE_ALPHA]
    halflife = np.exp(log_hl_alpha)
    decay = 2.0 ** (-(float(n_next) - 1.0) / halflife)
    log_alpha_n = log_alpha_inf + (log_alpha_1 - log_alpha_inf) * decay
    return float(1.0 - np.exp(-np.exp(log_alpha_n)))


def _build_derived(theta_map, samples, n_next):
    mc = _m_clear_samples_haz1(samples, n_next)
    mc_finite = mc[np.isfinite(mc)]
    if len(mc_finite) == 0:
        m_clear = {'median_ms': float('nan'), 'p5_ms': float('nan'), 'p95_ms': float('nan')}
    else:
        m_clear = {
            'median_ms': float(np.percentile(mc_finite, 50)),
            'p5_ms':     float(np.percentile(mc_finite, 5)),
            'p95_ms':    float(np.percentile(mc_finite, 95)),
        }
    return {
        'M_clear':         m_clear,
        'death_rate_next': _death_rate_next(theta_map, n_next),
    }


def _build_ppc(samples, time_ms, is_died, n_ppc_samples=500, seed=0):
    """Run PPC on a subsample of posterior draws for speed. The contract
    only exposes (obs, p_two_sided); the full sims arrays are dropped."""
    sub = samples if len(samples) <= n_ppc_samples else samples[:n_ppc_samples]
    res = ppc_mod.run_ppc(sub, time_ms, is_died, model='haz1', seed=seed)
    return {
        name: {
            'obs':         float(r['obs']),
            'p_two_sided': float(r['p_two_sided']),
        }
        for name, r in res.items()
    }


def _build_status(*, converged, band_source, laplace_pd, ppc_tension):
    return {
        'converged':   bool(converged),
        'band_source': band_source,
        'laplace_pd':  bool(laplace_pd),
        'ppc_tension': bool(ppc_tension),
        'fittable':    bool(converged and not ppc_tension),
    }


def _collect_caveats(*, status, ppc_block, n_attempts, pool_active):
    out = []
    if not status['converged']:
        out.append('unconverged')
    if status['band_source'] == 'nuts':
        out.append('nuts_fallback')
    if pool_active:
        out.append('ssp_band_suppressed')
    for stat_name, p in ppc_block.items():
        if p['p_two_sided'] < PPC_TENSION_THRESHOLD:
            out.append(f'ppc_tension_{stat_name}')
    if n_attempts < LOW_N_THRESHOLD:
        out.append('low_n')
    return out


def _empty_segment_envelope(segment_id, n_attempts, wall_time_s):
    """Skeleton for non-converged or pre-fit-failure cases."""
    return {
        'schema':       SCHEMA,
        'kind':         'segment_fit',
        'segment_id':   segment_id,
        'n_attempts':   int(n_attempts),
        'model':        'haz1',
        'wall_time_s':  float(wall_time_s),
        'status':       _build_status(
            converged=False, band_source='none',
            laplace_pd=False, ppc_tension=False,
        ),
        'result':       {},
        'caveats':      ['unconverged'],
    }


# ---------------------------------------------------------------------------
# Shared fit-and-serialize core
# ---------------------------------------------------------------------------


def _fit_and_serialize(time_ms, is_died, *, segment_id, theta_init,
                       pool, run_ppc, nuts_on_nonpd, n_samples, seed,
                       t0):
    """Fit one segment and return its segment_fit envelope. The two
    public segment entry points are thin wrappers around this."""
    pool_kw = pool.as_kwargs() if pool is not None else {}
    pool_active = pool is not None

    theta_map, info = fit_jax.find_map(
        time_ms, is_died, theta_init=theta_init, **pool_kw,
    )
    converged = bool(info.get('converged', False))

    if not converged:
        env = _empty_segment_envelope(segment_id, len(time_ms),
                                       time.time() - t0)
        return env

    # Bands: NUTS fallback path vs direct Laplace path.
    if nuts_on_nonpd:
        post = fit_nuts.posterior_samples(
            time_ms, is_died, theta_map, model='haz1',
            n_samples=n_samples, seed=seed, **pool_kw,
        )
        samples = np.asarray(post['samples'])
        band_source = post['source']               # 'laplace' or 'nuts'
        laplace_pd = bool(post.get('laplace_pd', True))
    else:
        # Live-cadence path: stick with Laplace even when non-PD (uses
        # the Tikhonov-regularized cov from fit_jax). Faster; bands may
        # be inflated but that's documented behavior.
        cov, pd = fit_jax.laplace_posterior(
            theta_map, time_ms, is_died, **pool_kw,
        )
        if cov is None:
            # Degenerate Hessian — flag unconverged rather than emit
            # nonsense bands.
            env = _empty_segment_envelope(segment_id, len(time_ms),
                                           time.time() - t0)
            env['caveats'] = ['unconverged']
            return env
        samples = fit_jax.sample_laplace(theta_map, cov,
                                          n_samples=n_samples, seed=seed)
        band_source = 'laplace'
        laplace_pd = bool(pd)

    map_block     = _build_map(theta_map)
    bands_block   = _build_bands(samples, pool_active=pool_active)
    derived_block = _build_derived(theta_map, samples, n_next=len(time_ms) + 1)
    ppc_block     = _build_ppc(samples, time_ms, is_died, seed=seed) if run_ppc else {}

    ppc_tension = any(
        p['p_two_sided'] < PPC_TENSION_THRESHOLD for p in ppc_block.values()
    )

    status = _build_status(
        converged=converged, band_source=band_source,
        laplace_pd=laplace_pd, ppc_tension=ppc_tension,
    )
    caveats = _collect_caveats(
        status=status, ppc_block=ppc_block,
        n_attempts=len(time_ms), pool_active=pool_active,
    )
    return {
        'schema':       SCHEMA,
        'kind':         'segment_fit',
        'segment_id':   segment_id,
        'n_attempts':   int(len(time_ms)),
        'model':        'haz1',
        'wall_time_s':  float(time.time() - t0),
        'status':       status,
        'result': {
            'map':     map_block,
            'bands':   bands_block,
            'derived': derived_block,
            'ppc':     ppc_block,
        },
        'caveats':      caveats,
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def fit_segment(attempts, segment_id=None, *, pool=None,
                run_ppc=True, n_samples=2000, seed=0):
    """Cold fit: no warm start, NUTS fallback enabled, PPC on. Suitable
    for first-call or post-session refresh. Returns the segment_fit
    payload (see external_docs/api_contract.md)."""
    t0 = time.time()
    time_ms, is_died = _attempts_to_arrays(attempts)
    return _fit_and_serialize(
        time_ms, is_died,
        segment_id=segment_id,
        theta_init=None,            # cold: fit_jax warms from data
        pool=pool,
        run_ppc=run_ppc,
        nuts_on_nonpd=True,
        n_samples=n_samples,
        seed=seed,
        t0=t0,
    )


def refit_segment(attempts, segment_id=None, prev_result=None, *,
                  pool=None, nuts_on_nonpd=False, run_ppc=False,
                  n_samples=2000, seed=0):
    """Streaming refit. Warm-starts from prev_result if given. Defaults
    to the live cadence: no NUTS fallback (would blow the 8 ms budget),
    no PPC (skip the simulator). Periodic refreshes should pass
    nuts_on_nonpd=True and run_ppc=True."""
    t0 = time.time()
    time_ms, is_died = _attempts_to_arrays(attempts)
    return _fit_and_serialize(
        time_ms, is_died,
        segment_id=segment_id,
        theta_init=_prev_theta_from_result(prev_result),
        pool=pool,
        run_ppc=run_ppc,
        nuts_on_nonpd=nuts_on_nonpd,
        n_samples=n_samples,
        seed=seed,
        t0=t0,
    )


def fit_pool(segments, *, run_ppc=True, n_samples=2000, seed=0,
             max_iter=10, tol=1e-3):
    """Multi-segment empirical-Bayes pool fit. Each segment in the input
    must be a dict with 'attempts' and optional 'segment_id'. Returns a
    pool_fit envelope wrapping the learned pool + per-segment fits
    refit under the pool prior.

    Heavy operation (minutes) — appropriate for daily background jobs."""
    t0 = time.time()

    seg_inputs = []
    seg_ids = []
    seg_time_ms = []
    seg_is_died = []
    for s in segments:
        attempts = s['attempts']
        seg_ids.append(s.get('segment_id'))
        time_ms, is_died = _attempts_to_arrays(attempts)
        seg_time_ms.append(time_ms)
        seg_is_died.append(is_died)
        seg_inputs.append({'data': [
            (row['outcome'] if isinstance(row, dict) else row[0],
             row['time_ms'] if isinstance(row, dict) else row[1])
            for row in attempts
        ]})

    pool, _maps, _hist = fit_eb_pool.fit_eb_pool(
        seg_inputs, max_iter=max_iter, tol=tol,
    )

    # Now serialize each segment under the pool prior using the
    # shared fit-and-serialize path. We re-run find_map for each segment
    # so we get per-segment info dicts and consistent samples.
    seg_envelopes = []
    for sid, time_ms, is_died in zip(seg_ids, seg_time_ms, seg_is_died):
        env = _fit_and_serialize(
            time_ms, is_died,
            segment_id=sid,
            theta_init=None,
            pool=pool,
            run_ppc=run_ppc,
            nuts_on_nonpd=True,
            n_samples=n_samples,
            seed=seed,
            t0=time.time(),         # per-segment wall time
        )
        # Strip the outer wrapper — pool envelopes nest just status+result
        # per the contract. Keep segment_id at the entry level for the UI.
        seg_envelopes.append({
            'segment_id': env['segment_id'],
            'status':     env['status'],
            'result':     env['result'],
            'caveats':    env['caveats'],
        })

    pool_block = {
        'halflife_sf':     {'mean': float(pool.log_halflife_sf_mean),
                            'sigma': float(pool.log_halflife_sf_sigma)},
        'halflife_ssp':    {'mean': float(pool.log_halflife_ssp_mean),
                            'sigma': float(pool.log_halflife_ssp_sigma)},
        'halflife_alpha':  {'mean': float(pool.log_halflife_alpha_mean),
                            'sigma': float(pool.log_halflife_alpha_sigma)},
        'n_segments_used': len(seg_envelopes),
    }

    # Roll up status and caveats per the contract.
    all_converged   = all(e['status']['converged']   for e in seg_envelopes)
    all_fittable    = all(e['status']['fittable']    for e in seg_envelopes)
    any_ppc_tension = any(e['status']['ppc_tension'] for e in seg_envelopes)
    any_laplace_pd_false = any(not e['status']['laplace_pd'] for e in seg_envelopes)
    any_nuts        = any(e['status']['band_source'] == 'nuts' for e in seg_envelopes)
    rollup_status = {
        'converged':   all_converged,
        'band_source': 'nuts' if any_nuts else 'laplace',
        'laplace_pd':  not any_laplace_pd_false,
        'ppc_tension': any_ppc_tension,
        'fittable':    all_fittable,
    }
    rollup_caveats = sorted({c for e in seg_envelopes for c in e['caveats']})

    return {
        'schema':       SCHEMA,
        'kind':         'pool_fit',
        'segment_id':   None,
        'n_attempts':   int(sum(len(t) for t in seg_time_ms)),
        'model':        'haz1',
        'wall_time_s':  float(time.time() - t0),
        'status':       rollup_status,
        'result': {
            'pool':     pool_block,
            'segments': seg_envelopes,
        },
        'caveats':      rollup_caveats,
    }
