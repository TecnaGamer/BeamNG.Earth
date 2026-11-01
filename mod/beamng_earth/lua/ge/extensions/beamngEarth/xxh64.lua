-- XXH64 (seed 0) of a string, as 16 lower-case hex digits: the game's terrain texture cache names its packed textures
-- by it (server/terraincache.py base_key: the photo's path three times and the ao map's), and the fine ground's
-- material takes the block photo from that sRGB BC7 file (terrain.lua fineTexture). LuaJIT's uint64 arithmetic wraps
-- mod 2^64 as XXH64 wants.
local M = {}
local ffi = require('ffi')
local bit = require('bit')
local U = ffi.typeof('uint64_t')
local P1, P2, P3, P4, P5 = 11400714785074694791ULL, 14029467366897019727ULL, 1609587929392839161ULL,
  9650029242287828579ULL, 2870177450012600261ULL
local function rotl(x, r) return bit.bor(bit.lshift(x, r), bit.rshift(x, 64 - r)) end
local function rd(s, i, n)       -- little-endian n bytes at s[i] (1-based) as uint64
  local v = U(0)
  for k = n - 1, 0, -1 do v = bit.bor(bit.lshift(v, 8), U(s:byte(i + k))) end
  return v
end
local function round(acc, v) acc = acc + v * P2 return rotl(acc, 31) * P1 end
local function merge(acc, v) acc = bit.bxor(acc, round(U(0), v)) return acc * P1 + P4 end
function M.hex(s)
  local n, i, h = #s, 1
  if n >= 32 then
    local v1, v2, v3, v4 = P1 + P2, P2, U(0), U(0) - P1
    while i + 31 <= n do
      v1, v2, v3, v4 = round(v1, rd(s, i, 8)), round(v2, rd(s, i + 8, 8)), round(v3, rd(s, i + 16, 8)), round(v4, rd(s, i + 24, 8))
      i = i + 32
    end
    h = rotl(v1, 1) + rotl(v2, 7) + rotl(v3, 12) + rotl(v4, 18)
    h = merge(merge(merge(merge(h, v1), v2), v3), v4)
  else
    h = P5 + 0ULL
  end
  h = h + U(n)
  while i + 7 <= n do
    h = bit.bxor(h, round(U(0), rd(s, i, 8)))
    h = rotl(h, 27) * P1 + P4
    i = i + 8
  end
  if i + 3 <= n then
    h = bit.bxor(h, rd(s, i, 4) * P1)
    h = rotl(h, 23) * P2 + P3
    i = i + 4
  end
  while i <= n do
    h = bit.bxor(h, U(s:byte(i)) * P5)
    h = rotl(h, 11) * P1
    i = i + 1
  end
  h = bit.bxor(h, bit.rshift(h, 33)); h = h * P2
  h = bit.bxor(h, bit.rshift(h, 29)); h = h * P3
  h = bit.bxor(h, bit.rshift(h, 32))
  return bit.tohex(h, 16)
end

return M
