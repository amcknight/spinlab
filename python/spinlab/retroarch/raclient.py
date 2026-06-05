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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from spinlab import log
from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.movie_io import RAMovieIO
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

# After LOAD_STATE_SLOT, poll game-state RAM for up to this long to confirm
# RA actually consumed the slot. The NCI protocol has no success/failure
# reply on LOAD_STATE_SLOT — RA silently rejects malformed state files (the
# failure shows up only in RA's own log). We infer success by comparing a
# small chunk of game-state RAM before and after the load: if it stays
# byte-identical across every poll in the window, the load was a no-op.
# 500ms is comfortably longer than RA's worst-case ~100ms processing while
# short enough that a real failure surfaces before the practice loop's
# 1s attempt-result wait times out.
LOAD_VALIDATION_POLL_INTERVAL_SEC = 0.05
LOAD_VALIDATION_POLL_MAX_SEC = 0.5

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
            movie_dir=self._ra_movie_dir,
            log_dir=self._ra_log_dir,
            game_basename=lambda: self._game_basename,
            on_state_load=self._note_replay_state_load,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def game_basename(self) -> str | None:
        """ROM basename cached at ``connect()`` (or refreshed via
        ``set_game_basename``). ``None`` until ``connect()`` succeeds.

        This is a *cached* value, not the live GET_STATUS ROM — movie
        record/replay staging builds file paths from it, so it must be
        refreshed when RA swaps ROMs mid-session (see ``set_game_basename``).
        """
        return self._game_basename

    def set_game_basename(self, basename: str) -> None:
        """Refresh the cached ROM basename after a mid-session ROM switch.

        ``connect()`` is the only other writer, and it doesn't re-run while the
        socket stays up — so without this, a switch left ``game_basename``
        stale and movie staging used the old game's name (backlog D #2). The
        orchestrator's GET_STATUS ROM-change poll calls this on a detected swap.
        """
        self._game_basename = basename

    async def resume_if_paused(self) -> None:
        """Unpause RA if it is currently paused.

        RA auto-pauses when a movie finishes playing. A replay started while
        RA is paused loads the movie's embedded savestate but never advances a
        frame — the "loads state, starts, then stops without doing anything"
        replay bug (backlog D). The replay path calls this both before
        PLAY_REPLAY (so playback runs) and after a replay ends (so a replay
        never leaves RA paused). PAUSE_TOGGLE is a blind flip with no state
        query, so we must read GET_STATUS first and only toggle when paused.
        """
        status = await self.get_status()
        if status.state == "PAUSED":
            await asyncio.to_thread(self._nci.pause_toggle)

    @property
    def state_version(self) -> int:
        """Monotonic counter; increments after every successful ``load_state``
        and on every ``PLAY_REPLAY`` (both load a savestate).

        The poller reads this each tick; a change tells it to treat the next
        snapshot as a fresh prev.
        """
        return self._state_version

    def _note_replay_state_load(self) -> None:
        """Bump ``state_version`` when PLAY_REPLAY loads the replay's embedded
        savestate.

        PLAY_REPLAY is a state-load channel that bypasses ``load_state``, so
        without this bump the poller never resyncs on replay start: it keeps
        the stale pre-replay ``prev`` and the detector misses the replay's
        level entrance (the replay-fixture flake). Wired into RAMovieIO as
        ``on_state_load`` and fired right after PLAY_REPLAY. See
        ``TransitionDetector.mark_replay_entrance``.
        """
        self._state_version += 1

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
        log.warn(
            logger, "save_state timed out",
            dest=str(dest_path), pattern=pattern, ra_game=cur_game,
            attempts=SAVE_RETRY_ATTEMPTS, detail=last_err,
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
            log.warn(
                logger, "move fell back to copy, source not deleted",
                src=str(src), dst=str(dst), attempts=MOVE_RETRY_ATTEMPTS,
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

        # Snapshot game-state RAM BEFORE firing LOAD_STATE_SLOT so we can
        # detect a silent rejection (RA does not return an error code; the
        # failure shows up only in RA's own log). See LOAD_VALIDATION_*.
        pre = self._read_load_validation_bytes()

        self._nci.load_state_slot(self._reserved_slot)
        # Bump version immediately after firing the command — poller sees the
        # change on its very next tick and resyncs.
        self._state_version += 1

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

        if not self._wait_for_load_validation_change(pre):
            raise StateLoadError(
                f"RA appears to have rejected the load: game-state RAM did "
                f"not change in {LOAD_VALIDATION_POLL_MAX_SEC:.1f}s after "
                f"LOAD_STATE_SLOT (src={src}, slot={self._reserved_slot}). "
                f"The source file may be malformed — check RA's log for "
                f"[State] Failed entries."
            )

        logger.info(
            'load_state ok src="%s" slot=%d version=%d',
            src, self._reserved_slot, self._state_version,
        )

    def _read_load_validation_bytes(self) -> bytes:
        """Read the game-state RAM chunk used to validate load_state success.

        Returns the 155-byte block from $0071 (player_anim) through $010B
        (room_num) — a single NCI round-trip that covers the detector's
        primary state region. After a successful load, at least one byte in
        this region differs from its pre-load value within ~1 emulator frame.
        """
        from spinlab.retroarch import addresses as a
        return self._nci.read_ram(
            a.ADDR_PLAYER_ANIM, a.ADDR_ROOM_NUM - a.ADDR_PLAYER_ANIM + 1,
        )

    def _wait_for_load_validation_change(self, pre: bytes) -> bool:
        """Poll the validation region until it differs from ``pre`` or the
        validation window expires. Returns True on change, False on timeout.

        Returns immediately on the first poll that differs — success path
        costs at most one NCI read.
        """
        deadline = time.monotonic() + LOAD_VALIDATION_POLL_MAX_SEC
        while True:
            if self._read_load_validation_bytes() != pre:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOAD_VALIDATION_POLL_INTERVAL_SEC)

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
            log.warn(
                logger, "startup_sweep could not remove slot file",
                exc=exc, slot_path=str(slot_path),
            )

    def _require_basename(self) -> None:
        if not self._game_basename:
            log.warn(
                logger, "RAClient: basename not set at op time",
                connected=self._connected,
            )
            raise RAClientError(
                "RAClient: game basename not set yet — call connect() first "
                "(or check that RetroArch is running and has a ROM loaded)."
            )

    # ------------------------------------------------------------------
    # Movie IO
    # ------------------------------------------------------------------

    @property
    def movie_io(self) -> RAMovieIO:
        return self._movie_io

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

