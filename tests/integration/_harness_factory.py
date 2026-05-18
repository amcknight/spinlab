"""Session-scoped RAHarness factory used by integration fixtures.

Cache key is `(rom_key, use_fresh_state)` so a single test session can hold
both a fresh-state-isolated and a no-reset harness for the same ROM.
"""
from __future__ import annotations

import logging
import socket

from tests.integration._rom_paths import (
    ROM_REGISTRY,
    resolve_ra_paths,
    state_path_for,
)
from tests.integration.ra_harness import (
    RAHarness,
    RAHarnessLaunchError,
)


def _free_udp_port() -> int:
    """Find a free UDP port.

    Small TOCTOU window between the bind here releasing and RetroArch binding
    to the same port — acceptable because the harness's NCI ping retries cover
    transient failures, and the loopback UDP port space is otherwise quiet on
    a test host.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HarnessFactory:
    """Session-scoped cache mapping (rom_key, use_fresh_state) -> RAHarness.

    Separated from the pytest fixture so unit tests can drive the cache and
    teardown logic without a real fixture lifecycle.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, bool], RAHarness] = {}

    def __call__(self, rom_key: str, use_fresh_state: bool = True) -> RAHarness:
        """Return (or create + cache) a harness for `rom_key`.

        `use_fresh_state=True` (the default) wires a per-launch isolated
        savestate_directory with the fresh-boot state pre-staged at
        FRESH_BOOT_STATE_SLOT, and causes RAPokeEngine to load it before
        each scenario. Required by the poke-transition tests.

        `use_fresh_state=False` is for fixtures whose RA process must talk
        to the user's actual savestate_directory — currently just the
        replay fixture.
        """
        cache_key = (rom_key, use_fresh_state)
        if cache_key in self._cache:
            return self._cache[cache_key]
        retroarch_exe, ra_core_path, rom_path = resolve_ra_paths(rom_key)
        fresh_state_path = (
            state_path_for(ROM_REGISTRY[rom_key]) if use_fresh_state else None
        )
        try:
            harness = RAHarness.launch(
                rom_path=rom_path,
                core_path=ra_core_path,
                retroarch_exe=retroarch_exe,
                nci_port=_free_udp_port(),
                fresh_state_path=fresh_state_path,
            )
        except RAHarnessLaunchError as exc:
            # CLAUDE.md: launch failure is a FAILURE, not a skip. Annotate args
            # with rom_key so the test report still names the harness that failed.
            exc.args = (
                f"ra_harness launch failed for rom_key={rom_key!r}: {exc.args[0]}",
            )
            raise
        self._cache[cache_key] = harness
        return harness

    def teardown_all(self) -> None:
        while self._cache:
            cache_key, harness = self._cache.popitem()
            try:
                harness.teardown()
            except Exception:
                logging.getLogger(__name__).exception(
                    "ra_harness teardown failed for %r", cache_key
                )


def harness_factory_impl() -> HarnessFactory:
    """Factory constructor surface used by both the pytest fixture and unit tests."""
    return HarnessFactory()
