-- spinrec.lua — .spinrec binary format I/O and SNES input encoding.
-- All functions are global (loaded via dofile).
-- Depends on global log() from spinlab.lua.
--
-- .spinrec format: 32-byte header + one uint16/frame (SNES joypad bitmask).
-- Header: SREC (4) + version (2) + game_id (16) + frame_count (4) + reserved (6)

function flush_spinrec(path, game_id_str, buffer)
  local f = io.open(path, "wb")
  if not f then
    log("ERROR: Cannot write spinrec: " .. path)
    return false
  end
  -- Magic
  f:write("SREC")
  -- Version (uint16 LE)
  f:write(string.char(1, 0))
  -- Game ID (16 bytes ASCII, pad with zeros if shorter)
  local gid = (game_id_str or ""):sub(1, 16)
  f:write(gid .. string.rep("\0", 16 - #gid))
  -- Frame count (uint32 LE)
  local n = #buffer
  f:write(string.char(n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >> 24) & 0xFF))
  -- Reserved (6 zeros)
  f:write(string.rep("\0", 6))
  -- Body: 2 bytes per frame (uint16 LE)
  for _, mask in ipairs(buffer) do
    f:write(string.char(mask & 0xFF, (mask >> 8) & 0xFF))
  end
  f:close()
  log("Wrote spinrec: " .. path .. " (" .. n .. " frames)")
  return true
end

function read_spinrec(path)
  local f = io.open(path, "rb")
  if not f then return nil, "file not found: " .. path end
  local data = f:read("*a")
  f:close()
  if #data < 32 then return nil, "file too short" end
  -- Validate magic
  if data:sub(1, 4) ~= "SREC" then return nil, "bad magic" end
  -- Parse header
  local b = function(i) return data:byte(i) end
  local frame_count = b(23) + b(24) * 256 + b(25) * 65536 + b(26) * 16777216
  local gid = data:sub(7, 22)
  -- Validate body length
  local expected_body = frame_count * 2
  if #data - 32 < expected_body then return nil, "body truncated" end
  -- Parse frames
  local frames = {}
  for i = 1, frame_count do
    local offset = 32 + (i - 1) * 2
    frames[i] = b(offset + 1) + b(offset + 2) * 256
  end
  return {game_id = gid, frame_count = frame_count, frames = frames}, nil
end

-- Input bitmask encoding: matches SNES joypad register layout
local INPUT_BITS = {
  b = 0, y = 1, select = 2, start = 3,
  up = 4, down = 5, left = 6, right = 7,
  a = 8, x = 9, l = 10, r = 11,
}

function encode_input(tbl)
  local mask = 0
  for name, bit in pairs(INPUT_BITS) do
    if tbl[name] then mask = mask + (1 << bit) end
  end
  return mask
end

function decode_input(mask)
  local tbl = {}
  for name, bit in pairs(INPUT_BITS) do
    tbl[name] = (mask & (1 << bit)) ~= 0
  end
  return tbl
end
