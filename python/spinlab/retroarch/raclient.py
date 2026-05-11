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
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from spinlab.retroarch.exceptions import NCIError
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

# Movie record/play file-stability polling.
MOVIE_POLL_INTERVAL_SEC = 0.2
MOVIE_POLL_ATTEMPTS = 5

# Movie playback verification window: sample WRAM, sleep, sample again. If
# both samples match, RA never advanced a frame — it refused the file.
# 150ms ≈ 9 frames at 60Hz; reliably catches a stuck emulator.
PLAYBACK_VERIFY_SLEEP_SEC = 0.15
PLAYBACK_VERIFY_BYTES = 16

# Pattern for parsing RA's log lines that report the current replay slot.
# RA emits in three forms:
#   - At startup:                "[Replay] Found last replay slot: #N"
#   - After SLOT_PLUS/MINUS:     "[Replay] Replay slot: N"
#   - When recording starts:     "[Replay] Starting movie record to "<path>.replay<N>""
# The third match is critical: replay_auto_index="true" bumps the slot at
# record-start without emitting a "Replay slot:" line. Without it the parser
# misses the auto-index bump and stages at the stale pre-record slot.
_REPLAY_SLOT_LOG_PATTERN = re.compile(
    r"\[Replay\] (?:"
    r"Replay slot: |"
    r"Found last replay slot: #|"
    r'Starting movie record to "[^"]*\.replay'
    r")(\d+)"
)


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


class MovieRecordError(RAClientError):
    """RA produced no new or rewritten movie file after HALT_REPLAY."""


class MoviePlaybackError(RAClientError):
    """RA refused to load the movie file — no WRAM advance after PLAY_REPLAY."""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConnectInfo:
    """Returned from ``RAClient.connect`` on success."""

    rom_filename: str
    system: str
    crc32: str


@dataclass
class MovieRecording:
    """Handle for an in-progress recording. Caller invokes ``.stop()`` to finish."""

    path: Path
    _stop: Callable[[], Awaitable[Path]] = field(repr=False)

    async def stop(self) -> Path:
        return await self._stop()


@dataclass
class MoviePlayback:
    """Handle for in-progress playback. Caller invokes ``.stop()`` to halt."""

    path: Path
    frame_count: int
    _stop: Callable[[], Awaitable[None]] = field(repr=False)

    async def stop(self) -> None:
        await self._stop()


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
        if self._ra_movie_dir is None:
            raise RAClientError(
                "record_movie called but ra_movie_dir is not configured — "
                "set it at RAClient construction."
            )
        movie_dir = self._ra_movie_dir
        movie_dir.mkdir(parents=True, exist_ok=True)

        # Snapshot {path → mtime} for every existing file. Detects both new
        # files and in-place rewrites (RA reuses .replay<slot> names even
        # with replay_auto_index=true if the slot collides).
        baseline = {
            f: f.stat().st_mtime
            for f in movie_dir.iterdir()
            if f.is_file()
        }
        await asyncio.to_thread(self._nci.record_replay)
        logger.info(
            'record_movie start dest="%s" baseline_files=%d',
            dest_path, len(baseline),
        )

        async def _stop() -> Path:
            return await asyncio.to_thread(self._stop_recording, dest_path, baseline)

        return MovieRecording(path=dest_path, _stop=_stop)

    def _stop_recording(self, dest_path: Path, baseline: dict[Path, float]) -> Path:
        assert self._ra_movie_dir is not None
        movie_dir = self._ra_movie_dir
        self._nci.halt_replay()

        # Two-phase poll: find the changed file, then wait for its size+mtime
        # to STABILIZE for a full poll interval before copying. RA's
        # halt_replay is fire-and-forget — without the stability check we
        # catch the file mid-flush and end up with a truncated .replay.
        changed: Path | None = None
        for _ in range(MOVIE_POLL_ATTEMPTS):
            for f in movie_dir.iterdir():
                if not f.is_file():
                    continue
                baseline_mt = baseline.get(f)
                cur_mt = f.stat().st_mtime
                if baseline_mt is None or cur_mt > baseline_mt:
                    if changed is None or cur_mt > changed.stat().st_mtime:
                        changed = f
            if changed is not None:
                break
            time.sleep(MOVIE_POLL_INTERVAL_SEC)

        if changed is not None:
            last_size = changed.stat().st_size
            last_mt = changed.stat().st_mtime
            for _ in range(MOVIE_POLL_ATTEMPTS):
                time.sleep(MOVIE_POLL_INTERVAL_SEC)
                cur = changed.stat()
                if cur.st_size == last_size and cur.st_mtime == last_mt:
                    break
                last_size = cur.st_size
                last_mt = cur.st_mtime

        if changed is None:
            # Most common cause: retroarch.cfg has replay_max_keep=0 (the
            # default) AND .replay files already exist in movie_dir. RA
            # silently refuses to write in that case.
            existing_replays = sum(
                1 for f in baseline if f.suffix.startswith(".replay")
            )
            hint = ""
            if existing_replays > 0:
                hint = (
                    f" — {existing_replays} existing .replay* file(s) in dir; "
                    'this often means retroarch.cfg has replay_max_keep=0. '
                    'Set replay_max_keep = "99" to fix.'
                )
            logger.warning(
                'record_movie no_new_file dir="%s" attempts=%d%s',
                movie_dir, MOVIE_POLL_ATTEMPTS, hint,
            )
            raise MovieRecordError(
                f"No new or rewritten movie file appeared in {movie_dir} "
                f"after {MOVIE_POLL_ATTEMPTS} polls{hint}"
            )

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(changed), str(dest_path))
        # Best-effort source unlink — Windows may hold the handle briefly.
        for attempt in range(MOVIE_POLL_ATTEMPTS):
            try:
                os.unlink(changed)
                break
            except PermissionError:
                if attempt < MOVIE_POLL_ATTEMPTS - 1:
                    time.sleep(MOVIE_POLL_INTERVAL_SEC)
        logger.info(
            'record_movie stop src="%s" dest="%s"', changed.name, dest_path,
        )
        return dest_path

    async def play_movie(self, src_path: Path) -> MoviePlayback:
        """Stage ``src_path`` at RA's current runtime slot, fire PLAY_REPLAY,
        verify by sampling WRAM advances.

        Returns a handle exposing ``frame_count`` (from a sibling ``.json``
        sidecar if present, else 0) and ``.stop()``. Raises
        ``MoviePlaybackError`` if RA refused the file (no WRAM advance
        within the verification window).
        """
        if self._ra_movie_dir is None:
            raise RAClientError(
                "play_movie called but ra_movie_dir is not configured."
            )
        src = Path(src_path)
        if not src.exists():
            raise StateLoadError(f"Movie source not found: {src}")

        # Find RA's current runtime replay slot. With replay_auto_index="true"
        # RA bumps this per-game across sessions and there's no NCI command
        # to query it — log scraping is the cleanest signal.
        target_slot = self._find_current_replay_slot() or 0

        staged = self._ra_movie_dir / f"{self._game_basename}.replay{target_slot}"
        await asyncio.to_thread(self._stage_and_play, src, staged)

        if not await self._verify_playback_advanced():
            # RA's "Failed to load movie file" popup is in-app only — invisible
            # over NCI. Halt and surface a clear error.
            await asyncio.to_thread(self._nci.halt_replay)
            logger.warning(
                'play_movie verify_failed src="%s" no_wram_advance_within_ms=%d',
                src, int(PLAYBACK_VERIFY_SLEEP_SEC * 1000),
            )
            raise MoviePlaybackError(
                f"RA refused to load {src.name} (likely ROM-checksum "
                "mismatch or unreadable file). Check RA's log for the "
                "underlying error."
            )

        frame_count = _read_frame_count(src)

        logger.info(
            'play_movie start src="%s" slot=%d frame_count=%d',
            src, target_slot, frame_count,
        )

        async def _stop() -> None:
            await asyncio.to_thread(self._stop_playback, staged)

        return MoviePlayback(path=src, frame_count=frame_count, _stop=_stop)

    def _stage_and_play(self, src: Path, staged: Path) -> None:
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(staged))
        self._nci.play_replay()

    def _stop_playback(self, staged: Path) -> None:
        try:
            self._nci.halt_replay()
        finally:
            if staged.exists():
                try:
                    staged.unlink()
                except OSError as exc:
                    logger.warning(
                        'play_movie stop unlink_failed staged="%s" err=%s',
                        staged, exc,
                    )
        logger.info('play_movie stop staged="%s"', staged)

    def _find_current_replay_slot(self) -> int | None:
        """Read RA's most recent log file to infer the current runtime slot.

        Returns ``None`` if log dir is missing, no logs exist, or no replay
        lines have been emitted yet.
        """
        if self._ra_log_dir is None or not self._ra_log_dir.exists():
            return None
        logs = sorted(
            self._ra_log_dir.glob("retroarch__*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not logs:
            return None
        try:
            text = logs[0].read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning('play_movie log_read_failed path="%s" err=%s', logs[0], exc)
            return None
        matches = _REPLAY_SLOT_LOG_PATTERN.findall(text)
        if not matches:
            return None
        return int(matches[-1])

    async def _verify_playback_advanced(self) -> bool:
        """True if WRAM advances over a brief sample window, i.e. RA is
        running frames after PLAY_REPLAY.
        """
        try:
            before = await self.read_ram(0x0000, PLAYBACK_VERIFY_BYTES)
            await asyncio.sleep(PLAYBACK_VERIFY_SLEEP_SEC)
            after = await self.read_ram(0x0000, PLAYBACK_VERIFY_BYTES)
        except NCIError as exc:
            logger.warning('play_movie verify_read_failed err=%s', exc)
            return False
        return before != after

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_frame_count(movie_path: Path) -> int:
    """Read frame_count from the sibling ``<movie>.json`` metadata file.

    SpinLab writes this sidecar at recording time so the dashboard can show
    progress for movies whose length RA itself doesn't report. Returns 0 if
    the sidecar doesn't exist or is unreadable.
    """
    meta = movie_path.with_suffix(".json")
    if not meta.exists():
        return 0
    try:
        return int(json.loads(meta.read_text()).get("frame_count", 0))
    except Exception as exc:
        logger.warning('frame_count_read_failed path="%s" err=%s', meta, exc)
        return 0
