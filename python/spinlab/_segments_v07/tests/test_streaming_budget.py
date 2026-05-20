"""Sentinel regression test for streaming refit perf.

The v1 contract claims `refit_segment` lives at p50 ~8 ms / p99 ~35 ms after
JIT prewarm. That budget is what makes the live-UI bands viable. It's easy
to break silently: a new derived.* field that breaks closed-form `M_clear`,
a forgotten Hessian prewarm, an accidental NUTS path in the hot loop.

`tmp/bench_refit_segment.py` measures this rigorously but is a manual script.
This test is a *sentinel* — not a tight bound. It asserts the p99 stays well
under 100 ms over 50 streaming calls, which would catch an order-of-magnitude
regression on any CI host without flaking on slow ones. For the full
distribution (p50/p90/p95/p99 over a longer window), use the manual bench in
`tmp/bench_refit_segment.py`.
"""
import time

import numpy as np
import pytest

import segments_v07 as sv
from generate_synthetic_learning_v07 import TRUTH_DEFAULTS, generate


def _attempts(rows):
    return [{'outcome': o, 'time_ms': int(t)} for o, t in rows]


@pytest.fixture(scope='module')
def prewarmed():
    """Warm the JIT buckets once for the module."""
    sv.prewarm_buckets()
    return True


def test_refit_p99_under_100ms(prewarmed):
    n_cold = 30
    n_stream = 50
    rows = generate(n_cold + n_stream, TRUTH_DEFAULTS, seed=0, hazard_truth='haz1')

    prev = sv.fit_segment(_attempts(rows[:n_cold]),
                          segment_id='bench', n_samples=200, seed=0)

    # Untimed warmup refit to stabilize caches.
    prev = sv.refit_segment(_attempts(rows[:n_cold + 1]),
                            segment_id='bench', prev_result=prev,
                            n_samples=200, seed=0)

    times_ms = []
    for n in range(n_cold + 2, n_cold + n_stream + 2):
        t0 = time.perf_counter()
        prev = sv.refit_segment(_attempts(rows[:n]),
                                segment_id='bench', prev_result=prev,
                                n_samples=200, seed=0)
        times_ms.append((time.perf_counter() - t0) * 1000.0)

    arr = np.asarray(times_ms)
    p99 = float(np.percentile(arr, 99))
    p50 = float(np.percentile(arr, 50))

    # Contract: p50 ~8 ms, p99 ~35 ms. Sentinel: 3x slack on p99.
    assert p99 < 100.0, (
        f'streaming refit p99 = {p99:.1f} ms (p50 = {p50:.1f} ms); '
        f'contract claims p99 ~35 ms — investigate prewarm / closed-form M_clear'
    )
