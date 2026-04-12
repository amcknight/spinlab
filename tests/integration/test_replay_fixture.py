"""Full-stack replay fixture test: replay a recorded two-level run through
headless Mesen and verify the capture pipeline produces correct segments,
save states, and attempts.

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

# How long to wait for the replay to return to idle mode.
REPLAY_TIMEOUT_S = 120

# How often to poll the dashboard state endpoint while waiting for idle.
POLL_INTERVAL_S = 0.5


def _api(base_url: str, method: str, path: str, **kwargs):
    """Issue an HTTP request to the dashboard and return the response."""
    return getattr(requests, method)(base_url + path, timeout=5, **kwargs)


def _wait_for_idle(base_url: str, timeout: float = REPLAY_TIMEOUT_S) -> dict:
    """Poll /api/state until mode returns to idle or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = _api(base_url, "get", "/api/state")
        state = resp.json()
        if state["mode"] == "idle":
            return state
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(f"Replay did not finish within {timeout}s")


class TestReplayFixture:
    """Replay a two-level Love Yourself recording and verify capture output.

    A single test method triggers one replay and then asserts all expected
    properties in one pass.  This avoids state-accumulation issues that would
    arise if each assertion method triggered its own replay against the
    session-scoped dashboard.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, replay_dashboard):
        base_url, db, tmp_path = replay_dashboard
        self.base_url = base_url
        self.db = db
        self.tmp_path = tmp_path

        # Copy fixture files into the data dir where the replay API expects them.
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

    def test_replay_produces_segments_and_attempts(self):
        """One replay: verify exactly 4 segments were captured and attempts recorded.

        The fixture covers 2 levels, each split into 2 segments
        (entrance→checkpoint and checkpoint→exit), giving exactly 4 segments.
        Each completed segment boundary produces one attempt record.
        """
        resp = _api(self.base_url, "post", "/api/replay/start",
                    json={"ref_id": self.ref_id, "speed": 0})
        assert resp.status_code == 200, f"replay start failed: {resp.text}"

        final_state = _wait_for_idle(self.base_url)

        # --- Segment count ---
        resp = _api(self.base_url, "get", "/api/segments")
        assert resp.status_code == 200
        segments = resp.json()["segments"]
        assert len(segments) == 4, (
            f"Expected exactly 4 segments (2 levels × 2 segments each), "
            f"got {len(segments)}: "
            f"{[s.get('description', s.get('id', '?')) for s in segments]}"
        )

        # --- Attempt count ---
        # recent is populated from the DB via get_recent_attempts; at least one
        # attempt must appear after a completed replay.
        recent = final_state.get("recent", [])
        assert len(recent) > 0, (
            "Expected at least one attempt in recent after replay, got none"
        )
