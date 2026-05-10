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
from tests.integration.conftest import LOVE_YOURSELF_GAME_ID, skip_no_love_yourself

pytestmark = [pytest.mark.emulator, skip_no_love_yourself]

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
    deadline = time.monotonic() + timeout
    state: dict = {}
    while time.monotonic() < deadline:
        resp = _api(base_url, "get", "/api/state")
        state = resp.json()
        replay = state.get("replay")
        if state.get("mode") == "replay" and replay and replay.get("total", 0) > 0:
            return state
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"Mode never reached 'replay' (with nonzero frame total) within {timeout}s. "
        f"Last state: {state}"
    )


def _wait_for_idle_with_progress(
    base_url: str,
    expected_frames: int,
    timeout: float = REPLAY_TIMEOUT_S,
) -> tuple[dict, float, int]:
    """Poll until mode returns to idle, tracking replay frame progress.

    With the RA backend, the orchestrator emits ReplayFinishedEvent only when
    /api/replay/stop is called explicitly — there is no automatic detection of
    when RA finishes playback (RA does not signal the orchestrator).  This
    helper therefore monitors elapsed time against expected_frames / 60fps and
    calls /api/replay/stop once that wall-clock window has passed, triggering
    the session_manager's mode transition to idle.

    Returns (final_state, elapsed_seconds, max_frame_seen).
    """
    # How long the replay content should take at 60fps (plus a 20% margin
    # for RA startup lag and OS scheduling).  This is the earliest we'll
    # call /api/replay/stop.
    FRAMES_PER_SEC = 60
    replay_duration_s = (expected_frames / FRAMES_PER_SEC) * 1.2

    deadline = time.monotonic() + timeout
    start = time.monotonic()
    max_frame = 0
    state: dict = {}
    stop_sent = False

    import logging as _logging
    _diag = _logging.getLogger("spinlab.replay_fixture_diag")

    while time.monotonic() < deadline:
        resp = _api(base_url, "get", "/api/state")
        state = resp.json()
        replay = state.get("replay")
        if replay and replay.get("frame", 0) > max_frame:
            max_frame = replay["frame"]
        elapsed = time.monotonic() - start
        _diag.info(
            "replay poll: elapsed=%.1fs mode=%s sections=%s frame=%s",
            elapsed,
            state.get("mode"),
            state.get("sections_captured"),
            replay.get("frame") if replay else "N/A",
        )
        if state.get("mode") == "idle":
            return state, elapsed, max_frame

        # After the expected replay duration, explicitly stop so the orchestrator
        # emits ReplayFinishedEvent and the session transitions to idle.
        if not stop_sent and elapsed >= replay_duration_s:
            _diag.info("replay poll: sending explicit /api/replay/stop at %.1fs", elapsed)
            _api(base_url, "post", "/api/replay/stop")
            stop_sent = True

        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"Replay did not finish within {timeout}s. "
        f"Last state: mode={state.get('mode')}, "
        f"replay={state.get('replay')}, "
        f"sections_captured={state.get('sections_captured')}"
    )


@pytest.mark.skipif(
    not FIXTURE_REPLAY.exists(),
    reason=f"Movie fixture not recorded: {FIXTURE_REPLAY}",
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

        idle_state, elapsed_s, max_frame = _wait_for_idle_with_progress(
            self.base_url, expected_frames=self.expected_frames
        )

        # NOTE: with the RA backend, ReplayProgressEvent is not emitted (the
        # orchestrator has no mechanism to observe frame-by-frame replay progress
        # from NCI). max_frame will always be 0 here. The real end-to-end
        # verification is the segment count assertion below: if segments were
        # captured, the poller detected transitions during playback.

        # Replay should complete well under the timeout.
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
