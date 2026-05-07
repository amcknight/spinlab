"""load_segment_state tests."""
import pytest

from spinlab.retroarch.state_io import DEFAULT_RESERVED_SLOT, StateIO


class _FakeNCI:
    def __init__(self) -> None:
        self.save_state_calls = 0
        self.load_state_slot_calls: list[int] = []

    def save_state(self) -> None:
        self.save_state_calls += 1

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
    )
    return io, nci, ra_dir, sl_dir


def test_load_copies_file_into_slot_then_calls_nci(setup):
    """SpinLab file exists -> copy to slot path, fire LOAD_STATE_SLOT 9999."""
    io, nci, ra_dir, sl_dir = setup
    sp_path = sl_dir / "seg-1.state"
    sp_path.write_bytes(b"STATEDATA")

    io.load_segment_state("seg-1")

    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    assert slot_path.read_bytes() == b"STATEDATA"
    assert sp_path.exists()  # copy, not move
    assert nci.load_state_slot_calls == [DEFAULT_RESERVED_SLOT]


def test_load_overwrites_existing_slot_file(setup):
    """Slot file already exists -> overwrite."""
    io, nci, ra_dir, sl_dir = setup
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    slot_path.write_bytes(b"OLD")
    sp_path = sl_dir / "seg-1.state"
    sp_path.write_bytes(b"NEW")

    io.load_segment_state("seg-1")

    assert slot_path.read_bytes() == b"NEW"


def test_load_missing_segment_state_raises(setup):
    """No SpinLab file for this segment -> FileNotFoundError, no NCI call."""
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
    )
    (sl_dir / "x.state").write_bytes(b"D")

    io.load_segment_state("x")

    assert (ra_dir / "G.state42").read_bytes() == b"D"
    assert nci.load_state_slot_calls == [42]
