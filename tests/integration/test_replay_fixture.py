"""Full-stack replay fixture test: replay a recorded two-level run through
headless Mesen and verify the capture pipeline produces correct segments
and save states.

Requires: Mesen2 + Love Yourself ROM (see conftest.py replay fixtures).
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
import requests

from tests.integration.conftest import LOVE_YOURSELF_GAME_ID, skip_no_love_yourself

pytestmark = [pytest.mark.emulator, skip_no_love_yourself]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "love_yourself"

# 6255-frame fixture completes in ~16s headless.  60s is generous enough
# to catch desync or pipe-deadlock regressions without masking them.
REPLAY_TIMEOUT_S = 60

POLL_INTERVAL_S = 0.5

# Expected frame count in the two_level fixture.
EXPECTED_FRAME_COUNT = 6255


def _api(base_url: str, method: str, path: str, **kwargs):
    return getattr(requests, method)(base_url + path, timeout=5, **kwargs)


def _wait_for_replay_mode(base_url: str, timeout: float = 10.0) -> dict:
    """Wait until mode is 'replay' AND replay_started has set frame total."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = _api(base_url, "get", "/api/state")
        state = resp.json()
        replay = state.get("replay")
        if state["mode"] == "replay" and replay and replay.get("total", 0) > 0:
            return state
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"Mode never reached 'replay' (with frame total) within {timeout}s. "
        f"Last state: {state}"
    )


def _wait_for_idle_with_progress(
    base_url: str, timeout: float = REPLAY_TIMEOUT_S,
) -> tuple[dict, float, int]:
    """Poll until mode returns to idle, tracking replay frame progress.

    Returns (final_state, elapsed_seconds, max_frame_seen).
    """
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    max_frame = 0
    while time.monotonic() < deadline:
        resp = _api(base_url, "get", "/api/state")
        state = resp.json()
        replay = state.get("replay")
        if replay and replay.get("frame", 0) > max_frame:
            max_frame = replay["frame"]
        if state["mode"] == "idle":
            return state, time.monotonic() - start, max_frame
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"Replay did not finish within {timeout}s. "
        f"Last state: mode={state.get('mode')}, "
        f"replay={state.get('replay')}, "
        f"sections_captured={state.get('sections_captured')}"
    )


class TestReplayFixture:
    """Replay a two-level Love Yourself recording and verify capture output.

    A single test method triggers one replay and then asserts all expected
    properties in one pass.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, replay_dashboard):
        base_url, db, tmp_path = replay_dashboard
        self.base_url = base_url
        self.db = db
        self.tmp_path = tmp_path

        game_rec_dir = tmp_path / LOVE_YOURSELF_GAME_ID / "rec"
        game_rec_dir.mkdir(parents=True, exist_ok=True)
        self.ref_id = "fixture_two_level"
        shutil.copy2(
            FIXTURE_DIR / "two_level.spinrec",
            game_rec_dir / f"{self.ref_id}.spinrec",
        )
        shutil.copy2(
            FIXTURE_DIR / "two_level.mss",
            game_rec_dir / f"{self.ref_id}.mss",
        )

    def test_replay_produces_segments(self):
        """Replay the two-level fixture and verify the capture pipeline.

        The fixture covers 2 levels, each split into 2 segments
        (entrance->checkpoint and checkpoint->exit), giving exactly 4 segments.
        """
        state = _api(self.base_url, "get", "/api/state").json()
        assert state["game_id"] == LOVE_YOURSELF_GAME_ID, (
            f"Game ID mismatch: expected {LOVE_YOURSELF_GAME_ID}, got {state['game_id']}"
        )

        resp = _api(self.base_url, "post", "/api/replay/start",
                    json={"ref_id": self.ref_id, "speed": 0})
        assert resp.status_code == 200, f"replay start failed: {resp.text}"

        replay_state = _wait_for_replay_mode(self.base_url)

        # Replay state should include frame progress from state API
        replay = replay_state.get("replay")
        assert replay is not None, "State missing 'replay' dict in replay mode"
        assert replay.get("total") == EXPECTED_FRAME_COUNT, (
            f"Expected replay total={EXPECTED_FRAME_COUNT}, got {replay.get('total')}"
        )

        idle_state, elapsed_s, max_frame = _wait_for_idle_with_progress(self.base_url)

        # Frames should have advanced (replay_progress events reached state API)
        assert max_frame > 0, (
            "No replay frame progress observed — replay may have been frozen"
        )

        # Replay should complete well under the timeout at uncapped speed
        assert elapsed_s < REPLAY_TIMEOUT_S, (
            f"Replay took {elapsed_s:.1f}s — expected under {REPLAY_TIMEOUT_S}s"
        )

        # Save the draft (replay produces a draft capture run)
        resp = _api(self.base_url, "post", "/api/references/draft/save",
                    json={"name": "Replay fixture test"})
        assert resp.status_code == 200, f"draft save failed: {resp.text}"

        # Exactly 1 reference after saving the draft
        refs = _api(self.base_url, "get", "/api/references").json()["references"]
        assert len(refs) == 1, (
            f"Expected exactly 1 reference after replay, got {len(refs)}"
        )

        # Exactly 4 segments: 2 levels x 2 segments each
        resp = _api(self.base_url, "get", "/api/segments")
        assert resp.status_code == 200
        segments = resp.json()["segments"]
        assert len(segments) == 4, (
            f"Expected exactly 4 segments (2 levels x 2 segments each), "
            f"got {len(segments)}: "
            f"{[s.get('description', s.get('id', '?')) for s in segments]}"
        )

        # Verify segment structure: each level has entrance->checkpoint and checkpoint->goal
        by_level: dict[int, list] = {}
        for seg in segments:
            lvl = seg["level_number"]
            by_level.setdefault(lvl, []).append(seg)

        assert len(by_level) == 2, f"Expected 2 levels, got {len(by_level)}: {list(by_level.keys())}"

        for lvl, segs in by_level.items():
            types = [(s["start_type"], s["end_type"]) for s in segs]
            assert ("entrance", "checkpoint") in types, (
                f"Level {lvl} missing entrance->checkpoint segment"
            )
            assert ("checkpoint", "goal") in types, (
                f"Level {lvl} missing checkpoint->goal segment"
            )
