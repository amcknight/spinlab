"""save_segment_state tests — fake NCI, real tmp_path filesystem."""
import time
from pathlib import Path

import pytest

from spinlab.retroarch.state_io import (
    DEFAULT_RESERVED_SLOT,
    StateIO,
    StateSaveTimeout,
)


class _FakeNCI:
    """NCIClient stub. save_state() optionally simulates RA writing the slot file."""

    def __init__(self) -> None:
        self.save_state_calls = 0
        self.load_state_slot_calls: list[int] = []
        self._on_save = None  # callable invoked when save_state fires

    def save_state(self) -> None:
        self.save_state_calls += 1
        if self._on_save:
            self._on_save()

    def load_state_slot(self, slot: int) -> None:
        self.load_state_slot_calls.append(slot)


@pytest.fixture
def setup(tmp_path):
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()
    nci = _FakeNCI()
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
        save_timeout_sec=0.5,
    )
    return io, nci, ra_dir, sl_dir


def test_save_segment_state_first_capture(setup):
    """No pre-existing slot file. save_state() writes one. We move it to SpinLab path."""
    io, nci, ra_dir, sl_dir = setup
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"

    nci._on_save = lambda: slot_path.write_bytes(b"FAKE_SAVE_DATA")

    result = io.save_segment_state("seg-1")

    assert nci.save_state_calls == 1
    assert result == sl_dir / "seg-1.state"
    assert result.read_bytes() == b"FAKE_SAVE_DATA"
    assert not slot_path.exists(), "slot file should have been moved out of RA dir"


def test_save_segment_state_overwrites_previous(setup):
    """Second capture for the same segment overwrites the SpinLab file."""
    io, nci, ra_dir, sl_dir = setup
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    sp_path = sl_dir / "seg-1.state"
    sp_path.write_bytes(b"OLD")

    # Pre-existing slot file at older mtime.
    slot_path.write_bytes(b"PREEXISTING_SLOT")

    def on_save():
        time.sleep(0.01)
        slot_path.write_bytes(b"NEW_SAVE_DATA")

    nci._on_save = on_save

    result = io.save_segment_state("seg-1")
    assert result.read_bytes() == b"NEW_SAVE_DATA"


def test_save_segment_state_times_out_when_save_doesnt_happen(setup):
    """If no slot file appears, raise StateSaveTimeout — after retries."""
    from spinlab.retroarch.state_io import SAVE_RETRY_ATTEMPTS

    io, nci, ra_dir, sl_dir = setup
    nci._on_save = None

    with pytest.raises(StateSaveTimeout):
        io.save_segment_state("seg-2")

    # NCI SAVE_STATE intermittently no-ops in real RA; we retry. Each attempt
    # waits save_timeout_sec for any state file to appear.
    assert nci.save_state_calls == SAVE_RETRY_ATTEMPTS


def test_save_segment_state_times_out_when_existing_file_unchanged(setup):
    """Pre-existing slot file with no mtime advance -> timeout."""
    io, nci, ra_dir, sl_dir = setup
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    slot_path.write_bytes(b"STALE")
    nci._on_save = None

    with pytest.raises(StateSaveTimeout):
        io.save_segment_state("seg-3")


def test_save_segment_state_succeeds_on_retry_after_intermittent_noop(setup):
    """Real-world: RA's NCI SAVE_STATE intermittently no-ops during level
    transitions. Should retry and succeed when the next attempt lands."""
    io, nci, ra_dir, sl_dir = setup
    slot_path = ra_dir / "Game.state500"
    nci._attempts = 0

    def on_save():
        nci._attempts += 1
        # First attempt: silently no-op. Second: write the file.
        if nci._attempts >= 2:
            slot_path.write_bytes(b"WROTE_ON_SECOND_TRY")

    nci._on_save = on_save

    result = io.save_segment_state("seg-retry")
    assert result.read_bytes() == b"WROTE_ON_SECOND_TRY"
    assert nci.save_state_calls == 2  # succeeded on the 2nd try


def test_save_segment_state_picks_up_any_state_file_RA_writes(setup):
    """Regression: RA's NCI SAVE_STATE writes to whatever slot RA's own
    state_slot counter is at — not to a fixed reserved slot we control.

    The original implementation watched <game>.state9999 specifically and
    timed out forever because RA was writing to <game>.state500 etc. This
    test simulates that scenario: RA writes to a non-reserved slot, and
    save_segment_state should still find and move it.
    """
    io, nci, ra_dir, sl_dir = setup

    # RA's auto-index counter is at 500; SAVE_STATE writes <game>.state500.
    actual_slot_file = ra_dir / "Game.state500"

    def on_save():
        actual_slot_file.write_bytes(b"FROM_RAS_AUTO_INDEX")

    nci._on_save = on_save

    result = io.save_segment_state("seg-X")

    assert result.read_bytes() == b"FROM_RAS_AUTO_INDEX"
    assert not actual_slot_file.exists(), \
        "RA's slot file should have been moved out of RA's dir"


def test_save_segment_state_creates_spinlab_dir_if_missing(tmp_path):
    """Constructor creates spinlab_state_dir; verify by passing one that doesn't exist."""
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "deep" / "nested" / "states"
    nci = _FakeNCI()
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
        save_timeout_sec=0.5,
    )
    assert sl_dir.exists()

    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    nci._on_save = lambda: slot_path.write_bytes(b"D")
    result = io.save_segment_state("a")
    assert result.exists()
