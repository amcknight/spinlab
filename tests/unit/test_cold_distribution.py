from spinlab.cold_distribution import (
    MAX_BINS, MIN_BINS, EFFECTIVE_WINDOW_HALFLIVES, HI_ROUND_MS,
)


def test_bin_count_constants_are_sane():
    assert MIN_BINS == 5
    assert MAX_BINS == 20
    assert MAX_BINS > MIN_BINS
    assert EFFECTIVE_WINDOW_HALFLIVES == 5
    assert HI_ROUND_MS == 1000
