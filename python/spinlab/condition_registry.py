"""Loads per-game condition definitions from YAML; decodes raw values.

Used in two roles, both backed by the same registry instance:
  - Capture-side decoding: ``decode(raw, level)`` turns raw memory bytes into
    typed conditions ('powerup' -> 'cape') for the segment recorder. Requires
    full schema (type/values/scope) loaded from per-game YAML.
  - RA backend reading: ``read_all(client)`` issues NCI READ_CORE_RAM for each
    definition and returns ``{name: int_value}``. Only needs name/address/size.
    The orchestrator builds a "read-only" registry from ``SetConditionsCmd``
    payload via ``replace_with_read_specs``; type/values/scope stay defaulted
    because the read path doesn't touch them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

import yaml

# Default death penalty: time added per death to account for death animation
# + respawn in a standard SMW retry (~3.2 s measured from SMW NTSC timing).
DEFAULT_DEATH_PENALTY_MS: int = 3200

# Sizes supported by ``read_all``: byte (size=1) or little-endian word (size=2).
# Larger sizes are rejected because the value-construction logic is byte-by-byte
# and would need a clearer endianness contract before extending. Matches what
# kaizosplits uses for SMW conditions.
SUPPORTED_READ_SIZES = (1, 2)


class _RamReader(Protocol):
    """Duck-typed surface ``read_all`` requires from its client argument.

    Parameter names match ``NCIClient.read_ram`` (``addr``, ``length``) so
    pyright accepts the concrete client at call sites without a cast.
    """

    def read_ram(self, addr: int, length: int) -> bytes: ...

@dataclass(frozen=True)
class Scope:
    """Scope of a condition: entire game, or specific levels only."""
    is_game_scope: bool
    levels: tuple[int, ...] = ()

    @classmethod
    def game(cls) -> "Scope":
        return cls(is_game_scope=True)

    @classmethod
    def levels_of(cls, levels: Iterable[int]) -> "Scope":
        return cls(is_game_scope=False, levels=tuple(levels))

    # Alias used by tests for readability.
    @classmethod
    def for_levels(cls, levels_: Iterable[int]) -> "Scope":
        return cls.levels_of(levels_)

    def covers(self, level: int) -> bool:
        return self.is_game_scope or level in self.levels


@dataclass(frozen=True)
class ConditionDef:
    """A single condition definition.

    For capture-side decoding all fields must be populated (from YAML). For
    the RA-backend read path only name/address/size are consulted; the other
    fields take harmless defaults so a registry built from ``SetConditionsCmd``
    payload is still usable for ``read_all``.
    """
    name: str
    address: int
    size: int
    type: str = ""                              # 'enum' or 'bool'; '' for read-only defs
    values: dict[int, str] | None = None
    scope: Scope = field(default_factory=Scope.game)


@dataclass
class ConditionRegistry:
    definitions: list[ConditionDef] = field(default_factory=list)
    death_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS

    @classmethod
    def from_yaml(cls, path: Path) -> "ConditionRegistry":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        defs: list[ConditionDef] = []
        for c in raw.get("conditions", []):
            scope_raw = c["scope"]
            if scope_raw == "game":
                scope = Scope.game()
            elif isinstance(scope_raw, dict) and "levels" in scope_raw:
                scope = Scope.levels_of(scope_raw["levels"])
            else:
                raise ValueError(f"unknown scope: {scope_raw!r}")
            defs.append(ConditionDef(
                name=c["name"],
                address=int(c["address"]),
                size=int(c["size"]),
                type=c["type"],
                values=({int(k): str(v) for k, v in c["values"].items()}
                        if c.get("values") else None),
                scope=scope,
            ))
        return cls(
            definitions=defs,
            death_penalty_ms=raw.get("death_penalty_ms", DEFAULT_DEATH_PENALTY_MS),
        )

    def in_scope(self, level: int) -> list[ConditionDef]:
        return [d for d in self.definitions if d.scope.covers(level)]

    def replace_with_read_specs(self, specs: list[dict]) -> None:
        """Replace ``definitions`` with read-only specs from ``SetConditionsCmd``.

        Each spec dict must have keys ``name`` (str), ``address`` (int),
        ``size`` (int in SUPPORTED_READ_SIZES). type/values/scope take their
        defaults — fine because only ``read_all`` touches definitions built
        this way; capture-side ``decode`` always uses YAML-loaded registries.
        """
        for s in specs:
            if s["size"] not in SUPPORTED_READ_SIZES:
                raise ValueError(
                    f"unsupported condition size {s['size']} for {s['name']!r}; "
                    f"only {SUPPORTED_READ_SIZES} supported"
                )
        self.definitions = [
            ConditionDef(name=s["name"], address=s["address"], size=s["size"])
            for s in specs
        ]

    def read_all(self, client: _RamReader) -> dict[str, int]:
        """Issue one read per definition; return {name: int_value}.

        Used by the RA backend's poller to stamp event.conditions. Caller
        provides anything with a ``.read_ram(address, size) -> bytes`` method
        — concretely, an ``NCIClient``, but the dependency is duck-typed so
        this module stays independent of the RA package.

        Two-byte values are decoded little-endian (matching SNES native word
        order, which is what every condition author expects).
        """
        out: dict[str, int] = {}
        for d in self.definitions:
            if d.size not in SUPPORTED_READ_SIZES:
                raise ValueError(
                    f"unsupported condition size {d.size} for {d.name!r}; "
                    f"only {SUPPORTED_READ_SIZES} supported"
                )
            data = client.read_ram(d.address, d.size)
            if d.size == 1:
                out[d.name] = data[0]
            else:
                out[d.name] = data[0] | (data[1] << 8)
        return out

    def decode(self, raw: dict[str, int], level: int) -> dict[str, Any]:
        """Decode raw memory values into logical conditions, filtering to in-scope."""
        result: dict[str, Any] = {}
        for d in self.in_scope(level):
            if d.name not in raw:
                continue
            v = raw[d.name]
            if d.type == "enum":
                if d.values is None:
                    raise ValueError(
                        f"enum condition '{d.name}' requires a 'values' map but got None"
                    )
                if v not in d.values:
                    raise ValueError(
                        f"unknown value {v} for enum condition '{d.name}'; known: {sorted(d.values.keys())}"
                    )
                result[d.name] = d.values[v]
            elif d.type == "bool":
                result[d.name] = bool(v)
            else:
                raise ValueError(f"unknown condition type: {d.type}")
        return result


def load_registry_for_game(
    game_id: str,
    games_root: Path | None = None,
) -> ConditionRegistry:
    """Load per-game conditions.yaml; return empty registry if file missing."""
    if games_root is None:
        games_root = Path(__file__).parent / "games"
    yaml_path = games_root / game_id / "conditions.yaml"
    if not yaml_path.exists():
        return ConditionRegistry(definitions=[])
    return ConditionRegistry.from_yaml(yaml_path)
