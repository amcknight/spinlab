"""Practice Simulation Engine — rollout matrix + reset policies + objectives.

Spec: docs/superpowers/specs/2026-06-01-practice-simulation-engine-design.md
"""
# PracticeEngine lands in Task 6; re-export from here once engine.py exists.
from spinlab.practice_engine.types import PerSegmentValue, ResetMasks

__all__ = ["PerSegmentValue", "ResetMasks"]
