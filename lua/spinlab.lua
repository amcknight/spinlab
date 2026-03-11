-- SpinLab — Mesen2 Lua Script
-- Step 1: Save State Proof of Concept
--
-- Tests:
-- 1. Save current state to a file (keyboard: F5)
-- 2. Load state from a file (keyboard: F6)
-- 3. TCP server that accepts "save" and "load" commands from Python
--
-- After this works, we build the full spinlab.lua on top of it.

local socket = require("socket.core")

-----------------------------------------------------------------------
-- CONFIG
-----------------------------------------------------------------------
local TCP_PORT = 15482
local TCP_HOST = "127.0.0.1"
local STATE_DIR = emu.getScriptDataFolder() .. "/states"
local TEST_STATE_FILE = STATE_DIR .. "/test_state.mss"

-----------------------------------------------------------------------
-- STATE
-----------------------------------------------------------------------
local server = nil      -- TCP server socket
local client = nil      -- Connected TCP client
local initialized = false

-----------------------------------------------------------------------
-- HELPERS
-----------------------------------------------------------------------
local function log(msg)
  emu.log("[SpinLab] " .. msg)
end

local function ensure_dir(path)
  -- Mesen2 Lua has os.execute if I/O is enabled
  os.execute('mkdir -p "' .. path .. '"')
end

local function save_state_to_file(path)
  local data = emu.saveSavestate()
  if not data then
    log("ERROR: saveSavestate returned nil")
    return false
  end
  local f = io.open(path, "wb")
  if not f then
    log("ERROR: Could not open file for writing: " .. path)
    return false
  end
  f:write(data)
  f:close()
  log("Saved state to: " .. path .. " (" .. #data .. " bytes)")
  return true
end

local function load_state_from_file(path)
  local f = io.open(path, "rb")
  if not f then
    log("ERROR: Could not open file for reading: " .. path)
    return false
  end
  local data = f:read("*a")
  f:close()
  if not data or #data == 0 then
    log("ERROR: State file is empty: " .. path)
    return false
  end
  emu.loadSavestate(data)
  log("Loaded state from: " .. path .. " (" .. #data .. " bytes)")
  return true
end

-----------------------------------------------------------------------
-- TCP SERVER
-----------------------------------------------------------------------
local function init_tcp()
  server = socket.tcp()
  server:setoption("reuseaddr", true)
  local ok, err = server:bind(TCP_HOST, TCP_PORT)
  if not ok then
    log("ERROR: TCP bind failed: " .. tostring(err))
    return false
  end
  server:listen(1)
  server:settimeout(0)  -- non-blocking
  log("TCP server listening on " .. TCP_HOST .. ":" .. TCP_PORT)
  return true
end

local function handle_tcp()
  -- Accept new connections
  if not client then
    local c = server:accept()
    if c then
      c:settimeout(0)
      client = c
      log("TCP client connected")
    end
  end

  -- Read from connected client
  if client then
    local line, err = client:receive("*l")
    if line then
      log("TCP received: " .. line)
      -- Simple command protocol for PoC
      if line == "save" then
        local ok = save_state_to_file(TEST_STATE_FILE)
        client:send(ok and "ok:saved\n" or "err:save_failed\n")
      elseif line == "load" then
        local ok = load_state_from_file(TEST_STATE_FILE)
        client:send(ok and "ok:loaded\n" or "err:load_failed\n")
      elseif line:sub(1, 5) == "load:" then
        -- load:/path/to/state.mss
        local path = line:sub(6)
        local ok = load_state_from_file(path)
        client:send(ok and "ok:loaded\n" or "err:load_failed\n")
      elseif line:sub(1, 5) == "save:" then
        -- save:/path/to/state.mss
        local path = line:sub(6)
        local ok = save_state_to_file(path)
        client:send(ok and "ok:saved\n" or "err:save_failed\n")
      elseif line == "ping" then
        client:send("pong\n")
      elseif line == "quit" then
        client:send("bye\n")
        client:close()
        client = nil
        log("TCP client disconnected")
      else
        client:send("err:unknown_command\n")
      end
    elseif err == "closed" then
      log("TCP client disconnected")
      client = nil
    end
    -- "timeout" is normal for non-blocking, just means no data ready
  end
end

-----------------------------------------------------------------------
-- KEYBOARD SHORTCUTS (for manual testing)
-----------------------------------------------------------------------
local function check_keyboard()
  -- F5 = save, F6 = load
  if emu.isKeyPressed("F5") then
    save_state_to_file(TEST_STATE_FILE)
  end
  if emu.isKeyPressed("F6") then
    load_state_from_file(TEST_STATE_FILE)
  end
end

-----------------------------------------------------------------------
-- MAIN FRAME CALLBACK
-----------------------------------------------------------------------
local function on_start_frame()
  if not initialized then
    ensure_dir(STATE_DIR)
    init_tcp()
    initialized = true
    log("SpinLab initialized (Step 1 — PoC)")
  end

  check_keyboard()
  handle_tcp()

  -- Draw a small indicator so we know the script is running
  emu.drawString(2, 2, "SpinLab PoC", 0xFFFFFF, 0x000000, 1)
end

-- Register the callback
emu.addEventCallback(on_start_frame, emu.eventType.startFrame)
log("SpinLab script loaded — registering startFrame callback")
