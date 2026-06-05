"""Tests for TransitionState dataclass."""
from spinlab.retroarch.transition_state import TransitionState


def test_reset_clears_all_fields():
    """reset() method clears all fields to initial state."""
    s = TransitionState()
    s.died_flag = True
    s.cp_ordinal = 3
    s.first_room = 0x42
    s.last_event_key = "some_key"

    s.reset()

    assert s.died_flag is False
    assert s.cp_ordinal == 0
    assert s.first_room == 0
    assert s.last_event_key is None
