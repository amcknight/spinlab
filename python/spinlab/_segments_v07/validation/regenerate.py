"""Regenerate the validation harness fixtures.

Usage:
    python validation/regenerate.py

Writes input TSVs to `validation/inputs/` and expected JSON payloads to
`validation/expected/`. Both are checked into git. The test in
`tests/test_validation_harness.py` runs each case through the live
serializer and compares against the expected payload with the tolerances
described in `external_docs/api_contract.md`.

When the contract changes or model numerics shift, re-run this script
and commit the result. Reviewers should inspect the diff to confirm the
shift is expected, not a regression.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mirror tests/conftest.py's import order so that JAX JIT compilation order
# matches the test environment. Otherwise the MAP optimizer settles at
# slightly different points (cross-process float reduction order).
import jax
jax.config.update("jax_enable_x64", True)
from generate_synthetic_learning_v07 import generate, TRUTH_DEFAULTS
import segments_v07 as sv


HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(HERE, 'inputs')
EXPECTED = os.path.join(HERE, 'expected')


# ---------------------------------------------------------------------------
# Dataset specs
# ---------------------------------------------------------------------------


def _write_tsv(rows, path):
    """rows: list of (outcome, time_ms). Writes a 3-column TSV with attempt_n."""
    with open(path, 'w', newline='\n') as f:
        f.write('attempt_n\toutcome\ttime_ms\n')
        for i, (outcome, time_ms) in enumerate(rows, start=1):
            f.write(f'{i}\t{outcome}\t{int(time_ms)}\n')


def _read_tsv(path):
    """Returns list of {'outcome': str, 'time_ms': int} dicts."""
    out = []
    with open(path, 'r') as f:
        next(f)   # header
        for line in f:
            _, outcome, time_ms = line.rstrip('\n').split('\t')
            out.append({'outcome': outcome, 'time_ms': int(time_ms)})
    return out


def _strip_volatile(payload):
    """Remove fields that change per-run (wall time)."""
    if isinstance(payload, dict):
        return {k: _strip_volatile(v) for k, v in payload.items()
                if k != 'wall_time_s'}
    if isinstance(payload, list):
        return [_strip_volatile(v) for v in payload]
    return payload


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def case_n40_haz1():
    """Normal-N happy path: 40 attempts, haz1 truth. Cold fit, Laplace bands."""
    rows = generate(40, TRUTH_DEFAULTS, seed=0, hazard_truth='haz1')
    tsv = os.path.join(INPUTS, 'n40_haz1.tsv')
    _write_tsv(rows, tsv)
    attempts = _read_tsv(tsv)
    payload = sv.fit_segment(attempts, segment_id='n40_haz1', n_samples=200, seed=0)
    return 'n40_haz1', payload


def case_n15_haz1():
    """Low-N case: 15 attempts. Exercises the `low_n` caveat and the
    streaming-path Laplace fallback (may go NUTS depending on the seed)."""
    rows = generate(15, TRUTH_DEFAULTS, seed=0, hazard_truth='haz1')
    tsv = os.path.join(INPUTS, 'n15_haz1.tsv')
    _write_tsv(rows, tsv)
    attempts = _read_tsv(tsv)
    payload = sv.fit_segment(attempts, segment_id='n15_haz1', n_samples=200, seed=0)
    return 'n15_haz1', payload


def case_no_learning():
    """Flat-curve truth: sf_1==sf_inf, etc. Exercises the joint
    (halflife, A) prior collapse — the trickiest piece of math in the
    model. Porters who don't replicate `halflife_sigma(A)` exactly will
    diverge here even when their happy-path matches."""
    truth = dict(TRUTH_DEFAULTS)
    truth['sf_1']    = truth['sf_inf']
    truth['ssp_1']   = truth['ssp_inf']
    truth['alpha_1'] = truth['alpha_inf']
    rows = generate(40, truth, seed=0, hazard_truth='haz1')
    tsv = os.path.join(INPUTS, 'no_learning.tsv')
    _write_tsv(rows, tsv)
    attempts = _read_tsv(tsv)
    payload = sv.fit_segment(attempts, segment_id='no_learning', n_samples=200, seed=0)
    return 'no_learning', payload


def case_all_died():
    """Every attempt dies (alpha truth = 10 ⇒ P(die)≈1). Exercises the
    bpt-upper-bound fallback (no survived attempts ⇒ bpt_upper = +inf)
    and the all-deaths likelihood path."""
    truth = dict(TRUTH_DEFAULTS, alpha_1=10.0, alpha_inf=10.0)
    rows = generate(20, truth, seed=0, hazard_truth='haz1')
    tsv = os.path.join(INPUTS, 'all_died.tsv')
    _write_tsv(rows, tsv)
    attempts = _read_tsv(tsv)
    payload = sv.fit_segment(attempts, segment_id='all_died', n_samples=200, seed=0)
    return 'all_died', payload


def case_all_survived():
    """No deaths (alpha truth = 0.001 ⇒ P(die)≈0). Exercises the
    all-survives likelihood path (no Simpson integral) and alpha being
    prior-dominated."""
    truth = dict(TRUTH_DEFAULTS, alpha_1=0.001, alpha_inf=0.001)
    rows = generate(20, truth, seed=0, hazard_truth='haz1')
    tsv = os.path.join(INPUTS, 'all_survived.tsv')
    _write_tsv(rows, tsv)
    attempts = _read_tsv(tsv)
    payload = sv.fit_segment(attempts, segment_id='all_survived', n_samples=200, seed=0)
    return 'all_survived', payload


def case_pool3():
    """EB pool over 3 segments of N=40 each. Tests the pool envelope +
    ssp band suppression."""
    seg_dir = os.path.join(INPUTS, 'pool3')
    os.makedirs(seg_dir, exist_ok=True)
    segs = []
    for i, sid in enumerate(['seg-A', 'seg-B', 'seg-C']):
        rows = generate(40, TRUTH_DEFAULTS, seed=i, hazard_truth='haz1')
        tsv = os.path.join(seg_dir, f'{sid}.tsv')
        _write_tsv(rows, tsv)
        segs.append({'segment_id': sid, 'attempts': _read_tsv(tsv)})
    payload = sv.fit_pool(segs, n_samples=150, max_iter=3, seed=0)
    return 'pool3', payload


CASES = [
    case_n40_haz1, case_n15_haz1,
    case_no_learning, case_all_died, case_all_survived,
    case_pool3,
]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    os.makedirs(INPUTS, exist_ok=True)
    os.makedirs(EXPECTED, exist_ok=True)
    for case in CASES:
        name, payload = case()
        clean = _strip_volatile(payload)
        out = os.path.join(EXPECTED, f'{name}.json')
        with open(out, 'w', newline='\n') as f:
            json.dump(clean, f, indent=2, sort_keys=True)
            f.write('\n')
        print(f'  wrote {out}')


if __name__ == '__main__':
    main()
