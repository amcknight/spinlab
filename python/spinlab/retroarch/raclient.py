"""High-level RetroArch client.

Owns every RA-specific operation: NCI socket, savestate persistence with
mtime confirmation, movie record/playback with slot resolution, hotkey-press
quirks, RA log parsing. The rest of SpinLab depends only on this surface.

Async throughout — the underlying NCI transport is sync UDP; RAClient wraps
``asyncio.to_thread`` once internally so callers never sprinkle it.

Logging discipline (for agent-debuggability): operations log at INFO with
keyword=value structured context; failures log at WARNING with the context
that led to them. Per-NCI-command traffic logs at DEBUG (off by default).
Callers log decisions ("entering practice mode"); RAClient logs execution
("save_state segment=X took_ms=120"). No double-logging.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.movie_io import (
    MoviePlayback,
    MoviePlaybackError,
    MovieRecordError,
    MovieRecording,
    RAMovieIO,
)
from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.responses import StatusInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Reserved RA savestate slot used as the staging file for SpinLab loads —
# well outside any plausible user slot range so we never clobber a manual save.
DEFAULT_RESERVED_SLOT = 9999

# A healthy SAVE_STATE finishes in well under 100ms; 1s is the budget for
# RA's slot file mtime to advance.
DEFAULT_SAVE_TIMEOUT_SEC = 1.0

# After LOAD_STATE_SLOT, RA reads the slot file synchronously on its next
# emulator frame (~16ms at 60Hz). 100ms is a generous margin before we delete
# the staging file.
DEFAULT_LOAD_SETTLE_SEC = 0.1

# SAVE_STATE intermittently no-ops when RA is in a transitioning state
# (level load, fade, brief pause). Total worst-case wait per save:
# attempts × (timeout + backoff) ≈ 3 × 1.2s = 3.6s.
SAVE_RETRY_ATTEMPTS = 3
SAVE_RETRY_BACKOFF_SEC = 0.2

# RA may hold the slot file open for ~100–300ms after writing it; on Windows
# os.rename raises PermissionError. Retry the move briefly before falling back
# to a copy that leaves the source for cleanup.
MOVE_RETRY_ATTEMPTS = 5
MOVE_RETRY_BACKOFF_SEC = 0.1

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RAClientError(Exception):
    """Base error for RAClient operations."""


class NotReachableError(RAClientError):
    """RA's NCI port did not respond to the connection probe."""


class StateSaveTimeoutError(RAClientError):
    """SAVE_STATE was issued but no slot file appeared or advanced in time."""


class StateLoadError(RAClientError):
    """LOAD_STATE_SLOT failed — usually a missing source file or filesystem error."""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConnectInfo:
    """Returned from ``RAClient.connect`` on success."""

    rom_filename: str
    system: str
    crc32: str


# ---------------------------------------------------------------------------
# Hotkeys
# ---------------------------------------------------------------------------

class RAHotkey(StrEnum):
    """RA hotkey names. ``press(key, taps=N)`` honours per-key tap spacing."""

    RESET = "RESET"
    PAUSE_TOGGLE = "PAUSE_TOGGLE"
    FRAME_ADVANCE = "FRAMEADVANCE"
    SAVE_STATE = "SAVE_STATE"
    RECORD_REPLAY = "RECORD_REPLAY"
    HALT_REPLAY = "HALT_REPLAY"
    PLAY_REPLAY = "PLAY_REPLAY"
    REPLAY_SLOT_MINUS = "REPLAY_SLOT_MINUS"
    REPLAY_SLOT_PLUS = "REPLAY_SLOT_PLUS"


@dataclass(frozen=True)
class HotkeyProfile:
    """Per-hotkey input-layer quirks."""

    # Minimum spacing between successive presses of this key to satisfy RA's
    # debounce / confirmation windows.
    min_tap_gap_sec: float


# RA's input layer debounces hotkeys at ~6Hz (167ms between accepts).
# RESET additionally requires the second press inside its anti-accident
# confirmation window; 300ms is comfortably inside both bounds.
_DEFAULT_HOTKEY_PROFILE = HotkeyProfile(min_tap_gap_sec=0.05)
_HOTKEY_PROFILES: dict[RAHotkey, HotkeyProfile] = {
    RAHotkey.RESET: HotkeyProfile(min_tap_gap_sec=0.3),
    RAHotkey.REPLAY_SLOT_MINUS: HotkeyProfile(min_tap_gap_sec=0.18),
    RAHotkey.REPLAY_SLOT_PLUS: HotkeyProfile(min_tap_gap_sec=0.18),
}


# ---------------------------------------------------------------------------
# RAClient
# ---------------------------------------------------------------------------

class RAClient:
    """High-level RetroArch client.

    Single chokepoint for every RA interaction. Manages the NCI socket, the
    reserved-slot dance for save/load, movie record/play with file staging
    and verification, hotkey-press quirks, and a monotonic ``state_version``
    counter the poller uses to detect "you just reloaded; resync your prev".
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 55355,
        ra_savestate_dir: Path,
        ra_log_dir: Path | None = None,
        ra_movie_dir: Path | None = None,
        reserved_slot: int = DEFAULT_RESERVED_SLOT,
        save_timeout_sec: float = DEFAULT_SAVE_TIMEOUT_SEC,
        load_settle_sec: float = DEFAULT_LOAD_SETTLE_SEC,
    ) -> None:
        self._nci = NCIClient(host=host, port=port)
        self._ra_savestate_dir = Path(ra_savestate_dir)
        self._ra_log_dir = ra_log_dir
        self._ra_movie_dir = ra_movie_dir
        self._reserved_slot = reserved_slot
        self._save_timeout_sec = save_timeout_sec
        self._load_settle_sec = load_settle_sec

        self._game_basename: str | None = None
        self._connected = False
        self._state_version = 0

        self._movie_io = RAMovieIO(
            nci=self._nci,
            movie_dir=lambda: self._ra_movie_dir,
            log_dir=lambda: self._ra_log_dir,
            game_basename=lambda: self._game_basename,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def game_basename(self) -> str | None:
        """ROM basename from the last GET_STATUS. ``None`` until ``connect()`` succeeds."""
        return self._game_basename

    @property
    def state_version(self) -> int:
        """Monotonic counter; increments after every successful ``load_state``.

        The poller reads this each tick; a change tells it to treat the next
        snapshot as a fresh prev (replaces the old ``mark_state_loaded`` flag).
        """
        return self._state_version

    @property
    def nci(self) -> NCIClient:
        """Underlying NCI transport. Primarily exposed for the Poller's sync
        RAM-polling loop — elsewhere, prefer ``await read_ram()`` etc.
        """
        return self._nci

    async def connect(self, timeout: float = 5.0) -> ConnectInfo:
        """Probe NCI + GET_STATUS. Caches ``game_basename`` for subsequent ops.

        Raises ``NotReachableError`` if NCI doesn't respond within ``timeout``.
        """
        self._nci.timeout = timeout
        try:
            await asyncio.to_thread(self._nci.version)
        except NCIError as exc:
            raise NotReachableError(f"NCI not reachable: {exc}") from exc

        try:
            status = await asyncio.to_thread(self._nci.get_status)
        except NCIError as exc:
            raise NotReachableError(f"GET_STATUS failed: {exc}") from exc

        old_basename = self._game_basename
        self._game_basename = status.game or None
        self._connected = True

        if self._game_basename and old_basename != self._game_basename:
            # Sweep any leftover reserved-slot file from a previous session
            # (matching the new basename). Best-effort.
            await asyncio.to_thread(self._cleanup_stale_slot_file)

        logger.info(
            'connect ok rom="%s" system=%s crc32=%s',
            status.game or "", status.system or "", status.crc32 or "",
        )
        return ConnectInfo(
            rom_filename=status.game or "",
            system=status.system or "",
            crc32=status.crc32 or "",
        )

    async def disconnect(self) -> None:
        """Close the NCI socket. Idempotent."""
        self._connected = False
        try:
            await asyncio.to_thread(self._nci.close)
        except Exception:
            logger.debug("disconnect: nci.close raised", exc_info=True)
        logger.info("disconnect ok")

    # ------------------------------------------------------------------
    # Status & memory
    # ------------------------------------------------------------------

    async def get_status(self) -> StatusInfo:
        return await asyncio.to_thread(self._nci.get_status)

    async def read_ram(self, addr: int, length: int) -> bytes:
        return await asyncio.to_thread(self._nci.read_ram, addr, length)

    async def write_ram(self, addr: int, data: bytes) -> None:
        await asyncio.to_thread(self._nci.write_ram, addr, data)

    # ------------------------------------------------------------------
    # Save states
    # ------------------------------------------------------------------

    async def save_state(self, dest_path: Path) -> Path:
        """SAVE_STATE → mtime-poll for confirmation → move to ``dest_path``.

        Returns the final destination path. Raises ``StateSaveTimeoutError``
        if RA's slot file does not appear / advance within the retry budget.

        Strategy: snapshot {filename → mtime} for every ``<basename>.state*``
        file in RA's savestate dir, fire SAVE_STATE, then look for whichever
        file was created or had its mtime advance — RA's NCI SAVE_STATE writes
        to *whatever slot* its ``state_slot`` counter is currently at, not a
        slot we control, so a "watch one fixed file" strategy fails when
        ``savestate_auto_index`` is on.
        """
        return await asyncio.to_thread(self._save_state_sync, dest_path)

    def _save_state_sync(self, dest_path: Path) -> Path:
        self._require_basename()
        pattern = f"{self._game_basename}.state*"
        start_ns = time.monotonic_ns()

        last_err = ""
        for attempt in range(SAVE_RETRY_ATTEMPTS):
            new_path = self._try_one_save(pattern)
            if new_path is not None:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                self._move_with_retry(new_path, dest_path)
                took_ms = (time.monotonic_ns() - start_ns) // 1_000_000
                logger.info(
                    'save_state ok dest="%s" attempt=%d took_ms=%d',
                    dest_path, attempt + 1, took_ms,
                )
                return dest_path
            last_err = (
                f"attempt {attempt + 1}: no {pattern} file appeared/advanced "
                f"in {self._save_timeout_sec}s"
            )
            if attempt + 1 < SAVE_RETRY_ATTEMPTS:
                time.sleep(SAVE_RETRY_BACKOFF_SEC)

        # Diagnostic: ask RA what game it actually has loaded right now. If
        # this differs from our cached basename, the user hot-swapped ROMs.
        try:
            cur_game = self._nci.get_status().game or "<none>"
        except Exception:
            cur_game = "<get_status failed>"
        logger.warning(
            'save_state timeout dest="%s" pattern=%s ra_game="%s" %s',
            dest_path, pattern, cur_game, last_err,
        )
        raise StateSaveTimeoutError(
            f"SAVE_STATE failed after {SAVE_RETRY_ATTEMPTS} attempts: "
            f"{last_err}. Watching pattern={pattern!r}; RA reports "
            f"game={cur_game!r}."
        )

    def _try_one_save(self, pattern: str) -> Path | None:
        snapshot: dict[str, float] = {
            p.name: p.stat().st_mtime
            for p in self._ra_savestate_dir.glob(pattern) if p.is_file()
        }
        self._nci.save_state()
        deadline = time.monotonic() + self._save_timeout_sec
        poll_interval = 0.01
        while time.monotonic() < deadline:
            for p in self._ra_savestate_dir.glob(pattern):
                if not p.is_file():
                    continue
                old_mtime = snapshot.get(p.name)
                cur_mtime = p.stat().st_mtime
                if old_mtime is None or cur_mtime > old_mtime:
                    return p
            time.sleep(poll_interval)
        return None

    def _move_with_retry(self, src: Path, dst: Path) -> None:
        last_exc: BaseException | None = None
        for attempt in range(MOVE_RETRY_ATTEMPTS):
            try:
                shutil.move(str(src), str(dst))
                return
            except PermissionError as exc:
                last_exc = exc
                if attempt + 1 < MOVE_RETRY_ATTEMPTS:
                    time.sleep(MOVE_RETRY_BACKOFF_SEC)
        # Best-effort fallback: copy and leave the source. The next save (or
        # the startup sweep) cleans it up.
        try:
            shutil.copyfile(str(src), str(dst))
            logger.warning(
                'save_state move_fallback src="%s" dst="%s" — copied but '
                "couldn't unlink source after %d retries (RA still holds handle)",
                src, dst, MOVE_RETRY_ATTEMPTS,
            )
        except OSError:
            raise last_exc if last_exc else OSError(f"move failed: {src} -> {dst}")

    async def load_state(self, src_path: Path) -> None:
        """Copy ``src_path`` into RA's reserved slot, fire LOAD_STATE_SLOT,
        bump ``state_version``.

        Increments ``state_version`` *immediately after* firing LOAD_STATE_SLOT
        so the poller's next tick sees the new version and resyncs before
        running detection on the post-load snapshot — eliminates the phantom-
        edge window between RA loading and the poller learning about it.

        Raises ``StateLoadError`` on filesystem failure.
        """
        await asyncio.to_thread(self._load_state_sync, src_path)

    def _load_state_sync(self, src_path: Path) -> None:
        self._require_basename()
        src = Path(src_path)
        if not src.exists():
            raise StateLoadError(f"No savestate at {src}")

        slot_path = self._ra_slot_path()
        slot_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copyfile(str(src), str(slot_path))
        except OSError as exc:
            raise StateLoadError(f"Could not stage {src} → {slot_path}: {exc}") from exc

        self._nci.load_state_slot(self._reserved_slot)
        # Bump version immediately after firing the command — poller sees the
        # change on its very next tick and resyncs.
        self._state_version += 1

        logger.info(
            'load_state ok src="%s" slot=%d version=%d',
            src, self._reserved_slot, self._state_version,
        )

        # Give RA time to read the slot file before we delete it. RA reads
        # synchronously on its next frame (~16ms); 100ms is a generous margin.
        if self._load_settle_sec > 0:
            time.sleep(self._load_settle_sec)
        try:
            slot_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                'load_state slot_cleanup_failed path="%s" err=%s',
                slot_path, exc,
            )

    def _ra_slot_path(self) -> Path:
        self._require_basename()
        return self._ra_savestate_dir / f"{self._game_basename}.state{self._reserved_slot}"

    def _cleanup_stale_slot_file(self) -> None:
        """Remove any leftover reserved-slot file from a previous session."""
        if not self._game_basename:
            return
        slot_path = self._ra_slot_path()
        if not slot_path.exists():
            return
        try:
            slot_path.unlink()
            logger.info('startup_sweep removed stale slot_file="%s"', slot_path)
        except OSError as exc:
            logger.warning(
                'startup_sweep could not remove slot_file="%s" err=%s',
                slot_path, exc,
            )

    def _require_basename(self) -> None:
        if not self._game_basename:
            raise RAClientError(
                "RAClient: game basename not set yet — call connect() first "
                "(or check that RetroArch is running and has a ROM loaded)."
            )

    # ------------------------------------------------------------------
    # Movie record / play
    # ------------------------------------------------------------------

    async def record_movie(self, dest_path: Path) -> MovieRecording:
        """Fire RECORD_REPLAY and return a handle whose ``.stop()`` halts RA
        and copies the resulting .replay file to ``dest_path``.
        """
        return await self._movie_io.record_movie(dest_path)

    async def play_movie(self, src_path: Path) -> MoviePlayback:
        """Stage ``src_path`` at RA's current runtime slot, fire PLAY_REPLAY,
        verify by sampling WRAM advances.
        """
        return await self._movie_io.play_movie(src_path)

    @property
    def movie_io(self) -> RAMovieIO:
        return self._movie_io

    # Delegation shim for backward-compat with tests that reach into the
    # private implementation. RAMovieIO owns the real logic; this forwards
    # to it so external tests don't need updating.
    def _find_current_replay_slot(self) -> int | None:
        return self._movie_io._find_current_replay_slot()

    # ------------------------------------------------------------------
    # Hotkeys
    # ------------------------------------------------------------------

    async def press(self, key: RAHotkey, *, taps: int = 1) -> None:
        """Press ``key`` ``taps`` times, honouring the per-key tap-spacing profile."""
        if taps < 1:
            raise ValueError("taps must be >= 1")
        profile = _HOTKEY_PROFILES.get(key, _DEFAULT_HOTKEY_PROFILE)
        for i in range(taps):
            if i > 0:
                await asyncio.sleep(profile.min_tap_gap_sec)
            await asyncio.to_thread(self._nci._send_no_reply, str(key))
        if taps > 1:
            logger.info(
                "press key=%s taps=%d gap_ms=%d",
                key, taps, int(profile.min_tap_gap_sec * 1000),
            )

    async def reset(self) -> None:
        """Hard-reset the emulated console. RA's RESET requires two presses
        to satisfy its anti-accident gate; this method handles that.
        """
        await self.press(RAHotkey.RESET, taps=2)

    def fast_forward_toggle(self) -> None:
        """Toggle RA's fast-forward state. See ``NCIClient.fast_forward_toggle``
        for semantics; the orchestrator uses this around ``PLAY_REPLAY`` so
        ``speed=SPEED_UNCAPPED`` actually runs uncapped instead of being a
        no-op hint that died with the Mesen/Lua era.
        """
        self._nci.fast_forward_toggle()

