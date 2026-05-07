"""ConditionRegistry — dynamic per-event memory probes.

Replaces lua/spinlab.lua's `condition_defs` + `read_conditions()` + the TCP
`set_conditions` command. The registry holds a list of (name, address, size)
tuples; `read_all(client)` returns {name: int_value} via NCI reads.

Sizes 1 and 2 are supported (matching what kaizosplits uses). Larger sizes
are rejected because the value-construction logic is byte-by-byte and would
need a clearer endianness contract before extending.
"""
from __future__ import annotations

from dataclasses import dataclass

from spinlab.retroarch.nci import NCIClient

SUPPORTED_SIZES = (1, 2)


@dataclass(frozen=True)
class ConditionSpec:
    name: str
    address: int
    size: int  # bytes; must be in SUPPORTED_SIZES


class ConditionRegistry:
    """Holds the active set of condition probes. Replace via set()."""

    def __init__(self) -> None:
        self._specs: list[ConditionSpec] = []

    def set(self, specs: list[ConditionSpec]) -> None:
        for s in specs:
            if s.size not in SUPPORTED_SIZES:
                raise ValueError(
                    f"unsupported condition size {s.size} for {s.name!r}; "
                    f"only {SUPPORTED_SIZES} supported"
                )
        self._specs = list(specs)

    def read_all(self, client: NCIClient) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self._specs:
            data = client.read_ram(s.address, s.size)
            if s.size == 1:
                out[s.name] = data[0]
            else:  # size == 2, little-endian per emu.readWord convention
                out[s.name] = data[0] | (data[1] << 8)
        return out
