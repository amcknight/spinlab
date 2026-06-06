"""SessionManager — practice/hyper-play start captures a session snapshot;
a clean stop FREEZES it (stamps ended_at) so the idle view persists; crash
and game-switch clear it. The snapshot is taken from the current sampler
states + the observed attempts for each segment, with started_at = time.time()."""
from __future__ import annotations

import time

import pytest

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


def test_take_session_snapshot_logs_segment_count_on_success(caplog):
    """Successful snapshot capture emits an INFO log with the segment count."""
    import logging
    sm = _make_sm_with_segments(["s0", "s1", "s2"])
    with caplog.at_level(logging.INFO, logger="spinlab.session_manager"):
        sm._take_session_snapshot()  # type: ignore[attr-defined]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("snapshot captured" in m and "n_segments=3" in m for m in msgs), msgs


def test_take_session_snapshot_logs_warning_and_clears_on_failure(caplog):
    """If _snapshot_inputs raises, the snapshot must be cleared to None and
    the failure logged at WARNING (not silently swallowed)."""
    import logging
    sm = _make_sm_with_segments(["s0"])
    sm.practice_session_snapshot = "previous"  # type: ignore[assignment]

    def boom():
        raise RuntimeError("simulated DB outage")

    sm._snapshot_inputs = boom  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="spinlab.session_manager"):
        with pytest.raises(RuntimeError):
            sm._take_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any("snapshot capture failed" in m for m in msgs), msgs


def test_take_session_snapshot_real_baseline_exercises_closed_form(monkeypatch, tmp_path):
    """Real-baseline path: build a SamplerState through process_event (the
    actual event-driven update path) and assert ONLY what's deterministic.

    Per Andrew's "don't fudge numbers" guidance: floor_ms is a pure
    min-reduction over clean_tail_ms of completed non-invalidated episodes
    (deterministic — pin it). expected_episode_ms and death_rate flow through
    EMA accumulators with a specific ALPHA_GRID; their exact values are NOT
    pinned here — assert only that they're non-None (gate cleared) and in
    their valid range. A specific-value pin would require hand-computing the
    alpha=0 EMA from ALPHA_GRID[DEFAULT_FAST_IDX], which is brittle if the
    grid changes."""
    import time

    from spinlab.db import Database
    from spinlab.estimators.em_suite_sampler import SamplerState, process_event
    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
    from spinlab.session_manager import SessionManager
    from spinlab.system_state import SystemState

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")

    sm = SessionManager.__new__(SessionManager)
    sm.practice_session_snapshot = None
    sm.state = SystemState()
    sm.state.game_id = "g"

    # Build a real SamplerState above the prediction gate
    # (n_successes >= 2 AND n_deaths >= 2 AND n_attempts_total >= 2 per
    # em_suite_sampler.gate_passes at line ~310).
    state = SamplerState()
    sess = "sess-test"
    for i, (outcome, time_ms) in enumerate([
        (AttemptOutcome.SURVIVED, 12_000),
        (AttemptOutcome.SURVIVED, 13_500),
        (AttemptOutcome.SURVIVED, 12_800),
        (AttemptOutcome.DIED, 8_000),
        (AttemptOutcome.DIED, 9_500),
        (AttemptOutcome.DIED, 8_400),
    ]):
        event = EventAttempt(
            segment_id="s0",
            episode_id=f"ep-{i}",
            outcome=outcome,
            time_ms=time_ms,
            session_id=sess,
            source=AttemptSource.PRACTICE,
        )
        state = process_event(state, event)
    # Sanity: did we actually clear the gate?
    assert state.n_successes >= 2 and state.n_deaths >= 2

    # Episodes are AttemptRow shape (TypedDict total=True). Three completed
    # non-invalidated episodes with clean_tail_ms set; floor_ms = min(those).
    episodes = [
        {"segment_id": "s0", "completed": 1, "time_ms": 12_000, "deaths": 0,
         "clean_tail_ms": 11_500, "created_at": "2026-06-03T00:00:00", "invalidated": 0},
        {"segment_id": "s0", "completed": 1, "time_ms": 13_500, "deaths": 1,
         "clean_tail_ms": 12_800, "created_at": "2026-06-03T00:00:01", "invalidated": 0},
        {"segment_id": "s0", "completed": 1, "time_ms": 12_800, "deaths": 0,
         "clean_tail_ms": 11_900, "created_at": "2026-06-03T00:00:02", "invalidated": 0},
    ]
    sm._snapshot_inputs = lambda: [("s0", state, episodes)]  # type: ignore[attr-defined]

    monkeypatch.setattr(time, "time", lambda: 1_717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]

    snap = sm.practice_session_snapshot
    assert snap is not None
    assert snap.started_at == 1_717_000_000.0
    base = snap.segments["s0"]
    # DETERMINISTIC: floor_ms = min(clean_tail_ms) over completed,
    # non-invalidated episodes = min(11_500, 12_800, 11_900) = 11_500.
    assert base.floor_ms == 11_500.0
    # STRUCTURAL: above-gate so EMA-derived values are non-None.
    # Don't pin specific EMA values — they depend on ALPHA_GRID order.
    assert base.expected_episode_ms is not None
    assert base.expected_episode_ms > 0.0
    assert 0.0 <= base.death_rate <= 1.0


def test_snapshot_inputs_reads_from_scheduler_cache(tmp_path):
    """_snapshot_inputs pulls SamplerStates from scheduler.sampler_states()
    rather than re-replaying the event log per segment.

    Test strategy: substitute a fake scheduler whose sampler_states() returns
    a sentinel object that replay_with_history cannot produce. If the snapshot
    surfaces that sentinel, we know it came from the cache, not a fresh
    rebuild. Catches a regression where someone re-introduces a replay path.
    """
    from spinlab.db import Database
    from spinlab.estimators.em_suite_sampler import SamplerState
    from spinlab.models import EndpointType, Segment
    from spinlab.session_manager import SessionManager
    from spinlab.system_state import SystemState

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    db.upsert_segment(Segment(
        id="s0", game_id="g", level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
    ))

    sentinel_state = SamplerState()
    # Tag it so we can identify it on the other side.
    sentinel_state.n_attempts_total = 9999

    class FakeScheduler:
        def sampler_states(self):
            return {"s0": sentinel_state}

    sm = SessionManager.__new__(SessionManager)
    sm.practice_session_snapshot = None
    sm.state = SystemState()
    sm.state.game_id = "g"
    sm.db = db
    sm.scheduler = FakeScheduler()  # type: ignore[assignment]

    inputs = sm._snapshot_inputs()  # type: ignore[attr-defined]

    by_seg = {seg_id: (state, eps) for seg_id, state, eps in inputs}
    assert by_seg["s0"][0] is sentinel_state


def test_snapshot_inputs_uses_empty_sampler_state_when_cache_misses(tmp_path):
    """Segments without a saved model_state row (newly added, no events yet)
    are absent from scheduler.sampler_states(). _snapshot_inputs must fall
    back to a default-constructed SamplerState so downstream gates still see
    a below-gate state and yield a None baseline, matching the prior
    replay-of-empty-events behavior."""
    from spinlab.db import Database
    from spinlab.estimators.em_suite_sampler import SamplerState
    from spinlab.models import EndpointType, Segment
    from spinlab.session_manager import SessionManager
    from spinlab.system_state import SystemState

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    db.upsert_segment(Segment(
        id="s_new", game_id="g", level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
    ))

    class FakeScheduler:
        def sampler_states(self):
            return {}  # cache miss for s_new

    sm = SessionManager.__new__(SessionManager)
    sm.practice_session_snapshot = None
    sm.state = SystemState()
    sm.state.game_id = "g"
    sm.db = db
    sm.scheduler = FakeScheduler()  # type: ignore[assignment]

    inputs = sm._snapshot_inputs()  # type: ignore[attr-defined]
    by_seg = {seg_id: state for seg_id, state, _eps in inputs}
    assert "s_new" in by_seg
    fallback = by_seg["s_new"]
    assert isinstance(fallback, SamplerState)
    # A default SamplerState is below the prediction gate.
    assert fallback.n_successes == 0
    assert fallback.n_deaths == 0


def test_freeze_session_snapshot_stamps_ended_at(monkeypatch):
    sm = _make_sm_with_segments(["s0", "s1"])
    monkeypatch.setattr(time, "time", lambda: 1717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot.ended_at is None
    monkeypatch.setattr(time, "time", lambda: 1717_000_060.0)
    sm._freeze_session_snapshot()  # type: ignore[attr-defined]
    snap = sm.practice_session_snapshot
    assert snap is not None
    assert snap.ended_at == 1717_000_060.0
    assert snap.started_at == 1717_000_000.0  # preserved
    assert set(snap.segments.keys()) == {"s0", "s1"}  # preserved


def test_freeze_session_snapshot_idempotent(monkeypatch):
    sm = _make_sm_with_segments(["s0"])
    monkeypatch.setattr(time, "time", lambda: 1717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    monkeypatch.setattr(time, "time", lambda: 1717_000_060.0)
    sm._freeze_session_snapshot()  # type: ignore[attr-defined]
    monkeypatch.setattr(time, "time", lambda: 1717_000_999.0)
    sm._freeze_session_snapshot()  # second freeze must not move ended_at
    assert sm.practice_session_snapshot.ended_at == 1717_000_060.0


def test_freeze_session_snapshot_noop_when_none():
    sm = _make_sm_with_segments(["s0"])
    assert sm.practice_session_snapshot is None
    sm._freeze_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is None


@pytest.mark.asyncio
async def test_stop_practice_freezes_snapshot(monkeypatch):
    """Clean stop must FREEZE (not clear) the snapshot so the idle view persists.
    Exercises the mode==PRACTICE / no-running-session branch of stop_practice,
    which returns without an SSE notify."""
    from spinlab.models import Mode

    sm = _make_sm_with_segments(["s0"])
    sm.practice_session = None
    sm.mode = Mode.PRACTICE
    monkeypatch.setattr(time, "time", lambda: 1717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    monkeypatch.setattr(time, "time", lambda: 1717_000_060.0)

    await sm.stop_practice()

    assert sm.mode == Mode.IDLE
    assert sm.practice_session_snapshot is not None
    assert sm.practice_session_snapshot.ended_at == 1717_000_060.0


@pytest.mark.asyncio
async def test_stop_hyper_play_freezes_snapshot(monkeypatch):
    """Clean stop must FREEZE (not clear) the hyper-play snapshot — mirror of
    test_stop_practice_freezes_snapshot, mode==HYPER_PLAY branch."""
    from spinlab.models import Mode

    sm = _make_sm_with_segments(["s0"])
    sm.hyper_play_session = None
    sm.mode = Mode.HYPER_PLAY
    monkeypatch.setattr(time, "time", lambda: 1717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    monkeypatch.setattr(time, "time", lambda: 1717_000_060.0)

    await sm.stop_hyper_play()

    assert sm.mode == Mode.IDLE
    assert sm.practice_session_snapshot is not None
    assert sm.practice_session_snapshot.ended_at == 1717_000_060.0


async def test_start_practice_rolls_back_on_snapshot_failure(tmp_path, monkeypatch):
    """If _take_session_snapshot raises inside start_practice, the session
    must roll back: mode=IDLE, practice_session=None, practice_task cancelled,
    and SnapshotFailedError surfaces to the caller."""
    from spinlab.db import Database
    from spinlab.errors import SnapshotFailedError
    from spinlab.models import Mode
    from spinlab.session_manager import SessionManager
    from tests.conftest import FakeEmuBackend

    db = Database(tmp_path / "sm.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "g"
    sm.mode = Mode.IDLE

    def boom():
        raise RuntimeError("synthetic snapshot crash")

    monkeypatch.setattr(sm, "_snapshot_inputs", boom)

    with pytest.raises(SnapshotFailedError):
        await sm.start_practice()

    assert sm.mode == Mode.IDLE
    assert sm.practice_session is None
    assert sm.practice_task is None
