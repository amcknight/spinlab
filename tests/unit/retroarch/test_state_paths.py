"""Pure path-helper tests — no I/O, no fixtures needed."""
import pytest

from spinlab.retroarch.state_paths import (
    ra_slot_filename,
    segment_state_filename,
)


def test_segment_state_filename_basic():
    assert segment_state_filename("seg-abc123") == "seg-abc123.state"


def test_segment_state_filename_sanitizes_path_separators():
    """segment_id may contain colons / slashes (e.g. game:level:cp1). Replace those."""
    assert segment_state_filename("game:5:cp1") == "game_5_cp1.state"
    assert segment_state_filename("foo/bar") == "foo_bar.state"


def test_segment_state_filename_backslash_sanitized():
    """Windows path separators also get replaced."""
    assert segment_state_filename("foo\\bar") == "foo_bar.state"


def test_segment_state_filename_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        segment_state_filename("")


def test_ra_slot_filename_basic():
    """Mirrors RA's <game_basename>.state<slot> convention."""
    assert ra_slot_filename("Toothpaste World", 9999) == "Toothpaste World.state9999"
    assert ra_slot_filename("game", 0) == "game.state0"


def test_ra_slot_filename_zero_slot_no_suffix_number():
    """RA's auto-index slot 0 still uses .state0 over NCI's LOAD_STATE_SLOT."""
    assert ra_slot_filename("g", 0) == "g.state0"
