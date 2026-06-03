"""SessionManager — practice/hyper-play start captures a session snapshot;
stop clears it. The snapshot is taken from the current sampler states + the
observed attempts for each segment, with started_at = time.time()."""
from __future__ import annotations

import time

from spinlab.session_manager import SessionManager


def _make_sm_with_segments(seg_ids):
    """Build a SessionManager skeleton with a stubbed _snapshot_inputs that yields
    (seg_id, fake_state, episodes) tuples. Stays out of the emu/db plumbing.

    Includes a real SystemState so the `mode` property (which delegates to
    self.state.mode) works end-to-end for callers that flip the mode."""
    from spinlab.system_state import SystemState

    sm = SessionManager.__new__(SessionManager)  # bypass __init__
    sm.practice_session_snapshot = None
    sm.state = SystemState()

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


def _fake_task(*, cancelled: bool = False, exc: Exception | None = None):
    """Minimal asyncio.Task stand-in for the _on_*_done callbacks.

    The done-callbacks call task.cancelled() and (if not cancelled) task.exception().
    Both are simple bool/Exception getters on the real Task — easy to stub."""
    class _T:
        def cancelled(self_inner):
            return cancelled
        def exception(self_inner):
            return exc
    return _T()


def test_on_practice_done_crash_clears_snapshot(monkeypatch):
    """If practice's run_loop crashes, _on_practice_done flips IDLE — and must
    clear the snapshot too so a stale baseline doesn't survive the crash."""
    from spinlab.models import Mode

    sm = _make_sm_with_segments(["s0"])
    sm.mode = Mode.PRACTICE
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is not None

    # Stub _notify_sse — the callback creates a task for it; we don't need the SSE
    # path in this test.
    monkeypatch.setattr(sm, "_notify_sse", lambda: None)
    # asyncio.create_task requires a running loop; replace with a no-op so the
    # callback can run synchronously.
    import asyncio as _asyncio
    monkeypatch.setattr(_asyncio, "create_task", lambda coro: None)

    sm._on_practice_done(_fake_task(exc=RuntimeError("boom")))

    assert sm.mode == Mode.IDLE
    assert sm.practice_session_snapshot is None


def test_on_hyper_play_done_crash_clears_snapshot(monkeypatch):
    """Mirror of test_on_practice_done_crash_clears_snapshot for hyper-play."""
    from spinlab.models import Mode

    sm = _make_sm_with_segments(["s0"])
    sm.mode = Mode.HYPER_PLAY
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is not None

    monkeypatch.setattr(sm, "_notify_sse", lambda: None)
    import asyncio as _asyncio
    monkeypatch.setattr(_asyncio, "create_task", lambda coro: None)

    sm._on_hyper_play_done(_fake_task(exc=RuntimeError("boom")))

    assert sm.mode == Mode.IDLE
    assert sm.practice_session_snapshot is None
