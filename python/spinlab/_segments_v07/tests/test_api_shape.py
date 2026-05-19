"""Shape-locking tests for the v1 JSON contract serializer.

These tests enforce that `api.fit_segment` / `refit_segment` / `fit_pool`
emit payloads matching `external_docs/api_contract.md`. Numerics are NOT
pinned here — that's the validation harness's job (test_validation_harness).
This file catches structural drift: a missing key, a renamed field, a
non-JSON-serializable value, a forgotten caveat.

If a contract change breaks these tests, either bump `SCHEMA` or fix the
serializer; don't relax the assertion silently.
"""
import json

import pytest

import segments_v07 as sv
from generate_synthetic_learning_v07 import generate, TRUTH_DEFAULTS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope='module')
def attempts_n40():
    """40 haz1-truth attempts. Big enough to converge, small enough to be fast."""
    data = generate(40, TRUTH_DEFAULTS, seed=0, hazard_truth='haz1')
    return [{'outcome': o, 'time_ms': int(t)} for o, t in data]


@pytest.fixture(scope='module')
def attempts_n15():
    """Low-N case — exercises the `low_n` caveat path."""
    data = generate(15, TRUTH_DEFAULTS, seed=0, hazard_truth='haz1')
    return [{'outcome': o, 'time_ms': int(t)} for o, t in data]


@pytest.fixture(scope='module')
def cold_fit(attempts_n40):
    return sv.fit_segment(attempts_n40, segment_id='toy-A', n_samples=200)


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


TOP_LEVEL_KEYS = {
    'schema', 'kind', 'segment_id', 'n_attempts', 'model',
    'wall_time_s', 'status', 'result', 'caveats',
}


def test_envelope_keys(cold_fit):
    assert set(cold_fit.keys()) == TOP_LEVEL_KEYS
    assert cold_fit['schema'] == 'segments-v1'
    assert cold_fit['kind'] == 'segment_fit'
    assert cold_fit['segment_id'] == 'toy-A'
    assert cold_fit['n_attempts'] == 40
    assert cold_fit['model'] == 'haz1'
    assert isinstance(cold_fit['wall_time_s'], float)
    assert isinstance(cold_fit['caveats'], list)


def test_status_shape(cold_fit):
    s = cold_fit['status']
    assert set(s.keys()) == {
        'converged', 'band_source', 'laplace_pd', 'ppc_tension', 'fittable',
    }
    assert isinstance(s['converged'], bool)
    assert s['band_source'] in ('laplace', 'nuts', 'none')
    assert isinstance(s['laplace_pd'], bool)
    assert isinstance(s['ppc_tension'], bool)
    assert s['fittable'] == (s['converged'] and not s['ppc_tension'])


def test_result_shape(cold_fit):
    r = cold_fit['result']
    assert set(r.keys()) == {'map', 'bands', 'derived', 'ppc'}


# ---------------------------------------------------------------------------
# Result blocks
# ---------------------------------------------------------------------------


EXPECTED_LATENT_NAMES = [
    'log_bpt',
    'log_sf_inf',  'log_ssp_inf',  'log_alpha_inf',
    'log_sf_1',    'log_ssp_1',    'log_alpha_1',
    'log_hl_sf',   'log_hl_ssp',   'log_hl_alpha',
]
EXPECTED_NATURAL_NAMES = [
    'bpt_ms',
    'sf_inf',      'ssp_inf',      'alpha_inf',
    'sf_1',        'ssp_1',        'alpha_1',
    'halflife_sf', 'halflife_ssp', 'halflife_alpha',
]


def test_map_block(cold_fit):
    m = cold_fit['result']['map']
    assert set(m.keys()) == {'log_theta', 'natural'}
    assert len(m['log_theta']) == 10
    assert all(isinstance(v, float) for v in m['log_theta'])
    assert set(m['natural'].keys()) == set(EXPECTED_NATURAL_NAMES)


def test_bands_block(cold_fit):
    b = cold_fit['result']['bands']
    assert set(b.keys()) == set(EXPECTED_LATENT_NAMES)
    for name, entry in b.items():
        if entry is None:
            continue
        assert set(entry.keys()) == {'p5', 'p50', 'p95'}
        assert entry['p5'] <= entry['p50'] <= entry['p95']


def test_derived_block(cold_fit):
    d = cold_fit['result']['derived']
    assert set(d.keys()) == {'M_clear', 'death_rate_next'}
    mc = d['M_clear']
    assert set(mc.keys()) == {'median_ms', 'p5_ms', 'p95_ms'}
    assert mc['p5_ms'] <= mc['median_ms'] <= mc['p95_ms']
    assert 0.0 <= d['death_rate_next'] <= 1.0


def test_ppc_block(cold_fit):
    p = cold_fit['result']['ppc']
    assert set(p.keys()) == {
        'died_rate', 'died_tau_skew', 'died_tau_kurt', 'died_s_mid_third',
    }
    for stat_name, entry in p.items():
        assert set(entry.keys()) == {'obs', 'p_two_sided'}
        assert 0.0 <= entry['p_two_sided'] <= 1.0


# ---------------------------------------------------------------------------
# Caveats + entry-point-specific behavior
# ---------------------------------------------------------------------------


def test_low_n_caveat_fires(attempts_n15):
    res = sv.fit_segment(attempts_n15, segment_id='toy-B', n_samples=200)
    assert 'low_n' in res['caveats']


def test_refit_warm_start_returns_same_shape(cold_fit, attempts_n40):
    # Add an extra attempt then refit
    extra = generate(41, TRUTH_DEFAULTS, seed=0, hazard_truth='haz1')
    attempts2 = [{'outcome': o, 'time_ms': int(t)} for o, t in extra]
    res = sv.refit_segment(attempts2, segment_id='toy-A',
                            prev_result=cold_fit, n_samples=200)
    assert set(res.keys()) == TOP_LEVEL_KEYS
    assert res['n_attempts'] == 41
    assert res['kind'] == 'segment_fit'


def test_pool_envelope():
    segs = []
    for i in range(3):
        d = generate(40, TRUTH_DEFAULTS, seed=i, hazard_truth='haz1')
        segs.append({
            'segment_id': f'seg-{i}',
            'attempts': [{'outcome': o, 'time_ms': int(t)} for o, t in d],
        })
    res = sv.fit_pool(segs, n_samples=150, max_iter=3)
    assert res['schema'] == 'segments-v1'
    assert res['kind'] == 'pool_fit'
    assert res['segment_id'] is None

    pool = res['result']['pool']
    assert set(pool.keys()) == {
        'halflife_sf', 'halflife_ssp', 'halflife_alpha', 'n_segments_used',
    }
    assert pool['n_segments_used'] == 3
    for k in ('halflife_sf', 'halflife_ssp', 'halflife_alpha'):
        assert set(pool[k].keys()) == {'mean', 'sigma'}

    segs_out = res['result']['segments']
    assert len(segs_out) == 3
    for seg in segs_out:
        assert set(seg.keys()) == {'segment_id', 'status', 'result', 'caveats'}
        # ssp band must be suppressed under pool prior
        assert seg['result']['bands']['log_hl_ssp'] is None
    assert 'ssp_band_suppressed' in res['caveats']


# ---------------------------------------------------------------------------
# Wire-format viability
# ---------------------------------------------------------------------------


def test_payload_is_json_serializable(cold_fit):
    s = json.dumps(cold_fit)
    back = json.loads(s)
    # Round-trip preserves the canonical fields
    assert back['result']['map']['log_theta'] == cold_fit['result']['map']['log_theta']
    assert back['status'] == cold_fit['status']
