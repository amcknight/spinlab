"""SpinLab practice session orchestrator."""
from __future__ import annotations

import json
from typing import Optional


def _parse_attempt_result_from_buffer(buf: str) -> tuple[Optional[dict], str]:
    """Parse one attempt_result JSON event from the buffer.

    Returns (result_dict, remaining_buf) if found, or (None, buf) if not enough data.
    Discards non-JSON lines and JSON lines that aren't attempt_result events.
    """
    while "\n" in buf:
        line, buf = buf.split("\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if msg.get("event") == "attempt_result":
                return msg, buf
        except json.JSONDecodeError:
            pass  # discard plain-text responses like ok:queued, pong
    return None, buf
