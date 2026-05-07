"""RetroArch NCI spike: validates whether RetroArch's Network Command Interface
can support SpinLab's needs (memory polling, savestate I/O, runahead coexistence).

Pre-reqs:
  1. RetroArch installed at C:\\Apps\\RetroArch (or wherever).
  2. snes9x_libretro core installed via Online Updater.
  3. retroarch.cfg has: network_cmd_enable = "true"
  4. RetroArch running, SMW romhack loaded, in a level (Mario visible & movable).

Run:
  python scripts/spike_retroarch.py

The script asks you to confirm Mario is moving / runahead is on at each step.
"""

from __future__ import annotations

import argparse
import socket
import statistics
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 55355
RECV_TIMEOUT_SEC = 0.5

# SMW WRAM addresses ($7E bank). Snes9x libretro typically exposes WRAM at offset
# 0x7E0000 in NCI's address space. If reads return all-zeros or all-FFs, try the
# alternate 0x000000-based offset (some cores map WRAM from 0).
ADDR_MARIO_X_LO = 0x7E0094  # low byte of Mario X position
ADDR_MARIO_X_HI = 0x7E0095
ADDR_LEVEL_NUM = 0x7E13BF  # current level number
ADDR_GAME_MODE = 0x7E0100  # main game mode

POLL_HZ = 60
POLL_DURATION_SEC = 30


class NCI:
    def __init__(self, host: str, port: int) -> None:
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(RECV_TIMEOUT_SEC)

    def send(self, command: str) -> str:
        self.sock.sendto(command.encode("ascii"), self.addr)
        data, _ = self.sock.recvfrom(4096)
        return data.decode("ascii", errors="replace").strip()

    def try_send(self, command: str) -> str | None:
        try:
            return self.send(command)
        except socket.timeout:
            return None

    def read_mem(
        self,
        addr: int,
        length: int = 1,
        *,
        verbose: bool = False,
        command: str = "READ_CORE_RAM",
    ) -> bytes | None:
        cmd = f"{command} {addr:x} {length}"
        reply = self.try_send(cmd)
        if reply is None:
            if verbose:
                print(f"  [{cmd}] -> <timeout>")
            return None
        if verbose:
            print(f"  [{cmd}] -> {reply!r}")
        # Success: "<CMD> <addr_hex> <byte0> <byte1> ..."
        # Failure: "<CMD> <addr_hex> -1 [error message]"
        parts = reply.split()
        if len(parts) < 3:
            return None
        # Skip command name and echoed address; data bytes start at parts[2].
        data_tokens = parts[2:]
        if data_tokens[0] == "-1":
            return None
        try:
            return bytes(int(p, 16) for p in data_tokens)
        except ValueError:
            return None


def step(label: str) -> None:
    print(f"\n=== {label} ===")


def confirm(prompt: str) -> bool:
    # Drain any buffered stdin (e.g. impatient Enters during a long step)
    # so they don't accidentally auto-answer this prompt.
    try:
        import msvcrt  # Windows
        while msvcrt.kbhit():
            msvcrt.getwch()
    except ImportError:
        pass
    return input(f"{prompt} [y/N]: ").strip().lower() == "y"


def test_version(nci: NCI) -> bool:
    step("Step 1: NCI alive?")
    reply = nci.try_send("VERSION")
    if reply is None:
        print("FAIL: no response to VERSION. Is RetroArch running with network_cmd_enable=true?")
        return False
    print(f"OK: VERSION -> {reply}")
    return True


def test_memory_read(nci: NCI) -> bool:
    step("Step 2: Read SMW memory")
    print("Probing READ_CORE_RAM with WRAM-flat addressing (snes9x exposes WRAM at offset 0)...")
    candidates = [
        ("0x000094 (WRAM offset)", 0x000094),
        ("0x7E0094 (full SNES bus)", 0x7E0094),
    ]
    best: tuple[str, int, bytes] | None = None
    for label, addr in candidates:
        data = nci.read_mem(addr, 4, verbose=True, command="READ_CORE_RAM")
        if data is None:
            continue
        non_trivial = not (all(b == 0 for b in data) or all(b == 0xFF for b in data))
        print(f"    {label}: {data.hex(' ')}  {'PLAUSIBLE' if non_trivial else 'all-zero/FF'}")
        if non_trivial and best is None:
            best = (label, addr, data)
    if best is None:
        print("FAIL: no address mapping returned plausible data.")
        print("      Check that you're in a level (Mario visible & moving), not on a title screen.")
        return False
    label, addr, data = best
    print(f"OK: {label} works. Mario X bytes = {data.hex(' ')}")
    nci.mario_x_addr = addr  # type: ignore[attr-defined]
    return True


def test_polling_60hz(nci: NCI) -> bool:
    step("Step 3: Sustained 60Hz polling")
    if not confirm("Mario should be running/moving on screen during this test. Ready?"):
        return False

    period = 1.0 / POLL_HZ
    deadline = time.perf_counter() + POLL_DURATION_SEC
    rtts_ms: list[float] = []
    timeouts = 0
    distinct_x_values: set[int] = set()

    start = time.perf_counter()
    next_tick = start
    last_progress = start
    while time.perf_counter() < deadline:
        next_tick += period
        t0 = time.perf_counter()
        data = nci.read_mem(getattr(nci, "mario_x_addr", ADDR_MARIO_X_LO), 2)
        t1 = time.perf_counter()
        if data is None:
            timeouts += 1
        else:
            rtts_ms.append((t1 - t0) * 1000)
            x = data[0] | (data[1] << 8)
            distinct_x_values.add(x)
        if t1 - last_progress >= 5.0:
            elapsed = t1 - start
            print(f"  ... {elapsed:.0f}s elapsed, {len(rtts_ms)} reads, {len(distinct_x_values)} distinct X")
            last_progress = t1
        sleep_for = next_tick - time.perf_counter()
        if sleep_for > 0:
            time.sleep(sleep_for)

    print(f"  reads: {len(rtts_ms)}  timeouts: {timeouts}")
    if rtts_ms:
        print(
            f"  RTT ms: mean={statistics.mean(rtts_ms):.2f} "
            f"p50={statistics.median(rtts_ms):.2f} "
            f"p95={statistics.quantiles(rtts_ms, n=20)[18]:.2f} "
            f"max={max(rtts_ms):.2f}"
        )
    print(f"  distinct Mario X values seen: {len(distinct_x_values)}")
    if timeouts > POLL_DURATION_SEC * POLL_HZ * 0.01:
        print("FAIL: >1% timeouts. NCI cannot sustain 60Hz reliably.")
        return False
    if len(distinct_x_values) < 5:
        print("WARN: Mario X barely changed. Were you moving him? Re-run if not.")
    print("OK: 60Hz polling sustained.")
    return True


def test_runahead_coexistence(nci: NCI) -> bool:
    step("Step 4: Memory reads under runahead")
    print("Open RetroArch -> Settings -> Latency -> Run-Ahead.")
    print("Enable run-ahead, set 'Number of Frames' to 3, leave 'Use Second Instance' default.")
    if not confirm("Run-ahead = 3 enabled, game still playing, Mario moving?"):
        return False

    # Sample for 10 sec, look for stuck/garbage reads
    samples = []
    for _ in range(POLL_HZ * 10):
        data = nci.read_mem(getattr(nci, "mario_x_addr", ADDR_MARIO_X_LO), 2)
        if data is not None:
            samples.append(data[0] | (data[1] << 8))
        time.sleep(1.0 / POLL_HZ)

    if not samples:
        print("FAIL: no samples returned under runahead.")
        return False

    distinct = len(set(samples))
    print(f"  samples: {len(samples)}  distinct X values: {distinct}")
    if distinct < 5:
        print("FAIL: Mario X barely changed under runahead — reads may be stale or stuck.")
        return False
    print("OK: memory polling works under runahead.")
    return True


def test_savestate_roundtrip(nci: NCI) -> bool:
    step("Step 5: Savestate round-trip")
    if not confirm("Stand still in a known spot. Ready to save state?"):
        return False

    before = nci.read_mem(getattr(nci, "mario_x_addr", ADDR_MARIO_X_LO), 2)
    if before is None:
        print("FAIL: pre-save read failed.")
        return False
    x_before = before[0] | (before[1] << 8)
    print(f"  pre-save Mario X: {x_before}")

    nci.try_send("SAVE_STATE")
    print("  sent SAVE_STATE")
    time.sleep(1.0)

    print("Now move Mario to a clearly different spot. You have 5 seconds.")
    time.sleep(5)

    after_move = nci.read_mem(getattr(nci, "mario_x_addr", ADDR_MARIO_X_LO), 2)
    if after_move is None:
        print("FAIL: post-move read failed.")
        return False
    x_after_move = after_move[0] | (after_move[1] << 8)
    print(f"  post-move Mario X: {x_after_move}")

    if x_after_move == x_before:
        print("WARN: position unchanged. Did you move? Re-run if not.")

    nci.try_send("LOAD_STATE")
    print("  sent LOAD_STATE")
    time.sleep(1.0)

    after_load = nci.read_mem(getattr(nci, "mario_x_addr", ADDR_MARIO_X_LO), 2)
    if after_load is None:
        print("FAIL: post-load read failed.")
        return False
    x_after_load = after_load[0] | (after_load[1] << 8)
    print(f"  post-load Mario X: {x_after_load}")

    if abs(x_after_load - x_before) > 4:
        print(f"FAIL: load did not restore X (expected ~{x_before}, got {x_after_load}).")
        return False
    print("OK: save/load round-trip restored position.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    nci = NCI(args.host, args.port)
    tests = [
        test_version,
        test_memory_read,
        test_polling_60hz,
        test_runahead_coexistence,
        test_savestate_roundtrip,
    ]
    results = []
    for t in tests:
        ok = t(nci)
        results.append((t.__name__, ok))
        if not ok:
            print(f"\nStopped at {t.__name__}. Fix and re-run.")
            break

    print("\n=== Summary ===")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if all(ok for _, ok in results) and len(results) == len(tests):
        print("\nAll tests passed. RetroArch path is viable. Migrate.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
