"""Smoke tests for movie record/playback against a live headless RetroArch.

These are sequenced gates — the record test must pass before the recorder
integration in Task 5 is meaningful. The determinism and polling-during-playback
tests in Tasks 7-8 require Andrew to first record a real fixture under Task 6.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from spinlab.retroarch.movie import MovieRecorder, discover_movie_dir
from tests.integration.conftest import (
    LOVE_YOURSELF_ROM_NAME,
    skip_no_love_yourself,
    _love_yourself_rom_path,
)
from tests.integration.ra_harness import RAHarness, RAHarnessLaunchError

pytestmark = [pytest.mark.emulator, skip_no_love_yourself]

# Core subdirectory name RA uses for Snes9x replays. This is the subdir
# under savestate_directory — confirmed by inspecting C:\RetroArch-Win64\states\.
_SNES9X_CORE_SUBDIR = "Snes9x"

# Fallback core subdir if config.yaml doesn't set ra_core_subdir.
_FALLBACK_CORE_SUBDIR = _SNES9X_CORE_SUBDIR


def _load_config() -> dict:
    """Load config.yaml from project root."""
    try:
        config_path = Path(__file__).resolve().parents[2] / "config.yaml"
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_core_subdir() -> str:
    """Read ra_core_subdir from config.yaml, falling back to the confirmed default."""
    config = _load_config()
    subdir = config.get("emulator", {}).get("ra_core_subdir")
    return str(subdir) if subdir else _FALLBACK_CORE_SUBDIR


@pytest.fixture(scope="module")
def movie_harness(tmp_path_factory):
    """Launch a RetroArch process with an isolated savestate directory.

    Uses the Love Yourself ROM (same as test_replay_fixture.py) so Phase E
    tests run against a representative SMW hack rather than the default test
    ROM (which has unusual properties — goes straight into gameplay without a
    title screen).

    Using a fresh temp savestate directory (not the user's states/) ensures
    RECORD_REPLAY can always create a new .replay file — RA's replay_max_keep
    setting can prevent new recordings when slots 1-N already exist in the
    configured save dir.
    """
    config = _load_config()
    emu = config.get("emulator", {})
    retroarch_exe_str = emu.get("retroarch_path")
    ra_core_path_str = emu.get("ra_core_path")
    rom_path_str = _love_yourself_rom_path()

    retroarch_exe = Path(retroarch_exe_str) if retroarch_exe_str else None
    ra_core_path = Path(ra_core_path_str) if ra_core_path_str else None
    rom_path = Path(rom_path_str) if rom_path_str else None

    missing = [
        label for label, p in
        [("retroarch_path", retroarch_exe), ("ra_core_path", ra_core_path), ("rom", rom_path)]
        if p is None or not p.exists()
    ]
    if missing:
        pytest.skip(
            f"movie smoke test requires: {', '.join(missing)} "
            "(retroarch_path/ra_core_path in config.yaml emulator section; "
            f"Love Yourself ROM from SPINLAB_REPLAY_ROM env or '{LOVE_YOURSELF_ROM_NAME}' in rom.dir)"
        )

    # Create an isolated temp savestate directory. Using tmp_path_factory so
    # the dir outlives the fixture (module scope).
    tmp_save = tmp_path_factory.mktemp("ra_save_")
    core_subdir = _load_core_subdir()
    movie_dir = tmp_save / core_subdir
    movie_dir.mkdir(parents=True, exist_ok=True)

    # Format path for retroarch.cfg (forward slashes, no trailing slash).
    save_path_cfg = str(tmp_save).replace("\\", "/")
    extra_cfg = f'savestate_directory = "{save_path_cfg}"\n'

    try:
        harness = RAHarness.launch(
            rom_path=rom_path,
            core_path=ra_core_path,
            retroarch_exe=retroarch_exe,
            extra_cfg=extra_cfg,
        )
    except RAHarnessLaunchError as exc:
        pytest.skip(f"movie harness launch failed: {exc}")

    yield harness, movie_dir

    harness.teardown()


def test_movie_record_creates_file(movie_harness, tmp_path: Path):
    """Record a movie via RECORD_REPLAY/HALT_REPLAY, expect a new .replay file."""
    harness, movie_dir = movie_harness
    client = harness.client

    # Verify the movie directory exists (RA should use the temp savestate dir).
    assert movie_dir.exists(), (
        f"Movie directory {movie_dir!r} does not exist"
    )

    core_subdir = _load_core_subdir()
    discovered = discover_movie_dir(client, core_subdir)
    # discovered should point to the temp dir (since we overrode savestate_directory).
    assert discovered.exists(), (
        f"discover_movie_dir returned {discovered!r} which does not exist"
    )

    # More generous poll window for the integration test harness.
    # 10 attempts × 300ms = 3s ceiling.
    recorder = MovieRecorder(
        client=client,
        movie_dir=discovered,
        _poll_attempts=10,
        _poll_interval_s=0.3,
    )
    dest = tmp_path / "smoke.replay"

    # RECORD_REPLAY requires content in PLAYING state; harness leaves RA paused.
    client.pause_toggle()
    time.sleep(0.3)

    try:
        recorder.start(dest)
        # Let RA run freely for a short time so the recording has content.
        time.sleep(1.0)
        result = recorder.stop()
    finally:
        # Re-pause to restore harness invariant regardless of success/failure.
        status = client.get_status()
        if status.state == "PLAYING":
            client.pause_toggle()
            time.sleep(0.1)

    assert result == dest, f"MovieRecorder.stop returned {result!r}, expected {dest!r}"
    assert dest.exists(), f"MovieRecorder.stop did not produce {dest}"
    assert dest.stat().st_size > 0, f"{dest} is empty"

    # Confirm RA is still responsive — no deep-pause, no crash.
    status = client.get_status()
    assert status.state in ("PAUSED", "PLAYING"), (
        f"RA in unexpected state {status.state!r} after movie record"
    )

    # Confirm FRAMEADVANCE still ticks the core.
    snap_before = client.read_ram(0x0000, 16)
    client.frame_advance()
    time.sleep(0.05)
    snap_after = client.read_ram(0x0000, 16)
    assert snap_before != snap_after, (
        "FRAMEADVANCE no longer ticks core after movie record"
    )
