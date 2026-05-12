"""Test that POST /api/reset logs the action.

Uses caplog to capture real log output regardless of which logger emits
the warning — survives refactors that move the warning into a helper
without breaking the test silently.
"""
import logging

import pytest
from tests.conftest import FakeEmuBackend

from spinlab.db import Database
from spinlab.models import Mode
from spinlab.routes.system import reset_data
from spinlab.session_manager import SessionManager


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "reset.db")
    d.upsert_game("abc123", "Test Game", "any%")
    return d


@pytest.fixture
def emu():
    return FakeEmuBackend(connected=False)


@pytest.mark.asyncio
async def test_reset_logs_warning_with_game_id(db, emu, caplog):
    """POST /api/reset should emit a WARNING log containing the game id."""
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = "abc123"
    sm.mode = Mode.IDLE

    with caplog.at_level(logging.WARNING, logger="spinlab.routes.system"):
        result = await reset_data(session=sm, db=db)

    assert result == {"status": "ok"}
    assert any(
        record.levelno == logging.WARNING and "abc123" in record.getMessage()
        for record in caplog.records
    ), f"expected WARNING mentioning 'abc123'; got {[(r.levelname, r.getMessage()) for r in caplog.records]}"


@pytest.mark.asyncio
async def test_reset_does_not_log_when_no_game_loaded(db, emu, caplog):
    """No game_id → no warning (and reset still succeeds idempotently)."""
    sm = SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")
    sm.game_id = None

    with caplog.at_level(logging.WARNING, logger="spinlab.routes.system"):
        result = await reset_data(session=sm, db=db)

    assert result == {"status": "ok"}
    assert all(
        "reset: clearing all data" not in record.getMessage()
        for record in caplog.records
    )
