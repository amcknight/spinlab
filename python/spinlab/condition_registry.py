"""Loads per-game condition definitions from YAML; decodes raw values."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml


# Scope types ----------------------------------------------------------
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
    def levels(cls, levels_: Iterable[int]) -> "Scope":
        return cls.levels_of(levels_)

    def covers(self, level: int) -> bool:
        return self.is_game_scope or level in self.levels


@dataclass(frozen=True)
class ConditionDef:
    name: str
    address: int
    size: int
    type: str                              # 'enum' or 'bool'
    values: dict[int, str] | None
    scope: Scope


@dataclass
class ConditionRegistry:
    definitions: list[ConditionDef] = field(default_factory=list)

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
        return cls(definitions=defs)

    def in_scope(self, level: int) -> list[ConditionDef]:
        return [d for d in self.definitions if d.scope.covers(level)]

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
