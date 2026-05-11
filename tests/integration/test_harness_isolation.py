"""Regression test: two session-scoped RAHarness fixtures coexist.

Before per-harness NCI port allocation landed, instantiating both
``ra_harness`` and ``ra_harness_love_yourself`` in the same pytest session
collided on UDP 55355 — three transition tests would flake when ``-m
emulator`` collected both harnesses' consumers (see
``project_emulator_fixture_port_conflict`` in agent memory). This test forces
the collision deliberately and asserts each harness's NCI is independently
reachable.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.emulator, pytest.mark.asyncio(loop_scope="session")]


async def test_two_harnesses_use_distinct_nci_ports(ra_harness, ra_harness_love_yourself):
    """Both harnesses should be reachable on their own ports."""
    assert ra_harness.client.port != ra_harness_love_yourself.client.port, (
        f"both harnesses bound NCI to the same port ({ra_harness.client.port})"
    )
    # version() is the cheapest round-trip; success proves the NCI socket is
    # actually talking to RA, not just configured with a port number.
    assert ra_harness.client.version()
    assert ra_harness_love_yourself.client.version()
