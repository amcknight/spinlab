"""Structural-conformance meta-test for the EmuBackend Protocol.

Catches the case where production gains an EmuBackend method but
FakeEmuBackend doesn't — every consumer test would still pass while
production calls into the new method blow up at runtime.

A single `isinstance` against a @runtime_checkable Protocol is enough:
if FakeEmuBackend ever drops an attribute or method the orchestrator
requires, this test fails at the boundary, not in a downstream test.
"""
from tests.conftest import FakeEmuBackend

from spinlab.emu_backend import EmuBackend


def test_fake_emu_backend_satisfies_protocol():
    assert isinstance(FakeEmuBackend(), EmuBackend)
