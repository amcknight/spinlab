from spinlab.cold_distribution import (
    EFFECTIVE_WINDOW_HALFLIVES,
    HI_ROUND_MS,
    MAX_BINS,
    MIN_BINS,
    _bin_count_for,
    _compute_attempt_weights,
)


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
