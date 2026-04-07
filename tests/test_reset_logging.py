"""Test that POST /api/reset logs the action."""
import logging

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.mark.asyncio
async def test_reset_logs_warning_with_game_id():
    """POST /api/reset should log a warning with the game ID."""
    from spinlab.routes.system import reset_data

    mock_session = MagicMock()
    mock_session.stop_practice = AsyncMock()
    mock_session.mode = MagicMock()
    mock_session.mode.__eq__ = lambda self, other: False  # not REFERENCE
    mock_session.game_id = "abc123"
    mock_session.scheduler = MagicMock()

    mock_db = MagicMock()

    with patch("spinlab.routes.system.logger") as mock_logger:
        await reset_data(session=mock_session, db=mock_db)
        mock_logger.warning.assert_called_once()
        assert "abc123" in str(mock_logger.warning.call_args)
