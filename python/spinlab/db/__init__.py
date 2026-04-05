"""SpinLab database layer — SQLite.

The Database class composes focused repository mixins so that query logic
is organized by domain while consumers see a single object.
"""

from .core import DatabaseCore
from .segments import SegmentsMixin
from .attempts import AttemptsMixin
from .sessions import SessionsMixin
from .model_state import ModelStateMixin
from .capture_runs import CaptureRunsMixin
from .waypoints import WaypointsMixin


class Database(
    WaypointsMixin,
    SegmentsMixin,
    AttemptsMixin,
    SessionsMixin,
    ModelStateMixin,
    CaptureRunsMixin,
    DatabaseCore,
):
    """Unified database interface composed from domain-specific mixins."""
    pass
