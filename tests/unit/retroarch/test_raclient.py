"""Unit tests for RAClient.

Covers the seams that production-only tests can't reach: save-state mtime
polling, load-state state_version contract, filesystem failure paths,
replay-slot resolution from RA's log, hotkey debounce timing, reset()'s
two-press requirement.

The NCI transport is mocked (MagicMock standing in for NCIClient); the
filesystem is real (tmp_path) because the mtime/copy/unlink behavior IS
what we're testing in several places.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.raclient import (
    MovieRecordError,
    MoviePlaybackError,
    NotReachableError,
    RAClient,
    RAClientError,
    RAHotkey,
    StateLoadError,
    StateSaveTimeoutError,
)
from spinlab.retroarch.responses import StatusInfo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ra_dirs(tmp_path):
    """A trio of (savestate_dir, log_dir, movie_dir) under tmp_path."""
    s = tmp_path / "ra-states"
    s.mkdir()
    l = tmp_path / "ra-logs"
    l.mkdir()
    m = tmp_path / "ra-movies"
    m.mkdir()
    return s, l, m


@pytest.fixture
def client(ra_dirs):
    """RAClient with mocked NCI and a fast save timeout for tests."""
    savestate_dir, log_dir, movie_dir = ra_dirs
    c = RAClient(
        ra_savestate_dir=savestate_dir,
        ra_log_dir=log_dir,
        ra_movie_dir=movie_dir,
        save_timeout_sec=0.1,
        load_settle_sec=0,  # don't actually sleep in tests
    )
    c._nci = MagicMock()
    c._game_basename = "Test"  # pre-connect for save/load tests
    return c


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_returns_info_on_success(client):
    client._nci.get_status.return_value = StatusInfo(
        state="PLAYING", game="Love Yourself", system="SNES", crc32="abc123",
    )
    info = await client.connect()
    assert info.rom_filename == "Love Yourself"
    assert info.system == "SNES"
    assert info.crc32 == "abc123"
    assert client.is_connected is True
    assert client.game_basename == "Love Yourself"


@pytest.mark.asyncio
async def test_connect_raises_not_reachable_when_version_fails(client):
    client._nci.version.side_effect = NCIError("timeout")
    with pytest.raises(NotReachableError, match="NCI not reachable"):
        await client.connect()


@pytest.mark.asyncio
async def test_connect_raises_not_reachable_when_get_status_fails(client):
    client._nci.get_status.side_effect = NCIError("nope")
    with pytest.raises(NotReachableError, match="GET_STATUS failed"):
        await client.connect()


@pytest.mark.asyncio
async def test_connect_empty_game_field_returns_empty_filename(client):
    """RA sometimes returns GET_STATUS before the ROM is loaded; game=None.
    RAClient surfaces this as rom_filename=""."""
    client._nci.get_status.return_value = StatusInfo(
        state="CONTENTLESS", game=None, system=None, crc32=None,
    )
    info = await client.connect()
    assert info.rom_filename == ""
    # Orchestrator handles this case (see test_connect_returns_false_when_rom_not_yet_loaded).


# ---------------------------------------------------------------------------
# save_state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_state_happy_path(client, ra_dirs, tmp_path):
    """SAVE_STATE → new slot file appears → moved to dest_path."""
    savestate_dir, _, _ = ra_dirs

    def fake_save():
        # Simulate RA writing a slot file when SAVE_STATE arrives.
        (savestate_dir / "Test.state").write_bytes(b"slot bytes")

    client._nci.save_state.side_effect = fake_save
    dest = tmp_path / "seg.state"

    result = await client.save_state(dest)

    assert result == dest
    assert dest.read_bytes() == b"slot bytes"
    assert not (savestate_dir / "Test.state").exists(), "source moved"


@pytest.mark.asyncio
async def test_save_state_detects_mtime_advance_on_existing_slot(client, ra_dirs, tmp_path):
    """Existing slot file with bumped mtime is treated as a fresh save."""
    savestate_dir, _, _ = ra_dirs
    slot = savestate_dir / "Test.state"
    slot.write_bytes(b"old")
    old_mtime = slot.stat().st_mtime

    def fake_save():
        time.sleep(0.01)
        slot.write_bytes(b"new")
        # Force a clearly-newer mtime in case the FS clamps to the same second.
        os_now = old_mtime + 5
        import os
        os.utime(slot, (os_now, os_now))

    client._nci.save_state.side_effect = fake_save
    dest = tmp_path / "seg.state"

    result = await client.save_state(dest)
    assert result == dest
    assert dest.read_bytes() == b"new"


@pytest.mark.asyncio
async def test_save_state_timeout_raises_after_retries(client, ra_dirs, tmp_path):
    """No slot file appears in the timeout window across all retries."""
    client._nci.save_state.return_value = None  # no-op
    client._nci.get_status.return_value = StatusInfo(
        state="PLAYING", game="Test", system="SNES", crc32="x",
    )
    dest = tmp_path / "seg.state"

    with pytest.raises(StateSaveTimeoutError, match="no Test.state\\* file appeared"):
        await client.save_state(dest)

    # Verify all retries were attempted.
    from spinlab.retroarch.raclient import SAVE_RETRY_ATTEMPTS
    assert client._nci.save_state.call_count == SAVE_RETRY_ATTEMPTS


@pytest.mark.asyncio
async def test_save_state_without_basename_raises(client, tmp_path):
    client._game_basename = None
    with pytest.raises(RAClientError, match="game basename not set"):
        await client.save_state(tmp_path / "dest.state")


# ---------------------------------------------------------------------------
# load_state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_state_increments_state_version_exactly_once(client, ra_dirs, tmp_path):
    """The poller depends on state_version incrementing exactly once per load."""
    savestate_dir, _, _ = ra_dirs
    src = tmp_path / "saved.state"
    src.write_bytes(b"data")

    before = client.state_version
    await client.load_state(src)
    assert client.state_version == before + 1


@pytest.mark.asyncio
async def test_load_state_fires_load_state_slot(client, ra_dirs, tmp_path):
    src = tmp_path / "saved.state"
    src.write_bytes(b"data")
    await client.load_state(src)
    client._nci.load_state_slot.assert_called_once_with(client._reserved_slot)


@pytest.mark.asyncio
async def test_load_state_missing_source_raises(client, tmp_path):
    with pytest.raises(StateLoadError, match="No savestate at"):
        await client.load_state(tmp_path / "does-not-exist.state")
    # state_version must not advance on failure.
    assert client.state_version == 0


@pytest.mark.asyncio
async def test_load_state_unlinks_staging_slot_after_settle(client, ra_dirs, tmp_path):
    """RA reads the slot file synchronously; RAClient unlinks after settle."""
    src = tmp_path / "saved.state"
    src.write_bytes(b"data")
    await client.load_state(src)
    slot_path = client._ra_slot_path()
    assert not slot_path.exists(), "staging slot should be cleaned up"


@pytest.mark.asyncio
async def test_load_state_without_basename_raises(client, tmp_path):
    client._game_basename = None
    src = tmp_path / "saved.state"
    src.write_bytes(b"data")
    with pytest.raises(RAClientError, match="game basename not set"):
        await client.load_state(src)


# ---------------------------------------------------------------------------
# Replay slot resolution from RA logs
# ---------------------------------------------------------------------------

def test_find_replay_slot_from_replay_slot_line(client, ra_dirs):
    _, log_dir, _ = ra_dirs
    (log_dir / "retroarch__1.log").write_text(
        "Some other line\n"
        "[Replay] Replay slot: 7\n"
        "More noise\n"
    )
    assert client._find_current_replay_slot() == 7


def test_find_replay_slot_from_found_last_line(client, ra_dirs):
    _, log_dir, _ = ra_dirs
    (log_dir / "retroarch__1.log").write_text(
        "[Replay] Found last replay slot: #3\n"
    )
    assert client._find_current_replay_slot() == 3


def test_find_replay_slot_from_starting_record_line(client, ra_dirs):
    """replay_auto_index bumps the slot at record-start with no 'Replay slot:'
    line — RAClient parses the record-start path for the slot number."""
    _, log_dir, _ = ra_dirs
    (log_dir / "retroarch__1.log").write_text(
        '[Replay] Starting movie record to "/movies/Test.replay5"\n'
    )
    assert client._find_current_replay_slot() == 5


def test_find_replay_slot_picks_most_recent_match(client, ra_dirs):
    """Multiple lines: last match wins (chronological order in the log)."""
    _, log_dir, _ = ra_dirs
    (log_dir / "retroarch__1.log").write_text(
        "[Replay] Found last replay slot: #2\n"
        "[Replay] Replay slot: 4\n"
        "[Replay] Replay slot: 6\n"
    )
    assert client._find_current_replay_slot() == 6


def test_find_replay_slot_returns_none_when_no_logs(client, tmp_path):
    """Empty log dir → None (falls back to slot 0 in caller)."""
    empty_log_dir = tmp_path / "empty-logs"
    empty_log_dir.mkdir()
    client._ra_log_dir = empty_log_dir
    assert client._find_current_replay_slot() is None


def test_find_replay_slot_returns_none_when_no_log_dir(client):
    client._ra_log_dir = None
    assert client._find_current_replay_slot() is None


def test_find_replay_slot_returns_none_when_no_match(client, ra_dirs):
    _, log_dir, _ = ra_dirs
    (log_dir / "retroarch__1.log").write_text("unrelated noise only\n")
    assert client._find_current_replay_slot() is None


def test_find_replay_slot_uses_most_recent_log_file(client, ra_dirs):
    """Multiple log files — picks the one with the newest mtime."""
    _, log_dir, _ = ra_dirs
    old = log_dir / "retroarch__1.log"
    new = log_dir / "retroarch__2.log"
    old.write_text("[Replay] Replay slot: 1\n")
    new.write_text("[Replay] Replay slot: 9\n")
    import os
    # Ensure 'new' has a strictly newer mtime.
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert client._find_current_replay_slot() == 9


# ---------------------------------------------------------------------------
# Hotkey press / debounce
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_press_single_tap_no_gap(client):
    """Single-tap path: no sleep, just one NCI send."""
    start = time.monotonic()
    await client.press(RAHotkey.PAUSE_TOGGLE, taps=1)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"single tap should be fast, took {elapsed:.3f}s"
    client._nci._send_no_reply.assert_called_once()


@pytest.mark.asyncio
async def test_press_reset_double_tap_observes_min_gap(client):
    """RESET profile mandates a 300ms gap; verify the second send waits."""
    start = time.monotonic()
    await client.press(RAHotkey.RESET, taps=2)
    elapsed = time.monotonic() - start
    # min_tap_gap_sec is 0.3 for RESET — allow some scheduler slack.
    assert elapsed >= 0.28, f"second tap fired too soon: {elapsed:.3f}s"
    assert client._nci._send_no_reply.call_count == 2


@pytest.mark.asyncio
async def test_press_rejects_zero_taps(client):
    with pytest.raises(ValueError, match="taps must be >= 1"):
        await client.press(RAHotkey.RESET, taps=0)


@pytest.mark.asyncio
async def test_reset_is_exactly_two_resets(client):
    """reset() must fire RESET twice — RA's anti-accident gate."""
    await client.reset()
    calls = [c[0][0] for c in client._nci._send_no_reply.call_args_list]
    assert calls == ["RESET", "RESET"]


# ---------------------------------------------------------------------------
# Misc / fast_forward
# ---------------------------------------------------------------------------

def test_fast_forward_toggle_delegates_to_nci(client):
    client.fast_forward_toggle()
    client._nci.fast_forward_toggle.assert_called_once()


# ---------------------------------------------------------------------------
# Movie record / play — guards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_movie_without_movie_dir_raises(client):
    client._ra_movie_dir = None
    with pytest.raises(RAClientError, match="ra_movie_dir is not configured"):
        await client.record_movie(Path("/tmp/x.replay"))


@pytest.mark.asyncio
async def test_play_movie_without_movie_dir_raises(client, tmp_path):
    client._ra_movie_dir = None
    src = tmp_path / "x.replay"
    src.write_bytes(b"x")
    with pytest.raises(RAClientError, match="ra_movie_dir is not configured"):
        await client.play_movie(src)


@pytest.mark.asyncio
async def test_play_movie_missing_source_raises(client, tmp_path):
    with pytest.raises(StateLoadError, match="Movie source not found"):
        await client.play_movie(tmp_path / "nonexistent.replay")
