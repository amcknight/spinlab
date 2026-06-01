"""End-to-end pipeline test for the death-aware rolling estimator.

Drives several multi-death practice episodes through the real DB +
scheduler + estimator stack and asserts the persisted output is
populated with sensible values.

No emulator — uses the DB directly. Events are written via
log_event_attempt the same way PracticeSession.receive_event_attempt does.
"""
import json
from datetime import datetime

import pytest


def _log_episode(db, segment_id, session_id, episode_id, outcomes_and_times):
    """Persist one episode as a series of event rows. Returns the closing id."""
    from spinlab.models import (
        AttemptOutcome, AttemptSource, EventAttempt,
    )
    last_id = None
    for outcome, time_ms in outcomes_and_times:
        last_id = db.log_event_attempt(EventAttempt(
            segment_id=segment_id,
            episode_id=episode_id,
            outcome=AttemptOutcome(outcome),
            time_ms=time_ms,
            session_id=session_id,
            capture_run_id=None,
            source=AttemptSource.PRACTICE,
            chosen_allocator=None,
            invalidated=False,
            created_at=datetime.now(),
        ))
    return last_id


