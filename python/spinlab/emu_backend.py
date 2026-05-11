"""EmuBackend — Protocol defining the emulator backend surface.

The dashboard talks to a RetroArchOrchestrator via this protocol. The rest of
the codebase (SessionManager, capture controllers, practice/speed-run loops)
depends only on this surface, not on any concrete backend implementation.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmuBackend(Protocol):
    """Protocol satisfied by RetroArchOrchestrator (and any future backend).

    Structural duck-typing — implementations inherit from nothing; the protocol
    checks structural compatibility only.
    """

    on_disconnect: Callable | None

    @property
    def is_connected(self) -> bool: ...

    async def connect(self, timeout: float = ...) -> bool: ...

    async def disconnect(self) -> None: ...

    async def send_command(self, cmd: object) -> None: ...

    async def recv_event(self, timeout: float | None = ...) -> object | None: ...

    async def save_state(self, segment_id: str) -> None:
        """Persist a savestate file for the given segment id.

        The orchestrator triggers an NCI SAVE_STATE and moves the resulting
        file into SpinLab's segment-keyed directory.
        """
        ...

    async def load_state(self, state_path: str) -> None:
        """Load a savestate file from an absolute path.

        The orchestrator copies the file into RA's reserved slot and fires
        LOAD_STATE_SLOT via NCI.
        """
        ...
