"""SpinLab scheduler — legacy SM-2 stub (superseded by Kalman allocator)."""

from typing import Optional

from .db import Database
from .models import Split, SplitCommand


class Scheduler:
    """Legacy SM-2 scheduler stub.

    The SM-2 scheduling logic and Rating/Schedule types have been removed in
    favour of the Kalman-filter-based allocator.  This class is kept as a thin
    shim so that any existing code that instantiates it doesn't crash; it will
    be deleted once the Kalman allocator is wired into the orchestrator.
    """

    def __init__(self, db: Database, game_id: str, base_interval: float = 5.0):
        self.db = db
        self.game_id = game_id
        self.base_interval = base_interval

    def pick_next(self) -> Optional[SplitCommand]:
        """Return None — callers should use the Kalman allocator instead."""
        return None

    def peek_next_n(self, n: int) -> list[str]:
        """Return empty list — callers should use the Kalman allocator instead."""
        return []

    def reset_strat(self, split_id: str) -> None:
        self.db.increment_strat_version(split_id)
