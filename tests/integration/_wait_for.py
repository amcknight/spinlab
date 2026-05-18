"""Typed polling helper used by integration fixtures and test bodies.

Wraps the "poll a fetch() until predicate(value) is True, or time out" loop
in a single function so every call site reports a structured outcome that
names the operation, the elapsed time, the attempt count, and why the
predicate was last unsatisfied. The old ad-hoc helpers (e.g. the original
`_wait_for_dashboard_state`) returned only `last_error` and lost the name
of what was being waited on.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class WaitOutcome:
    """Result of a `wait_for` call. `succeeded=True` means the predicate
    returned `(True, ...)` before the deadline.
    """

    succeeded: bool
    name: str
    elapsed_s: float
    attempts: int
    last_reason: str  # empty string on success

    def format_message(self) -> str:
        if self.succeeded:
            return (
                f"wait_for({self.name}) succeeded after "
                f"{self.attempts} attempt(s) in {self.elapsed_s:.2f}s"
            )
        return (
            f"wait_for({self.name}) timed out after "
            f"{self.attempts} attempt(s) in {self.elapsed_s:.2f}s; "
            f"last reason: {self.last_reason}"
        )


def wait_for(
    *,
    name: str,
    fetch: Callable[[], T],
    predicate: Callable[[T], tuple[bool, str]],
    timeout_s: float = 10.0,
    interval_s: float = 0.25,
) -> WaitOutcome:
    """Poll `fetch()` until `predicate(value)` returns `(True, _)` or `timeout_s` elapses.

    `predicate` returns `(ok, reason)`. When `ok=False`, `reason` describes
    why the predicate was unsatisfied so the timeout message can be specific
    instead of "Last state: <dump>". `reason` is ignored when `ok=True`.

    If `fetch()` raises, the exception's `f"{type(exc).__name__}: {exc}"` becomes
    the next `last_reason` and polling continues until the deadline.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    attempts = 0
    last_reason = ""
    while True:
        attempts += 1
        try:
            value = fetch()
            ok, reason = predicate(value)
            if ok:
                return WaitOutcome(
                    succeeded=True, name=name,
                    elapsed_s=time.monotonic() - start,
                    attempts=attempts, last_reason="",
                )
            last_reason = reason
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            return WaitOutcome(
                succeeded=False, name=name,
                elapsed_s=time.monotonic() - start,
                attempts=attempts, last_reason=last_reason,
            )
        time.sleep(interval_s)
