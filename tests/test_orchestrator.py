import json
import pytest
from spinlab.orchestrator import _parse_attempt_result_from_buffer


GOOD_RESULT = {
    "event": "attempt_result",
    "split_id": "smw_cod:5:0:normal",
    "completed": True,
    "time_ms": 11234,
    "goal": "normal",
    "rating": "good",
}


def test_parses_complete_line():
    buf = json.dumps(GOOD_RESULT) + "\n"
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result == GOOD_RESULT
    assert remaining == ""


def test_returns_none_for_incomplete_line():
    buf = json.dumps(GOOD_RESULT)  # no newline
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result is None
    assert remaining == buf


def test_discards_non_attempt_result_lines():
    buf = "ok:queued\npong\n" + json.dumps(GOOD_RESULT) + "\n"
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result == GOOD_RESULT
    assert remaining == ""


def test_discards_malformed_json():
    buf = "this is not json\n" + json.dumps(GOOD_RESULT) + "\n"
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result == GOOD_RESULT
    assert remaining == ""


def test_discards_json_without_attempt_result_event():
    other = json.dumps({"event": "something_else", "data": 1}) + "\n"
    buf = other + json.dumps(GOOD_RESULT) + "\n"
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result == GOOD_RESULT


def test_leaves_partial_second_message_in_buffer():
    partial = '{"event": "attempt_result"'  # incomplete second message
    buf = json.dumps(GOOD_RESULT) + "\n" + partial
    result, remaining = _parse_attempt_result_from_buffer(buf)
    assert result == GOOD_RESULT
    assert remaining == partial
