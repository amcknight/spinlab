from spinlab.cold_distribution import (
    HI_ROUND_MS,
    MAX_BINS,
    MIN_BINS,
    _bin_count_for,
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
    assert HI_ROUND_MS == 1000


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
        compute_cold_distribution([])


def test_compute_single_death_at_2s():
    # One cold attempt that died at 2000ms.
    events = [_ev(2000, AttemptOutcome.DIED)]
    dist = compute_cold_distribution(events)
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
    dist = compute_cold_distribution(events)
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


def test_compute_no_truncation():
    # v0: equal-weight cold distribution retains every cold event — no
    # halflife-based truncation. With 200 events, n_cold_attempts == 200.
    events = [
        _ev(time_ms=100 + i, outcome=AttemptOutcome.SURVIVED, ep=f"e{i}")
        for i in range(200)
    ]
    dist = compute_cold_distribution(events)
    assert dist.n_cold_attempts == 200
    # Bin count from sqrt(200) ≈ 14.14 → ceil 15, clamped under MAX_BINS=20.
    assert len(dist.bins) == 15


def test_compute_p_die_aggregates_uniform_weights():
    # Two episodes (cold-distribution v0: equal-weight, no recency knob):
    #   ep1: died at 2000  ->  1 death
    #   ep2: died at 1500, then survived at 5000  ->  episode "attempted" had a death
    # With uniform weight=1.0 per event:
    #   p_die_per_life = deaths / (deaths + survivals) = 2 / 3
    #   p_die_per_attempt = episodes-with-death / total episodes = 2 / 2 = 1.0
    import pytest
    events = [
        _ev(2000, AttemptOutcome.DIED, ep="e1"),
        _ev(1500, AttemptOutcome.DIED, ep="e2"),
        _ev(5000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events)
    assert dist.p_die_per_life == pytest.approx(2.0 / 3.0, rel=1e-9)
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
    dist = compute_cold_distribution(events)
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
    # Died at 2000, survived at 8000. Equal weights (1.0 each):
    #   deaths_w_in_2s_bin = 1, at_risk_w = 2 → hazard = 0.5
    # Bins after 8000 have at_risk_w = 0 → hazard = None.
    events = [
        _ev(2000, AttemptOutcome.DIED, ep="e1"),
        _ev(8000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events)
    bin_at_2s = next(b for b in dist.bins if b.lo_ms <= 2000 <= b.hi_ms)
    assert bin_at_2s.hazard == 0.5
    assert bin_at_2s.at_risk_w == 2.0


def test_compute_echoes_zero_halflife():
    # v0: there is no halflife knob; the schema field is echoed as 0.
    events = [_ev(2000, AttemptOutcome.DIED)]
    dist = compute_cold_distribution(events)
    assert dist.halflife == 0


def test_completion_sigma_zero_for_single_survival():
    # One completion -> std is exactly 0.0 (single point has no spread).
    events = [_ev(5000, AttemptOutcome.SURVIVED)]
    dist = compute_cold_distribution(events)
    assert dist.sigma_c_ms == 0.0
    # Log-moments populated for the single positive-t completion.
    assert dist.mu_log_c is not None
    assert dist.sigma_log_c == 0.0


def test_completion_sigma_two_survivals_matches_population_formula():
    # Two completions at 1000 and 3000 ms. Equal weights (1.0 each);
    # σ² = E[t²] − μ² with μ=2000:
    #   E[t²] = (1e6 + 9e6)/2 = 5e6; var = 5e6 − 4e6 = 1e6; σ = 1000.
    import pytest
    events = [
        _ev(1000, AttemptOutcome.SURVIVED, ep="e1"),
        _ev(3000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events)
    assert dist.mu_c_ms == pytest.approx(2000.0, rel=1e-3)
    assert dist.sigma_c_ms == pytest.approx(1000.0, rel=1e-3)


def test_completion_log_moments_for_two_survivals():
    # ln(1000) ≈ 6.9078, ln(3000) ≈ 8.0064.
    # Equal weights => μ_log = mean of the two; σ_log = half the spread.
    import math
    import pytest
    events = [
        _ev(1000, AttemptOutcome.SURVIVED, ep="e1"),
        _ev(3000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events)
    l1, l2 = math.log(1000), math.log(3000)
    expected_mu = (l1 + l2) / 2
    expected_sigma = (l2 - l1) / 2  # |x - μ| for two-point population sample
    assert dist.mu_log_c == pytest.approx(expected_mu, rel=1e-3)
    assert dist.sigma_log_c == pytest.approx(expected_sigma, rel=1e-3)


def test_hazard_uniform_at_risk():
    # Equal-weight regime: both events count for at_risk_w = 1.0 each.
    # Bin containing 2000ms: both events at-risk (2000 >= bin.lo_ms),
    # one death weighted 1.0 → hazard = 1 / 2 = 0.5; at_risk_w = 2.0.
    # Bin containing 8000ms: only the SURVIVED event at-risk → at_risk_w = 1.0,
    # no deaths in this bin → hazard = 0.0. This pins the hazard math under
    # the v0 (uniform-weight) regime.
    import pytest
    events = [
        _ev(2000, AttemptOutcome.DIED, ep="e1"),
        _ev(8000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events)
    bin_at_2s = next(b for b in dist.bins if b.lo_ms <= 2000 <= b.hi_ms)
    assert bin_at_2s.at_risk_w == pytest.approx(2.0, rel=1e-9)
    assert bin_at_2s.hazard == pytest.approx(0.5, rel=1e-9)
    bin_at_8s = next(b for b in dist.bins if b.lo_ms <= 8000 <= b.hi_ms)
    assert bin_at_8s.at_risk_w == pytest.approx(1.0, rel=1e-9)
    assert bin_at_8s.hazard == pytest.approx(0.0, rel=1e-9)
