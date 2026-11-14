-- A small JSON encoder / decoder for the tests (the BeamMP server's Util.JsonEncode / JsonDecode stand-in): objects,
-- arrays (a table with t[1] or marked empty with EMPTY_ARRAY), strings, numbers, booleans, null.
local J = {}
J.EMPTY_ARRAY = setmetatable({}, { __tostring = function() return '[]' end })

local function esc(s)
  return '"' .. s:gsub('[%c"\\]', function(c)
    local m = { ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
    return m[c] or string.format('\\u%04x', c:byte())
  end) .. '"'
end

function J.encode(v)
  local t = type(v)
  if v == nil then return 'null' end
  if t == 'boolean' then return tostring(v) end
  if t == 'number' then
    if v ~= v or v == math.huge or v == -math.huge then return 'null' end
    if v == math.floor(v) and math.abs(v) < 2 ^ 53 then return string.format('%d', v) end
    return string.format('%.17g', v)
  end
  if t == 'string' then return esc(v) end
  if t == 'table' then
    if v == J.EMPTY_ARRAY then return '[]' end
    if v[1] ~= nil or next(v) == nil then
      local out = {}
      for i = 1, #v do out[i] = J.encode(v[i]) end
      return '[' .. table.concat(out, ',') .. ']'
    end
    local keys = {}
    for k in pairs(v) do keys[#keys + 1] = tostring(k) end
    table.sort(keys)
    local out = {}
    for _, k in ipairs(keys) do
      local val = v[k]
      if val == nil then val = v[tonumber(k)] end
      out[#out + 1] = esc(k) .. ':' .. J.encode(val)
    end
    return '{' .. table.concat(out, ',') .. '}'
  end
  error('cannot encode ' .. t)
end

function J.decode(s)
  local i = 1
  local function ws() i = s:find('[^ \t\r\n]', i) or #s + 1 end
  local value
  local function str()
    local out = {}
    i = i + 1
    while true do
      local c = s:sub(i, i)
      if c == '' then error('unterminated string') end
      if c == '"' then i = i + 1 return table.concat(out) end
      if c == '\\' then
        local e = s:sub(i + 1, i + 1)
        local m = { b = '\b', f = '\f', n = '\n', r = '\r', t = '\t', ['"'] = '"', ['\\'] = '\\', ['/'] = '/' }
        if e == 'u' then
          out[#out + 1] = string.char(tonumber(s:sub(i + 2, i + 5), 16) % 256)
          i = i + 6
        else
          out[#out + 1] = m[e] or e
          i = i + 2
        end
      else
        local j = s:find('["\\]', i) or #s + 1
        out[#out + 1] = s:sub(i, j - 1)
        i = j
      end
    end
  end
  function value()
    ws()
    local c = s:sub(i, i)
    if c == '{' then
      local t = {}
      i = i + 1
      ws()
      if s:sub(i, i) == '}' then i = i + 1 return t end
      while true do
        ws()
        local k = str()
        ws()
        if s:sub(i, i) ~= ':' then error('expected : at ' .. i) end
        i = i + 1
        t[k] = value()
        ws()
        local d = s:sub(i, i)
        i = i + 1
        if d == '}' then return t end
        if d ~= ',' then error('expected , at ' .. i) end
      end
    elseif c == '[' then
      local t = {}
      i = i + 1
      ws()
      if s:sub(i, i) == ']' then i = i + 1 return t end
      while true do
        t[#t + 1] = value()
        ws()
        local d = s:sub(i, i)
        i = i + 1
        if d == ']' then return t end
        if d ~= ',' then error('expected , at ' .. i) end
      end
    elseif c == '"' then return str()
    elseif s:sub(i, i + 3) == 'true' then i = i + 4 return true
    elseif s:sub(i, i + 4) == 'false' then i = i + 5 return false
    elseif s:sub(i, i + 3) == 'null' then i = i + 4 return nil
    else
      local num = s:match('^-?%d+%.?%d*[eE]?[-+]?%d*', i)
      if not num or num == '' then error('bad json at ' .. i .. ': ' .. s:sub(i, i + 10)) end
      i = i + #num
      return tonumber(num)
    end
  end
  local v = value()
  return v
end

return J
