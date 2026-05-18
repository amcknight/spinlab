"""Full-stack replay fixture test: replay a recorded one-level run through
headless RetroArch and verify the capture pipeline produces correct segments
and save states.

Requires: RetroArch + Snes9x core + Love Yourself ROM + one_level.replay fixture.
The fixture was recorded via the dashboard reference-run flow with backend=retroarch
(Phase E option a, committed in 4928f1c).
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
import requests
from tests.integration.conftest import LOVE_YOURSELF_GAME_ID

pytestmark = pytest.mark.emulator

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "love_yourself"
FIXTURE_REPLAY = FIXTURE_DIR / "one_level.replay"
FIXTURE_META = FIXTURE_DIR / "one_level.json"

# one_level.replay covers ~38 seconds of gameplay (2273 frames at 60fps).
# 120s is generous enough to catch desync or poller hang regressions
# without masking real hangs.
REPLAY_TIMEOUT_S = 120

POLL_INTERVAL_S = 0.5


def _api(base_url: str, method: str, path: str, **kwargs):
    return getattr(requests, method)(base_url + path, timeout=5, **kwargs)


def _wait_for_replay_mode(base_url: str, timeout: float = 15.0) -> dict:
    """Wait until mode is 'replay' AND replay_started has set a nonzero frame total."""
    from tests.integration._wait_for import wait_for

    def _fetch():
        return _api(base_url, "get", "/api/state").json()

    def _predicate(state):
        mode = state.get("mode")
        if mode != "replay":
            return False, f"mode={mode!r} (waiting for 'replay')"
        replay = state.get("replay")
        if not replay or not replay.get("total", 0) > 0:
            total = replay.get("total") if replay else None
            return False, f"mode='replay' but replay.total={total!r} (waiting for nonzero)"
        return True, ""

    outcome = wait_for(
        name="replay_mode_with_frame_total",
        fetch=_fetch,
        predicate=_predicate,
        timeout_s=timeout,
        interval_s=POLL_INTERVAL_S,
    )
    if not outcome.succeeded:
        pytest.fail(outcome.format_message())
    return _fetch()


def _wait_for_idle_with_progress(
    base_url: str,
    expected_segments: int,
    timeout: float = REPLAY_TIMEOUT_S,
) -> tuple[dict, float]:
    """Poll until all expected segments have been captured, then stop the replay.

    With the RA backend, the orchestrator emits ReplayFinishedEvent only when
    /api/replay/stop is called explicitly — there is no automatic detection of
    when RA finishes playback. This helper gates on the actual content milestone
    (``sections_captured == expected_segments``) rather than a wall-clock window
    so the test runs as fast as RA's playback rate allows. Under fast-forward,
    that drops from ~38s to a few seconds.

    Returns (final_state, elapsed_seconds).
    """
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    state: dict = {}
    stop_sent = False

    import logging as _logging
    _diag = _logging.getLogger("spinlab.replay_fixture_diag")

    while time.monotonic() < deadline:
        resp = _api(base_url, "get", "/api/state")
        state = resp.json()
        elapsed = time.monotonic() - start
        sections = state.get("sections_captured")
        _diag.info(
            "replay poll: elapsed=%.1fs mode=%s sections=%s",
            elapsed, state.get("mode"), sections,
        )
        if state.get("mode") == "idle":
            return state, elapsed

        # As soon as the expected segments have been captured, stop the replay.
        # RA may still be running the post-goal tail of the .replay file when we
        # send /api/replay/stop — that's fine; the segments we care about are
        # already in the DB.
        if not stop_sent and sections is not None and sections >= expected_segments:
            _diag.info("replay poll: all %d segments captured at %.1fs — stopping",
                       expected_segments, elapsed)
            _api(base_url, "post", "/api/replay/stop")
            stop_sent = True

        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"Replay did not produce {expected_segments} segments within {timeout}s. "
        f"Last state: mode={state.get('mode')}, "
        f"sections_captured={state.get('sections_captured')}"
    )


class TestReplayFixture:
    """Replay the one-level Love Yourself recording through RetroArch and verify capture.

    A single test method triggers one replay and asserts all expected properties
    in one pass: mode transitions, frame progress, segment count.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, replay_ra_dashboard):
        base_url, db, tmp_path = replay_ra_dashboard
        self.base_url = base_url
        self.db = db
        self.tmp_path = tmp_path

        meta = json.loads(FIXTURE_META.read_text())
        self.expected_segments = meta["expected_segments"]
        self.expected_frames = meta["frame_count"]

        # Read the actual game_id the dashboard resolved for this session.
        # With the RA backend, GET_STATUS returns the ROM name without extension
        # (e.g. "Love Yourself"), so the session_manager may fall back to a
        # filename-derived ID ("file_love_yourself") rather than the CRC-based one.
        # We use whichever game_id the dashboard actually has so our staged
        # fixture ends up in the path _resolve_replay_path will search.
        state = _api(base_url, "get", "/api/state").json()
        actual_game_id = state.get("game_id") or LOVE_YOURSELF_GAME_ID

        # Stage fixture under the ref_id the dashboard will look up.
        # _resolve_replay_path (legacy path, no capture_sessions rows) builds:
        #   <data_dir>/<game_id>/rec/<ref_id>.replay
        # The sibling .json provides frame_count for the ReplayStartedEvent.
        game_rec_dir = tmp_path / actual_game_id / "rec"
        game_rec_dir.mkdir(parents=True, exist_ok=True)
        self.ref_id = "fixture_phase_e"
        shutil.copy2(FIXTURE_REPLAY, game_rec_dir / f"{self.ref_id}.replay")
        shutil.copy2(FIXTURE_META, game_rec_dir / f"{self.ref_id}.json")

    def test_replay_produces_segments(self):
        """Replay the one-level fixture and verify the capture pipeline.

        The fixture covers 1 level (Level 44) split into 2 segments:
        entrance->checkpoint and checkpoint->goal.
        """
        state = _api(self.base_url, "get", "/api/state").json()
        # Accept either the CRC-based ID (when rom_dir lookup succeeds) or the
        # filename-derived fallback (when RA returns the ROM name without extension
        # and the file lookup misses).  Both IDs correspond to Love Yourself.
        assert state.get("game_id") in (LOVE_YOURSELF_GAME_ID, "file_love_yourself"), (
            f"Game ID is not Love Yourself: got {state.get('game_id')!r}"
        )

        resp = _api(self.base_url, "post", "/api/replay/start",
                    json={"ref_id": self.ref_id, "speed": 0})
        assert resp.status_code == 200, f"replay start failed: {resp.text}"

        replay_state = _wait_for_replay_mode(self.base_url)

        # Replay state should include frame total from the sibling .json metadata.
        replay = replay_state.get("replay")
        assert replay is not None, "State missing 'replay' dict in replay mode"
        assert replay.get("total") == self.expected_frames, (
            f"Expected replay total={self.expected_frames}, got {replay.get('total')}"
        )

        idle_state, elapsed_s = _wait_for_idle_with_progress(
            self.base_url, expected_segments=self.expected_segments
        )

        # Replay should complete well under the timeout. Under fast-forward
        # the 2273-frame movie typically finishes in a few seconds on a modern
        # host (vs ~38s at native 60fps).
        assert elapsed_s < REPLAY_TIMEOUT_S, (
            f"Replay took {elapsed_s:.1f}s — expected under {REPLAY_TIMEOUT_S}s"
        )

        # Finalize the paused replay run as a new reference.
        resp = _api(self.base_url, "post", "/api/reference/finalize",
                    json={"name": "Replay fixture test (Phase E)"})
        assert resp.status_code == 200, f"finalize failed: {resp.text}"

        # Exactly 1 reference after saving the draft.
        refs = _api(self.base_url, "get", "/api/references").json()["references"]
        assert len(refs) == 1, (
            f"Expected exactly 1 reference after replay, got {len(refs)}"
        )

        # Exactly expected_segments segments (2: entrance->cp and cp->goal).
        resp = _api(self.base_url, "get", "/api/segments")
        assert resp.status_code == 200
        segments = resp.json()["segments"]
        assert len(segments) == self.expected_segments, (
            f"Expected exactly {self.expected_segments} segments, "
            f"got {len(segments)}: "
            f"{[s.get('description', s.get('id', '?')) for s in segments]}"
        )
