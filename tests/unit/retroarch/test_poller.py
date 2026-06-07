import asyncio
import logging
from typing import Iterator

import pytest

from spinlab.protocol import DeathEvent
from spinlab.retroarch.poller import Poller, PollerDeps
from spinlab.retroarch.snapshot import MemorySnapshot


class _FakeClient:
    """Minimal NCIClient stand-in for poller tests."""

    def __init__(self) -> None:
        self.read_calls = 0


def _make_snapshots(seq: Iterator[MemorySnapshot]):
    """Wrap a snapshot iterator into a callable matching the deps signature."""

    def fn(_client) -> MemorySnapshot:
        return next(seq)

    return fn


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


@pytest.mark.asyncio
async def test_poller_gives_up_when_ra_is_dead(monkeypatch):
    """A permanently failing read must make the poller surface + STOP, not
    reconnect-loop forever (which silently wedges the dashboard)."""
    import spinlab.retroarch.poller as poller_mod
    # Shrink the thresholds so the give-up path is reached in a few iterations.
    monkeypatch.setattr(poller_mod, "_READ_RECONNECT_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(poller_mod, "_MAX_RECONNECT_ATTEMPTS", 2)

    class _DeadClient:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    def _always_fail(_client):
        raise ConnectionError("RA gone")

    client = _DeadClient()
    deps = PollerDeps(
        client=client, read_snapshot=_always_fail, on_event=lambda e: None,
    )
    poller = Poller(deps, period_sec=0.0001)
    # Must terminate on its own; before the fix this hung forever.
    await asyncio.wait_for(poller.run(), timeout=2.0)
    assert poller._stopped is True
    assert client.closed >= 1  # at least one reconnect attempted before giving up


@pytest.mark.asyncio
async def test_poller_emits_death_event():
    """Poller fed a death sequence emits a Death event to the callback."""
    snapshots = iter([
        _snap(player_anim=0),  # frame 1
        _snap(player_anim=9),  # frame 2 -> death
        _snap(player_anim=9),  # frame 3 -> still dying, no event
    ])
    received: list = []

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    assert any(isinstance(e, DeathEvent) for e in received), f"got: {received}"


@pytest.mark.asyncio
async def test_poller_resync_clears_phantom_edges():
    """When state_version increments, the next snapshot becomes prev — no phantom Death.

    Real-world scenario: user loads a state where Mario is already dead. Without
    the resync hook, prev (pre-load, alive) → curr (post-load, dead) would fire
    a phantom Death. The resync replaces prev with the loaded state instead.
    """
    snapshots = iter([
        _snap(player_anim=9),  # post-load: state where Mario is dead
        _snap(player_anim=9),  # next frame: still dead, no edge
    ])
    received: list = []
    version = [0]

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
        state_version=lambda: version[0],
    )
    poller = Poller(deps, period_sec=0.001)  # captures _last_seen_state_version=0
    version[0] = 1  # simulate a state load — poller's next tick sees the bump
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    assert not any(isinstance(e, DeathEvent) for e in received), f"got: {received}"


@pytest.mark.asyncio
async def test_poll_count_increments_on_successful_reads():
    """poll_count tracks the number of successful RAM reads.

    Each iteration that returns a snapshot without raising increments the
    counter. Iterations that raise (NCI error path) do not increment it.
    """
    # Serve a fixed number of snapshots, then raise forever. Once the reads
    # start raising, poll_count is frozen at _SNAPSHOTS_TO_SERVE, so the final
    # assertion proves BOTH that successful reads counted and that the raising
    # reads did not increment the counter.
    _SNAPSHOTS_TO_SERVE = 5
    _calls = 0

    def _read_snapshot(_client) -> MemorySnapshot:
        nonlocal _calls
        _calls += 1
        if _calls > _SNAPSHOTS_TO_SERVE:
            raise RuntimeError("no more snapshots")
        return _snap()

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_read_snapshot,
        on_event=lambda _: None,
    )
    # period_sec=0 → asyncio.sleep(0) is a pure yield. Drive on the observable
    # read counter rather than a fixed wall-clock sleep: on Windows
    # asyncio.sleep(0.001) can take ~15ms, so a 50ms sleep does NOT reliably
    # give a 1ms-period poller >=5 ticks. That wall-clock assumption flaked at
    # ~5-15% under suite load (see project_test_reliability_known_issues).
    poller = Poller(deps, period_sec=0)
    task = asyncio.create_task(poller.run())

    async def _until_raise_path_exercised() -> None:
        # Wait until the read fn has been called past the served count — i.e.
        # the raising branch has run at least once. At that point poll_count is
        # settled and cannot change, so the assertion below is deterministic.
        while _calls <= _SNAPSHOTS_TO_SERVE:
            await asyncio.sleep(0)

    await asyncio.wait_for(_until_raise_path_exercised(), timeout=5.0)
    poller.stop()
    await task

    # poll_count must equal the number of successful reads, not total attempts.
    assert poller.poll_count == _SNAPSHOTS_TO_SERVE, (
        f"Expected poll_count={_SNAPSHOTS_TO_SERVE}, got {poller.poll_count}"
    )


@pytest.mark.asyncio
async def test_poller_uses_injected_detectors():
    """An injected detector is the one Poller drives — DI works.

    Smoke test for C2: when callers (production or tests) want to substitute
    the transition detector, they pass a constructed instance to Poller's
    constructor, and that's the instance whose .step is called per tick.
    """
    from spinlab.retroarch.cold_fill_detector import ColdFillSpawnDetector
    from spinlab.retroarch.detector import TransitionDetector

    class _SpyDetector(TransitionDetector):
        def __init__(self) -> None:
            super().__init__()
            self.step_calls = 0

        def step(self, snapshot, timestamp_ms):
            self.step_calls += 1
            return []

    snapshots = iter([_snap(), _snap(), _snap()])
    spy = _SpyDetector()
    cold_fill = ColdFillSpawnDetector()

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=lambda _ev: None,
    )
    poller = Poller(deps, period_sec=0.001, detector=spy, cold_fill=cold_fill)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.02)
    poller.stop()
    await task

    assert spy.step_calls >= 1


@pytest.mark.asyncio
async def test_poller_event_handler_exception_does_not_crash_tick(caplog):
    """A handler that raises should be logged at ERROR and not stop the poller."""
    # Alternate dead/alive to keep firing DeathEvents across multiple snapshots.
    # The TransitionDetector emits a DeathEvent on the 0→9 edge, so cycling
    # back to 0 lets us trigger another edge on the next pair.
    cycle = [
        _snap(player_anim=0),
        _snap(player_anim=9),
        _snap(player_anim=0),
        _snap(player_anim=9),
        _snap(player_anim=0),
        _snap(player_anim=9),
        _snap(player_anim=0),
        _snap(player_anim=9),
        _snap(player_anim=0),
        _snap(player_anim=9),
    ]
    snap_iter = iter(cycle)

    def crashy_handler(_event):
        raise ValueError("handler exploded")

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snap_iter),
        on_event=crashy_handler,
    )
    poller = Poller(deps, period_sec=0.001)

    with caplog.at_level(logging.ERROR, logger="spinlab.retroarch.poller"):
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.05)
        poller.stop()
        await task

    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("event handler raised" in r.getMessage() for r in errs), (
        f"expected ERROR log with 'event handler raised', got: {[r.getMessage() for r in errs]}"
    )
    # Crucially, the poller must have continued to tick (poll_count > 1).
    assert poller.poll_count > 1, f"poller.poll_count={poller.poll_count}, expected >1"


@pytest.mark.asyncio
async def test_poller_logs_read_failure_then_recovery(caplog):
    """Poller should log exactly one warning on read failure and one info on recovery."""
    fail_then_recover: list = [RuntimeError("nci dead")] + [None] * 10
    idx = [0]

    def _read_with_failure(_client) -> MemorySnapshot:
        item = fail_then_recover[idx[0]]
        idx[0] = min(idx[0] + 1, len(fail_then_recover) - 1)
        if isinstance(item, Exception):
            raise item
        return _snap()

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_read_with_failure,
        on_event=lambda _: None,
    )
    poller = Poller(deps, period_sec=0.001)

    with caplog.at_level(logging.INFO, logger="spinlab.retroarch.poller"):
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.05)
        poller.stop()
        await task

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(warns) == 1, f"expected 1 warn, got {[r.getMessage() for r in warns]}"
    assert "poller read failed" in warns[0].getMessage()
    assert any("poller read recovered" in r.getMessage() for r in infos)


@pytest.mark.asyncio
async def test_poller_reconnects_after_persistent_read_failures(caplog):
    """After _READ_RECONNECT_FAILURE_THRESHOLD consecutive failures the poller
    calls client.close() so the next read gets a fresh socket.

    Regression for the BlockingIOError[WinError 10035] incident where
    _read_failing suppressed log spam but the socket was never reconnected,
    leaving the poller in a permanent silent failure loop for minutes.
    """
    from spinlab.retroarch.poller import _READ_RECONNECT_FAILURE_THRESHOLD

    class _TrackingClient:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    def _always_fail(_client) -> MemorySnapshot:
        raise BlockingIOError("[WinError 10035] A non-blocking socket operation")

    client = _TrackingClient()
    deps = PollerDeps(
        client=client,
        read_snapshot=_always_fail,
        on_event=lambda _: None,
    )
    # period_sec=0 → asyncio.sleep(0) → pure yield, no real delay.
    # This lets the poller iterate fast enough to hit the threshold in 50ms
    # even on Windows where asyncio.sleep(0.001) can take ~15ms per call.
    poller = Poller(deps, period_sec=0)

    with caplog.at_level(logging.WARNING, logger="spinlab.retroarch.poller"):
        task = asyncio.create_task(poller.run())
        # Allow enough iterations to exceed the threshold at least once.
        await asyncio.sleep(0.05)
        poller.stop()
        await task

    assert client.close_calls >= 1, (
        f"expected client.close() after {_READ_RECONNECT_FAILURE_THRESHOLD} failures, "
        f"got {client.close_calls} close calls"
    )
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("reconnect" in r.getMessage().lower() for r in warns), (
        f"expected a reconnect warning, got: {[r.getMessage() for r in warns]}"
    )
