"""Tests for reset policies."""
import numpy as np

from spinlab.practice_engine.reset_policies import no_reset, target_paced
from spinlab.practice_engine.types import ResetMasks


class TestNoReset:
    def test_all_finished(self):
        T = np.array([[100.0, 200.0, 300.0],
                      [150.0, 250.0, 350.0]])
        masks = no_reset(T)
        assert isinstance(masks, ResetMasks)
        assert masks.finished.tolist() == [True, True]
        assert masks.abort_at.tolist() == [-1, -1]
        assert masks.wall_ms.tolist() == [600.0, 750.0]

    def test_empty_matrix(self):
        T = np.zeros((0, 3))
        masks = no_reset(T)
        assert masks.finished.shape == (0,)
        assert masks.abort_at.shape == (0,)
        assert masks.wall_ms.shape == (0,)


class TestTargetPaced:
    def test_threshold_none_acts_as_no_reset(self):
        T = np.array([[100.0, 200.0, 300.0]])
        masks = target_paced(T, threshold_cum_ms=None)
        assert masks.finished.tolist() == [True]
        assert masks.abort_at.tolist() == [-1]
        assert masks.wall_ms.tolist() == [600.0]

    def test_threshold_finishes_when_well_below(self):
        T = np.array([[100.0, 200.0, 300.0]])
        # Cumulative: 100, 300, 600. Threshold: 200, 500, 800 (x 1.0 slack).
        # Row never exceeds threshold => finished.
        threshold = np.array([200.0, 500.0, 800.0])
        masks = target_paced(T, threshold_cum_ms=threshold, slack=0.0)
        assert masks.finished.tolist() == [True]
        assert masks.abort_at.tolist() == [-1]
        assert masks.wall_ms.tolist() == [600.0]

    def test_aborts_at_first_over(self):
        T = np.array([[100.0, 200.0, 300.0]])
        # Cumulative: 100, 300, 600. Threshold: 90, 500, 800.
        # 100 > 90 => abort at segment 0.
        threshold = np.array([90.0, 500.0, 800.0])
        masks = target_paced(T, threshold_cum_ms=threshold, slack=0.0)
        assert masks.finished.tolist() == [False]
        assert masks.abort_at.tolist() == [0]
        assert masks.wall_ms.tolist() == [100.0]

    def test_aborts_at_middle_segment(self):
        T = np.array([[100.0, 200.0, 300.0]])
        # Cumulative: 100, 300, 600. Threshold: 200, 250, 800.
        # 300 > 250 => abort at segment 1.
        threshold = np.array([200.0, 250.0, 800.0])
        masks = target_paced(T, threshold_cum_ms=threshold, slack=0.0)
        assert masks.finished.tolist() == [False]
        assert masks.abort_at.tolist() == [1]
        assert masks.wall_ms.tolist() == [300.0]

    def test_slack_widens_threshold(self):
        T = np.array([[100.0, 200.0, 300.0]])
        # Cumulative: 100, 300, 600. Threshold: 200, 250, 800 x 1.5 = 300, 375, 1200.
        # 300 vs 300 (not strict >) so segment 1 NOT aborted.
        threshold = np.array([200.0, 250.0, 800.0])
        masks = target_paced(T, threshold_cum_ms=threshold, slack=0.5)
        assert masks.finished.tolist() == [True]

    def test_mixed_rollouts(self):
        T = np.array([[100.0, 200.0, 300.0],   # cum 100, 300, 600 => aborts at seg 1
                      [50.0,  100.0, 150.0],   # cum 50, 150, 300  => finished
                      [400.0, 100.0, 100.0]])  # cum 400, 500, 600 => aborts at seg 0
        threshold = np.array([200.0, 250.0, 800.0])
        masks = target_paced(T, threshold_cum_ms=threshold, slack=0.0)
        assert masks.finished.tolist() == [False, True, False]
        assert masks.abort_at.tolist() == [1, -1, 0]
        assert masks.wall_ms.tolist() == [300.0, 300.0, 400.0]
