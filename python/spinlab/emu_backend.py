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
