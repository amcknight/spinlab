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
    # Provide enough snapshots for ~10ms at 1ms period, then raise to stop.
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
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
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
