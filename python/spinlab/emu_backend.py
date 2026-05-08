"""EmuBackend — duck-typed surface shared by TcpManager and RetroArchOrchestrator.

The dashboard talks to one emulator backend at a time, selected at startup
from `config.emulator.backend`. Both backends expose the same surface so
the rest of the codebase (SessionManager, capture controllers, practice/
speed-run loops) doesn't need to know which one is wired up.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmuBackend(Protocol):
    """The subset of TcpManager that capture/practice/dashboard code uses.

    Both `TcpManager` (Mesen-Lua) and `RetroArchOrchestrator` (RetroArch NCI)
    satisfy this protocol via duck typing. No inheritance — the protocol is
    structural so the two backends remain free of a common base class.
    """

    on_disconnect: Callable | None

    @property
    def is_connected(self) -> bool: ...

    async def connect(self, timeout: float = ...) -> bool: ...

    async def disconnect(self) -> None: ...

    async def send_command(self, cmd: object) -> None: ...

    async def recv_event(self, timeout: float | None = ...) -> dict | None: ...

    async def save_state(self, segment_id: str) -> None:
        """Persist a savestate file for the given segment id.

        Under RetroArch the orchestrator triggers an NCI SAVE_STATE and moves
        the resulting file into SpinLab's segment-keyed directory. Under
        Mesen this is a no-op because Lua writes states autonomously when
        it observes save-eligible events; Python does not need to act.
        """
        ...

    async def load_state(self, state_path: str) -> None:
        """Load a savestate file from an absolute path.

        Under RetroArch the orchestrator copies the file into RA's reserved
        slot and fires LOAD_STATE_SLOT. Under Mesen this is a no-op because
        Lua's practice loop loads states autonomously after every
        ``practice_load`` command and on every detected death.
        """
        ...
