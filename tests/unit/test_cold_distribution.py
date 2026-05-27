from spinlab.cold_distribution import (
    EFFECTIVE_WINDOW_HALFLIVES,
    HI_ROUND_MS,
    MAX_BINS,
    MIN_BINS,
    _bin_count_for,
    _compute_attempt_weights,
    compute_cold_distribution,
)
from spinlab.models import AttemptOutcome
from tests.factories import make_event_attempt


def _ev(time_ms: int, outcome: AttemptOutcome, ep: str = "e1"):
    return make_event_attempt(time_ms=time_ms, outcome=outcome, episode_id=ep)


def test_bin_count_constants_are_sane():
    assert MIN_BINS == 5
    assert MAX_BINS == 20
    assert MAX_BINS > MIN_BINS
    assert EFFECTIVE_WINDOW_HALFLIVES == 5
    assert HI_ROUND_MS == 1000


def test_attempt_weights_most_recent_is_one():
    # Five attempts in chronological order, halflife=2.
    # Last (index 4) should weigh 1.0.
    # Index 2 is 2 steps back = 1 halflife → weight 0.5.
    # Index 0 is 4 steps back = 2 halflives → weight 0.25.
    weights = _compute_attempt_weights(n=5, halflife=2)
    assert weights[-1] == 1.0
    assert weights[-3] == 0.5    # 1 halflife back ⇒ 2^-1
    assert weights[-5] == 0.25   # 2 halflives back ⇒ 2^-2 = 0.25


def test_attempt_weights_empty():
    assert _compute_attempt_weights(n=0, halflife=20) == []


def test_bin_count_clamps_low():
    assert _bin_count_for(n=0) == 5
    assert _bin_count_for(n=4) == 5
    assert _bin_count_for(n=25) == 5      # sqrt(25)=5
    assert _bin_count_for(n=26) == 6      # sqrt(26)≈5.099 → ceil 6


def test_bin_count_clamps_high():
    assert _bin_count_for(n=400) == 20    # sqrt(400)=20
    assert _bin_count_for(n=401) == 20    # capped
    assert _bin_count_for(n=10_000) == 20


def test_schema_imports():
    from spinlab.api_schemas import ColdBin, ColdDistribution
    bin_ = ColdBin(lo_ms=0.0, hi_ms=500.0, n_deaths=2, n_completions=1)
    dist = ColdDistribution(
        bins=[bin_], n_cold_attempts=3,
        mu_d_ms=200.0, mu_c_ms=400.0,
        p_die_per_attempt=0.5, p_die_per_life=0.5,
    )
    assert dist.bins[0].n_deaths == 2
    assert dist.n_cold_attempts == 3


def test_compute_empty_inputs_disallowed():
    # Caller (route) substitutes None on empty input; the function itself
    # should never be called with an empty list. The contract is to raise
    # ValueError specifically.
    import pytest
    with pytest.raises(ValueError, match="non-empty"):
        compute_cold_distribution([], halflife=20)


def test_compute_single_death_at_2s():
    # One cold attempt that died at 2000ms.
    events = [_ev(2000, AttemptOutcome.DIED)]
    dist = compute_cold_distribution(events, halflife=20)
    # n_cold_attempts is post-truncation; with 1 event there is no truncation
    assert dist.n_cold_attempts == 1
    # bin count = max(5, ceil(sqrt(1))) = 5
    assert len(dist.bins) == 5
    # X-axis range: 0 to ceil(2000/1000)*1000 = 2000
    assert dist.bins[0].lo_ms == 0
    assert dist.bins[-1].hi_ms == 2000
    # Single death lands in topmost bin (time 2000 == hi -> clamped)
    total_deaths = sum(b.n_deaths for b in dist.bins)
    assert total_deaths == 1
    assert dist.mu_d_ms == 2000.0
    assert dist.mu_c_ms is None  # no completions


def test_compute_two_attempts_mixed_outcomes():
    # One died at 2000, one survived at 8000.
    events = [
        _ev(2000, AttemptOutcome.DIED, ep="e1"),
        _ev(8000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events, halflife=20)
    assert dist.n_cold_attempts == 2
    assert len(dist.bins) == 5  # sqrt(2) -> ceil 2 -> clamped to 5
    # hi rounds up to ceil(8000/1000)*1000 = 8000
    assert dist.bins[-1].hi_ms == 8000
    total_deaths = sum(b.n_deaths for b in dist.bins)
    total_completions = sum(b.n_completions for b in dist.bins)
    assert total_deaths == 1
    assert total_completions == 1
    # Weighted means: only one death, only one completion -> equal to the
    # raw times regardless of weight.
    assert dist.mu_d_ms == 2000.0
    assert dist.mu_c_ms == 8000.0


def test_compute_truncates_to_5x_halflife():
    # 200 cold attempts; halflife=20 -> window = 100 attempts.
    events = [
        _ev(time_ms=100 + i, outcome=AttemptOutcome.SURVIVED, ep=f"e{i}")
        for i in range(200)
    ]
    dist = compute_cold_distribution(events, halflife=20)
    # n_cold_attempts reflects POST-truncation count
    assert dist.n_cold_attempts == 100
    assert len(dist.bins) == min(20, max(5, 10))  # sqrt(100)=10


def test_compute_p_die_aggregates():
    # Two episodes:
    #   ep1: died at 2000  ->  1 death
    #   ep2: died at 1500, then survived at 5000  ->  episode "attempted" had a death
    events = [
        _ev(2000, AttemptOutcome.DIED, ep="e1"),
        _ev(1500, AttemptOutcome.DIED, ep="e2"),
        _ev(5000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events, halflife=20)
    import pytest
    w0 = 2.0 ** (-2/20)
    w1 = 2.0 ** (-1/20)
    w2 = 1.0
    expected_p_die_per_life = (w0 + w1) / (w0 + w1 + w2)
    assert dist.p_die_per_life == pytest.approx(expected_p_die_per_life, rel=1e-9)
    # Both episodes had a death; episode e2's representative weight is w2 (the
    # latest event), episode e1's representative weight is w0. Numerator and
    # denominator are identical, so p_die_per_attempt = 1.0 exactly.
    assert dist.p_die_per_attempt == pytest.approx(1.0, rel=1e-9)


def test_hazard_fields_on_schema():
    from spinlab.api_schemas import ColdBin, ColdDistribution
    bin_ = ColdBin(
        lo_ms=0.0, hi_ms=500.0, n_deaths=1, n_completions=0,
        hazard=0.5, at_risk_w=2.0,
    )
    dist = ColdDistribution(
        bins=[bin_], n_cold_attempts=2,
        mu_d_ms=200.0, mu_c_ms=None,
        p_die_per_attempt=0.5, p_die_per_life=0.5,
        halflife=20,
    )
    assert dist.bins[0].hazard == 0.5
    assert dist.bins[0].at_risk_w == 2.0
    assert dist.halflife == 20


def test_hazard_single_death():
    # One cold attempt died at 2000ms. The bin containing 2000ms gets
    # hazard = 1/1 = 1.0; bins after the death have at_risk_w = 0, hazard = None.
    events = [_ev(2000, AttemptOutcome.DIED)]
    dist = compute_cold_distribution(events, halflife=20)
    # Find the bin containing 2000ms
    target_idx = next(
        i for i, b in enumerate(dist.bins) if b.lo_ms <= 2000 <= b.hi_ms
    )
    assert dist.bins[target_idx].hazard == 1.0
    assert dist.bins[target_idx].at_risk_w == 1.0
    # Bins before the death: at_risk_w = 1.0, hazard = 0.0 (no deaths yet)
    for i in range(target_idx):
        assert dist.bins[i].at_risk_w == 1.0
        assert dist.bins[i].hazard == 0.0
    # Bins after the death: at_risk_w = 0, hazard = None
    for i in range(target_idx + 1, len(dist.bins)):
        assert dist.bins[i].at_risk_w == 0.0
        assert dist.bins[i].hazard is None


def test_hazard_one_death_one_completion():
    # Died at 2000, survived at 8000.
    # Use a very large halflife so both weights are effectively 1.0:
    #   deaths_w_in_2s_bin ≈ 1, at_risk_w ≈ 2 → hazard ≈ 0.5
    # Bins after 8000 have at_risk_w = 0 → hazard = None.
    events = [
        _ev(2000, AttemptOutcome.DIED, ep="e1"),
        _ev(8000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events, halflife=10_000)
    bin_at_2s = next(b for b in dist.bins if b.lo_ms <= 2000 <= b.hi_ms)
    # With halflife=10000, weight[0] = 2^(-1/10000) ≈ 0.99993; close enough.
    assert abs(bin_at_2s.hazard - 0.5) < 1e-3
    assert abs(bin_at_2s.at_risk_w - 2.0) < 1e-3


def test_hazard_curve_returns_halflife():
    events = [_ev(2000, AttemptOutcome.DIED)]
    dist = compute_cold_distribution(events, halflife=42)
    assert dist.halflife == 42
