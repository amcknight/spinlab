"""StateIO — sync save/load + path resolution against RA + SpinLab filesystem.

Replaces lua/spinlab.lua's save_state_to_file/load_state_from_file plus the
pending_saves/pending_loads/cpuExec-deferred drain pattern. The cpuExec
deferral was a Mesen-specific requirement; NCI has no such constraint.

Filesystem-shuffle strategy (Decision 1): uses a reserved slot (9999 by
default) to capture SAVE_STATE commands atomically, then verifies capture via
mtime polling (Decision 5) before moving to the segment-named destination.

Phase D scope: this module owns the SAVE_STATE -> mtime-poll -> move flow,
and the reverse for load. Wiring into session_manager / practice.py / the
capture pipeline is Phase F-live.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from spinlab.retroarch.events import (
    Checkpoint,
    LevelEntrance,
    Spawn,
    TransitionEvent,
)
from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.state_paths import (
    ra_slot_filename,
    segment_state_filename,
)

DEFAULT_RESERVED_SLOT = 9999  # see Decision 6 in Phase D plan
DEFAULT_SAVE_TIMEOUT_SEC = 1.0  # mtime-advance wait; healthy RA writes in <100ms
DEFAULT_LOAD_SETTLE_SEC = 0.1   # wait after LOAD_STATE_SLOT before deleting the slot
                                 # file; RA processes the command on its next frame
                                 # (~16ms at 60Hz) and reads the file synchronously

# NCI SAVE_STATE intermittently no-ops when RA is in a transitioning state
# (level load, fade, brief pause). Retry a few times with backoff to give the
# emulator a chance to settle. Total worst-case wait per save: ATTEMPTS *
# (TIMEOUT_SEC + BACKOFF_SEC) ≈ 3 * 1.2s = 3.6s.
SAVE_RETRY_ATTEMPTS = 3
SAVE_RETRY_BACKOFF_SEC = 0.2

# After RA writes the slot file, it may keep an open handle for ~100-300ms
# more; on Windows os.rename then raises PermissionError. Retry the move
# briefly before falling back to a copy that leaves the source for cleanup.
MOVE_RETRY_ATTEMPTS = 5
MOVE_RETRY_BACKOFF_SEC = 0.1

logger = logging.getLogger(__name__)


class StateSaveTimeout(RuntimeError):
    """SAVE_STATE was issued but the slot file mtime did not advance in time."""


class StateIO:
    """Sync owner of SpinLab's segment-keyed savestate files.

    Side-effecting; not thread-safe (don't share an instance across threads).
    Async callers wrap individual methods in `asyncio.to_thread` if they need
    to call from an event loop.
    """

    def __init__(
        self,
        client: NCIClient,
        ra_savestate_dir: Path,
        spinlab_state_dir: Path,
        ra_game_basename: str = "",
        reserved_slot: int = DEFAULT_RESERVED_SLOT,
        save_timeout_sec: float = DEFAULT_SAVE_TIMEOUT_SEC,
        load_settle_sec: float = DEFAULT_LOAD_SETTLE_SEC,
    ) -> None:
        """Construct a StateIO bound to the (RA savestate dir, SpinLab state dir, game) triple.

        CALLER CONTRACT: pass a `spinlab_state_dir` that is already scoped to the
        current game/ROM (e.g., `<data_dir>/states/<game_id>/`). The resolver's path
        keys (e.g., `entrance_<level>_<room>`) do not include game_id, so two games
        sharing the same level+room would clobber each other's saves if the dir
        isn't scoped. F-live wiring constructs one StateIO per game session.

        `ra_savestate_dir` is RA's `savestate_directory` and is shared across games —
        RA owns it and uses `<game_basename>.state<slot>` filenames there.

        `ra_game_basename` is optional. The orchestrator sets it at connect()
        time from RA's GET_STATUS response, which is always authoritative.
        Until then save/load operations raise a clear error rather than
        timing out invisibly because of a stale basename.
        """
        self._client = client
        self._ra_dir = Path(ra_savestate_dir)
        self._sl_dir = Path(spinlab_state_dir)
        self._game_basename = ra_game_basename
        self._reserved_slot = reserved_slot
        self._save_timeout_sec = save_timeout_sec
        self._load_settle_sec = load_settle_sec
        # Ensure SpinLab dir exists; the RA dir is RA's responsibility.
        self._sl_dir.mkdir(parents=True, exist_ok=True)
        # Sweep any stale reserved-slot file from a previous run. Without this,
        # a hard-killed dashboard leaves <game>.state<reserved_slot> sitting in
        # the user's savestate_dir indefinitely.
        self._cleanup_stale_slot_file()

    # -- pure path resolution --------------------------------------------------

    def state_path_for(self, segment_id: str) -> Path:
        """Where SpinLab keeps the savestate for a given segment id."""
        return self._sl_dir / segment_state_filename(segment_id)

    def has_state_for(self, segment_id: str) -> bool:
        return self.state_path_for(segment_id).exists()

    def _ra_slot_path(self) -> Path:
        if not self._game_basename:
            raise RuntimeError(
                "StateIO: game basename not set yet. The orchestrator sets it "
                "at connect() from RA's GET_STATUS — make sure RetroArch is "
                "running and the orchestrator finished connecting before "
                "calling save/load operations."
            )
        return self._ra_dir / ra_slot_filename(self._game_basename, self._reserved_slot)

    @property
    def game_basename(self) -> str:
        return self._game_basename

    def update_game_basename(self, name: str) -> None:
        """Update which slot filename StateIO targets at <ra_savestate_dir>/.

        Called by the orchestrator at connect() after a GET_STATUS round-trip
        so the basename always matches the ROM RA actually has loaded — no
        more "wrong filename → mtime polling times out" silent save failures.
        Re-runs the startup sweep against the new basename.
        """
        if not name or name == self._game_basename:
            return
        old = self._game_basename
        self._game_basename = name
        logger.info("StateIO: game basename %r -> %r", old, name)
        self._cleanup_stale_slot_file()

    def _cleanup_stale_slot_file(self) -> None:
        """Remove any leftover reserved-slot file from a previous session."""
        if not self._game_basename:
            return  # nothing to clean up until we know the basename
        slot_path = self._ra_slot_path()
        if not slot_path.exists():
            return
        try:
            slot_path.unlink()
            logger.info("StateIO: cleaned up stale slot file %s", slot_path)
        except OSError as exc:
            logger.warning(
                "StateIO: could not clean up stale slot file %s: %s",
                slot_path, exc,
            )

    def _cleanup_slot_after_load(self) -> None:
        """Delete the reserved slot file after RA has loaded it.

        RA processes LOAD_STATE_SLOT on its next emulator frame (~16ms at 60Hz)
        and reads the file synchronously; the default 100ms wait is a generous
        margin. Without this cleanup, <game>.state<reserved_slot> sits in the
        user's savestate_dir until the next save (or never, if no save follows).
        Best-effort: a delete failure is logged and skipped — the next load
        overwrites it, and the startup sweep catches it on next launch.
        """
        if self._load_settle_sec > 0:
            time.sleep(self._load_settle_sec)
        slot_path = self._ra_slot_path()
        try:
            slot_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "StateIO: could not delete slot file %s after load: %s",
                slot_path, exc,
            )

    # -- event-shaped resolver -------------------------------------------------

    def resolve_event_path(self, event: TransitionEvent) -> str:
        """Resolver for `PollerDeps.state_path_for`.

        Returns the absolute path string to stamp onto the event, or "" when no
        path applies (Death, LevelExit, Spawn with no segment_id).

        Naming conventions chosen to match lua/spinlab.lua's filename layout but
        flattened to the segment_id idiom where possible:
        - LevelEntrance  -> "entrance_<level>_<room>" (anonymous; F-live maps to segment_id)
        - Checkpoint     -> "cp_<level>_<ordinal>_hot" (hot only)
        - Spawn (cold-fill) -> "<segment_id>" (segment_id-keyed; cold paths flow through here)

        Note: Phase F-live owns the bridge from anonymous (level+room / level+ordinal)
        keys to actual segment_ids. The path returned here is the file LOCATION; whether
        that file gets created/loaded is the F-live caller's responsibility.

        Returns `str` (not `Path`) because the event field `state_path: str` is the
        sink. Use `state_path_for(segment_id)` directly if you want a `Path` object.
        """
        if isinstance(event, LevelEntrance):
            return str(self.state_path_for(f"entrance_{event.level}_{event.room}"))
        if isinstance(event, Checkpoint):
            return str(self.state_path_for(f"cp_{event.level_num}_{event.cp_ordinal}_hot"))
        if isinstance(event, Spawn):
            if not event.segment_id:
                return ""
            return str(self.state_path_for(event.segment_id))
        return ""

    # -- save/load (Tasks 4 & 5; stubbed below until those tasks land) --------

    def save_segment_state(self, segment_id: str) -> Path:
        """Trigger SAVE_STATE, find the file RA wrote, move it to the SpinLab path.

        RA's NCI SAVE_STATE writes to whatever slot RA's `state_slot` counter
        is currently at — NOT to a slot we control. (Phase 0's spike flagged
        this as INCONCLUSIVE; it bit hard during F-live.) So we don't watch
        a fixed reserved-slot file; instead we snapshot the directory before
        the save, fire SAVE_STATE, then look for whichever `<game>.state*`
        file was created or had its mtime advance, and move that.

        Retries: NCI SAVE_STATE intermittently no-ops when the emulator is in
        a transitioning state (level load, fade, brief pause). Up to
        ``save_retry_attempts`` attempts with ``save_timeout_sec`` each.

        Move retries: RA may still hold the slot file open for a few hundred
        ms after writing it; ``shutil.move`` raises PermissionError on Windows
        when the source is locked. We retry the move briefly.

        The user's auto-index counter advances by one per capture — the
        documented tradeoff in Phase D Decision 1 (Option C). Returns the
        SpinLab path. Raises StateSaveTimeout if no file changed across all
        attempts.
        """
        # Refresh basename from RA before saving, in case the user switched
        # ROMs since the last connect. Without this, RA writes
        # `<NewROM>.state...` while we watch `<OldROM>.state*` and time out
        # forever. Best-effort: if GET_STATUS fails we keep the cached value.
        self._refresh_game_basename_from_ra()

        if not self._game_basename:
            raise RuntimeError(
                "StateIO: game basename not set; orchestrator must connect first."
            )
        pattern = f"{self._game_basename}.state*"

        last_err: str = ""
        for attempt in range(SAVE_RETRY_ATTEMPTS):
            new_path = self._try_one_save(pattern)
            if new_path is not None:
                target = self.state_path_for(segment_id)
                self._move_with_retry(new_path, target)
                logger.debug(
                    "StateIO: saved segment %s on attempt %d — RA wrote %s -> %s",
                    segment_id, attempt + 1, new_path.name, target,
                )
                return target
            last_err = (
                f"attempt {attempt + 1}: no {pattern} file appeared/advanced "
                f"in {self._save_timeout_sec}s"
            )
            if attempt + 1 < SAVE_RETRY_ATTEMPTS:
                time.sleep(SAVE_RETRY_BACKOFF_SEC)

        # Diagnostic: ask RA what game it actually has loaded right now. If
        # this differs from our basename, the user changed ROMs after the
        # orchestrator connected and our cached basename is stale.
        try:
            cur_status = self._client.get_status()
            cur_game = cur_status.game or "<none>"
        except Exception:
            cur_game = "<get_status failed>"
        raise StateSaveTimeout(
            f"SAVE_STATE for segment {segment_id!r} failed after "
            f"{SAVE_RETRY_ATTEMPTS} attempts: {last_err}. "
            f"Watching pattern={pattern!r}; RA reports game={cur_game!r}."
        )

    def _refresh_game_basename_from_ra(self) -> None:
        """Best-effort: ask RA what's loaded and update basename if it changed."""
        try:
            status = self._client.get_status()
        except Exception:
            return
        new_basename = status.game
        if new_basename and new_basename != self._game_basename:
            self.update_game_basename(new_basename)

    def _try_one_save(self, pattern: str) -> Path | None:
        """One SAVE_STATE round: snapshot, fire, poll. Returns the new file or None."""
        snapshot: dict[str, float] = {
            p.name: p.stat().st_mtime
            for p in self._ra_dir.glob(pattern) if p.is_file()
        }
        self._client.save_state()

        deadline = time.monotonic() + self._save_timeout_sec
        poll_interval = 0.01
        while time.monotonic() < deadline:
            for p in self._ra_dir.glob(pattern):
                if not p.is_file():
                    continue
                old_mtime = snapshot.get(p.name)
                cur_mtime = p.stat().st_mtime
                if old_mtime is None or cur_mtime > old_mtime:
                    return p
            time.sleep(poll_interval)
        return None

    def _move_with_retry(self, src: Path, dst: Path) -> None:
        """shutil.move that retries when RA still has the slot file locked.

        Windows raises PermissionError if RA holds an open handle; in practice
        RA releases within a few hundred ms after writing. We retry briefly
        before giving up.
        """
        last_exc: BaseException | None = None
        for attempt in range(MOVE_RETRY_ATTEMPTS):
            try:
                shutil.move(str(src), str(dst))
                return
            except PermissionError as exc:
                last_exc = exc
                if attempt + 1 < MOVE_RETRY_ATTEMPTS:
                    time.sleep(MOVE_RETRY_BACKOFF_SEC)
        # Best-effort fallback: copy the bytes and leave the source for the
        # next save / startup sweep to clean up. We still raise so the caller
        # knows the slot file leaked, but the segment file is on disk.
        try:
            shutil.copyfile(str(src), str(dst))
            logger.warning(
                "StateIO: copied %s -> %s but couldn't unlink source after "
                "%d retries (RA still holds the file). Leaving for cleanup.",
                src, dst, MOVE_RETRY_ATTEMPTS,
            )
        except OSError:
            raise last_exc if last_exc else OSError(f"move failed: {src} -> {dst}")

    def load_segment_state(self, segment_id: str) -> None:
        """Copy SpinLab's segment file into RA's reserved slot, fire LOAD_STATE_SLOT.

        Raises FileNotFoundError if no SpinLab file exists for this segment.
        Caller can gate via has_state_for().
        """
        sp_path = self.state_path_for(segment_id)
        if not sp_path.exists():
            raise FileNotFoundError(
                f"No SpinLab savestate for segment {segment_id!r} at {sp_path}"
            )
        slot_path = self._ra_slot_path()
        slot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(sp_path), str(slot_path))
        self._client.load_state_slot(self._reserved_slot)
        logger.debug(
            "StateIO: loaded segment %s (slot=%d)", segment_id, self._reserved_slot
        )
        self._cleanup_slot_after_load()

    def load_state_from_path(self, path: Path | str) -> None:
        """Copy a savestate file from an arbitrary path into RA's reserved slot, fire LOAD_STATE_SLOT.

        Variant of load_segment_state for callers that already have the source path
        (e.g. ColdFillLoadCmd/FillGapLoadCmd carry state_path directly). Raises
        FileNotFoundError if the source path doesn't exist.
        """
        src = Path(path)
        if not src.exists():
            raise FileNotFoundError(
                f"No savestate at {src}"
            )
        slot_path = self._ra_slot_path()
        slot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(src), str(slot_path))
        self._client.load_state_slot(self._reserved_slot)
        logger.debug(
            "StateIO: loaded state from %s (slot=%d)", src, self._reserved_slot
        )
        self._cleanup_slot_after_load()
