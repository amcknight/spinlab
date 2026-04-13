"""Insert reference run segment times as seed attempts."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .models import Attempt, AttemptSource

if TYPE_CHECKING:
    from .db import Database
    from .capture.recorder import RecordedSegmentTime

logger = logging.getLogger(__name__)


def seed_reference_attempts(
    db: "Database",
    capture_run_id: str,
    segment_times: list["RecordedSegmentTime"],
) -> int:
    """Insert seed attempts from reference segment times. Returns count inserted."""
    if not segment_times:
        return 0

    now = datetime.now(UTC)
    count = 0
    for rst in segment_times:
        attempt = Attempt(
            segment_id=rst.segment_id,
            session_id=capture_run_id,
            completed=True,
            time_ms=rst.time_ms,
            deaths=rst.deaths,
            clean_tail_ms=rst.clean_tail_ms,
            source=AttemptSource.REFERENCE,
            created_at=now,
        )
        db.log_attempt(attempt)
        count += 1
        logger.info("seed: segment=%s time=%dms deaths=%d clean_tail=%dms",
                     rst.segment_id, rst.time_ms, rst.deaths, rst.clean_tail_ms)

    return count
