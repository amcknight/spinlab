"""Movie (libretro deterministic replay) record/play wrappers.

Both classes drive RA via NCI. The recorder writes a movie file under RA's
movie output directory and renames it to a SpinLab-keyed path on stop. The
player stages a SpinLab-keyed movie into RA's movie directory before
triggering playback.

RA 1.22.2 writes .replay<slot> files (e.g. the.replay2), not .bsv. NCI
commands and movie-file lifecycle are validated by the smoke tests in
tests/integration/test_movie_smoke.py before this module gets used in
production paths.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# How long to wait after halt_replay for RA to finalize the .replay file.
# 5 attempts × 200ms = 1s ceiling — RA finalizes essentially instantly when
# the command is processed, but the NCI command is fire-and-forget so we need
# a margin for command-processing latency.
_DEFAULT_POLL_INTERVAL_S = 0.2
_DEFAULT_POLL_ATTEMPTS = 5


class _NCIRecorder(Protocol):
    def record_replay(self) -> None: ...
    def halt_replay(self) -> None: ...
    def get_status(self): ...  # returns StatusInfo


@dataclass
class MovieRecorder:
    """Toggles movie recording and shuffles the resulting file to a target path.

    Lifecycle:
      start(dest) — fires record_replay, snapshots existing files in
                    movie_dir as the baseline.
      stop()      — fires halt_replay, polls movie_dir for a NEW file
                    (anything not in the baseline), moves it to dest.
                    Returns the final path.
    """

    client: _NCIRecorder
    movie_dir: Path
    _active_dest: Path | None = field(default=None, init=False, repr=False)
    _baseline_files: set[Path] = field(default_factory=set, init=False, repr=False)
    _poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S
    _poll_attempts: int = _DEFAULT_POLL_ATTEMPTS

    def is_recording(self) -> bool:
        return self._active_dest is not None

    def start(self, dest: Path) -> None:
        if self.is_recording():
            raise RuntimeError(f"already recording to {self._active_dest}")
        if not self.movie_dir.exists():
            self.movie_dir.mkdir(parents=True, exist_ok=True)
        # Snapshot all existing files (format-agnostic baseline).
        self._baseline_files = set(f for f in self.movie_dir.iterdir() if f.is_file())
        self.client.record_replay()
        self._active_dest = dest
        logger.info("MovieRecorder.start: dest=%s baseline=%d files", dest, len(self._baseline_files))

    def stop(self) -> Path:
        if not self.is_recording():
            raise RuntimeError("not recording")
        dest = self._active_dest
        assert dest is not None
        self.client.halt_replay()
        # Poll for a new file (not in baseline) appearing in movie_dir.
        new_file: Path | None = None
        for _ in range(self._poll_attempts):
            current = set(f for f in self.movie_dir.iterdir() if f.is_file())
            new_files = current - self._baseline_files
            if new_files:
                new_file = max(new_files, key=lambda p: p.stat().st_mtime)
                break
            time.sleep(self._poll_interval_s)
        self._active_dest = None
        self._baseline_files = set()
        if new_file is None:
            raise FileNotFoundError(
                f"MovieRecorder.stop: no new file appeared in {self.movie_dir} "
                f"after {self._poll_attempts} attempts × {self._poll_interval_s}s"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        # On Windows, RA may still hold the file handle briefly after writing.
        # Copy first, then retry-delete the source to handle that race.
        shutil.copy2(str(new_file), str(dest))
        for attempt in range(self._poll_attempts):
            try:
                os.unlink(new_file)
                break
            except PermissionError:
                if attempt < self._poll_attempts - 1:
                    time.sleep(self._poll_interval_s)
        logger.info("MovieRecorder.stop: %s → %s", new_file.name, dest)
        return dest


class _NCIPlayer(Protocol):
    def play_replay(self) -> None: ...
    def halt_replay(self) -> None: ...
    def get_status(self): ...


@dataclass
class MoviePlayer:
    """Stages a movie file into RA's movie_dir and triggers playback on/off.

    Stateless across plays — each call to play() copies the source into
    movie_dir under a deterministic name and tells RA to start playback.
    """

    client: _NCIPlayer
    movie_dir: Path
    _staged_path: Path | None = field(default=None, init=False, repr=False)
    _is_playing: bool = field(default=False, init=False, repr=False)
    _staged_name: str = field(default="spinlab_movie.replay", init=False, repr=False)

    def is_playing(self) -> bool:
        return self._is_playing

    def play(self, src: Path) -> None:
        if self._is_playing:
            raise RuntimeError("already playing")
        if not src.exists():
            raise FileNotFoundError(f"Movie source not found: {src}")
        self.movie_dir.mkdir(parents=True, exist_ok=True)
        staged = self.movie_dir / self._staged_name
        shutil.copy2(str(src), str(staged))
        self._staged_path = staged
        self.client.play_replay()
        self._is_playing = True
        logger.info("MoviePlayer.play: %s → %s", src, staged)

    def stop(self) -> None:
        if not self._is_playing:
            return  # idempotent stop, like NCI's pause_toggle gating
        try:
            self.client.halt_replay()
        finally:
            self._is_playing = False
            if self._staged_path is not None and self._staged_path.exists():
                try:
                    self._staged_path.unlink()
                except OSError as exc:
                    logger.warning("MoviePlayer.stop: could not unlink staged %s: %s",
                                   self._staged_path, exc)
            self._staged_path = None
        logger.info("MoviePlayer.stop")


def discover_movie_dir(client, core_name: str) -> Path:
    """Compute RA's movie output directory.

    RA 1.22.2 writes movies to <savestate_directory>/<core_name>/. The
    movie_directory config key returns "unsupported" — this function
    reads the savestate dir and joins with the caller-provided core name.

    The core name matches the subdirectory RA creates under savestate_directory.
    For the Snes9x core (snes9x_libretro.dll), RA uses "Snes9x" as the subdir
    name (confirmed by inspecting C:\\RetroArch-Win64\\states\\). Caller is
    responsible for providing the correct core_name string.
    """
    raw = client.get_config_param("savestate_directory")
    return Path(raw) / core_name
