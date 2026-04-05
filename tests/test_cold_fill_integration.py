"""Integration test: full cold-fill cycle with real DB."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.db import Database
from spinlab.models import Mode, Segment, SegmentVariant, Status
from spinlab.session_manager import SessionManager

pytestmark = pytest.mark.skip(
    reason="Task 10: capture_controller.py still calls add_variant/get_variant (old segment-level "
           "API); needs rewrite to attach save states to waypoints before this test can run"
)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    d.upsert_game("g1", "Test Game", "any%")
    return d


@pytest.fixture
def tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    return tcp


@pytest.fixture
def sm(db, tcp):
    return SessionManager(db=db, tcp=tcp, rom_dir=None)


def _create_segments_with_hot_only(db):
    """Create 3 segments: entrance>cp1, cp1>cp2, cp2>goal. All hot only."""
    segs = [
        Segment(id="g1:105:entrance.0:checkpoint.1", game_id="g1",
                level_number=105, start_type="entrance", start_ordinal=0,
                end_type="checkpoint", end_ordinal=1, reference_id="run1"),
        Segment(id="g1:105:checkpoint.1:checkpoint.2", game_id="g1",
                level_number=105, start_type="checkpoint", start_ordinal=1,
                end_type="checkpoint", end_ordinal=2, reference_id="run1"),
        Segment(id="g1:105:checkpoint.2:goal.0", game_id="g1",
                level_number=105, start_type="checkpoint", start_ordinal=2,
                end_type="goal", end_ordinal=0, reference_id="run1"),
    ]
    for s in segs:
        db.upsert_segment(s)
    # Entrance segment gets cold by default (entrance state IS the cold state)
    db.add_variant(SegmentVariant(segs[0].id, "hot", "/hot0.mss", False))
    db.add_variant(SegmentVariant(segs[0].id, "cold", "/cold0.mss", True))
    # cp1 and cp2 segments only have hot
    db.add_variant(SegmentVariant(segs[1].id, "hot", "/hot1.mss", False))
    db.add_variant(SegmentVariant(segs[2].id, "hot", "/hot2.mss", False))
    return segs


class TestColdFillIntegration:
    async def test_full_cycle(self, sm, db, tcp):
        sm.game_id = "g1"

        # Set up and save draft — capture run must exist before segments (FK)
        db.create_capture_run("run1", "g1", "Test Run", draft=True)
        segs = _create_segments_with_hot_only(db)
        sm.capture.draft.enter_draft("run1", 3)
        result = await sm.save_draft("Test Run")

        assert result.status == Status.OK
        assert sm.mode == Mode.COLD_FILL

        # Verify first segment loaded
        sent = json.loads(tcp.send.call_args[0][0])
        assert sent["event"] == "cold_fill_load"
        assert sent["segment_id"] == segs[1].id

        # Simulate spawn for first segment
        await sm.route_event({
            "event": "spawn",
            "state_captured": True,
            "state_path": "/cold1.mss",
        })
        assert sm.mode == Mode.COLD_FILL  # still filling

        # Verify cold variant stored
        v = db.get_variant(segs[1].id, "cold")
        assert v is not None
        assert v.state_path == "/cold1.mss"
        assert v.is_default is True

        # Simulate spawn for second segment
        await sm.route_event({
            "event": "spawn",
            "state_captured": True,
            "state_path": "/cold2.mss",
        })
        assert sm.mode == Mode.IDLE  # done

        # Verify both cold variants exist
        v2 = db.get_variant(segs[2].id, "cold")
        assert v2 is not None
        assert v2.state_path == "/cold2.mss"

        # Verify no more gaps
        assert db.segments_missing_cold("g1") == []
