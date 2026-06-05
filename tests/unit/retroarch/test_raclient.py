"""Unit tests for RAClient.

Covers the seams that production-only tests can't reach: save-state mtime
polling, load-state state_version contract, filesystem failure paths,
hotkey debounce timing, reset()'s two-press requirement.

Movie-related tests (replay-slot resolution, movie_dir guards) live in
test_movie_io.py — they exercise RAMovieIO directly.

The NCI transport is mocked (MagicMock standing in for NCIClient); the
filesystem is real (tmp_path) because the mtime/copy/unlink behavior IS
what we're testing in several places.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.raclient import (
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
    """A pair of (savestate_dir, log_dir) under tmp_path."""
    states = tmp_path / "ra-states"
    states.mkdir()
    logs = tmp_path / "ra-logs"
    logs.mkdir()
    return states, logs


@pytest.fixture
def client(ra_dirs):
    """RAClient with mocked NCI and a fast save timeout for tests."""
    savestate_dir, log_dir = ra_dirs
    c = RAClient(
        ra_savestate_dir=savestate_dir,
        ra_log_dir=log_dir,
        save_timeout_sec=0.1,
        load_settle_sec=0,  # don't actually sleep in tests
    )
    c._nci = MagicMock()
    c._game_basename = "Test"  # pre-connect for save/load tests
    # Default: read_ram returns a fresh value each call so the post-load
    # validation in `_load_state_sync` sees a "change" and treats the load
    # as successful. Tests that exercise the validation path itself override
    # this side_effect explicitly.
    _read_call_count = [0]
    def _default_read_ram(addr: int, length: int) -> bytes:
        _read_call_count[0] += 1
        return bytes([_read_call_count[0] % 256] * length)
    c._nci.read_ram.side_effect = _default_read_ram
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
async def test_resume_if_paused_unpauses_when_paused(client):
    """A replay started while RA is paused loads the movie's embedded state but
    never advances a frame (RA auto-pauses when a prior movie ends) — the
    'loads state, starts, then stops' replay bug. resume_if_paused unpauses so
    playback can run."""
    client._nci.get_status.return_value = StatusInfo(
        state="PAUSED", game="X", system="SNES", crc32="x",
    )
    await client.resume_if_paused()
    client._nci.pause_toggle.assert_called_once()


@pytest.mark.asyncio
async def test_resume_if_paused_noop_when_playing(client):
    """PAUSE_TOGGLE is a blind flip, so resume must check state first and only
    toggle when actually paused — otherwise it would pause a running RA."""
    client._nci.get_status.return_value = StatusInfo(
        state="PLAYING", game="X", system="SNES", crc32="x",
    )
    await client.resume_if_paused()
    client._nci.pause_toggle.assert_not_called()


def test_set_game_basename_refreshes_cached_basename(client):
    """After a ROM switch (detected by the orchestrator's GET_STATUS poll), the
    cached basename must be refreshable so movie record/replay staging uses the
    live ROM name instead of the stale one from connect() (backlog D #2: a
    Love Yourself movie was staged at 'Beto.replay*' after a switch)."""
    client._game_basename = "Beto"
    client.set_game_basename("Love Yourself")
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
    savestate_dir, _ = ra_dirs

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
    savestate_dir, _ = ra_dirs
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
    savestate_dir, _ = ra_dirs
    src = tmp_path / "saved.state"
    src.write_bytes(b"data")

    before = client.state_version
    await client.load_state(src)
    assert client.state_version == before + 1


def test_replay_state_load_increments_state_version(client):
    """PLAY_REPLAY loads a savestate too. RAMovieIO's on_state_load is wired to
    RAClient._note_replay_state_load so replay start bumps state_version,
    letting the poller resync and synthesize the replay entrance."""
    before = client.state_version
    client._movie_io._on_state_load()
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


@pytest.mark.asyncio
async def test_load_state_raises_when_ram_unchanged_after_settle(client, tmp_path):
    """RA silently rejects malformed state files — NCI returns no error code.

    SpinLab infers success by reading game-state RAM before and after the
    load. If every poll in LOAD_VALIDATION_POLL_MAX_SEC matches the pre-load
    bytes, the load was a no-op and we raise so the practice loop can
    surface a clear error instead of hanging waiting for events that will
    never fire.
    """
    # Tighten the validation window so the test doesn't sit on the full
    # 500ms when failing — patch the module constant for the duration.
    import spinlab.retroarch.raclient as raclient_mod

    src = tmp_path / "saved.state"
    src.write_bytes(b"malformed-but-existent")
    client._nci.read_ram.side_effect = None
    client._nci.read_ram.return_value = b"\x00" * 155

    original_max = raclient_mod.LOAD_VALIDATION_POLL_MAX_SEC
    raclient_mod.LOAD_VALIDATION_POLL_MAX_SEC = 0.05
    try:
        with pytest.raises(StateLoadError, match="game-state RAM did not change"):
            await client.load_state(src)
    finally:
        raclient_mod.LOAD_VALIDATION_POLL_MAX_SEC = original_max


@pytest.mark.asyncio
async def test_load_state_succeeds_when_ram_changes_within_window(client, tmp_path):
    """Happy path: post-load RAM differs from pre-load on the very first
    post-load poll. No raise, state_version advances exactly once."""
    src = tmp_path / "saved.state"
    src.write_bytes(b"valid-state")

    call_idx = [0]
    def side_effect(addr: int, length: int) -> bytes:
        call_idx[0] += 1
        # First read = pre-load snapshot; subsequent reads differ.
        return b"\x00" * length if call_idx[0] == 1 else b"\xff" * length
    client._nci.read_ram.side_effect = side_effect

    before = client.state_version
    await client.load_state(src)
    assert client.state_version == before + 1


@pytest.mark.asyncio
async def test_load_state_polls_until_ram_changes(client, tmp_path):
    """RA may take longer than load_settle_sec to actually process the load.
    The validation loop should keep polling until the change is observed."""
    src = tmp_path / "saved.state"
    src.write_bytes(b"valid-state")

    call_idx = [0]
    def side_effect(addr: int, length: int) -> bytes:
        call_idx[0] += 1
        # Pre-load read = call 1. Next 2 polls still show pre-load bytes
        # (RA still processing). Poll 3 finally shows the post-load state.
        return b"\x00" * length if call_idx[0] <= 3 else b"\xff" * length
    client._nci.read_ram.side_effect = side_effect

    await client.load_state(src)  # must not raise
    assert call_idx[0] >= 4, "should have polled past the still-pre-load reads"


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

