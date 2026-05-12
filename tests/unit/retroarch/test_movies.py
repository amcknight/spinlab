"""Tests for MovieController — owns RA movie record/playback lifecycle."""
from __future__ import annotations

from pathlib import Path

import pytest
from tests.fakes.raclient import FakeRAClient

from spinlab.errors import BackendNotImplementedError
from spinlab.protocol import (
    SPEED_UNCAPPED,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayStartedEvent,
)
from spinlab.retroarch.movies import MovieController


@pytest.fixture
def raclient():
    return FakeRAClient()


@pytest.fixture
def events():
    return []


@pytest.fixture
def mc(raclient, events):
    return MovieController(raclient=raclient, enable=True, on_event=events.append)


@pytest.mark.asyncio
async def test_start_recording_noop_when_disabled(raclient, events):
    mc = MovieController(raclient=raclient, enable=False, on_event=events.append)
    await mc.start_recording(Path("/tmp/x.replay"))
    assert raclient.record_movie_calls == []
    assert mc.is_recording is False


@pytest.mark.asyncio
async def test_start_recording_nonfatal_on_raclient_error(raclient, events):
    from spinlab.retroarch.raclient import RAClientError
    raclient.fail_record_movie = True
    raclient.record_movie_error_message = "boom"

    mc = MovieController(raclient=raclient, enable=True, on_event=events.append)
    # Patch record_movie to raise — FakeRAClient doesn't support this flag
    # natively, so monkey it.
    async def _raises(_path):
        raise RAClientError("boom")
    raclient.record_movie = _raises

    await mc.start_recording(Path("/tmp/x.replay"))
    assert mc.is_recording is False


@pytest.mark.asyncio
async def test_stop_recording_noop_when_nothing_active(mc):
    await mc.stop_recording()
    # No exception; no recording handle existed


@pytest.mark.asyncio
async def test_start_playback_raises_when_disabled(raclient, events):
    mc = MovieController(raclient=raclient, enable=False, on_event=events.append)
    with pytest.raises(BackendNotImplementedError):
        await mc.start_playback(Path("/tmp/x.replay"), speed=0)


@pytest.mark.asyncio
async def test_start_playback_emits_started(mc, events, raclient):
    raclient.frame_count = 1234
    await mc.start_playback(Path("/x.replay"), speed=1)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ReplayStartedEvent)
    assert ev.frame_count == 1234


@pytest.mark.asyncio
async def test_start_playback_uncapped_toggles_fast_forward(mc, raclient):
    await mc.start_playback(Path("/x.replay"), speed=SPEED_UNCAPPED)
    assert raclient.fast_forward_toggles == 1


@pytest.mark.asyncio
async def test_start_playback_capped_does_not_toggle(mc, raclient):
    await mc.start_playback(Path("/x.replay"), speed=1)
    assert raclient.fast_forward_toggles == 0


@pytest.mark.asyncio
async def test_start_playback_movie_error_emits_replay_error(mc, raclient, events):
    raclient.fail_play_movie = True
    raclient.play_movie_error_message = "verify failed"
    await mc.start_playback(Path("/x.replay"), speed=1)
    assert len(events) == 1
    assert isinstance(events[0], ReplayErrorEvent)
    assert "verify failed" in events[0].message


@pytest.mark.asyncio
async def test_stop_playback_symmetric_toggle(mc, raclient, events):
    await mc.start_playback(Path("/x.replay"), speed=SPEED_UNCAPPED)
    assert raclient.fast_forward_toggles == 1
    await mc.stop_playback()
    assert raclient.fast_forward_toggles == 2, "stop must toggle off"
    assert any(isinstance(e, ReplayFinishedEvent) for e in events)


@pytest.mark.asyncio
async def test_stop_playback_no_toggle_when_not_fast_forwarding(mc, raclient):
    await mc.start_playback(Path("/x.replay"), speed=1)
    assert raclient.fast_forward_toggles == 0
    await mc.stop_playback()
    assert raclient.fast_forward_toggles == 0


@pytest.mark.asyncio
async def test_stop_playback_idempotent(mc, events):
    await mc.stop_playback()
    await mc.stop_playback()
    # No ReplayFinishedEvent emitted from a stop with no active playback
    assert not any(isinstance(e, ReplayFinishedEvent) for e in events)
