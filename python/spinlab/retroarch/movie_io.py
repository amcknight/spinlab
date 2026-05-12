"""RAMovieIO — RA movie record/playback over NCI.

Extracted from RAClient. The orchestrator's MovieController composes this
object directly; RAClient holds an internal instance and delegates its
public record_movie / play_movie to it for the transition window before
those wrappers go away.

The class wraps NCI directly. Construction needs:
  - the NCI client (for record/halt/play_replay + read_ram during verify)
  - movie_dir: where RA writes .replay files (None disables this object)
  - log_dir: RA's logs dir, for replay-slot resolution (None means slot 0)
  - game_basename: callable that returns the current ROM basename (changes
    after RAClient.connect, so it's a getter rather than a captured string)

Errors raised here are MovieRecordError / MoviePlaybackError. StateLoadError
is imported from raclient and reused for "source file missing" — semantically
a load-side error, kept for test backward compat.
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
    ) -> None:
        self._nci = nci
        self._movie_dir = movie_dir
        self._log_dir = log_dir
        self._game_basename = game_basename

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
        staged = self._movie_dir / f"{basename}.replay{target_slot}"
        await asyncio.to_thread(self._stage_and_play, src, staged)

        if not await self._verify_playback_advanced():
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
        if self._log_dir is None or not self._log_dir.exists():
            return None
        logs = sorted(
            self._log_dir.glob("retroarch__*.log"),
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
        return before != after


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
