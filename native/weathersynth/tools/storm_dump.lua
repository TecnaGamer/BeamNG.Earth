-- The strike lattice dump from storm.lua itself, compared with `weathersynth storm-dump` (src/storm.rs) byte for byte.
-- usage: luajit tools/storm_dump.lua PATH/TO/storm.lua > lua.txt; weathersynth storm-dump > rust.txt; cmp lua.txt rust.txt
local path = arg[1]
local chunk = assert(loadfile(path))
-- storm.lua's pure part needs only `bit`; its runtime part reads engine globals inside functions (never called here)
local M = chunk()
local function g17(x)
  if x == 0 then return '0' end
  return string.format('%.17g', x)
end
local function num(a)
  if a == math.floor(a) and math.abs(a) < 1e15 then return string.format('%d', a) end
  return g17(a)
end
local out = {}
for _, a in ipairs({ -3, -1, 0, 1, 7, 123456, 4294967295, 4294967296, -4294967297, 2.5, -2.5 }) do
  out[#out + 1] = string.format('hash %s %d', num(a), M.hash(a))
  out[#out + 1] = string.format('hash2 %s %d', num(a), M.hash(a, 17, -5))
end
for c = 0, 3 do
  for _, i in ipairs({ -900, -1, 0, 1, 371, 899 }) do
    for _, j in ipairs({ -1800, -2, 0, 3, 1799 }) do
      for _, slot in ipairs({ 0, 1, 176400000, 176400001, 176412345 }) do
        local s = M.strikeAt(i, j, slot, c)
        if s then
          out[#out + 1] = string.format('strike %d %d %d %d %s %s %s %s %d %d %d', c, i, j, slot, g17(s.t), g17(s.lat),
            g17(s.lon), tostring(s.cg), s.pulses, s.variant, s.seed)
        end
      end
    end
  end
end
for _, p in ipairs({ { 52.52, 13.405 }, { -33.8688, 151.2093 } }) do
  for _, s in ipairs(M.strikesNear(p[1], p[2], 1764000000, 1764000600, 3)) do
    out[#out + 1] = string.format('near %s %s %s %s %d', g17(s.t), g17(s.lat), g17(s.lon), tostring(s.cg), s.seed)
  end
end
io.write(table.concat(out, '\n'), '\n')
