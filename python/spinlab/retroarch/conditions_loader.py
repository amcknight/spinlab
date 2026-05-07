"""Loader: list[dict] -> ConditionRegistry. Tiny adapter for SetConditionsCmd.definitions."""
from __future__ import annotations

from spinlab.retroarch.conditions import ConditionRegistry, ConditionSpec


def apply_definitions(registry: ConditionRegistry, definitions: list[dict]) -> None:
    """Replace registry contents with specs converted from definition dicts.

    Each dict must have keys: 'name' (str), 'address' (int), 'size' (int).
    Missing keys raise KeyError; unsupported sizes raise ValueError (from registry.set).
    """
    specs = [
        ConditionSpec(name=d["name"], address=d["address"], size=d["size"])
        for d in definitions
    ]
    registry.set(specs)
