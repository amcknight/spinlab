"""Validation harness: frozen TSV inputs → expected JSON payloads.

This is the contract that porters reproduce. Each case loads a TSV from
`validation/inputs/`, runs it through the live serializer, and asserts
the result matches `validation/expected/<case>.json` within the
tolerances defined in `external_docs/api_contract.md`:

  - structural fields (schema, kind, n_attempts, status flags, caveats):
    exact equality
  - numerical fields under `map` / `derived` / pool numerics: relative
    tolerance 1e-5 (~5 sig figs)
  - numerical fields under `bands` / `ppc`: relative tolerance 1e-4
    (~4 sig figs)

Floats are reproducible across processes when JAX JIT compilation order
matches `tests/conftest.py`. `validation/regenerate.py` mirrors that
import order. If the harness drifts after a benign refactor that
changes import order, the fix is to re-run `regenerate.py`, not to
loosen tolerances.

If a regenerated fixture causes this test to fail, inspect the diff and
decide:
  - structural change → bump `SCHEMA` in `api.py` and update the contract
  - intentional numeric shift → re-run `python validation/regenerate.py`
    and commit, with a CHANGELOG note
  - unexpected regression → revert the underlying change

`wall_time_s` is excluded from the comparison.
"""
import json
import os

import pytest

import segments_v07 as sv


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUTS = os.path.join(REPO_ROOT, 'validation', 'inputs')
EXPECTED = os.path.join(REPO_ROOT, 'validation', 'expected')


# Tolerances per the contract. Keys are top-level result-block names.
TIGHT_BLOCKS  = {'map', 'derived'}                  # rel 1e-5 (~5 sig figs)
LOOSE_BLOCKS  = {'bands', 'ppc'}                    # rel 1e-4 (~4 sig figs)
TOL_TIGHT = 1e-5
TOL_LOOSE = 1e-4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_tsv(path):
    with open(path, 'r') as f:
        next(f)   # header
        return [
            {'outcome': parts[1], 'time_ms': int(parts[2])}
            for parts in (line.rstrip('\n').split('\t') for line in f)
        ]


def _load_expected(name):
    with open(os.path.join(EXPECTED, f'{name}.json'), 'r') as f:
        return json.load(f)


def _compare(actual, expected, *, rel, path=''):
    """Recursive deep-compare with relative tolerance on floats. Asserts
    structural equality on keys/types and approximate equality on numerics."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f'at {path}: type mismatch (expected dict, got {type(actual).__name__})'
        assert set(actual.keys()) == set(expected.keys()), (
            f'at {path}: key mismatch. extra={set(actual)-set(expected)}, '
            f'missing={set(expected)-set(actual)}'
        )
        for k, v in expected.items():
            _compare(actual[k], v, rel=rel, path=f'{path}.{k}')
    elif isinstance(expected, list):
        assert isinstance(actual, list), f'at {path}: type mismatch'
        assert len(actual) == len(expected), f'at {path}: length differs'
        for i, v in enumerate(expected):
            _compare(actual[i], v, rel=rel, path=f'{path}[{i}]')
    elif isinstance(expected, bool) or expected is None:
        assert actual == expected, f'at {path}: {actual!r} != {expected!r}'
    elif isinstance(expected, (int, float)):
        assert actual == pytest.approx(expected, rel=rel, nan_ok=True), (
            f'at {path}: {actual} != {expected} (rel tol {rel})'
        )
    else:
        assert actual == expected, f'at {path}: {actual!r} != {expected!r}'


def _strip_volatile(payload):
    """Mirror of validation/regenerate.py — drop wall_time_s."""
    if isinstance(payload, dict):
        return {k: _strip_volatile(v) for k, v in payload.items()
                if k != 'wall_time_s'}
    if isinstance(payload, list):
        return [_strip_volatile(v) for v in payload]
    return payload


def _check_against_expected(actual_payload, expected_payload):
    """Apply tolerances per contract: tight on map/derived,
    loose on bands/ppc, exact on the envelope."""
    actual   = _strip_volatile(actual_payload)
    expected = expected_payload

    # Envelope is structural — exact match.
    envelope_fields = (
        'schema', 'kind', 'segment_id', 'n_attempts', 'model', 'caveats',
    )
    for f in envelope_fields:
        assert actual.get(f) == expected.get(f), (
            f'envelope field {f}: {actual.get(f)!r} != {expected.get(f)!r}'
        )

    # Status is structural too — exact match.
    assert actual['status'] == expected['status'], (
        f'status mismatch: {actual["status"]} != {expected["status"]}'
    )

    # Result body — depends on `kind`.
    if expected['kind'] == 'segment_fit':
        _compare_segment_result(actual['result'], expected['result'])
    elif expected['kind'] == 'pool_fit':
        # pool block (means/sigmas in log-space, numerical) — tight tolerance.
        _compare(actual['result']['pool'], expected['result']['pool'],
                  rel=TOL_TIGHT, path='result.pool')
        # nested segments — each gets the segment-fit comparator.
        assert len(actual['result']['segments']) == len(expected['result']['segments'])
        for i, (a, e) in enumerate(zip(actual['result']['segments'],
                                       expected['result']['segments'])):
            # segment entries carry segment_id + status + result + caveats
            assert a['segment_id'] == e['segment_id']
            assert a['status'] == e['status']
            assert a['caveats'] == e['caveats']
            _compare_segment_result(a['result'], e['result'],
                                     path=f'result.segments[{i}].result')
    else:
        raise AssertionError(f"unknown kind {expected['kind']}")


def _compare_segment_result(actual_result, expected_result, path='result'):
    """Apply per-block tolerances to a segment-fit `result` dict."""
    assert set(actual_result.keys()) == set(expected_result.keys())
    for block_name, expected_block in expected_result.items():
        actual_block = actual_result[block_name]
        if block_name in TIGHT_BLOCKS:
            _compare(actual_block, expected_block, rel=TOL_TIGHT,
                     path=f'{path}.{block_name}')
        elif block_name in LOOSE_BLOCKS:
            _compare(actual_block, expected_block, rel=TOL_LOOSE,
                     path=f'{path}.{block_name}')
        else:
            raise AssertionError(f'unknown result block: {block_name}')


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_n40_haz1():
    """Normal-N happy path. Cold fit, Laplace bands, no caveats."""
    attempts = _read_tsv(os.path.join(INPUTS, 'n40_haz1.tsv'))
    actual = sv.fit_segment(attempts, segment_id='n40_haz1',
                             n_samples=200, seed=0)
    _check_against_expected(actual, _load_expected('n40_haz1'))


def test_n15_haz1():
    """Low-N case. Exercises the `low_n` caveat."""
    attempts = _read_tsv(os.path.join(INPUTS, 'n15_haz1.tsv'))
    actual = sv.fit_segment(attempts, segment_id='n15_haz1',
                             n_samples=200, seed=0)
    _check_against_expected(actual, _load_expected('n15_haz1'))


def test_no_learning():
    """Flat-curve truth. Tests the joint (halflife, A) prior collapse."""
    attempts = _read_tsv(os.path.join(INPUTS, 'no_learning.tsv'))
    actual = sv.fit_segment(attempts, segment_id='no_learning',
                             n_samples=200, seed=0)
    _check_against_expected(actual, _load_expected('no_learning'))


def test_all_died():
    """Every attempt died. Tests bpt-upper +inf path + all-deaths likelihood.
    Honestly fires PPC tension because haz1 can't reproduce all-died shape."""
    attempts = _read_tsv(os.path.join(INPUTS, 'all_died.tsv'))
    actual = sv.fit_segment(attempts, segment_id='all_died',
                             n_samples=200, seed=0)
    _check_against_expected(actual, _load_expected('all_died'))


def test_all_survived():
    """No deaths. Tests the all-survives likelihood (no Simpson integral)."""
    attempts = _read_tsv(os.path.join(INPUTS, 'all_survived.tsv'))
    actual = sv.fit_segment(attempts, segment_id='all_survived',
                             n_samples=200, seed=0)
    _check_against_expected(actual, _load_expected('all_survived'))


def test_pool3():
    """EB pool over 3 N=40 segments. Tests pool envelope + ssp band
    suppression."""
    seg_dir = os.path.join(INPUTS, 'pool3')
    segs = []
    for sid in ['seg-A', 'seg-B', 'seg-C']:
        attempts = _read_tsv(os.path.join(seg_dir, f'{sid}.tsv'))
        segs.append({'segment_id': sid, 'attempts': attempts})
    actual = sv.fit_pool(segs, n_samples=150, max_iter=3, seed=0)
    _check_against_expected(actual, _load_expected('pool3'))
