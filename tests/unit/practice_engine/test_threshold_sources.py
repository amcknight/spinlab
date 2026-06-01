"""Tests for threshold source helpers."""
import numpy as np

from spinlab.practice_engine.threshold_sources import (
    thresholds_from_gold_default,
    thresholds_from_user,
)


class TestThresholdsFromUser:
    def test_cumulative_order(self):
        seg_ids = ["s1", "s2", "s3"]
        splits = {"s1": 5000, "s2": 12000, "s3": 18000}
        result = thresholds_from_user(seg_ids, splits)
        assert result.tolist() == [5000.0, 12000.0, 18000.0]
        assert result.dtype == np.float64

    def test_missing_segment_raises(self):
        seg_ids = ["s1", "s2"]
        splits = {"s1": 5000}
        try:
            thresholds_from_user(seg_ids, splits)
        except KeyError:
            return
        raise AssertionError("Expected KeyError for missing segment")


class TestThresholdsFromGoldDefault:
    def test_cumulative_sum(self):
        seg_ids = ["s1", "s2", "s3"]
        golds_ms = {"s1": 3000, "s2": 5000, "s3": 8000}
        result = thresholds_from_gold_default(seg_ids, golds_ms)
        assert result.tolist() == [3000.0, 8000.0, 16000.0]
        assert result.dtype == np.float64

    def test_missing_gold_raises(self):
        seg_ids = ["s1", "s2"]
        golds_ms = {"s1": 3000}
        try:
            thresholds_from_gold_default(seg_ids, golds_ms)
        except KeyError:
            return
        raise AssertionError("Expected KeyError for missing gold")
