#!/usr/bin/env bash
# SpinLab — Launch Harness
# Launches Mesen2 with the SpinLab Lua script.
# Usage: launch.sh [rom_path]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="$PROJECT_ROOT/config.yaml"

if [[ ! -f "$CONFIG" ]]; then
    echo "ERROR: config.yaml not found at $CONFIG"
    exit 1
fi

# Use Python to read config (already a dependency)
read_config() {
    python3 -c "import yaml; c=yaml.safe_load(open('$CONFIG')); print($1)"
}

MESEN_PATH="$(read_config "c['emulator']['path']")"
LUA_SCRIPT="$(read_config "c['emulator'].get('lua_script','')")"
ROM_PATH="$(read_config "c.get('rom',{}).get('path','')")"

# CLI arg overrides config ROM path
if [[ ${1:-} ]]; then
    ROM_PATH="$1"
fi

# Resolve relative lua script path
if [[ "$LUA_SCRIPT" != /* && "$LUA_SCRIPT" != ?:* ]]; then
    LUA_SCRIPT="$PROJECT_ROOT/$LUA_SCRIPT"
fi

# Validate
if [[ -z "$MESEN_PATH" ]]; then echo "ERROR: emulator.path not set in config.yaml"; exit 1; fi
if [[ ! -f "$MESEN_PATH" ]]; then echo "ERROR: Mesen not found at: $MESEN_PATH"; exit 1; fi
if [[ ! -f "$LUA_SCRIPT" ]]; then echo "ERROR: Lua script not found at: $LUA_SCRIPT"; exit 1; fi

echo "SpinLab — Launch Harness"
echo "  Mesen:  $MESEN_PATH"
echo "  Script: $LUA_SCRIPT"

if [[ -n "$ROM_PATH" && -f "$ROM_PATH" ]]; then
    echo "  ROM:    $ROM_PATH"
    "$MESEN_PATH" "$ROM_PATH" "$LUA_SCRIPT" &
else
    [[ -n "$ROM_PATH" ]] && echo "  ROM not found: $ROM_PATH — launching without ROM"
    echo "  ROM:    (none — load from Mesen UI)"
    "$MESEN_PATH" "$LUA_SCRIPT" &
fi
