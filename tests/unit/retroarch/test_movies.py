"""Tests for MovieController — owns RA movie record/playback lifecycle."""
from __future__ import annotations

from pathlib import Path

import pytest
from tests.fakes.raclient import FakeMovieIO, FakeRAClient

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
def movie_io():
    return FakeMovieIO()


@pytest.fixture
def events():
    return []


@pytest.fixture
def mc(movie_io, raclient, events):
    return MovieController(
        movie_io=movie_io, raclient=raclient,
        enable=True, on_event=events.append,
    )


@pytest.mark.asyncio
async def test_start_recording_noop_when_disabled(movie_io, raclient, events):
    mc = MovieController(movie_io=movie_io, raclient=raclient, enable=False, on_event=events.append)
    await mc.start_recording(Path("/tmp/x.replay"))
    assert movie_io.record_movie_calls == []
    assert mc.is_recording is False


@pytest.mark.asyncio
async def test_start_recording_nonfatal_on_raclient_error(movie_io, raclient, events):
    from spinlab.retroarch.raclient import RAClientError

    mc = MovieController(movie_io=movie_io, raclient=raclient, enable=True, on_event=events.append)
    # Patch record_movie to raise — override on FakeMovieIO directly.
    async def _raises(_path):
        raise RAClientError("boom")
    movie_io.record_movie = _raises

    await mc.start_recording(Path("/tmp/x.replay"))
    assert mc.is_recording is False


@pytest.mark.asyncio
async def test_stop_recording_noop_when_nothing_active(mc):
    await mc.stop_recording()
    # No exception; no recording handle existed


@pytest.mark.asyncio
async def test_start_playback_raises_when_disabled(movie_io, raclient, events):
    mc = MovieController(movie_io=movie_io, raclient=raclient, enable=False, on_event=events.append)
    with pytest.raises(BackendNotImplementedError):
        await mc.start_playback(Path("/tmp/x.replay"), speed=0)


@pytest.mark.asyncio
async def test_start_playback_emits_started(mc, movie_io, events):
    movie_io.frame_count = 1234
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
async def test_start_playback_movie_error_emits_replay_error(mc, movie_io, events):
    movie_io.fail_play_movie = True
    movie_io.play_movie_error_message = "verify failed"
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


@pytest.mark.asyncio
async def test_start_playback_calls_on_replay_started_before_play_movie(
    movie_io, raclient, events,
):
    """The on_replay_started hook (used to fire `detector.mark_replay_entrance`)
    must run BEFORE play_movie awaits. play_movie sends PLAY_REPLAY then waits
    ~150ms verifying WRAM advanced; during that window the poller (60Hz
    concurrent asyncio task) already sees the post-load level_start=1 state
    and the detector's natural rising-edge check misses if prev=1, curr=1.
    Setting the flag before play_movie closes the race.
    """
    order: list[str] = []
    def _on_replay_started() -> None:
        order.append("hook")

    orig_play_movie = movie_io.play_movie
    async def _instrumented_play(path):
        order.append("play_movie")
        return await orig_play_movie(path)
    movie_io.play_movie = _instrumented_play

    def _capture_event(ev) -> None:
        order.append(type(ev).__name__)
        events.append(ev)

    mc = MovieController(
        movie_io=movie_io, raclient=raclient,
        enable=True, on_event=_capture_event,
        on_replay_started=_on_replay_started,
    )
    await mc.start_playback(Path("/x.replay"), speed=1)
    assert order == ["hook", "play_movie", "ReplayStartedEvent"]


@pytest.mark.asyncio
async def test_start_playback_hook_fires_even_on_movie_error(
    movie_io, raclient, events,
):
    """The hook is intentionally fired pre-play, so it still runs when
    play_movie raises. Leaving the flag set is safe (idempotent for any
    subsequent legitimate level entrance — the natural rising edge and the
    forced flag combine into a single event)."""
    called: list[None] = []
    movie_io.fail_play_movie = True
    movie_io.play_movie_error_message = "verify failed"
    mc = MovieController(
        movie_io=movie_io, raclient=raclient,
        enable=True, on_event=events.append,
        on_replay_started=lambda: called.append(None),
    )
    await mc.start_playback(Path("/x.replay"), speed=1)
    # Hook fired once, before play_movie's verify-fail caused the early return.
    assert len(called) == 1
    assert any(isinstance(e, ReplayErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_stop_recording_nonfatal_on_movie_record_error(movie_io, raclient, events):
    """stop_recording must swallow MovieRecordError (which extends MovieIOError,
    not RAClientError after the Task 1 refactor)."""
    from spinlab.retroarch.movie_io import MovieRecordError, MovieRecording

    async def _failing_stop() -> Path:
        raise MovieRecordError("RA produced no file")

    mc = MovieController(movie_io=movie_io, raclient=raclient, enable=True, on_event=events.append)
    # Inject a recording handle whose stop() raises.
    mc._active_recording = MovieRecording(path=Path("/tmp/x.replay"), _stop=_failing_stop)
    await mc.stop_recording()
    # Did NOT propagate. Recording cleared.
    assert mc.is_recording is False
