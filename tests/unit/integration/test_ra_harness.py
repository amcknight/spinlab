"""Tests for RAHarness using mocked subprocess + NCIClient.

Uses tmp_path for paths so existence checks resolve naturally without patching
pathlib.Path.exists (which has session-wide side effects on a class method).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spinlab.retroarch.exceptions import NCITimeout
from tests.integration.ra_harness import RAHarness, RAHarnessLaunchError


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
    """NCI client that reports running on first is_core_running, paused on second."""
    client = MagicMock()
    client.version.return_value = "1.0"
    client.is_core_running.side_effect = [True, False]
    return client


def test_launch_happy_path(fake_paths, fake_proc, fake_client_running_then_paused):
    rom, core, exe = fake_paths

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=fake_client_running_then_paused):
        harness = RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)

    fake_client_running_then_paused.pause_toggle.assert_called_once()
    assert harness.engine is not None
    harness.teardown()


def test_launch_raises_when_rom_missing(tmp_path):
    """Use a path to a file that genuinely does not exist."""
    rom = tmp_path / "missing.smc"
    core = tmp_path / "core.dll"; core.write_bytes(b"")
    exe = tmp_path / "retroarch.exe"; exe.write_bytes(b"")

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
    """Deep-pause guard: if PAUSE_TOGGLE doesn't stop advancing frames,
    refuse to proceed rather than enter a hung state."""
    rom, core, exe = fake_paths
    runaway_client = MagicMock()
    runaway_client.version.return_value = "1.0"
    runaway_client.is_core_running.side_effect = [True, True]

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=runaway_client):
        with pytest.raises(RAHarnessLaunchError, match="did not stop frame advance"):
            RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)


def test_teardown_calls_quit_then_terminates_on_timeout(fake_client_running_then_paused):
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd=[], timeout=2.0), None]

    harness = RAHarness(proc=proc, client=fake_client_running_then_paused)
    harness.teardown()

    fake_client_running_then_paused.quit.assert_called_once()
    proc.terminate.assert_called_once()
