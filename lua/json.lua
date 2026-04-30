-- json.lua — Minimal JSON helpers for Mesen2 Lua scripts.
-- No dependencies. All functions are global (loaded via dofile).

-- Serialize a Lua table to a JSON object string.
-- Supports string, number, boolean, and nested table values.
function to_json(t)
  local parts = {}
  for k, v in pairs(t) do
    local val
    if type(v) == "string" then
      val = '"' .. v:gsub('\\', '\\\\'):gsub('"', '\\"') .. '"'
    elseif type(v) == "boolean" then
      val = tostring(v)
    elseif type(v) == "table" then
      val = to_json(v)
    else
      val = tostring(v)
    end
    parts[#parts + 1] = '"' .. k .. '":' .. val
  end
  return "{" .. table.concat(parts, ",") .. "}"
end

-- Extract a string field from a flat JSON object string.
-- Handles backslash-escaped backslashes (e.g. Windows paths).
function json_get_str(json_str, key)
  local raw = json_str:match('"' .. key .. '"%s*:%s*"(.-)"[%s,}%]]')
  if not raw then return nil end
  -- unescape in the correct order: \\ -> \ first, then \" -> "
  return (raw:gsub('\\\\', '\\'):gsub('\\"', '"'))
end

-- Extract a number field from a flat JSON object string.
function json_get_num(json_str, key)
  return tonumber(json_str:match('"' .. key .. '"%s*:%s*(%d+)'))
end

-- Extract a boolean field from a flat JSON object string. Returns nil if absent.
function json_get_bool(json_str, key)
  local val = json_str:match('"' .. key .. '"%s*:%s*(%a+)')
  if val == "true" then return true
  elseif val == "false" then return false
  else return nil end
end

-- Extract a JSON array field as a raw substring.
-- Balances brackets to find the full array, even if nested.
-- Returns nil if the key is not found.
function json_get_arr(json_str, key)
  local start = json_str:find('"' .. key .. '"%s*:%s*%[')
  if not start then return nil end
  local arr_start = json_str:find('%[', start)
  if not arr_start then return nil end
  local depth = 0
  for i = arr_start, #json_str do
    local c = json_str:sub(i, i)
    if c == '[' then depth = depth + 1
    elseif c == ']' then
      depth = depth - 1
      if depth == 0 then
        return json_str:sub(arr_start, i)
      end
    end
  end
  return nil
end

-- Parse a JSON array of plain strings: ["L","Select"] -> {"L","Select"}
-- Fails loud (error) on malformed input so misconfiguration is obvious.
function parse_string_array(json_str)
  local body = json_str:match("^%s*%[(.*)%]%s*$")
  if not body then
    error("parse_string_array: expected JSON array, got: " .. tostring(json_str))
  end
  local result = {}
  for s in body:gmatch('"([^"]*)"') do
    result[#result + 1] = s
  end
  if #result == 0 then
    error("parse_string_array: no strings found in: " .. tostring(json_str))
  end
  return result
end
