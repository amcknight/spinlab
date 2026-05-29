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

logger = logging.getLogger(__name__)


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
        # One (rom_key, use_fresh_state, exit_code) tuple per transparent
        # relaunch of a crashed RA. Surfaced at session end so the upstream
        # snes9x ACCESS_VIOLATION crashes the suite recovered from are visible
        # rather than silently swallowed.
        self.recoveries: list[tuple[str, bool, int | None]] = []

    def __call__(self, rom_key: str, use_fresh_state: bool = True) -> RAHarness:
        """Return (or create + cache) a *live* harness for `rom_key`.

        On a cache hit the cached harness is health-probed via ``is_alive()``.
        A dead RA proc (e.g. Windows ACCESS_VIOLATION 0xC0000005) is torn down,
        evicted, and recorded in ``self.recoveries``; the call then falls
        through to relaunch a fresh harness. This makes a crash recoverable
        instead of cascading 8-11 NCITimeout failures across every later test
        that shares the session-scoped harness.

        `use_fresh_state=True` (the default) wires a per-launch isolated
        savestate_directory with the fresh-boot state pre-staged at
        FRESH_BOOT_STATE_SLOT, and causes RAPokeEngine to load it before
        each scenario. Required by the poke-transition tests.

        `use_fresh_state=False` is for the no-reset Love Yourself harness whose
        RA process must talk to the user's actual savestate_directory — the
        replay fixture (needs RA to find staged .replay files) and the harness-
        isolation test (uses it as the second distinct process).
        """
        cache_key = (rom_key, use_fresh_state)
        cached = self._cache.get(cache_key)
        if cached is not None:
            if cached.is_alive():
                return cached
            # Dead cached harness: tear down the corpse, evict, record the
            # recovery, then fall through to relaunch a fresh one.
            exit_code = cached.last_returncode
            exit_hex = (
                f"0x{exit_code & 0xFFFFFFFF:X}" if exit_code is not None else "<unknown>"
            )
            logger.warning(
                "ra_harness_factory: cached harness for %r died (exit %s); "
                "relaunching a fresh RA process",
                cache_key, exit_hex,
            )
            self.recoveries.append((rom_key, use_fresh_state, exit_code))
            try:
                cached.teardown()
            except Exception:
                logger.exception(
                    "ra_harness_factory: teardown of dead harness %r failed", cache_key
                )
            del self._cache[cache_key]
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

    def release(self, rom_key: str, use_fresh_state: bool = True) -> None:
        """Tear down and evict a single cached harness, if present.

        Used to serialize RA processes: a fixture that only needs a non-workhorse
        harness for its own test releases it on teardown so it isn't left alive
        during the crash-prone transition phase. The next ``__call__`` for the
        same key relaunches fresh. No-op if the key isn't cached.
        """
        cache_key = (rom_key, use_fresh_state)
        harness = self._cache.pop(cache_key, None)
        if harness is None:
            return
        try:
            harness.teardown()
        except Exception:
            logger.exception("ra_harness_factory: release teardown failed for %r", cache_key)

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
