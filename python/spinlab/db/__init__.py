"""SpinLab database layer — SQLite.

The Database class composes focused repository mixins so that query logic
is organized by domain while consumers see a single object.
"""

from .attempts import AttemptsMixin
from .capture_runs import CaptureRunsMixin
from .capture_sessions import CaptureSessionsMixin
from .core import DatabaseCore
from .model_state import ModelStateMixin
from .segments import SegmentsMixin
from .sessions import SessionsMixin
from .waypoints import WaypointsMixin


class Database(
    WaypointsMixin,
    SegmentsMixin,
    AttemptsMixin,
    SessionsMixin,
    ModelStateMixin,
    CaptureRunsMixin,
    CaptureSessionsMixin,
    DatabaseCore,
):
    """Unified database interface composed from domain-specific mixins."""
    pass
