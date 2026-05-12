"""FakeRAClient — async stand-in for spinlab.retroarch.raclient.RAClient.

Records every call. Exposes the same public surface so callers (production
code and tests) interact identically. Configurable per-test via mutable
attributes (``raise_on_connect``, …).

Movie record/play behavior lives in FakeMovieIO, not here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spinlab.retroarch.movie_io import (
    MoviePlayback,
    MoviePlaybackError,
    MovieRecording,
)
from spinlab.retroarch.raclient import (
    ConnectInfo,
    RAHotkey,
)
from spinlab.retroarch.responses import StatusInfo


@dataclass
class FakeRAClient:
    """In-process replacement for RAClient. Async surface matches.

    Default behavior is happy-path: connect returns a ConnectInfo with
    "Test Game" basename, all operations succeed silently. Override per-test
    by setting fields after construction.
    """

    # Connect-time identity.
    rom_filename: str = "Test Game"
    system: str = "SNES"
    crc32: str = "ff"

    # Failure injection.
    raise_on_connect: Exception | None = None

    # Call recording.
    def __post_init__(self) -> None:
        self.save_state_calls: list[Path] = []
        self.load_state_calls: list[Path] = []
        self.press_calls: list[tuple[RAHotkey, int]] = []
        self.reset_calls = 0
        self.fast_forward_toggles = 0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.read_ram_calls: list[tuple[int, int]] = []
        self.write_ram_calls: list[tuple[int, bytes]] = []
        self._state_version = 0
        self._connected = False

    # --- Properties matching RAClient ---

    @property
    def state_version(self) -> int:
        return self._state_version

    @property
    def game_basename(self) -> str | None:
        return self.rom_filename if self._connected else None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def nci(self) -> Any:
        """Returns self — many old tests typed this slot as ``client``. Hands
        back something with ``.read_ram`` etc. for poller-style consumers.
        """
        return self

    # --- Lifecycle ---

    async def connect(self, timeout: float = 5.0) -> ConnectInfo:
        self.connect_calls += 1
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        self._connected = True
        return ConnectInfo(
            rom_filename=self.rom_filename,
            system=self.system,
            crc32=self.crc32,
        )

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    # --- Memory ---

    async def get_status(self) -> StatusInfo:
        return StatusInfo(
            state="PAUSED" if self._connected else "CONTENTLESS",
            system=self.system,
            game=self.rom_filename,
            crc32=self.crc32,
        )

    async def read_ram(self, addr: int, length: int) -> bytes:
        self.read_ram_calls.append((addr, length))
        return b"\x00" * length

    async def write_ram(self, addr: int, data: bytes) -> None:
        self.write_ram_calls.append((addr, data))

    # --- States ---

    async def save_state(self, dest_path: Path) -> Path:
        self.save_state_calls.append(Path(dest_path))
        return Path(dest_path)

    async def load_state(self, src_path: Path) -> None:
        self.load_state_calls.append(Path(src_path))
        self._state_version += 1

    # --- Hotkeys ---

    async def press(self, key: RAHotkey, *, taps: int = 1) -> None:
        if taps < 1:
            raise ValueError("taps must be >= 1")
        self.press_calls.append((key, taps))

    async def reset(self) -> None:
        self.reset_calls += 1
        await self.press(RAHotkey.RESET, taps=2)

    def fast_forward_toggle(self) -> None:
        self.fast_forward_toggles += 1


@dataclass
class FakeMovieIO:
    """In-process replacement for RAMovieIO. Async surface matches.

    Default behavior is happy-path: record/play return live handles whose
    ``.stop()`` is a no-op. Override per-test by setting fields after
    construction.
    """

    fail_play_movie: bool = False
    play_movie_error_message: str = "fake refusal"
    frame_count: int = 0

    def __post_init__(self) -> None:
        self.record_movie_calls: list[Path] = []
        self.play_movie_calls: list[Path] = []
        self.last_recording: MovieRecording | None = None
        self.last_playback: MoviePlayback | None = None

    async def record_movie(self, dest_path: Path) -> MovieRecording:
        path = Path(dest_path)
        self.record_movie_calls.append(path)

        async def _stop() -> Path:
            return path

        rec = MovieRecording(path=path, _stop=_stop)
        self.last_recording = rec
        return rec

    async def play_movie(self, src_path: Path) -> MoviePlayback:
        path = Path(src_path)
        self.play_movie_calls.append(path)
        if self.fail_play_movie:
            raise MoviePlaybackError(self.play_movie_error_message)

        async def _stop() -> None:
            pass

        pb = MoviePlayback(path=path, frame_count=self.frame_count, _stop=_stop)
        self.last_playback = pb
        return pb


class FakePoller:
    """Minimal Poller stand-in for orchestrator tests.

    Tracks cold-fill activations and stop calls. ``run()`` loops with brief
    sleeps until ``stop()`` so tests can let it run during ``connect()`` and
    cancel cleanly during ``disconnect()``.
    """

    def __init__(self) -> None:
        self.cold_fill_activations: list[str] = []
        self._stopped = False

    async def run(self) -> None:
        while not self._stopped:
            await asyncio.sleep(0.001)

    def activate_cold_fill(self, segment_id: str) -> None:
        self.cold_fill_activations.append(segment_id)

    def stop(self) -> None:
        self._stopped = True

    @property
    def is_stopped(self) -> bool:
        return self._stopped
