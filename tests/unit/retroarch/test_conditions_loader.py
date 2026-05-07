"""Tests for conditions_loader.apply_definitions — list[dict] -> ConditionRegistry."""
import pytest

from spinlab.retroarch.conditions import ConditionRegistry, ConditionSpec
from spinlab.retroarch.conditions_loader import apply_definitions


class _FakeNCI:
    def __init__(self, fixed: dict[int, bytes]) -> None:
        self._fixed = fixed

    def read_ram(self, addr: int, size: int) -> bytes:
        return self._fixed[addr][:size]


def test_apply_definitions_basic():
    reg = ConditionRegistry()
    apply_definitions(reg, [
        {"name": "powerup", "address": 0x19, "size": 1},
        {"name": "lives", "address": 0xDBE, "size": 2},
    ])
    nci = _FakeNCI({0x19: b"\x02", 0xDBE: b"\x05\x00"})
    out = reg.read_all(nci)
    assert out == {"powerup": 2, "lives": 5}


def test_apply_definitions_empty_clears_registry():
    reg = ConditionRegistry()
    apply_definitions(reg, [{"name": "x", "address": 0x100, "size": 1}])
    apply_definitions(reg, [])  # empty list clears
    nci = _FakeNCI({})
    assert reg.read_all(nci) == {}


def test_apply_definitions_unsupported_size_raises():
    reg = ConditionRegistry()
    with pytest.raises(ValueError, match="size"):
        apply_definitions(reg, [{"name": "bad", "address": 0x100, "size": 4}])


def test_apply_definitions_missing_keys_raises_keyerror():
    """Defensive: malformed defs surface as KeyError so the orchestrator can log/handle."""
    reg = ConditionRegistry()
    with pytest.raises(KeyError):
        apply_definitions(reg, [{"name": "a"}])  # no address, no size
