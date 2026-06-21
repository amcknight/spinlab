"""RAMovieIO — RA movie record/playback over NCI.

Owns the file-staging and log-scraping mechanics for RA's movie commands.
MovieController composes this directly; RAClient holds an instance and
exposes it via the `movie_io` property purely as a wiring convenience.

Construction:
  - nci: NCI client (for record/halt/play_replay + read_ram during verify)
  - movie_dir: where RA writes .replay files (None disables this object)
  - log_dir: RA's logs dir, for replay-slot resolution (None falls back to
    slot 0)
  - game_basename: callable returning the current ROM basename. A callable
    because the basename only becomes known after RAClient.connect().

Errors raised: MovieRecordError / MoviePlaybackError (record/playback) and
StateLoadError (imported from raclient, raised for "source file missing").
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
from pathlib import Path

from spinlab import log
from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.nci import NCIClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Movie record/play file-stability polling.
MOVIE_POLL_INTERVAL_SEC = 0.2
MOVIE_POLL_ATTEMPTS = 5

# Movie playback verification window: sample WRAM, sleep, sample again. If
# both samples match, RA never advanced a frame — it refused the file.
# 150ms ≈ 9 frames at 60Hz; reliably catches a stuck emulator.
PLAYBACK_VERIFY_SLEEP_SEC = 0.15
PLAYBACK_VERIFY_BYTES = 16

# RA writes this to its log when a movie finishes playing (the only signal RA
# gives — there is no NCI end-of-playback event). MovieController watches for it
# to auto-finalize a replay. Exact string from input/bsv/bsvmovie.c.
_REPLAY_END_MARKER = "Input replay movie playback ended"

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
# Errors
# ---------------------------------------------------------------------------

class MovieIOError(Exception):
    """Base error for RAMovieIO operations."""


class MovieRecordError(MovieIOError):
    """RA produced no new or rewritten movie file after HALT_REPLAY."""


class MoviePlaybackError(MovieIOError):
    """RA refused to load the movie file — no WRAM advance after PLAY_REPLAY."""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

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
# RAMovieIO
# ---------------------------------------------------------------------------

class RAMovieIO:
    """Movie record/playback over RA's NCI.

    Construct with a configured NCI client, the movie dir RA writes to, an
    optional log dir for replay-slot resolution, and a callable returning
    the current ROM basename (which changes after RAClient.connect).
    """

    def __init__(
        self,
        nci: NCIClient,
        movie_dir: Path | None,
        log_dir: Path | None,
        game_basename: Callable[[], str | None],
        on_state_load: Callable[[], None] | None = None,
    ) -> None:
        self._nci = nci
        self._movie_dir = movie_dir
        self._log_dir = log_dir
        self._game_basename = game_basename
        # Called right after PLAY_REPLAY fires. PLAY_REPLAY loads the replay's
        # embedded savestate — a state-load channel that bypasses load_state —
        # so RAClient bumps its state_version here, letting the poller resync
        # (and synthesize the replay entrance) just as it does for load_state.
        self._on_state_load: Callable[[], None] = on_state_load or (lambda: None)

    async def record_movie(self, dest_path: Path) -> MovieRecording:
        """Fire RECORD_REPLAY and return a handle whose ``.stop()`` halts RA
        and copies the resulting .replay file to ``dest_path``.
        """
        from spinlab.retroarch.raclient import RAClientError
        if self._movie_dir is None:
            raise RAClientError(
                "record_movie called but ra_movie_dir is not configured — "
                "set it at RAMovieIO construction."
            )
        movie_dir = self._movie_dir
        movie_dir.mkdir(parents=True, exist_ok=True)

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
        assert self._movie_dir is not None
        movie_dir = self._movie_dir
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
            log.warn(
                logger, "record_movie: no new file appeared",
                movie_dir=str(movie_dir), attempts=MOVIE_POLL_ATTEMPTS,
                existing_replays=existing_replays, hint=hint.strip() or None,
            )
            raise MovieRecordError(
                f"No new or rewritten movie file appeared in {movie_dir} "
                f"after {MOVIE_POLL_ATTEMPTS} polls{hint}"
            )

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(changed), str(dest_path))
        for attempt in range(MOVIE_POLL_ATTEMPTS):
            try:
                os.unlink(changed)
                break
            except PermissionError:
                if attempt < MOVIE_POLL_ATTEMPTS - 1:
                    time.sleep(MOVIE_POLL_INTERVAL_SEC)
        else:
            # All retries exhausted: the source file is still locked. Surface it
            # like _cleanup_staged does — a stranded .replay* makes the *next*
            # record's "no new file appeared" mtime-diff confusing to debug.
            log.warn(
                logger, "record_movie: source cleanup failed; file left in place",
                src=str(changed), attempts=MOVIE_POLL_ATTEMPTS,
            )
        logger.info(
            'record_movie stop src="%s" dest="%s"', changed.name, dest_path,
        )
        return dest_path

    async def play_movie(self, src_path: Path) -> MoviePlayback:
        """Stage ``src_path`` at RA's current runtime slot, fire PLAY_REPLAY,
        verify by sampling WRAM advances.
        """
        from spinlab.retroarch.raclient import RAClientError, StateLoadError
        if self._movie_dir is None:
            raise RAClientError(
                "play_movie called but ra_movie_dir is not configured."
            )
        src = Path(src_path)
        if not src.exists():
            raise StateLoadError(f"Movie source not found: {src}")

        target_slot = self._find_current_replay_slot() or 0
        basename = self._game_basename()
        # PLAY_REPLAY plays RA's *current runtime* replay slot, which
        # replay_auto_index advances on record — so it can differ from the slot
        # we recorded to (and from what the log's "record to …replayN" line
        # shows). NCI has no read/set-slot command (only relative PLUS/MINUS),
        # so we can't target RA's slot directly. Stage the movie at a small
        # window of slots around the guessed one so RA finds it wherever its
        # runtime pointer landed. Every staged copy is cleaned up on stop/fail.
        window = list(range(max(0, target_slot - 1), target_slot + 3))
        staged_paths = [self._movie_dir / f"{basename}.replay{s}" for s in window]
        existing = sorted(p.name for p in self._movie_dir.glob(f"{basename}.replay*"))
        logger.info(
            'play_movie staging src="%s" basename="%s" target_slot=%d '
            'window=%s existing_replays=%s',
            src.name, basename, target_slot, window, existing,
        )
        await asyncio.to_thread(self._stage_and_play, src, staged_paths)

        if not await self._verify_playback_advanced():
            await asyncio.to_thread(self._nci.halt_replay)
            await asyncio.to_thread(self._cleanup_staged, staged_paths)
            logger.warning(
                'play_movie verify_failed src="%s" target_slot=%d window=%s '
                'no_wram_advance_within_ms=%d',
                src, target_slot, window,
                int(PLAYBACK_VERIFY_SLEEP_SEC * 1000),
            )
            raise MoviePlaybackError(
                f"RA produced no frame advance after PLAY_REPLAY of {src.name} "
                f"(staged across slots {window}). PLAY_REPLAY plays RA's current "
                f"runtime replay slot; even staging a window around the recorded "
                f"slot did not land on it. Check RA's log for the slot it played."
            )

        frame_count = _read_frame_count(src)

        logger.info(
            'play_movie start src="%s" target_slot=%d window=%s frame_count=%d',
            src, target_slot, window, frame_count,
        )

        async def _stop() -> None:
            await asyncio.to_thread(self._stop_playback, staged_paths)

        return MoviePlayback(path=src, frame_count=frame_count, _stop=_stop)

    def _stage_and_play(self, src: Path, staged_paths: list[Path]) -> None:
        for staged in staged_paths:
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(staged))
        self._nci.play_replay()
        # PLAY_REPLAY loads the replay's embedded savestate — signal the
        # state-load so the poller resyncs before detecting on post-load frames.
        self._on_state_load()

    def _cleanup_staged(self, staged_paths: list[Path]) -> None:
        for staged in staged_paths:
            try:
                if staged.exists():
                    staged.unlink()
            except OSError as exc:
                logger.warning(
                    'play_movie cleanup unlink_failed staged="%s" err=%s',
                    staged, exc,
                )

    def _stop_playback(self, staged_paths: list[Path]) -> None:
        try:
            self._nci.halt_replay()
        finally:
            self._cleanup_staged(staged_paths)
        logger.info('play_movie stop staged=%s', [p.name for p in staged_paths])

    def _current_log_path(self) -> Path | None:
        """Most-recently-modified RA log, or None if no log dir / no logs."""
        if self._log_dir is None or not self._log_dir.exists():
            return None
        logs = sorted(
            self._log_dir.glob("retroarch__*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return logs[0] if logs else None

    def _find_current_replay_slot(self) -> int | None:
        log_path = self._current_log_path()
        if log_path is None:
            return None
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning('play_movie log_read_failed path="%s" err=%s', log_path, exc)
            return None
        matches = _REPLAY_SLOT_LOG_PATTERN.findall(text)
        if not matches:
            return None
        return int(matches[-1])

    def replay_log_anchor(self) -> tuple[Path, int] | None:
        """Snapshot (current RA log, byte size) to anchor end-of-playback
        detection. Returns None when no log is available (caller then skips
        auto-end and falls back to manual stop)."""
        log_path = self._current_log_path()
        if log_path is None:
            return None
        try:
            return (log_path, log_path.stat().st_size)
        except OSError:
            return None

    def playback_ended_since(self, anchor: tuple[Path, int] | None) -> bool:
        """True if RA logged the end-of-playback marker after ``anchor``.

        RA emits no NCI event when a movie finishes — it only writes
        ``Input replay movie playback ended`` to its log. Reading from the
        anchored byte offset ignores markers from earlier playbacks in the
        same session.
        """
        if anchor is None:
            return False
        log_path, offset = anchor
        try:
            with log_path.open("rb") as fh:
                fh.seek(offset)
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return False
        return _REPLAY_END_MARKER in tail

    async def _verify_playback_advanced(self) -> bool:
        try:
            before = await asyncio.to_thread(
                self._nci.read_ram, 0x0000, PLAYBACK_VERIFY_BYTES,
            )
            await asyncio.sleep(PLAYBACK_VERIFY_SLEEP_SEC)
            after = await asyncio.to_thread(
                self._nci.read_ram, 0x0000, PLAYBACK_VERIFY_BYTES,
            )
        except NCIError as exc:
            logger.warning('play_movie verify_read_failed err=%s', exc)
            return False
        advanced = before != after
        if not advanced:
            # DIAGNOSTIC: dump the static WRAM window so a failed verify shows
            # whether the core is frozen/paused vs RA played an empty slot.
            logger.warning(
                'play_movie verify no WRAM advance: before=%s after=%s '
                '(addr=0x0000 bytes=%d)',
                before.hex() if isinstance(before, (bytes, bytearray)) else before,
                after.hex() if isinstance(after, (bytes, bytearray)) else after,
                PLAYBACK_VERIFY_BYTES,
            )
        return advanced


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_frame_count(movie_path: Path) -> int:
    """Read frame_count from the sibling ``<movie>.json`` metadata file."""
    meta = movie_path.with_suffix(".json")
    if not meta.exists():
        return 0
    try:
        return int(json.loads(meta.read_text()).get("frame_count", 0))
    except Exception as exc:
        logger.warning('frame_count_read_failed path="%s" err=%s', meta, exc)
        return 0
