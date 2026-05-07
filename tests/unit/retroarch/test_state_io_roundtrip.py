"""save -> load roundtrip with the same fake NCI client."""
from spinlab.retroarch.state_io import DEFAULT_RESERVED_SLOT, StateIO


class _FakeNCI:
    def __init__(self) -> None:
        self._next_save_payload: bytes = b""
        self.save_state_calls = 0
        self.load_state_slot_calls: list[int] = []
        self._slot_path = None

    def bind(self, slot_path) -> None:
        self._slot_path = slot_path

    def stage_save_payload(self, payload: bytes) -> None:
        self._next_save_payload = payload

    def save_state(self) -> None:
        self.save_state_calls += 1
        self._slot_path.write_bytes(self._next_save_payload)

    def load_state_slot(self, slot: int) -> None:
        self.load_state_slot_calls.append(slot)


def test_save_then_load_roundtrip(tmp_path):
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"

    nci = _FakeNCI()
    nci.bind(slot_path)
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
        save_timeout_sec=0.5,
    )

    nci.stage_save_payload(b"PAYLOAD_AT_T=0")
    sp_path = io.save_segment_state("seg-A")
    assert sp_path.read_bytes() == b"PAYLOAD_AT_T=0"
    assert not slot_path.exists()

    io.load_segment_state("seg-A")
    assert slot_path.read_bytes() == b"PAYLOAD_AT_T=0"
    assert nci.load_state_slot_calls == [DEFAULT_RESERVED_SLOT]
    assert sp_path.exists()  # SpinLab file persists for re-load


def test_save_two_segments_then_load_each(tmp_path):
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"

    nci = _FakeNCI()
    nci.bind(slot_path)
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
        save_timeout_sec=0.5,
    )

    nci.stage_save_payload(b"DATA-A")
    io.save_segment_state("seg-A")
    nci.stage_save_payload(b"DATA-B")
    io.save_segment_state("seg-B")

    io.load_segment_state("seg-A")
    assert slot_path.read_bytes() == b"DATA-A"
    io.load_segment_state("seg-B")
    assert slot_path.read_bytes() == b"DATA-B"
