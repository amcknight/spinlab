"""Playwright smoke: dashboard crash + restart + resume preserves segments.

This is the high-visibility one-shot UI assertion for the multi-session
reference run feature. The deep data-layer assertions live in
test_crash_recovery.py (Python-only, no UI).

The full implementation requires:
- Built frontend assets (`cd frontend && npm run build`)
- A FakeTcpManager-driven dashboard fixture (see test_frontend_smoke.py
  for the pattern)
- Process tear-down + relaunch against the same DB file
- Playwright UI navigation and assertion

Currently scaffolded as a skipped test until the fixture chain is built.
The Python integration test in test_crash_recovery.py covers the
data-layer correctness exhaustively.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.frontend


@pytest.mark.asyncio
async def test_resume_after_dashboard_restart(tmp_path):
    """Paused run survives dashboard restart and shows in the UI.

    Steps the implementation will need:
    1. Start dashboard #1 against tmp_path DB. Use Playwright to navigate to /.
    2. Drive a reference start via FakeTcp; fabricate one segment-close event.
    3. Tear down dashboard #1 without graceful shutdown.
    4. Start dashboard #2 against the same DB.
    5. Navigate to /. Assert the paused-run card is visible with
       "1 segments captured across 1 sessions".
    6. Click Resume. Assert mode transitions to recording.
    """
    pytest.skip(
        "Implementation requires extending the existing smoke fixture for "
        "process restart; see test_crash_recovery.py for data-layer assertions"
    )
