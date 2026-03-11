#!/usr/bin/env python3
"""
SpinLab — Step 1 PoC test client

Connects to the Lua TCP server running in Mesen2 and tests:
1. ping/pong
2. Save current state
3. Load saved state
4. Save to custom path
5. Load from custom path

Usage:
    python scripts/test_poc.py
    python scripts/test_poc.py --host 127.0.0.1 --port 15482
"""

import socket
import argparse
import time
import sys


def send_cmd(sock: socket.socket, cmd: str) -> str:
    """Send a command and wait for response."""
    sock.sendall((cmd + "\n").encode())
    data = b""
    while b"\n" not in data:
        chunk = sock.recv(1024)
        if not chunk:
            raise ConnectionError("Connection closed")
        data += chunk
    return data.decode().strip()


def main():
    parser = argparse.ArgumentParser(description="SpinLab PoC test client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=15482)
    args = parser.parse_args()

    print(f"Connecting to {args.host}:{args.port}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((args.host, args.port))
        sock.settimeout(5.0)
    except ConnectionRefusedError:
        print("Connection refused. Is Mesen2 running with the SpinLab Lua script?")
        sys.exit(1)

    print("Connected!\n")

    tests = [
        ("Ping",       "ping", "pong"),
        ("Save state", "save", "ok:saved"),
    ]

    for name, cmd, expected in tests:
        resp = send_cmd(sock, cmd)
        status = "PASS" if resp == expected else f"FAIL (expected {expected!r}, got {resp!r})"
        print(f"{name}: {status}")

    print("\nWaiting 2s before load — play a bit to see the jump back...")
    time.sleep(2)

    resp = send_cmd(sock, "load")
    status = "PASS" if resp == "ok:loaded" else f"FAIL (expected 'ok:loaded', got {resp!r})"
    print(f"Load state: {status}")

    custom_path = "data/states/custom_test.mss"
    resp = send_cmd(sock, f"save:{custom_path}")
    print(f"Save custom path: {resp}")

    resp = send_cmd(sock, f"load:{custom_path}")
    print(f"Load custom path: {resp}")

    send_cmd(sock, "quit")
    sock.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
