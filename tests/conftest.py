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
