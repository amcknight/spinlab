"""Tests for the test factories themselves."""


class TestMakeEventAttempt:
    def test_is_hot_defaults_to_false(self):
        from tests.factories import make_event_attempt
        ev = make_event_attempt()
        assert ev.is_hot is False

    def test_is_hot_can_be_set(self):
        from tests.factories import make_event_attempt
        ev = make_event_attempt(is_hot=True)
        assert ev.is_hot is True
