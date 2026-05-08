"""BSV (libretro deterministic movie) record/play wrappers.

Both classes drive RA via NCI. The recorder writes a movie file under RA's
movie_directory and renames it to a SpinLab-keyed path on stop. The player
loads a SpinLab-keyed movie back into RA's movie_directory before triggering
playback.

NCI commands and movie-file lifecycle are validated by the smoke tests in
tests/integration/test_bsv_smoke.py before this module gets used in
production paths.
"""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# How long to wait after toggle-off for RA to finalize the .bsv file.
# 5 attempts × 200ms = 1s ceiling — RA finalizes essentially instantly when
# the toggle is processed, but the NCI command is fire-and-forget so we need
# a margin for command-processing latency.
_DEFAULT_POLL_INTERVAL_S = 0.2
_DEFAULT_POLL_ATTEMPTS = 5


class _NCIRecorder(Protocol):
    def bsv_record_toggle(self) -> None: ...
    def get_status(self): ...  # returns StatusInfo


@dataclass
class BSVRecorder:
    """Toggles BSV recording and shuffles the resulting .bsv to a target path.

    Lifecycle:
      start(dest) — toggles record on, snapshots existing .bsv files in
                    movie_dir as the baseline.
      stop()      — toggles record off, polls movie_dir for a NEW .bsv
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
        self._baseline_files = set(self.movie_dir.glob("*.bsv"))
        self.client.bsv_record_toggle()
        self._active_dest = dest
        logger.info("BSVRecorder.start: dest=%s baseline=%d files", dest, len(self._baseline_files))

    def stop(self) -> Path:
        if not self.is_recording():
            raise RuntimeError("not recording")
        dest = self._active_dest
        assert dest is not None
        self.client.bsv_record_toggle()
        # Poll for a new .bsv (not in baseline) appearing in movie_dir.
        new_file: Path | None = None
        for _ in range(self._poll_attempts):
            current = set(self.movie_dir.glob("*.bsv"))
            new_files = current - self._baseline_files
            if new_files:
                new_file = max(new_files, key=lambda p: p.stat().st_mtime)
                break
            time.sleep(self._poll_interval_s)
        self._active_dest = None
        self._baseline_files = set()
        if new_file is None:
            raise FileNotFoundError(
                f"BSVRecorder.stop: no new .bsv appeared in {self.movie_dir} "
                f"after {self._poll_attempts} attempts × {self._poll_interval_s}s"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(new_file), str(dest))
        logger.info("BSVRecorder.stop: %s → %s", new_file.name, dest)
        return dest


class _NCIPlayer(Protocol):
    def bsv_play(self) -> None: ...
    def bsv_stop(self) -> None: ...
    def get_status(self): ...


@dataclass
class BSVPlayer:
    """Stages a BSV file into RA's movie_dir and toggles playback on/off.

    Stateless across plays — each call to play() copies the source into
    movie_dir under a deterministic name and tells RA to start playback.
    """

    client: _NCIPlayer
    movie_dir: Path
    _staged_path: Path | None = field(default=None, init=False, repr=False)
    _is_playing: bool = field(default=False, init=False, repr=False)
    _staged_name: str = "spinlab_replay.bsv"

    def is_playing(self) -> bool:
        return self._is_playing

    def play(self, src: Path) -> None:
        if self._is_playing:
            raise RuntimeError("already playing")
        if not src.exists():
            raise FileNotFoundError(f"BSV source not found: {src}")
        self.movie_dir.mkdir(parents=True, exist_ok=True)
        staged = self.movie_dir / self._staged_name
        shutil.copy2(str(src), str(staged))
        self._staged_path = staged
        self.client.bsv_play()
        self._is_playing = True
        logger.info("BSVPlayer.play: %s → %s", src, staged)

    def stop(self) -> None:
        if not self._is_playing:
            return  # idempotent stop, like NCI's pause_toggle gating
        self.client.bsv_stop()
        self._is_playing = False
        if self._staged_path is not None and self._staged_path.exists():
            try:
                self._staged_path.unlink()
            except OSError as exc:
                logger.warning("BSVPlayer.stop: could not unlink staged %s: %s",
                               self._staged_path, exc)
        self._staged_path = None
        logger.info("BSVPlayer.stop")


def discover_movie_dir(client) -> Path:
    """Read RA's movie_directory via NCI GET_CONFIG_PARAM.

    Used at orchestrator construction time when EmulatorConfig.ra_movie_dir
    is None (auto-discovery). If RA reports a relative path, returns it as-is
    — caller is responsible for resolution if needed.
    """
    raw = client.get_config_param("movie_directory")
    return Path(raw)
