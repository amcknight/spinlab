"""Tests for AttemptRecord and ModelOutput dataclasses."""
from spinlab.models import AttemptRecord, ModelOutput


class TestAttemptRecord:
    def test_completed_attempt(self):
        ar = AttemptRecord(
            time_ms=12000, completed=True, deaths=2,
            clean_tail_ms=4500, created_at="2026-03-27T12:00:00",
        )
        assert ar.time_ms == 12000
        assert ar.completed is True
        assert ar.deaths == 2
        assert ar.clean_tail_ms == 4500

    def test_incomplete_attempt(self):
        ar = AttemptRecord(
            time_ms=None, completed=False, deaths=0,
            clean_tail_ms=None, created_at="2026-03-27T12:00:00",
        )
        assert ar.time_ms is None
        assert ar.completed is False
        assert ar.clean_tail_ms is None

    def test_zero_death_clean_tail_equals_time(self):
        ar = AttemptRecord(
            time_ms=8000, completed=True, deaths=0,
            clean_tail_ms=8000, created_at="2026-03-27T12:00:00",
        )
        assert ar.clean_tail_ms == ar.time_ms


class TestModelOutput:
    def test_round_trip_serialization(self):
        mo = ModelOutput(
            expected_time_ms=12000.0,
            clean_expected_ms=8000.0,
            ms_per_attempt=150.0,
            floor_estimate_ms=7000.0,
            clean_floor_estimate_ms=6000.0,
        )
        d = mo.to_dict()
        mo2 = ModelOutput.from_dict(d)
        assert mo2.expected_time_ms == 12000.0
        assert mo2.clean_expected_ms == 8000.0
        assert mo2.ms_per_attempt == 150.0
        assert mo2.floor_estimate_ms == 7000.0
        assert mo2.clean_floor_estimate_ms == 6000.0

    def test_all_five_fields_present(self):
        mo = ModelOutput(0.0, 0.0, 0.0, 0.0, 0.0)
        d = mo.to_dict()
        assert set(d.keys()) == {
            "expected_time_ms", "clean_expected_ms", "ms_per_attempt",
            "floor_estimate_ms", "clean_floor_estimate_ms",
        }
