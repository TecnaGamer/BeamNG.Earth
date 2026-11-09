-- Wire framing (docs/V1_SPEC.md "Transport"): 4-byte little-endian uint32 length + payload.
-- Pure Lua: lengths are parsed with string.byte math (ffi.cast is a stub in BeamNG's sandbox).
-- The reassembler accepts arbitrary chunk splits, as delivered by non-blocking receives.

local M = {}

local byte, char, floor = string.byte, string.char, math.floor

M.MAX_FRAME = 64 * 1024 * 1024 -- anything larger means a desynced or hostile stream

function M.encode(payload)
  local n = #payload
  return char(n % 256, floor(n / 256) % 256, floor(n / 65536) % 256, floor(n / 16777216) % 256) .. payload
end

local Reassembler = {}
Reassembler.__index = Reassembler

function Reassembler:new()
  -- `parts` collects chunks cheaply; they are joined only when a frame might be complete.
  return setmetatable({ buf = '', pos = 1, parts = {}, pending = 0, need = nil, err = nil }, Reassembler)
end

function M.newReassembler()
  return Reassembler:new()
end

function Reassembler:push(chunk)
  if chunk and #chunk > 0 then
    self.parts[#self.parts + 1] = chunk
    self.pending = self.pending + #chunk
  end
end

Reassembler.feed = Reassembler.push

-- Returns the next complete payload, or nil (need more data, or self.err is set).
function Reassembler:pop()
  if self.err then return nil end
  local have = (#self.buf - self.pos + 1) + self.pending
  if not self.need then
    if have < 4 then return nil end
  elseif have < 4 + self.need then
    return nil
  end
  -- Every copy here is paid for by the bytes already read past (amortised O(n)): the rest of the buffer cut off only
  -- once over half of it is read, the new chunks joined behind it in one concat. Before (2026-11-08) the rest was
  -- copied again after every 32 KB read and joined twice: a burst of small frames allocated 40-60x its size, much of
  -- the game's GE Lua garbage on a drive (300+ MB/min, the "net" part) and its collector's hitches.
  if self.pending > 0 then
    local parts = self.parts
    if self.pos > #self.buf then
      self.buf = table.concat(parts)
    else
      table.insert(parts, 1, self.pos > 1 and self.buf:sub(self.pos) or self.buf)
      self.buf = table.concat(parts)
    end
    self.pos = 1
    self.parts, self.pending = {}, 0
  elseif self.pos > 32768 and self.pos > #self.buf / 2 then
    self.buf = self.buf:sub(self.pos)
    self.pos = 1
  end
  if not self.need then
    local b1, b2, b3, b4 = byte(self.buf, self.pos, self.pos + 3)
    local n = b1 + b2 * 256 + b3 * 65536 + b4 * 16777216
    if n > M.MAX_FRAME then
      self.err = 'frame too large: ' .. n
      return nil
    end
    self.need = n
    if #self.buf - self.pos + 1 < 4 + n then return nil end
  end
  local n = self.need
  local payload = self.buf:sub(self.pos + 4, self.pos + 3 + n)
  self.pos = self.pos + 4 + n
  if self.pos > #self.buf then
    self.buf = ''
    self.pos = 1
  end
  self.need = nil
  return payload
end

function Reassembler:reset()
  self.buf, self.pos, self.parts, self.pending, self.need, self.err = '', 1, {}, 0, nil, nil
end

M.Reassembler = Reassembler

return M
