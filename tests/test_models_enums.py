"""Tests for StrEnum types in models.py."""

import pytest

from spinlab.models import ActionResult, AttemptSource, EndpointType, EventType, Mode, Status


class TestEndpointType:
    def test_values(self):
        assert EndpointType.ENTRANCE == "entrance"
        assert EndpointType.CHECKPOINT == "checkpoint"
        assert EndpointType.GOAL == "goal"

    def test_from_string(self):
        assert EndpointType("entrance") is EndpointType.ENTRANCE
        assert EndpointType("checkpoint") is EndpointType.CHECKPOINT
        assert EndpointType("goal") is EndpointType.GOAL

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            EndpointType("bogus")


class TestEventType:
    def test_tcp_events_present(self):
        assert EventType.ROM_INFO == "rom_info"
        assert EventType.DEATH == "death"
        assert EventType.ATTEMPT_RESULT == "attempt_result"
        assert EventType.REPLAY_FINISHED == "replay_finished"

    def test_all_tcp_events(self):
        expected = {
            "rom_info",
            "game_context",
            "level_entrance",
            "checkpoint",
            "death",
            "spawn",
            "level_exit",
            "attempt_result",
            "rec_saved",
            "replay_started",
            "replay_progress",
            "replay_finished",
            "replay_error",
            "attempt_invalidated",
        }
        actual = {e.value for e in EventType}
        assert expected == actual

    def test_from_string(self):
        assert EventType("rom_info") is EventType.ROM_INFO
        assert EventType("death") is EventType.DEATH
        assert EventType("attempt_result") is EventType.ATTEMPT_RESULT
        assert EventType("replay_finished") is EventType.REPLAY_FINISHED

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            EventType("bogus")


class TestStatus:
    def test_success_statuses(self):
        assert Status.OK == "ok"
        assert Status.STARTED == "started"
        assert Status.STOPPED == "stopped"

    def test_error_statuses(self):
        assert Status.NOT_CONNECTED == "not_connected"
        assert Status.DRAFT_PENDING == "draft_pending"

    def test_all_statuses_present(self):
        expected = {
            "ok",
            "started",
            "stopped",
            "not_connected",
            "draft_pending",
            "practice_active",
            "reference_active",
            "already_running",
            "already_replaying",
            "not_in_reference",
            "not_replaying",
            "not_running",
            "no_draft",
            "no_hot_variant",
            "no_gaps",
            "shutting_down",
        }
        actual = {s.value for s in Status}
        assert expected == actual

    def test_from_string(self):
        assert Status("ok") is Status.OK
        assert Status("not_connected") is Status.NOT_CONNECTED

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            Status("bogus")


class TestAttemptSource:
    def test_values(self):
        assert AttemptSource.PRACTICE == "practice"
        assert AttemptSource.REPLAY == "replay"
        assert AttemptSource.REFERENCE == "reference"

    def test_from_string(self):
        assert AttemptSource("practice") is AttemptSource.PRACTICE
        assert AttemptSource("replay") is AttemptSource.REPLAY
        assert AttemptSource("reference") is AttemptSource.REFERENCE

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            AttemptSource("bogus")


class TestActionResult:
    def test_to_response_basic(self):
        r = ActionResult(status=Status.OK)
        assert r.to_response() == {"status": "ok"}

    def test_to_response_with_session_id(self):
        r = ActionResult(status=Status.STARTED, session_id="abc123")
        assert r.to_response() == {"status": "started", "session_id": "abc123"}

    def test_to_response_strips_new_mode(self):
        r = ActionResult(status=Status.STARTED, new_mode=Mode.REFERENCE)
        resp = r.to_response()
        assert "new_mode" not in resp
        assert resp == {"status": "started"}

    def test_new_mode_accessible(self):
        r = ActionResult(status=Status.OK, new_mode=Mode.PRACTICE)
        assert r.new_mode == Mode.PRACTICE
