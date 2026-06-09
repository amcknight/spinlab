import pytest
from unittest.mock import MagicMock

from spinlab.protocol import PracticePauseCmd
from spinlab.retroarch.orchestrator import RetroArchOrchestrator


def _orch():
    return RetroArchOrchestrator(
        raclient=MagicMock(), poller=MagicMock(), conditions=MagicMock(),
        practice_timing=MagicMock(), hyper_play_timing=MagicMock(),
        state_paths=MagicMock(), movies=MagicMock(),
    )


@pytest.mark.asyncio
async def test_practice_pause_cmd_disarms_timing():
    orch = _orch()
    await orch.send_command(PracticePauseCmd())
    orch._practice_timing.disarm.assert_called_once()
