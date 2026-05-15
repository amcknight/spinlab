"""Tests for RAHarness using mocked subprocess + NCIClient.

Uses tmp_path for paths so existence checks resolve naturally without patching
pathlib.Path.exists (which has session-wide side effects on a class method).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.integration.ra_harness import RAHarness, RAHarnessLaunchError

from spinlab.retroarch.exceptions import NCITimeout


@pytest.fixture
def fake_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Three real (empty) files standing in for rom/core/exe."""
    rom = tmp_path / "rom.smc"
    rom.write_bytes(b"")
    core = tmp_path / "core.dll"
    core.write_bytes(b"")
    exe = tmp_path / "retroarch.exe"
    exe.write_bytes(b"")
    return rom, core, exe


@pytest.fixture
def fake_proc():
    proc = MagicMock()
    proc.poll.return_value = None
    proc.returncode = None
    return proc


@pytest.fixture
def fake_client_running_then_paused():
    """NCI client that reports PLAYING on first GET_STATUS, then PAUSED after toggle."""
    from spinlab.retroarch.responses import StatusInfo

    client = MagicMock()
    client.version.return_value = "1.0"
    # First call: PLAYING — harness will toggle. Second call: PAUSED — toggle confirmed.
    client.get_status.side_effect = [
        StatusInfo(state="PLAYING"),
        StatusInfo(state="PAUSED"),
    ]
    return client


def test_launch_happy_path(fake_paths, fake_proc, fake_client_running_then_paused):
    rom, core, exe = fake_paths

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=fake_client_running_then_paused), \
         patch("tests.integration.ra_harness.time.sleep"):
        harness = RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)

    fake_client_running_then_paused.pause_toggle.assert_called_once()
    assert harness.engine is not None
    harness.teardown()


def test_launch_raises_when_rom_missing(tmp_path):
    """Use a path to a file that genuinely does not exist."""
    rom = tmp_path / "missing.smc"
    core = tmp_path / "core.dll"
    core.write_bytes(b"")
    exe = tmp_path / "retroarch.exe"
    exe.write_bytes(b"")

    with pytest.raises(RAHarnessLaunchError, match="rom_path does not exist"):
        RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)


def test_launch_raises_when_nci_never_replies(fake_paths, fake_proc):
    rom, core, exe = fake_paths
    timeout_client = MagicMock()
    timeout_client.version.side_effect = NCITimeout("no reply")

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=timeout_client), \
         patch("tests.integration.ra_harness.time.sleep"):
        with pytest.raises(RAHarnessLaunchError, match="NCI did not reply"):
            RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)
    fake_proc.terminate.assert_called_once()


def test_launch_raises_when_pause_doesnt_stop_frames(fake_paths, fake_proc):
    """Deep-pause guard: if PAUSE_TOGGLE doesn't result in PAUSED state
    after every retry, refuse to proceed rather than enter a hung state."""
    from tests.integration.ra_harness import PAUSE_VERIFY_RETRIES

    from spinlab.retroarch.responses import StatusInfo

    rom, core, exe = fake_paths
    runaway_client = MagicMock()
    runaway_client.version.return_value = "1.0"
    # First GET_STATUS: PLAYING (triggers toggle).
    # Every subsequent verify (one per retry): still PLAYING (toggle keeps failing).
    runaway_client.get_status.side_effect = (
        [StatusInfo(state="PLAYING")] * (1 + PAUSE_VERIFY_RETRIES)
    )

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=runaway_client), \
         patch("tests.integration.ra_harness.time.sleep"):
        with pytest.raises(RAHarnessLaunchError, match="PAUSE_TOGGLE did not pause RA"):
            RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)


def test_teardown_calls_quit_then_terminates_on_timeout(fake_client_running_then_paused):
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd=[], timeout=2.0), None]

    harness = RAHarness(proc=proc, client=fake_client_running_then_paused)
    harness.teardown()

    fake_client_running_then_paused.quit.assert_called_once()
    proc.terminate.assert_called_once()
