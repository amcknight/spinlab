"""SessionManager — practice/hyper-play start captures a session snapshot;
stop clears it. The snapshot is taken from the current sampler states + the
observed attempts for each segment, with started_at = time.time()."""
from __future__ import annotations

import time

from spinlab.session_manager import SessionManager


def _make_sm_with_segments(seg_ids):
    """Build a SessionManager skeleton with a stubbed _snapshot_inputs that yields
    (seg_id, fake_state, episodes) tuples. Stays out of the emu/db plumbing."""
    sm = SessionManager.__new__(SessionManager)  # bypass __init__
    sm.practice_session_snapshot = None

    class FakeState:
        # Counts below the prediction gate (n>=2 each) so snapshot_from_segments
        # short-circuits per-segment in _baseline_for_segment and per-route in
        # _route_baseline — no EMA methods are required on this stub.
        def __init__(self):
            self.n_successes = 0
            self.n_deaths = 0
            self.n_attempts_total = 0

    sm._snapshot_inputs = lambda: [(sid, FakeState(), []) for sid in seg_ids]  # type: ignore[attr-defined]
    return sm


def test_take_session_snapshot_records_started_at_and_segments(monkeypatch):
    sm = _make_sm_with_segments(["s0", "s1"])
    monkeypatch.setattr(time, "time", lambda: 1717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    snap = sm.practice_session_snapshot
    assert snap is not None
    assert snap.started_at == 1717_000_000.0
    assert set(snap.segments.keys()) == {"s0", "s1"}


def test_clear_session_snapshot_resets_to_none():
    sm = _make_sm_with_segments(["s0"])
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is not None
    sm._clear_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is None
