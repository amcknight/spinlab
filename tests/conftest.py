"""Shared test fixtures for unit tests."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.db import Database
from spinlab.models import Segment, Waypoint, WaypointSaveState


def make_test_config(**overrides) -> AppConfig:
    """Build an AppConfig for tests. Override any field via kwargs."""
    # savestate_dir and spinlab_state_dir are required by build_orchestrator.
    # Tests that never actually connect to RA can use dummy paths.
    default_emu = EmulatorConfig(
        savestate_dir=Path("/tmp/test-ra-states"),
        spinlab_state_dir=Path("/tmp/test-spinlab-states"),
    )
    return AppConfig(
        network=NetworkConfig(port=overrides.get("port", 59999)),
        emulator=overrides.get("emulator", default_emu),
        data_dir=overrides.get("data_dir", Path("data")),
        rom_dir=overrides.get("rom_dir"),
        category=overrides.get("category", "any%"),
    )


@pytest.fixture
def mock_emu():
    """Mock EmuBackend with connected state."""
    emu = MagicMock()
    emu.is_connected = True
    emu.send = AsyncMock()
    emu.send_command = AsyncMock()
    emu.recv_event = AsyncMock(return_value=None)
    emu.disconnect = AsyncMock()
    emu.save_state = AsyncMock()
    emu.load_state = AsyncMock()
    return emu


class FakeEmuBackend:
    """In-process EmuBackend stand-in that records commands and exposes state.

    Use in place of a mock when you want to verify *what* was sent without
    tying tests to mock call syntax. Tests can read `sent_commands` to see
    every command that was sent, in order.

    Implements the full ``EmuBackend`` Protocol — see
    ``tests/unit/test_emu_backend_protocol.py`` for the conformance check.
    """
    def __init__(self, connected: bool = True) -> None:
        self.is_connected: bool = connected
        self.sent_commands: list[object] = []
        self.save_state_calls: list[str] = []
        self.load_state_calls: list[str] = []
        self.on_disconnect = None
        # Test hook: when True, save_state raises after recording the call,
        # simulating an RA backend that accepted the request but failed to
        # write a file (e.g., StateSaveTimeoutError).
        self.save_state_should_raise: bool = False

    async def connect(self, timeout: float = 0) -> bool:
        # No-op for tests; consumer code reads `is_connected` directly.
        return self.is_connected

    async def disconnect(self) -> None:
        self.is_connected = False

    async def send_command(self, cmd: object) -> None:
        if not self.is_connected:
            raise ConnectionError("Not connected")
        self.sent_commands.append(cmd)

    async def send(self, msg: str) -> None:
        pass

    async def recv_event(self, timeout: float | None = None) -> object | None:
        # Tests that need event delivery drive SessionManager.route_event directly.
        return None

    async def save_state(self, segment_id: str) -> None:
        self.save_state_calls.append(segment_id)
        if self.save_state_should_raise:
            raise RuntimeError(f"simulated save_state failure for {segment_id}")

    async def load_state(self, state_path: str) -> None:
        self.load_state_calls.append(state_path)


@pytest.fixture
def fake_emu():
    """Fresh FakeEmuBackend per test, starts connected."""
    return FakeEmuBackend(connected=True)


def make_seg_with_state(
    db: Database,
    game_id: str,
    level: int,
    start_type,
    end_type,
    state_path: Path,
    ordinal: int = 1,
) -> Segment:
    """Create waypoints + segment + save state; return segment.

    Picks the conventional variant for the start_type (cold for entrance,
    hot for checkpoint) so `get_all_segments_with_model` resolves the
    state_path correctly in tests.
    """
    wp_start = Waypoint.make(game_id, level, start_type, 0, {})
    wp_end = Waypoint.make(game_id, level, end_type, 0, {})
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id(game_id, level, start_type, 0, end_type, 0,
                           wp_start.id, wp_end.id),
        game_id=game_id, level_number=level,
        start_type=start_type, start_ordinal=0,
        end_type=end_type, end_ordinal=0,
        description=f"L{level}" if start_type == "entrance" else "",
        ordinal=ordinal,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
    )
    db.upsert_segment(seg)
    variant = "cold" if start_type == "entrance" else "hot"
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id, variant_type=variant,
        state_path=str(state_path),
    ))
    return seg


@pytest.fixture
def practice_db(tmp_path):
    """Real DB with one game + one entrance->goal segment for practice tests."""
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    state_file = tmp_path / "test.mss"
    state_file.write_bytes(b"fake state")
    seg = make_seg_with_state(d, "g", 1, "entrance", "goal", state_file)
    d._test_seg_id = seg.id
    d._test_state_file = state_file
    return d
