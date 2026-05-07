# RetroArch Migration — Phase B: Python NCI Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fully-unit-tested synchronous Python client for RetroArch's Network Command Interface (NCI), covering every NCI command SpinLab needs and surfacing the deep-pause gotcha as a typed error.

**Architecture:** Sync UDP `socket` client. Single `NCIClient` class wraps a low-level send-and-receive primitive. Response parsing accounts for the echoed-command-and-address prefix (the bug the spike caught). Custom exception hierarchy distinguishes timeout, protocol error, and "core frozen" states. A pytest fixture spins up a fake UDP server that responds to scripted commands, so the client tests run without any real RetroArch.

**Tech Stack:** Python 3.11+, stdlib `socket`, `dataclasses`, pytest.

**Phase A audit reference:** [`docs/retroarch-migration/lua-audit.md`](../../retroarch-migration/lua-audit.md). Spec: [`docs/superpowers/specs/2026-05-06-retroarch-migration-design.md`](../specs/2026-05-06-retroarch-migration-design.md).

---

## File Structure

| Path | Purpose |
|------|---------|
| `python/spinlab/retroarch/__init__.py` | Package init; re-exports `NCIClient`, `StatusInfo`, exception types. |
| `python/spinlab/retroarch/exceptions.py` | `NCIError`, `NCITimeout`, `NCIProtocolError`, `NCICoreFrozen`. |
| `python/spinlab/retroarch/responses.py` | `StatusInfo` dataclass (parsed `GET_STATUS` reply). |
| `python/spinlab/retroarch/nci.py` | `NCIClient` — every command method + running-state probe. |
| `tests/unit/retroarch/__init__.py` | Empty package marker. |
| `tests/unit/retroarch/conftest.py` | `fake_nci_server` pytest fixture (UDP responder thread). |
| `tests/unit/retroarch/test_nci.py` | All client unit tests. |

The `python/spinlab/retroarch/` package is the single home for everything emulator-facing. Phases C–E will add `addresses.py`, `poller.py`, `state_io.py`, `bsv.py` next to `nci.py`.

---

## Task 1: Package skeleton + exception hierarchy

**Files:**
- Create: `python/spinlab/retroarch/__init__.py`
- Create: `python/spinlab/retroarch/exceptions.py`
- Create: `tests/unit/retroarch/__init__.py`
- Create: `tests/unit/retroarch/test_nci.py` (initial — just import smoke test)

- [ ] **Step 1: Create empty package init files**

```python
# python/spinlab/retroarch/__init__.py
"""RetroArch integration: NCI client, polling, savestate I/O, BSV adapter."""

from spinlab.retroarch.exceptions import (
    NCICoreFrozen,
    NCIError,
    NCIProtocolError,
    NCITimeout,
)

__all__ = ["NCICoreFrozen", "NCIError", "NCIProtocolError", "NCITimeout"]
```

```python
# tests/unit/retroarch/__init__.py
```

- [ ] **Step 2: Write the exception hierarchy test**

```python
# tests/unit/retroarch/test_nci.py
"""Unit tests for spinlab.retroarch — NCI client and helpers."""
from spinlab.retroarch import (
    NCICoreFrozen,
    NCIError,
    NCIProtocolError,
    NCITimeout,
)


def test_exception_hierarchy():
    assert issubclass(NCITimeout, NCIError)
    assert issubclass(NCIProtocolError, NCIError)
    assert issubclass(NCICoreFrozen, NCIError)
```

- [ ] **Step 3: Run test to verify it fails**

```
python -m pytest tests/unit/retroarch/test_nci.py::test_exception_hierarchy -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'spinlab.retroarch'` (or similar — exceptions module doesn't exist yet).

- [ ] **Step 4: Implement the exceptions module**

```python
# python/spinlab/retroarch/exceptions.py
"""Typed errors for the NCI client."""


class NCIError(Exception):
    """Base error for all NCI failures."""


class NCITimeout(NCIError):
    """UDP receive timed out before RetroArch responded."""


class NCIProtocolError(NCIError):
    """RetroArch responded but the reply did not match the expected protocol shape."""


class NCICoreFrozen(NCIError):
    """NCI service is responsive but the emulator core thread isn't advancing frames.

    Detected by the running-state probe (NCIClient.is_core_running). Surfaces
    the spike-discovered deep-pause state where READ_CORE_RAM continues to work
    but the game is frozen.
    """
```

- [ ] **Step 5: Run test to verify it passes**

```
python -m pytest tests/unit/retroarch/test_nci.py::test_exception_hierarchy -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/retroarch/__init__.py python/spinlab/retroarch/exceptions.py tests/unit/retroarch/__init__.py tests/unit/retroarch/test_nci.py
git commit -m "feat(retroarch): NCI exception hierarchy"
```

---

## Task 2: Fake NCI server fixture

A test-time stand-in for RetroArch's UDP listener. Lets every other test be fully synchronous and reproducible.

**Files:**
- Create: `tests/unit/retroarch/conftest.py`
- Modify: `tests/unit/retroarch/test_nci.py` (add fixture smoke test)

- [ ] **Step 1: Write the fixture smoke test first**

```python
# Append to tests/unit/retroarch/test_nci.py
import socket


def test_fake_nci_server_responds(fake_nci_server):
    """Fixture spins up a UDP responder; we can hit it and get a scripted reply."""
    fake_nci_server.handle("VERSION", lambda _: "1.22.2\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    sock.sendto(b"VERSION", fake_nci_server.address)
    data, _ = sock.recvfrom(4096)
    sock.close()

    assert data.decode() == "1.22.2\n"
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/unit/retroarch/test_nci.py::test_fake_nci_server_responds -v
```

Expected: FAIL with `fixture 'fake_nci_server' not found`.

- [ ] **Step 3: Implement the fixture**

```python
# tests/unit/retroarch/conftest.py
"""Fake NCI server fixture — a UDP responder thread for unit-testing the NCI client."""
from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

# Test commands either echo a scripted response or get explicitly ignored
# (timeout). Default behavior is "no handler -> drop the packet" so
# unhandled commands surface as NCITimeout, matching production.
ResponseFn = Callable[[str], str | None]


@dataclass
class FakeNCIServer:
    address: tuple[str, int]
    _handlers: dict[str, ResponseFn] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _sock: socket.socket | None = None

    def handle(self, command_prefix: str, response: ResponseFn | str) -> None:
        """Register a response for any incoming command starting with command_prefix.

        If response is a string, it's returned verbatim. If callable, it receives
        the full incoming command and returns the reply (or None to drop the packet).
        Most-specific (longest) prefix wins.
        """
        if isinstance(response, str):
            literal = response
            response = lambda _cmd, _lit=literal: _lit  # noqa: E731
        self._handlers[command_prefix] = response

    def _serve(self) -> None:
        assert self._sock is not None
        self._sock.settimeout(0.05)
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            cmd = data.decode("ascii", errors="replace")
            # Longest-matching-prefix wins so "READ_CORE_RAM 94 4" can be
            # handled distinctly from "READ_CORE_MEMORY 94 4" if both registered.
            handler = None
            best_len = -1
            for prefix, fn in self._handlers.items():
                if cmd.startswith(prefix) and len(prefix) > best_len:
                    handler = fn
                    best_len = len(prefix)
            if handler is None:
                continue  # drop -> client sees timeout
            reply = handler(cmd)
            if reply is None:
                continue
            self._sock.sendto(reply.encode("ascii"), addr)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._sock is not None:
            self._sock.close()


@pytest.fixture
def fake_nci_server():
    """Spin up a fake NCI server on a random localhost UDP port for one test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))  # ephemeral port
    server = FakeNCIServer(address=sock.getsockname())
    server._sock = sock
    server._thread = threading.Thread(target=server._serve, daemon=True)
    server._thread.start()
    try:
        yield server
    finally:
        server.stop()
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/unit/retroarch/test_nci.py::test_fake_nci_server_responds -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/retroarch/conftest.py tests/unit/retroarch/test_nci.py
git commit -m "test(retroarch): fake NCI server fixture for unit tests"
```

---

## Task 3: Low-level UDP transport

The single primitive every NCI command builds on. Sends a string, returns a string. Raises `NCITimeout` on timeout.

**Files:**
- Modify: `python/spinlab/retroarch/nci.py` (create)
- Modify: `tests/unit/retroarch/test_nci.py` (add `_send` tests)

- [ ] **Step 1: Write the failing tests for the transport**

```python
# Append to tests/unit/retroarch/test_nci.py
import pytest

from spinlab.retroarch.nci import NCIClient


def test_transport_round_trip(fake_nci_server):
    fake_nci_server.handle("PING", "PONG\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    assert client._send("PING") == "PONG"


def test_transport_timeout_raises(fake_nci_server):
    # No handler registered; server drops the packet.
    client = NCIClient(
        host=fake_nci_server.address[0],
        port=fake_nci_server.address[1],
        timeout=0.1,
    )
    with pytest.raises(NCITimeout):
        client._send("UNHANDLED_COMMAND")
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/unit/retroarch/test_nci.py -v -k "transport"
```

Expected: FAIL with `ModuleNotFoundError: No module named 'spinlab.retroarch.nci'`.

- [ ] **Step 3: Implement the transport**

```python
# python/spinlab/retroarch/nci.py
"""Synchronous NCI client for RetroArch's Network Command Interface.

Uses UDP — RetroArch's NCI is fire-and-forget for most commands and one-shot
request/reply for memory reads. We keep the client sync; async callers wrap
calls in `asyncio.to_thread` if they need to.
"""
from __future__ import annotations

import socket

from spinlab.retroarch.exceptions import NCITimeout

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 55355
DEFAULT_TIMEOUT_SEC = 0.5


class NCIClient:
    """Synchronous UDP client for RetroArch's Network Command Interface."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def _send(self, command: str) -> str:
        """Send command and return the reply text (whitespace-stripped).

        Raises NCITimeout if no reply arrives within self.timeout seconds.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.sendto(command.encode("ascii"), (self.host, self.port))
            data, _ = sock.recvfrom(4096)
        except socket.timeout as exc:
            raise NCITimeout(f"no reply within {self.timeout}s for {command!r}") from exc
        finally:
            sock.close()
        return data.decode("ascii", errors="replace").strip()

    def _send_no_reply(self, command: str) -> None:
        """Send command and don't wait for any reply. For fire-and-forget commands
        like SAVE_STATE that simulate a hotkey press and return nothing.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(command.encode("ascii"), (self.host, self.port))
        finally:
            sock.close()
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/unit/retroarch/test_nci.py -v -k "transport"
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/nci.py tests/unit/retroarch/test_nci.py
git commit -m "feat(retroarch): UDP transport with NCITimeout on no-reply"
```

---

## Task 4: VERSION command

Simplest end-to-end command. Validates the wrapper pattern and gives us a heartbeat-style probe.

**Files:**
- Modify: `python/spinlab/retroarch/nci.py`
- Modify: `tests/unit/retroarch/test_nci.py`

- [ ] **Step 1: Write failing test**

```python
# Append to tests/unit/retroarch/test_nci.py
def test_version(fake_nci_server):
    fake_nci_server.handle("VERSION", "1.22.2\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    assert client.version() == "1.22.2"
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/unit/retroarch/test_nci.py::test_version -v
```

Expected: FAIL with `AttributeError: 'NCIClient' object has no attribute 'version'`.

- [ ] **Step 3: Implement `version()`**

Add to `NCIClient` in `python/spinlab/retroarch/nci.py`:

```python
    def version(self) -> str:
        """Return RetroArch's reported version string (e.g. "1.22.2")."""
        return self._send("VERSION")
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/unit/retroarch/test_nci.py::test_version -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/nci.py tests/unit/retroarch/test_nci.py
git commit -m "feat(retroarch): VERSION command"
```

---

## Task 5: READ_CORE_RAM with response parser

The workhorse. Format is `READ_CORE_RAM <addr_hex> <byte0> <byte1> ...` on success, `READ_CORE_RAM <addr_hex> -1 [error]` on failure. **The parser must skip the echoed command name (`parts[0]`) and the echoed address (`parts[1]`)** — the spike script's bug was treating the address echo as a data byte.

**Files:**
- Modify: `python/spinlab/retroarch/nci.py`
- Modify: `tests/unit/retroarch/test_nci.py`

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/unit/retroarch/test_nci.py
def test_read_ram_parses_data_bytes(fake_nci_server):
    # 4 bytes at $7E0094 returned as: command-echo + addr-echo + 4 hex bytes
    fake_nci_server.handle(
        "READ_CORE_RAM 94 4",
        "READ_CORE_RAM 94 AA BB CC DD\n",
    )
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    assert client.read_ram(0x94, 4) == bytes([0xAA, 0xBB, 0xCC, 0xDD])


def test_read_ram_skips_address_echo(fake_nci_server):
    """Regression: spike's bug was treating the echoed address as a data byte.
    For an unfortunately-shaped address like 0x94 (looks like a byte), the
    parser must not include it in the result.
    """
    fake_nci_server.handle(
        "READ_CORE_RAM 94 1",
        "READ_CORE_RAM 94 7F\n",
    )
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    # Single requested byte returns single byte — NOT [0x94, 0x7F].
    assert client.read_ram(0x94, 1) == bytes([0x7F])


def test_read_ram_protocol_error_on_minus_one(fake_nci_server):
    fake_nci_server.handle(
        "READ_CORE_RAM ffff 1",
        "READ_CORE_RAM ffff -1\n",
    )
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    with pytest.raises(NCIProtocolError, match="-1"):
        client.read_ram(0xFFFF, 1)


def test_read_ram_protocol_error_on_unparseable(fake_nci_server):
    fake_nci_server.handle("READ_CORE_RAM 94 1", "garbage\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    with pytest.raises(NCIProtocolError):
        client.read_ram(0x94, 1)
```

Add the import at the top of the test file if not already present:

```python
from spinlab.retroarch import NCIProtocolError
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/unit/retroarch/test_nci.py -v -k "read_ram"
```

Expected: 4 FAIL with `AttributeError: 'NCIClient' object has no attribute 'read_ram'`.

- [ ] **Step 3: Implement `read_ram()`**

Add the import at the top of `nci.py`:

```python
from spinlab.retroarch.exceptions import NCIProtocolError, NCITimeout
```

Add the method to `NCIClient`:

```python
    def read_ram(self, addr: int, length: int) -> bytes:
        """Read `length` bytes from WRAM-flat offset `addr`.

        Reply format: "READ_CORE_RAM <addr_hex> <byte0> <byte1> ..." on success,
        or "READ_CORE_RAM <addr_hex> -1 [error]" on failure.

        Raises NCIProtocolError if the reply is malformed or contains -1.
        """
        reply = self._send(f"READ_CORE_RAM {addr:x} {length}")
        parts = reply.split()
        if len(parts) < 2:
            raise NCIProtocolError(f"reply too short: {reply!r}")
        # parts[0] = command echo, parts[1] = address echo, parts[2:] = data bytes.
        data_tokens = parts[2:]
        if not data_tokens:
            raise NCIProtocolError(f"reply has no data bytes: {reply!r}")
        if data_tokens[0] == "-1":
            raise NCIProtocolError(f"RetroArch returned error for read at {addr:#x}: {reply!r}")
        try:
            return bytes(int(t, 16) for t in data_tokens)
        except ValueError as exc:
            raise NCIProtocolError(f"unparseable reply: {reply!r}") from exc
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/unit/retroarch/test_nci.py -v -k "read_ram"
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/nci.py tests/unit/retroarch/test_nci.py
git commit -m "feat(retroarch): READ_CORE_RAM with strict response parsing"
```

---

## Task 6: WRITE_CORE_RAM

**Files:**
- Modify: `python/spinlab/retroarch/nci.py`
- Modify: `tests/unit/retroarch/test_nci.py`

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/unit/retroarch/test_nci.py
def test_write_ram_sends_correct_command(fake_nci_server):
    received = []

    def capture(cmd):
        received.append(cmd)
        return "WRITE_CORE_RAM 94 1\n"  # RA echoes addr + count on success

    fake_nci_server.handle("WRITE_CORE_RAM", capture)
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    client.write_ram(0x94, bytes([0xAA, 0xBB]))

    assert received[0] == "WRITE_CORE_RAM 94 aa bb"
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/unit/retroarch/test_nci.py::test_write_ram_sends_correct_command -v
```

Expected: FAIL with `AttributeError: 'NCIClient' object has no attribute 'write_ram'`.

- [ ] **Step 3: Implement `write_ram()`**

```python
    def write_ram(self, addr: int, data: bytes) -> None:
        """Write `data` to WRAM-flat offset `addr`. Returns nothing on success.

        WRITE_CORE_RAM is fire-and-forget from our side: RetroArch echoes the
        write back but we don't currently parse the response (no reliable signal
        beyond "no exception thrown").
        """
        hex_bytes = " ".join(f"{b:02x}" for b in data)
        self._send(f"WRITE_CORE_RAM {addr:x} {hex_bytes}")
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/unit/retroarch/test_nci.py::test_write_ram_sends_correct_command -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/nci.py tests/unit/retroarch/test_nci.py
git commit -m "feat(retroarch): WRITE_CORE_RAM"
```

---

## Task 7: GET_STATUS with parsed StatusInfo

`GET_STATUS` reply format: `GET_STATUS <state> <system>,<game>,crc32=<hex>` where state is `PLAYING`, `PAUSED`, or `CONTENTLESS`.

**Files:**
- Create: `python/spinlab/retroarch/responses.py`
- Modify: `python/spinlab/retroarch/__init__.py`
- Modify: `python/spinlab/retroarch/nci.py`
- Modify: `tests/unit/retroarch/test_nci.py`

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/unit/retroarch/test_nci.py
from spinlab.retroarch import StatusInfo


def test_get_status_playing(fake_nci_server):
    fake_nci_server.handle(
        "GET_STATUS",
        "GET_STATUS PLAYING super_nes,Toothpaste,crc32=41b3c49d\n",
    )
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    status = client.get_status()

    assert status == StatusInfo(
        state="PLAYING",
        system="super_nes",
        game="Toothpaste",
        crc32="41b3c49d",
    )


def test_get_status_contentless(fake_nci_server):
    """No game loaded — still a valid response, just sparse."""
    fake_nci_server.handle("GET_STATUS", "GET_STATUS CONTENTLESS\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    status = client.get_status()

    assert status == StatusInfo(state="CONTENTLESS", system=None, game=None, crc32=None)
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/unit/retroarch/test_nci.py -v -k "get_status"
```

Expected: 2 FAIL with `ImportError: cannot import name 'StatusInfo'`.

- [ ] **Step 3: Define `StatusInfo`**

```python
# python/spinlab/retroarch/responses.py
"""Parsed response dataclasses for NCI commands."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusInfo:
    """Parsed reply from GET_STATUS.

    Examples:
      "GET_STATUS PLAYING super_nes,Toothpaste,crc32=41b3c49d"
        -> state=PLAYING, system=super_nes, game=Toothpaste, crc32=41b3c49d
      "GET_STATUS CONTENTLESS"
        -> state=CONTENTLESS, others None
    """

    state: str  # PLAYING | PAUSED | CONTENTLESS
    system: str | None = None
    game: str | None = None
    crc32: str | None = None
```

Update `__init__.py`:

```python
# python/spinlab/retroarch/__init__.py
"""RetroArch integration: NCI client, polling, savestate I/O, BSV adapter."""

from spinlab.retroarch.exceptions import (
    NCICoreFrozen,
    NCIError,
    NCIProtocolError,
    NCITimeout,
)
from spinlab.retroarch.responses import StatusInfo

__all__ = [
    "NCICoreFrozen",
    "NCIError",
    "NCIProtocolError",
    "NCITimeout",
    "StatusInfo",
]
```

- [ ] **Step 4: Implement `get_status()`**

Add to `nci.py` (and import StatusInfo):

```python
from spinlab.retroarch.responses import StatusInfo
```

```python
    def get_status(self) -> StatusInfo:
        """Return parsed emulator state. Raises NCIProtocolError on malformed reply."""
        reply = self._send("GET_STATUS")
        # Format: "GET_STATUS <STATE> [<system>,<game>,crc32=<hex>]"
        parts = reply.split(maxsplit=2)
        if len(parts) < 2:
            raise NCIProtocolError(f"GET_STATUS reply too short: {reply!r}")
        state = parts[1]
        if len(parts) < 3:
            return StatusInfo(state=state)
        # Third field is comma-separated; last entry is "crc32=<hex>".
        bits = parts[2].split(",")
        if len(bits) != 3 or not bits[2].startswith("crc32="):
            raise NCIProtocolError(f"GET_STATUS metadata malformed: {reply!r}")
        return StatusInfo(
            state=state,
            system=bits[0],
            game=bits[1],
            crc32=bits[2].removeprefix("crc32="),
        )
```

- [ ] **Step 5: Run tests to verify they pass**

```
python -m pytest tests/unit/retroarch/test_nci.py -v -k "get_status"
```

Expected: 2 PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/retroarch/responses.py python/spinlab/retroarch/__init__.py python/spinlab/retroarch/nci.py tests/unit/retroarch/test_nci.py
git commit -m "feat(retroarch): GET_STATUS with parsed StatusInfo"
```

---

## Task 8: Fire-and-forget commands

`SAVE_STATE`, `LOAD_STATE_SLOT N`, `RESET`, `PAUSE_TOGGLE`, `FRAMEADVANCE`, `QUIT`. These simulate hotkey presses; RA does not respond. Use `_send_no_reply` so we don't block waiting for a reply that never comes.

**Files:**
- Modify: `python/spinlab/retroarch/nci.py`
- Modify: `tests/unit/retroarch/test_nci.py`

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/unit/retroarch/test_nci.py
@pytest.mark.parametrize(
    "method,args,expected_command",
    [
        ("save_state", (), "SAVE_STATE"),
        ("load_state_slot", (9999,), "LOAD_STATE_SLOT 9999"),
        ("reset", (), "RESET"),
        ("pause_toggle", (), "PAUSE_TOGGLE"),
        ("frame_advance", (), "FRAMEADVANCE"),
        ("quit", (), "QUIT"),
    ],
)
def test_fire_and_forget_commands(fake_nci_server, method, args, expected_command):
    received = []
    fake_nci_server.handle("", lambda cmd: received.append(cmd) or None)
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    getattr(client, method)(*args)

    # Give the fake server a moment to process the datagram.
    import time
    time.sleep(0.05)
    assert received == [expected_command]
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/unit/retroarch/test_nci.py -v -k "fire_and_forget"
```

Expected: 6 FAIL with `AttributeError: 'NCIClient' object has no attribute 'save_state'` (etc).

- [ ] **Step 3: Implement the fire-and-forget commands**

Add to `NCIClient`:

```python
    def save_state(self) -> None:
        """Save state to RA's current slot. Increments state_slot if savestate_auto_index is on.

        SpinLab's slot strategy lives in Phase D's state_io module; this method
        is the raw command and does no slot management.
        """
        self._send_no_reply("SAVE_STATE")

    def load_state_slot(self, slot: int) -> None:
        """Load state from a specific slot, ignoring RA's current slot."""
        self._send_no_reply(f"LOAD_STATE_SLOT {slot}")

    def reset(self) -> None:
        """Hard-reset the emulated console."""
        self._send_no_reply("RESET")

    def pause_toggle(self) -> None:
        """Toggle paused state. WARNING: don't call blindly — see is_core_running.

        Phase 2 spike found a 'deep pause' state where PAUSE_TOGGLE could not
        recover the emulator core. Use is_core_running() to confirm state before
        toggling.
        """
        self._send_no_reply("PAUSE_TOGGLE")

    def frame_advance(self) -> None:
        """Advance one frame (only meaningful while paused)."""
        self._send_no_reply("FRAMEADVANCE")

    def quit(self) -> None:
        """Tell RetroArch to shut down."""
        self._send_no_reply("QUIT")
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/unit/retroarch/test_nci.py -v -k "fire_and_forget"
```

Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/nci.py tests/unit/retroarch/test_nci.py
git commit -m "feat(retroarch): SAVE_STATE, LOAD_STATE_SLOT, RESET, PAUSE_TOGGLE, FRAMEADVANCE, QUIT"
```

---

## Task 9: Running-state probe (`is_core_running`)

Detects the spike-discovered deep-pause state by sampling a memory address twice with a small delay; if the bytes are identical, the core isn't advancing.

**Files:**
- Modify: `python/spinlab/retroarch/nci.py`
- Modify: `tests/unit/retroarch/test_nci.py`

The address SpinLab passes will be a known-changing one (in SMW: a frame counter or random animation timer). Phase C's `addresses.py` will define a canonical "tick" address; Phase B just takes it as a parameter.

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/unit/retroarch/test_nci.py
import itertools


def test_is_core_running_true_when_memory_changes(fake_nci_server):
    """Two reads of the same address return different values -> core is advancing."""
    counter = itertools.count(start=0)

    def respond(cmd):
        n = next(counter)
        return f"READ_CORE_RAM 13 {n & 0xFF:02x}\n"

    fake_nci_server.handle("READ_CORE_RAM 13 1", respond)
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    assert client.is_core_running(tick_addr=0x13, sample_delay=0.01) is True


def test_is_core_running_false_when_memory_static(fake_nci_server):
    """Identical reads -> core frozen (deep-pause state)."""
    fake_nci_server.handle("READ_CORE_RAM 13 1", "READ_CORE_RAM 13 6d\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    assert client.is_core_running(tick_addr=0x13, sample_delay=0.01) is False


def test_is_core_running_raises_on_read_error(fake_nci_server):
    fake_nci_server.handle("READ_CORE_RAM ffff 1", "READ_CORE_RAM ffff -1\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    with pytest.raises(NCIProtocolError):
        client.is_core_running(tick_addr=0xFFFF, sample_delay=0.01)
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/unit/retroarch/test_nci.py -v -k "is_core_running"
```

Expected: 3 FAIL with `AttributeError: 'NCIClient' object has no attribute 'is_core_running'`.

- [ ] **Step 3: Implement `is_core_running`**

Add an import to `nci.py`:

```python
import time
```

Add the method:

```python
    def is_core_running(self, tick_addr: int, sample_delay: float = 0.05) -> bool:
        """Return True if the emulator core is advancing frames.

        Detects the spike-discovered deep-pause state where NCI stays responsive
        but the core thread is frozen — see docs/retroarch-migration/spike-log.md.

        Strategy: read a single byte at `tick_addr` twice with `sample_delay`
        between samples. If the bytes are identical, the core is not advancing.
        Caller picks `tick_addr` (typically a frame counter or fast-changing
        animation register).

        Note: a False result can occur transiently if `tick_addr` happens to
        wrap back to its previous value within `sample_delay`. For deep-pause
        detection, sample_delay should be significantly larger than one frame
        period (16.67ms) — default 0.05s ≈ 3 frames is comfortable.
        """
        a = self.read_ram(tick_addr, 1)
        time.sleep(sample_delay)
        b = self.read_ram(tick_addr, 1)
        return a != b
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/unit/retroarch/test_nci.py -v -k "is_core_running"
```

Expected: 3 PASS.

- [ ] **Step 5: Run the full unit suite to confirm no regressions**

```
python -m pytest tests/unit/retroarch/ -v
```

Expected: all NCI tests pass (~16 tests).

- [ ] **Step 6: Run the project-wide fast suite for sanity**

```
python -m pytest -m "not (emulator or slow or frontend)" -q
```

Expected: same pass count as the baseline (650+) plus the new `tests/unit/retroarch/` cases.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/retroarch/nci.py tests/unit/retroarch/test_nci.py
git commit -m "feat(retroarch): is_core_running probe for deep-pause detection"
```

---

## Phase B exit criteria

- All NCI commands SpinLab will need (per Phase A audit) are wrapped in `NCIClient` with typed errors.
- Every method has unit-test coverage against the fake NCI server.
- The deep-pause gotcha from the spike has a typed surface (`NCICoreFrozen`) and a probe (`is_core_running`).
- Full fast test suite is green.
- Zero dependency on a real RetroArch — everything testable from any environment.

## What's deliberately not in Phase B

- **Async wrapping.** Calls take 10–20ms each. Async callers can use `asyncio.to_thread` until measurable contention argues for an async-native client.
- **Retry logic.** UDP is best-effort; we surface timeouts as exceptions and let the caller decide. Phase C will likely add a thin retry shim around `read_ram` if 60Hz polling exposes transient drops.
- **`NCICoreFrozen` autoraise.** `is_core_running` returns a bool; callers that want to abort on frozen-core should raise `NCICoreFrozen` themselves. Keeps the probe composable.
- **Address constants.** Live in Phase C's `addresses.py` — the NCI client takes ints.
- **Slot file management.** Phase D's `state_io` module handles RA-slot ↔ SpinLab-segment file shuffling. NCI client just exposes the raw save/load primitives.

## Next phase

After Phase B lands, write the Phase C plan: `python/spinlab/retroarch/poller.py` + `addresses.py` — port `spinlab.lua`'s memory polling and transition detection to Python. That's the largest single phase of the migration.

## Self-review notes

- Spec coverage: every command listed in Phase A's "per-phase impact summary, Phase B" row is covered (READ_CORE_RAM, WRITE_CORE_RAM, SAVE_STATE, LOAD_STATE_SLOT, RESET, PAUSE_TOGGLE, FRAMEADVANCE, VERSION, GET_STATUS, QUIT, plus the deep-pause probe).
- Type/method consistency: `read_ram(addr, length)`, `write_ram(addr, data)`, `load_state_slot(slot)`, `is_core_running(tick_addr, sample_delay)` — names used identically across task code and tests.
- Placeholder scan: no TBDs, no "implement later" — every step has explicit code.
- The `fake_nci_server.handle("", ...)` pattern in Task 8 catches all commands as fallback because empty-string is a prefix of every command and is the shortest possible prefix; fine for that test.
