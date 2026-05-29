"""Regression test: two session-scoped RAHarness fixtures coexist.

Before per-harness NCI port allocation landed, instantiating two
session-scoped RAHarness fixtures in the same pytest session collided on
UDP 55355 — three transition tests would flake when ``-m emulator``
collected both harnesses' consumers (see
``project_emulator_fixture_port_conflict`` in agent memory). This test
forces the collision deliberately and asserts each harness's NCI is
independently reachable.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.emulator, pytest.mark.asyncio(loop_scope="session")]


async def test_two_harnesses_use_distinct_nci_ports(
    ra_harness_love_yourself, ra_harness_love_yourself_no_reset
):
    """Two distinct RA processes should be reachable on their own ports.

    This is the one place the suite deliberately runs 2 concurrent RA processes
    (the fresh-state and no-reset Love Yourself harnesses — distinct cache keys,
    distinct ports). The no-reset harness is function-scoped and released on
    teardown, so it doesn't linger into the transition phase.
    """
    assert ra_harness_love_yourself.client.port != ra_harness_love_yourself_no_reset.client.port, (
        f"both harnesses bound NCI to the same port ({ra_harness_love_yourself.client.port})"
    )
    # version() is the cheapest round-trip; success proves the NCI socket is
    # actually talking to RA, not just configured with a port number.
    assert ra_harness_love_yourself.client.version()
    assert ra_harness_love_yourself_no_reset.client.version()
