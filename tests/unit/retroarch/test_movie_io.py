"""Unit tests for RAMovieIO.

Covers replay-slot resolution from RA's log, and movie_dir guard checks.
Tests migrated from test_raclient.py as part of the RAClient movie-shim
cleanup (Task 3, Bundle 2).
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from spinlab.retroarch.movie_io import RAMovieIO
from spinlab.retroarch.nci import NCIClient

# ---------------------------------------------------------------------------
# Stub NCI
# ---------------------------------------------------------------------------

def _StubNCI() -> NCIClient:
    """Return a MagicMock standing in for NCIClient."""
    return MagicMock(spec=NCIClient)


# ---------------------------------------------------------------------------
# Replay slot resolution from RA logs
# ---------------------------------------------------------------------------

def test_find_replay_slot_from_replay_slot_line(tmp_path):
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=tmp_path,
        log_dir=tmp_path,
        game_basename=lambda: "Test Game",
    )
    (tmp_path / "retroarch__1.log").write_text(
        "Some other line\n"
        "[Replay] Replay slot: 7\n"
        "More noise\n"
    )
    assert movie_io._find_current_replay_slot() == 7


def test_find_replay_slot_from_found_last_line(tmp_path):
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=tmp_path,
        log_dir=tmp_path,
        game_basename=lambda: "Test Game",
    )
    (tmp_path / "retroarch__1.log").write_text(
        "[Replay] Found last replay slot: #3\n"
    )
    assert movie_io._find_current_replay_slot() == 3


def test_find_replay_slot_from_starting_record_line(tmp_path):
    """replay_auto_index bumps the slot at record-start with no 'Replay slot:'
    line — RAMovieIO parses the record-start path for the slot number."""
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=tmp_path,
        log_dir=tmp_path,
        game_basename=lambda: "Test Game",
    )
    (tmp_path / "retroarch__1.log").write_text(
        '[Replay] Starting movie record to "/movies/Test.replay5"\n'
    )
    assert movie_io._find_current_replay_slot() == 5


def test_find_replay_slot_picks_most_recent_match(tmp_path):
    """Multiple lines: last match wins (chronological order in the log)."""
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=tmp_path,
        log_dir=tmp_path,
        game_basename=lambda: "Test Game",
    )
    (tmp_path / "retroarch__1.log").write_text(
        "[Replay] Found last replay slot: #2\n"
        "[Replay] Replay slot: 4\n"
        "[Replay] Replay slot: 6\n"
    )
    assert movie_io._find_current_replay_slot() == 6


def test_find_replay_slot_returns_none_when_no_logs(tmp_path):
    """Empty log dir → None (falls back to slot 0 in caller)."""
    empty_log_dir = tmp_path / "empty-logs"
    empty_log_dir.mkdir()
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=tmp_path,
        log_dir=empty_log_dir,
        game_basename=lambda: "Test Game",
    )
    assert movie_io._find_current_replay_slot() is None


def test_find_replay_slot_returns_none_when_no_log_dir(tmp_path):
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=tmp_path,
        log_dir=None,
        game_basename=lambda: "Test Game",
    )
    assert movie_io._find_current_replay_slot() is None


def test_find_replay_slot_returns_none_when_no_match(tmp_path):
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=tmp_path,
        log_dir=tmp_path,
        game_basename=lambda: "Test Game",
    )
    (tmp_path / "retroarch__1.log").write_text("unrelated noise only\n")
    assert movie_io._find_current_replay_slot() is None


def test_find_replay_slot_uses_most_recent_log_file(tmp_path):
    """Multiple log files — picks the one with the newest mtime."""
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=tmp_path,
        log_dir=tmp_path,
        game_basename=lambda: "Test Game",
    )
    old = tmp_path / "retroarch__1.log"
    new = tmp_path / "retroarch__2.log"
    old.write_text("[Replay] Replay slot: 1\n")
    new.write_text("[Replay] Replay slot: 9\n")
    # Ensure 'new' has a strictly newer mtime.
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert movie_io._find_current_replay_slot() == 9


# ---------------------------------------------------------------------------
# movie_dir guard tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_movie_raises_when_movie_dir_unconfigured(tmp_path):
    from spinlab.retroarch.raclient import RAClientError
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=None,
        log_dir=None,
        game_basename=lambda: "Test Game",
    )
    with pytest.raises(RAClientError, match="ra_movie_dir is not configured"):
        await movie_io.record_movie(tmp_path / "x.replay")


@pytest.mark.asyncio
async def test_play_movie_raises_when_movie_dir_unconfigured(tmp_path):
    from spinlab.retroarch.raclient import RAClientError
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=None,
        log_dir=None,
        game_basename=lambda: "Test Game",
    )
    src = tmp_path / "x.replay"
    src.write_bytes(b"data")
    with pytest.raises(RAClientError, match="ra_movie_dir is not configured"):
        await movie_io.play_movie(src)


@pytest.mark.asyncio
async def test_play_movie_missing_source_raises(tmp_path):
    from spinlab.retroarch.raclient import StateLoadError
    movie_io = RAMovieIO(
        nci=_StubNCI(),
        movie_dir=tmp_path,
        log_dir=tmp_path,
        game_basename=lambda: "Test Game",
    )
    with pytest.raises(StateLoadError, match="Movie source not found"):
        await movie_io.play_movie(tmp_path / "nonexistent.replay")


def test_stage_and_play_signals_state_load(tmp_path):
    """PLAY_REPLAY loads the replay's embedded savestate, so _stage_and_play
    must fire the on_state_load callback (RAClient bumps state_version there so
    the poller resyncs and synthesizes the replay entrance)."""
    called: list[None] = []
    nci = _StubNCI()
    movie_io = RAMovieIO(
        nci=nci,
        movie_dir=tmp_path,
        log_dir=tmp_path,
        game_basename=lambda: "Test Game",
        on_state_load=lambda: called.append(None),
    )
    src = tmp_path / "x.replay"
    src.write_bytes(b"data")
    movie_io._stage_and_play(src, [tmp_path / "Test Game.replay0"])
    assert nci.play_replay.called
    assert called == [None]
    assert (tmp_path / "Test Game.replay0").exists()


def test_stage_and_play_stages_every_window_slot(tmp_path):
    """play_movie stages the movie across a window of slots; _stage_and_play
    must write a copy at each one so RA finds it wherever its runtime slot is."""
    nci = _StubNCI()
    movie_io = RAMovieIO(
        nci=nci, movie_dir=tmp_path, log_dir=tmp_path,
        game_basename=lambda: "Test Game", on_state_load=lambda: None,
    )
    src = tmp_path / "x.replay"
    src.write_bytes(b"data")
    paths = [tmp_path / f"Test Game.replay{s}" for s in (0, 1, 2)]
    movie_io._stage_and_play(src, paths)
    for p in paths:
        assert p.exists()
    movie_io._cleanup_staged(paths)
    for p in paths:
        assert not p.exists()
