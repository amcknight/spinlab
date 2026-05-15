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


def test_launch_raises_when_get_status_fails(fake_paths, fake_proc):
    """If client.get_status() raises after a successful NCI ping, the harness
    raises RAHarnessLaunchError and cleans up proc + tmp_dir + log handle."""
    rom, core, exe = fake_paths

    fake_client = MagicMock()
    fake_client.version.return_value = None
    fake_client.get_status.side_effect = RuntimeError("status broke")

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=fake_client), \
         patch("tests.integration.ra_harness.time.sleep"), \
         pytest.raises(RAHarnessLaunchError, match="GET_STATUS failed"):
        RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)

    fake_proc.terminate.assert_called_once()


def test_launch_raises_on_unexpected_status(fake_paths, fake_proc):
    """If RA reports a state that isn't PAUSED or PLAYING after launch, the
    harness raises RAHarnessLaunchError and cleans up."""
    rom, core, exe = fake_paths

    fake_client = MagicMock()
    fake_client.version.return_value = None
    status = MagicMock()
    status.state = "UNKNOWN"
    fake_client.get_status.return_value = status

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=fake_client), \
         patch("tests.integration.ra_harness.time.sleep"), \
         pytest.raises(RAHarnessLaunchError, match="Unexpected RA status"):
        RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)

    fake_proc.terminate.assert_called_once()


def test_launch_raises_when_nci_client_construction_fails(fake_paths, fake_proc):
    """If NCIClient(port=...) raises, the harness cleans up proc + tmp_dir +
    log handle and raises RAHarnessLaunchError."""
    rom, core, exe = fake_paths

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch(
             "tests.integration.ra_harness.NCIClient",
             side_effect=OSError("port already bound"),
         ), \
         pytest.raises(RAHarnessLaunchError, match="NCIClient"):
        RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)

    fake_proc.terminate.assert_called_once()


def test_launch_error_carries_context_fields(fake_paths, fake_proc):
    """When NCI never replies, the raised error carries pid + port + duration."""
    rom, core, exe = fake_paths

    fake_client = MagicMock()
    fake_client.version.side_effect = NCITimeout("no reply")

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=fake_client), \
         patch("tests.integration.ra_harness.time.sleep"), \
         pytest.raises(RAHarnessLaunchError) as excinfo:
        RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe, nci_port=55355)

    err = excinfo.value
    assert err.pid == fake_proc.pid
    assert err.port == 55355
    assert err.stage == "nci_ping"
    # With time.sleep patched, the loop iterates instantly and _cleanup_launch
    # is excluded from the measurement — duration should be well under a second.
    assert err.startup_duration_s is not None
    assert 0.0 <= err.startup_duration_s < 1.0, (
        f"expected sub-second startup duration with patched sleep, got {err.startup_duration_s}"
    )
    assert err.log_path is not None and err.log_path.exists()


def test_launch_writes_ra_stdout_stderr_to_logfile(fake_paths, fake_proc):
    """Launch must point RA's stdout/stderr at a real file we can inspect later."""
    rom, core, exe = fake_paths

    captured: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):
        captured["stdout"] = kwargs.get("stdout")
        captured["stderr"] = kwargs.get("stderr")
        return fake_proc

    fake_client = MagicMock()
    fake_client.version.return_value = None
    status = MagicMock()
    status.state = "PAUSED"
    fake_client.get_status.return_value = status

    with patch("tests.integration.ra_harness.subprocess.Popen", side_effect=fake_popen), \
         patch("tests.integration.ra_harness.NCIClient", return_value=fake_client):
        harness = RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)

    assert captured["stdout"] is not None
    assert captured["stderr"] is not None
    assert captured["stdout"] is captured["stderr"], "stderr must alias stdout for combined log"

    assert harness.log_path is not None
    assert harness.log_path.exists()
    assert harness.log_path.parent == harness._tmp_dir
    harness.teardown()
