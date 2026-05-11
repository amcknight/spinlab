"""End-to-end orchestrator pipeline test -- real Poller, real adapter, fake NCI."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterator

import pytest

from spinlab.protocol import DeathEvent, LevelExitEvent, PracticeLoadCmd
from spinlab.condition_registry import ConditionRegistry
from spinlab.retroarch.orchestrator import RetroArchOrchestrator
from spinlab.retroarch.poller import Poller, PollerDeps
from spinlab.retroarch.responses import StatusInfo
from spinlab.retroarch.snapshot import MemorySnapshot
from spinlab.retroarch.state_io import StateIO
from spinlab.retroarch.timing import PracticeTiming, SpeedRunTiming


class _FakeNCI:
    """NCIClient stub. version()/get_status()/reset()/load_state_slot()/save_state()/close() supported."""

    def __init__(self) -> None:
        self.load_state_slot_calls: list[int] = []
        self.save_state_calls = 0
        self.close_calls = 0

    def version(self) -> str:
        return "1.22.2-fake"

    def get_status(self) -> StatusInfo:
        return StatusInfo(state="PAUSED", system="SNES", game="Test Game", crc32="ff")

    def load_state_slot(self, slot: int) -> None:
        self.load_state_slot_calls.append(slot)

    def save_state(self) -> None:
        self.save_state_calls += 1

    def reset(self) -> None:
        pass

    def close(self) -> None:
        self.close_calls += 1


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


def _make_snapshot_fn(seq: Iterator[MemorySnapshot]):
    """Return a read_snapshot callable that drains seq then loops the last dead snapshot."""
    def fn(_client) -> MemorySnapshot:
        try:
            return next(seq)
        except StopIteration:
            # Loop the last snapshot indefinitely so the poller can keep running.
            return _snap(player_anim=9)
    return fn


def _build_orchestrator(tmp_path: Path, snapshots) -> tuple:
    """Wire all the components together end-to-end."""
    nci = _FakeNCI()
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()

    state_io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
        save_timeout_sec=0.5,
    )
    conditions = ConditionRegistry()
    practice = PracticeTiming()
    speed_run = SpeedRunTiming()

    deps = PollerDeps(
        client=nci,
        read_snapshot=_make_snapshot_fn(snapshots),
        on_event=lambda ev: None,  # wired below to orchestrator.on_poller_event
        state_path_for=state_io.resolve_event_path,
        conditions_registry=conditions,
    )
    poller = Poller(deps, period_sec=0.001)

    orch = RetroArchOrchestrator(
        client=nci,
        state_io=state_io,
        poller=poller,
        conditions=conditions,
        practice_timing=practice,
        speed_run_timing=speed_run,
    )
    # Wire the poller's on_event into the orchestrator's adapter+enqueue path.
    # PollerDeps is a mutable dataclass; poller holds a reference to the same
    # object and calls self._deps.on_event(...) so this assignment takes effect.
    deps.on_event = orch.on_poller_event
    return orch, nci, state_io, sl_dir


@pytest.mark.asyncio
async def test_end_to_end_death_flows_through_pipeline(tmp_path):
    """Death event from the poller arrives in the orchestrator's queue as a dict."""
    snapshots = iter([
        _snap(player_anim=0),  # seed: prev starts here
        _snap(player_anim=9),  # death edge: curr transitions to dead
    ])
    orch, nci, state_io, sl_dir = _build_orchestrator(tmp_path, snapshots)

    ok = await orch.connect()
    assert ok is True

    from spinlab.protocol import RomInfoEvent

    # First event is rom_info from connect().
    rom = await orch.recv_event(timeout=0.5)
    assert isinstance(rom, RomInfoEvent)

    # Then a death event from the poller (after the 0->9 transition).
    received: list = []
    deadline = asyncio.get_event_loop().time() + 1.0
    while asyncio.get_event_loop().time() < deadline:
        ev = await orch.recv_event(timeout=0.1)
        if ev is not None:
            received.append(ev)
            if isinstance(ev, DeathEvent):
                break

    assert any(isinstance(ev, DeathEvent) for ev in received), f"got: {received}"

    await orch.disconnect()
    assert nci.close_calls == 1


@pytest.mark.asyncio
async def test_end_to_end_practice_attempt_result_emitted(tmp_path):
    """PracticeLoadCmd -> injected events -> attempt_result emitted from queue."""
    # No real death sequence from the poller in this test -- we drive timing
    # directly via on_poller_event calls rather than the poller's snapshot loop.
    snapshots = iter([_snap()])  # poller will see one snapshot then loop the default
    orch, nci, state_io, sl_dir = _build_orchestrator(tmp_path, snapshots)

    # A SpinLab state file must exist so load_state_from_path doesn't raise.
    spinlab_state = sl_dir / "saved.state"
    spinlab_state.write_bytes(b"FAKE_STATE_DATA")

    await orch.connect()
    await orch.recv_event(timeout=0.5)  # drain rom_info

    cmd = PracticeLoadCmd(
        id="seg-test",
        state_path=str(spinlab_state),
        end_type="goal",
        expected_time_ms=5000,
        auto_advance_delay_ms=50,  # very short for test speed
        death_penalty_ms=3200,
    )
    await orch.send_command(cmd)
    assert orch._practice_timing.is_armed is True

    # Inject a death and a goal-exit through the orchestrator's event path.
    # This bypasses the poller's snapshot loop so the test is deterministic.
    orch.on_poller_event(DeathEvent(timestamp_ms=100))
    orch.on_poller_event(
        LevelExitEvent(timestamp_ms=200, level=5, room=0, goal="normal", elapsed_ms=2000, frame=120)
    )

    from spinlab.protocol import AttemptResultEvent

    # The tick loop is running; attempt_result should fire after auto_advance_delay_ms.
    received: list = []
    deadline = asyncio.get_event_loop().time() + 1.0
    while asyncio.get_event_loop().time() < deadline:
        ev = await orch.recv_event(timeout=0.1)
        if ev is not None:
            received.append(ev)
            if isinstance(ev, AttemptResultEvent):
                break

    attempt_results = [r for r in received if isinstance(r, AttemptResultEvent)]
    assert len(attempt_results) == 1, f"got events: {received}"
    r = attempt_results[0]
    assert r.segment_id == "seg-test"
    assert r.completed is True
    assert r.deaths == 1

    await orch.disconnect()
