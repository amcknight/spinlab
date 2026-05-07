"""StateIO.resolve_event_path wired into a real Poller via PollerDeps."""
import asyncio
from typing import Iterator

import pytest

from spinlab.retroarch.events import (
    LevelEntrance,
    TransitionEvent,
)
from spinlab.retroarch.poller import Poller, PollerDeps
from spinlab.retroarch.snapshot import MemorySnapshot
from spinlab.retroarch.state_io import StateIO


class _FakeNCI:
    def save_state(self) -> None: ...
    def load_state_slot(self, slot: int) -> None: ...


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


def _make_snapshots(seq: Iterator[MemorySnapshot]):
    def fn(_client) -> MemorySnapshot:
        return next(seq)
    return fn


@pytest.mark.asyncio
async def test_poller_uses_state_io_resolver_for_level_entrance(tmp_path):
    sl_dir = tmp_path / "sl"
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir.mkdir()

    nci = _FakeNCI()
    state_io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="G",
    )

    snapshots = iter([
        _snap(level_num=5),
        _snap(level_num=5, level_start=1),  # entrance
    ])
    received: list[TransitionEvent] = []

    deps = PollerDeps(
        client=nci,
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
        state_path_for=state_io.resolve_event_path,
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    entrances = [e for e in received if isinstance(e, LevelEntrance)]
    assert len(entrances) == 1
    assert entrances[0].state_path.endswith("entrance_5_0.state")
