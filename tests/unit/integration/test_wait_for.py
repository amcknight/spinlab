"""Tests for the typed wait_for polling helper used by integration fixtures."""
from __future__ import annotations

from tests.integration._wait_for import WaitOutcome, wait_for


def test_wait_for_returns_succeeded_outcome_when_predicate_true_immediately():
    state = {"ready": True}
    outcome = wait_for(
        name="ready_flag",
        fetch=lambda: state,
        predicate=lambda s: (s["ready"], ""),
        timeout_s=1.0,
        interval_s=0.05,
    )
    assert outcome.succeeded is True
    assert outcome.name == "ready_flag"
    assert outcome.attempts == 1
    assert outcome.last_reason == ""


def test_wait_for_returns_timeout_outcome_when_predicate_never_true():
    outcome = wait_for(
        name="never_ready",
        fetch=lambda: {"ready": False},
        predicate=lambda s: (s["ready"], f"ready={s['ready']}"),
        timeout_s=0.2,
        interval_s=0.05,
    )
    assert outcome.succeeded is False
    assert outcome.name == "never_ready"
    assert outcome.attempts >= 1
    assert outcome.last_reason == "ready=False"
    assert outcome.elapsed_s >= 0.2


def test_wait_for_eventually_succeeds():
    counter = {"n": 0}

    def fetch():
        counter["n"] += 1
        return counter["n"]

    outcome = wait_for(
        name="counter_reaches_3",
        fetch=fetch,
        predicate=lambda n: (n >= 3, f"n={n}"),
        timeout_s=1.0,
        interval_s=0.01,
    )
    assert outcome.succeeded is True
    assert counter["n"] >= 3
    assert outcome.last_reason == ""


def test_wait_for_records_fetch_exception_as_last_reason():
    def fetch():
        raise RuntimeError("fetch boom")

    outcome = wait_for(
        name="boomy",
        fetch=fetch,
        predicate=lambda _v: (True, ""),  # never reached
        timeout_s=0.15,
        interval_s=0.05,
    )
    assert outcome.succeeded is False
    assert "fetch boom" in outcome.last_reason
    assert "RuntimeError" in outcome.last_reason


def test_wait_outcome_format_message_includes_name_elapsed_attempts_reason():
    outcome = WaitOutcome(
        succeeded=False,
        name="orchestrator_ready",
        elapsed_s=2.5,
        attempts=10,
        last_reason="emu_connected=False game_id=None",
    )
    msg = outcome.format_message()
    assert "orchestrator_ready" in msg
    assert "2.5" in msg
    assert "10" in msg
    assert "emu_connected=False game_id=None" in msg


def test_wait_for_succeeded_outcome_format_message_is_concise():
    outcome = WaitOutcome(
        succeeded=True, name="ok", elapsed_s=0.1, attempts=1, last_reason="",
    )
    msg = outcome.format_message()
    assert "ok" in msg
    assert "succeeded" in msg.lower()
