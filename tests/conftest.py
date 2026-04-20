"""Shared test fixtures for unit tests."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
from spinlab.estimators import list_estimators, get_estimator


def make_test_config(**overrides) -> AppConfig:
    """Build an AppConfig for tests. Override any field via kwargs."""
    return AppConfig(
        network=NetworkConfig(port=overrides.get("port", 59999)),
        emulator=overrides.get("emulator", EmulatorConfig()),
        data_dir=overrides.get("data_dir", Path("data")),
        rom_dir=overrides.get("rom_dir"),
        category=overrides.get("category", "any%"),
    )


@pytest.fixture
def mock_tcp():
    """Mock TcpManager with connected state."""
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    tcp.send_command = AsyncMock()
    tcp.recv_event = AsyncMock(return_value=None)
    tcp.disconnect = AsyncMock()
    return tcp


@pytest.fixture
def mock_db():
    """Mock Database with all methods stubbed."""
    db = MagicMock()
    db.upsert_game = MagicMock()
    db.create_session = MagicMock()
    db.end_session = MagicMock()
    db.create_capture_run = MagicMock()
    db.set_active_capture_run = MagicMock()
    db.get_recent_attempts = MagicMock(return_value=[])
    db.get_all_segments_with_model = MagicMock(return_value=[])
    db.load_model_state = MagicMock(return_value=None)
    db.load_allocator_config = MagicMock(return_value=None)
    db.upsert_segment = MagicMock()
    db.add_variant = MagicMock()
    db.get_active_segments = MagicMock(return_value=[])
    db.promote_draft = MagicMock()
    db.hard_delete_capture_run = MagicMock()
    db.segments_missing_cold = MagicMock(return_value=[])
    return db


@pytest.fixture(params=list_estimators())
def estimator_name(request):
    """Parametrized fixture that yields each registered estimator name."""
    return request.param


@pytest.fixture
def estimator(estimator_name):
    """Instantiated estimator from parametrized name."""
    return get_estimator(estimator_name)


class FakeTcpManager:
    """Fake TcpManager that records commands and lets tests control state.

    Use in place of a mock when you want to verify *what* was sent without
    tying tests to mock call syntax. Tests can read `sent_commands` to see
    every command that was sent, in order.
    """
    def __init__(self, connected: bool = True) -> None:
        self.is_connected: bool = connected
        self.sent_commands: list = []
        self.on_disconnect = None

    async def send_command(self, cmd) -> None:
        if not self.is_connected:
            raise ConnectionError("Not connected")
        self.sent_commands.append(cmd)

    async def send(self, msg: str) -> None:
        pass

    async def disconnect(self) -> None:
        self.is_connected = False


@pytest.fixture
def fake_tcp():
    """Fresh FakeTcpManager per test, starts connected."""
    return FakeTcpManager(connected=True)


from spinlab.db import Database
from spinlab.models import Segment, Waypoint, WaypointSaveState


def make_seg_with_state(db, game_id, level, start_type, end_type,
                        state_path, ordinal=1):
    """Create waypoints + segment + hot save state; return segment."""
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
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id, variant_type="hot",
        state_path=str(state_path), is_default=True,
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
