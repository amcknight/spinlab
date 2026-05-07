"""load_segment_state / load_state_from_path tests.

Captures the slot file's contents *during* load_state_slot via a side-effect on
the fake NCI — necessary because the production code now deletes the slot file
after RA finishes reading it (StateIO._cleanup_slot_after_load), so we can't
assert post-call state on the slot path.
"""
from pathlib import Path

import pytest

from spinlab.retroarch.state_io import DEFAULT_RESERVED_SLOT, StateIO


class _FakeNCI:
    """Fake NCI that snapshots the slot file's bytes at the moment of load.

    Tests bind .slot_path to the expected slot file; load_state_slot reads it
    and stashes the bytes so tests can assert what RA *would have seen*.
    """

    def __init__(self) -> None:
        self.save_state_calls = 0
        self.load_state_slot_calls: list[int] = []
        self.captured_payloads: list[bytes] = []
        self.slot_path: Path | None = None

    def save_state(self) -> None:
        self.save_state_calls += 1

    def load_state_slot(self, slot: int) -> None:
        self.load_state_slot_calls.append(slot)
        if self.slot_path is not None and self.slot_path.exists():
            self.captured_payloads.append(self.slot_path.read_bytes())


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
        load_settle_sec=0,  # no sleep in tests
    )
    nci.slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    return io, nci, ra_dir, sl_dir


def test_load_copies_file_into_slot_then_calls_nci(setup):
    """SpinLab file exists → copy to slot path, fire LOAD_STATE_SLOT 9999, then clean up."""
    io, nci, ra_dir, sl_dir = setup
    sp_path = sl_dir / "seg-1.state"
    sp_path.write_bytes(b"STATEDATA")

    io.load_segment_state("seg-1")

    # Captured at moment of NCI call: RA saw the right bytes in the slot.
    assert nci.captured_payloads == [b"STATEDATA"]
    # Cleaned up after load — the slot file is gone from RA's dir.
    assert not (ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}").exists()
    # SpinLab source persists (copy, not move).
    assert sp_path.exists()
    assert nci.load_state_slot_calls == [DEFAULT_RESERVED_SLOT]


def test_load_overwrites_existing_slot_file(setup):
    """Slot file already exists → overwrite, RA sees new bytes, then it's cleaned up."""
    io, nci, ra_dir, sl_dir = setup
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    # Pre-existing leftover (e.g. dropped by a prior load) — but the StateIO
    # constructor's startup sweep already removed it. Re-create here to
    # simulate the scenario this test name implies.
    slot_path.write_bytes(b"OLD")
    sp_path = sl_dir / "seg-1.state"
    sp_path.write_bytes(b"NEW")

    io.load_segment_state("seg-1")

    assert nci.captured_payloads == [b"NEW"]
    assert not slot_path.exists()


def test_load_missing_segment_state_raises(setup):
    """No SpinLab file for this segment → FileNotFoundError, no NCI call."""
    io, nci, ra_dir, sl_dir = setup

    with pytest.raises(FileNotFoundError, match="seg-missing"):
        io.load_segment_state("seg-missing")

    assert nci.load_state_slot_calls == []


def test_load_uses_custom_reserved_slot(tmp_path):
    """If reserved_slot=42, file goes to <game>.state42 and load_state_slot(42)."""
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()
    nci = _FakeNCI()
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="G",
        reserved_slot=42,
        load_settle_sec=0,
    )
    nci.slot_path = ra_dir / "G.state42"
    (sl_dir / "x.state").write_bytes(b"D")

    io.load_segment_state("x")

    assert nci.captured_payloads == [b"D"]
    assert not (ra_dir / "G.state42").exists()
    assert nci.load_state_slot_calls == [42]


def test_load_state_from_path_copies_into_slot(setup, tmp_path):
    """Direct path → copy into slot, fire LOAD_STATE_SLOT, clean up."""
    io, nci, ra_dir, sl_dir = setup
    src = tmp_path / "external.state"
    src.write_bytes(b"EXTERNAL_PAYLOAD")

    io.load_state_from_path(src)

    assert nci.captured_payloads == [b"EXTERNAL_PAYLOAD"]
    assert not (ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}").exists()
    assert src.exists()  # source persists (copy, not move)
    assert nci.load_state_slot_calls == [DEFAULT_RESERVED_SLOT]


def test_load_state_from_path_missing_raises(setup):
    """No source file → FileNotFoundError, no NCI call."""
    io, nci, ra_dir, sl_dir = setup
    with pytest.raises(FileNotFoundError, match="missing.state"):
        io.load_state_from_path(sl_dir / "missing.state")
    assert nci.load_state_slot_calls == []


def test_load_state_from_path_accepts_str_path(setup, tmp_path):
    """Caller may pass a str path; method coerces internally."""
    io, nci, ra_dir, sl_dir = setup
    src = tmp_path / "external.state"
    src.write_bytes(b"X")

    io.load_state_from_path(str(src))

    assert nci.captured_payloads == [b"X"]
    assert not (ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}").exists()


def test_startup_sweep_removes_stale_slot_file(tmp_path):
    """If a prior session left <game>.state<reserved_slot>, __init__ wipes it."""
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()
    stale = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    stale.write_bytes(b"LEFTOVER_FROM_PRIOR_RUN")

    StateIO(
        client=_FakeNCI(),
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
    )

    assert not stale.exists(), "stale slot file should be swept on construction"
