"""Tests for reset policies."""
import numpy as np

from spinlab.practice_engine.reset_policies import no_reset
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
