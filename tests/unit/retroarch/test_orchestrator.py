"""RetroArchOrchestrator unit tests with fake NCIClient/StateIO/Poller."""
from __future__ import annotations

import asyncio

import pytest

from spinlab.protocol import (
    ColdFillLoadCmd,
    FillGapLoadCmd,
    GameContextCmd,
    PracticeLoadCmd,
    PracticeStopCmd,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayStopCmd,
    ResetCmd,
    SetConditionsCmd,
    SetInvalidateComboCmd,
    SpeedRunLoadCmd,
    SpeedRunStopCmd,
)
from spinlab.retroarch.conditions import ConditionRegistry
from spinlab.retroarch.orchestrator import RetroArchOrchestrator
from spinlab.retroarch.timing import PracticeTiming, SpeedRunTiming


class _FakeClient:
    """Stub NCIClient. Records calls; does not hit the wire."""

    def __init__(self) -> None:
        self.reset_calls = 0
        self.version_response = "1.22.2"
        self.close_calls = 0

    def version(self) -> str:
        return self.version_response

    def get_status(self):
        from spinlab.retroarch.responses import StatusInfo
        return StatusInfo(state="PAUSED", system="SNES", game="Test Game", crc32="ff")

    def reset(self) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _FakeStateIO:
    def __init__(self) -> None:
        self.load_segment_calls: list[str] = []
        self.load_path_calls: list = []
        self.saved_segments: list[str] = []
        self.set_basename_calls: list[str] = []

    def load_segment_state(self, segment_id: str) -> None:
        self.load_segment_calls.append(segment_id)

    def load_state_from_path(self, path) -> None:
        self.load_path_calls.append(path)

    def save_segment_state(self, segment_id: str) -> None:
        self.saved_segments.append(segment_id)

    def update_game_basename(self, name: str) -> None:
        self.set_basename_calls.append(name)

    def resolve_event_path(self, ev) -> str:
        return ""


class _FakePoller:
    """Implements the subset of Poller surface the orchestrator uses."""

    def __init__(self) -> None:
        self.mark_state_loaded_calls = 0
        self.cold_fill_activations: list[str] = []
        self._stopped = False

    async def run(self) -> None:
        # Loop forever until stopped, sleeping each tick.
        while not self._stopped:
            await asyncio.sleep(0.001)

    def mark_state_loaded(self) -> None:
        self.mark_state_loaded_calls += 1

    def activate_cold_fill(self, segment_id: str) -> None:
        self.cold_fill_activations.append(segment_id)

    def stop(self) -> None:
        self._stopped = True


def _build_orchestrator():
    client = _FakeClient()
    state_io = _FakeStateIO()
    poller = _FakePoller()
    conditions = ConditionRegistry()
    practice = PracticeTiming()
    speed_run = SpeedRunTiming()
    orch = RetroArchOrchestrator(
        client=client,
        state_io=state_io,
        poller=poller,
        conditions=conditions,
        practice_timing=practice,
        speed_run_timing=speed_run,
    )
    return orch, client, state_io, poller, conditions


@pytest.mark.asyncio
async def test_connect_emits_rom_info_event():
    """connect() should put a rom_info dict on events."""
    orch, client, state_io, poller, conditions = _build_orchestrator()
    ok = await orch.connect()
    assert ok is True
    assert orch.is_connected is True
    ev = await asyncio.wait_for(orch.events.get(), timeout=0.1)
    assert ev["event"] == "rom_info"
    assert ev["filename"] == "Test Game"
    await orch.disconnect()


@pytest.mark.asyncio
async def test_connect_propagates_game_basename_to_state_io():
    """connect() should update state_io's game basename from RA's GET_STATUS.

    This is the fix for the silent save failure bug: previously a stale
    `ra_game_basename` in config caused mtime polling to time out forever
    because it watched the wrong filename. Now RA's report is authoritative.
    """
    orch, client, state_io, poller, conditions = _build_orchestrator()
    # State_io starts with whatever the fake builds it with (or empty).
    await orch.connect()
    # The fake client.get_status() returns game="Test Game", so basename
    # should now be "Test Game".
    assert state_io.set_basename_calls == ["Test Game"]
    await orch.disconnect()


@pytest.mark.asyncio
async def test_connect_returns_false_when_nci_fails():
    """If client.version() raises NCIError, connect returns False."""
    from spinlab.retroarch.exceptions import NCITimeout
    client = _FakeClient()

    def fail() -> str:
        raise NCITimeout("simulated")

    client.version = fail  # type: ignore
    state_io = _FakeStateIO()
    poller = _FakePoller()
    orch = RetroArchOrchestrator(
        client=client, state_io=state_io, poller=poller,
        conditions=ConditionRegistry(),
        practice_timing=PracticeTiming(),
        speed_run_timing=SpeedRunTiming(),
    )
    ok = await orch.connect()
    assert ok is False
    assert orch.is_connected is False


@pytest.mark.asyncio
async def test_disconnect_stops_poller_and_closes_client():
    orch, client, state_io, poller, conditions = _build_orchestrator()
    await orch.connect()
    await orch.disconnect()
    assert orch.is_connected is False
    assert poller._stopped is True
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_practice_load_cmd_loads_state_and_arms_timing():
    orch, client, state_io, poller, conditions = _build_orchestrator()
    await orch.connect()
    # Drain rom_info
    await orch.events.get()
    cmd = PracticeLoadCmd(id="seg-x", state_path="/p/seg.state", end_type="goal",
                          expected_time_ms=5000, auto_advance_delay_ms=200,
                          death_penalty_ms=3200)
    await orch.send_command(cmd)
    assert state_io.load_path_calls == ["/p/seg.state"]
    assert poller.mark_state_loaded_calls == 1
    # Practice timing should be armed
    assert orch._practice_timing.is_armed is True
    await orch.disconnect()


@pytest.mark.asyncio
async def test_practice_stop_cmd_disarms():
    orch, client, state_io, poller, conditions = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    await orch.send_command(PracticeLoadCmd(id="seg-x", state_path="/p", end_type="goal",
                                            auto_advance_delay_ms=200, death_penalty_ms=3200))
    await orch.send_command(PracticeStopCmd())
    assert orch._practice_timing.is_armed is False
    await orch.disconnect()


@pytest.mark.asyncio
async def test_death_during_practice_triggers_state_reload():
    """Regression: practice mode must reload the segment's start state on
    Death (matches Lua's `pending_loads` queue). Without this, the player
    retries from wherever they respawned, not from the segment boundary."""
    from spinlab.retroarch.events import Death

    orch, _, state_io, poller, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()  # drain rom_info

    await orch.send_command(PracticeLoadCmd(
        id="seg-x", state_path="/p/seg.state", end_type="goal",
        auto_advance_delay_ms=200, death_penalty_ms=3200,
    ))
    # Initial load happened.
    assert state_io.load_path_calls == ["/p/seg.state"]

    # Simulate the player dying mid-segment.
    orch.on_poller_event(Death(timestamp_ms=100, level_num=5))
    await asyncio.sleep(0.05)  # let the worker-thread reload run

    # Reload fired with the same state path.
    assert state_io.load_path_calls == ["/p/seg.state", "/p/seg.state"]
    # mark_state_loaded was called twice: once on PracticeLoad, once on death.
    assert poller.mark_state_loaded_calls == 2

    await orch.disconnect()


@pytest.mark.asyncio
async def test_death_outside_practice_does_not_reload():
    """Death events outside practice (e.g. during reference) must NOT trigger
    a state reload — that would clobber the user's recording."""
    from spinlab.retroarch.events import Death

    orch, _, state_io, poller, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    # Not in practice — practice_timing is unarmed.
    orch.on_poller_event(Death(timestamp_ms=0, level_num=5))
    await asyncio.sleep(0.05)
    # No load fired from the death.
    assert state_io.load_path_calls == []
    await orch.disconnect()


@pytest.mark.asyncio
async def test_cold_fill_load_activates_poller():
    orch, client, state_io, poller, conditions = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    await orch.send_command(ColdFillLoadCmd(state_path="/p/cold.state", segment_id="seg-cold"))
    assert state_io.load_path_calls == ["/p/cold.state"]
    assert poller.cold_fill_activations == ["seg-cold"]
    assert poller.mark_state_loaded_calls == 1
    await orch.disconnect()


@pytest.mark.asyncio
async def test_fill_gap_load_loads_state_no_cold_fill():
    orch, client, state_io, poller, conditions = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    await orch.send_command(FillGapLoadCmd(state_path="/p/gap.state"))
    assert state_io.load_path_calls == ["/p/gap.state"]
    assert poller.cold_fill_activations == []  # NOT activated
    assert poller.mark_state_loaded_calls == 1
    await orch.disconnect()


@pytest.mark.asyncio
async def test_reset_cmd_calls_client_reset():
    orch, client, state_io, poller, conditions = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    await orch.send_command(ResetCmd())
    assert client.reset_calls == 1
    await orch.disconnect()


@pytest.mark.asyncio
async def test_set_conditions_cmd_updates_registry():
    orch, client, state_io, poller, conditions = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    defs = [{"name": "x", "address": 0x100, "size": 1}]
    await orch.send_command(SetConditionsCmd(definitions=defs))
    # Registry should now have one spec.
    assert len(conditions._specs) == 1
    assert conditions._specs[0].name == "x"
    await orch.disconnect()


@pytest.mark.asyncio
async def test_set_invalidate_combo_is_noop():
    """Under RA backend the combo is dashboard-button only."""
    orch, _, _, _, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    # Should not raise.
    await orch.send_command(SetInvalidateComboCmd(combo=["L", "Select"]))
    await orch.disconnect()


@pytest.mark.asyncio
async def test_game_context_is_noop():
    orch, _, _, _, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    await orch.send_command(GameContextCmd(game_id="gid", game_name="Game Name"))
    await orch.disconnect()


@pytest.mark.parametrize("cmd_cls", [ReplayCmd, ReplayStopCmd])
@pytest.mark.asyncio
async def test_replay_commands_raise_backend_not_implemented(cmd_cls):
    """BSV replay genuinely needs Phase E; orchestrator must raise the typed
    ActionError so the route layer surfaces it as HTTP 501 instead of 500."""
    from spinlab.errors import BackendNotImplementedError

    orch, _, _, _, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    with pytest.raises(BackendNotImplementedError) as exc_info:
        await orch.send_command(cmd_cls())
    assert exc_info.value.http_code == 501
    await orch.disconnect()


@pytest.mark.asyncio
async def test_reference_start_enables_recording_does_not_raise():
    """ReferenceStart succeeds under RA backend (state captures, no BSV)."""
    orch, _, _, _, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    assert orch._recording is False
    await orch.send_command(ReferenceStartCmd(path="/tmp/seg.spinrec"))
    assert orch._recording is True
    await orch.disconnect()


@pytest.mark.asyncio
async def test_reference_stop_clears_recording_and_emits_rec_saved():
    """ReferenceStop clears the flag and emits a synthetic rec_saved (empty path)."""
    orch, _, _, _, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()  # drain rom_info
    await orch.send_command(ReferenceStartCmd(path="/tmp/seg.spinrec"))
    await orch.send_command(ReferenceStopCmd())
    assert orch._recording is False
    ev = await asyncio.wait_for(orch.events.get(), timeout=0.1)
    assert ev == {"event": "rec_saved", "path": "", "frame_count": 0}
    await orch.disconnect()


@pytest.mark.asyncio
async def test_reference_recording_triggers_save_on_level_entrance():
    """During reference recording, LevelEntrance fires state_io.save_segment_state."""
    from spinlab.retroarch.events import LevelEntrance

    orch, _, state_io, _, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    await orch.send_command(ReferenceStartCmd(path="/tmp/x.spinrec"))

    orch.on_poller_event(LevelEntrance(timestamp_ms=0, level=5, room=2))
    # Save happens in a worker thread to keep the event loop responsive;
    # let the scheduled task run before asserting.
    await asyncio.sleep(0.05)

    # FakeStateIO records save_segment_state calls.
    assert state_io.saved_segments == ["entrance_5_2"]
    await orch.disconnect()


@pytest.mark.asyncio
async def test_no_save_outside_recording_for_entrance():
    """Without reference recording active, LevelEntrance must NOT trigger a save."""
    from spinlab.retroarch.events import LevelEntrance

    orch, _, state_io, _, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()

    orch.on_poller_event(LevelEntrance(timestamp_ms=0, level=5, room=2))
    assert state_io.saved_segments == []
    await orch.disconnect()


@pytest.mark.asyncio
async def test_cold_fill_spawn_always_saves_regardless_of_recording_flag():
    """Cold-fill captures are decoupled from reference recording — they fire
    through their own segment_id and must save independently."""
    from spinlab.retroarch.events import Spawn

    orch, _, state_io, _, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()

    orch.on_poller_event(Spawn(
        timestamp_ms=0, level_num=5,
        is_cold_cp=True, state_captured=True,
        cp_ordinal=1, segment_id="seg-cold-x",
    ))
    await asyncio.sleep(0.05)  # let the worker-thread save complete
    assert state_io.saved_segments == ["seg-cold-x"]
    await orch.disconnect()


@pytest.mark.asyncio
async def test_speed_run_load_arms_and_stop_disarms():
    orch, _, state_io, poller, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    await orch.send_command(SpeedRunLoadCmd(id="run-1", state_path="/p/sr.state"))
    assert orch._speed_run_timing.is_armed is True
    assert state_io.load_path_calls == ["/p/sr.state"]
    await orch.send_command(SpeedRunStopCmd())
    assert orch._speed_run_timing.is_armed is False
    await orch.disconnect()


@pytest.mark.asyncio
async def test_speed_run_load_forwards_delay_kwargs():
    """SpeedRunLoadCmd.death_delay_ms and auto_advance_delay_ms reach the timing object."""
    orch, _, state_io, poller, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()
    cmd = SpeedRunLoadCmd(
        id="run-1",
        state_path="/p/sr.state",
        death_delay_ms=2000,
        auto_advance_delay_ms=2500,
    )
    await orch.send_command(cmd)
    assert orch._speed_run_timing._death_delay_ms == 2000
    assert orch._speed_run_timing._auto_advance_delay_ms == 2500
    await orch.disconnect()


@pytest.mark.asyncio
async def test_unknown_command_logged_no_raise():
    orch, _, _, _, _ = _build_orchestrator()
    await orch.connect()
    await orch.events.get()

    class WeirdCmd:
        pass

    # Should not raise.
    await orch.send_command(WeirdCmd())
    await orch.disconnect()


@pytest.mark.asyncio
async def test_recv_event_returns_dict():
    """recv_event matches TcpManager's API."""
    orch, _, _, _, _ = _build_orchestrator()
    await orch.connect()
    ev = await orch.recv_event(timeout=0.1)
    assert ev is not None
    assert ev["event"] == "rom_info"
    await orch.disconnect()


@pytest.mark.asyncio
async def test_recv_event_timeout_returns_none():
    orch, _, _, _, _ = _build_orchestrator()
    await orch.connect()
    await orch.recv_event(timeout=0.1)  # drain rom_info
    ev = await orch.recv_event(timeout=0.05)
    assert ev is None
    await orch.disconnect()
