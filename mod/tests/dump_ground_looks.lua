-- luajit mod/tests/dump_ground_looks.lua  (server/tests/test_terrainprefill.py: the ground looks terrain.lua can give a
-- TerrainMaterial, M.LOOKS and M.TILE_FALLBACK, one "table<TAB>material<TAB>field<TAB>value" line per texture field,
-- for the server's terrain cache prefill to be checked against)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local terrain = require('beamngEarth/terrain')
local rows = {}
for tname, t in pairs({ LOOKS = terrain.LOOKS, TILE_FALLBACK = terrain.TILE_FALLBACK }) do
  for name, look in pairs(t) do
    for k, v in pairs(look) do
      if k:sub(-3) == 'Tex' then rows[#rows + 1] = tname .. '\t' .. name .. '\t' .. k .. '\t' .. tostring(v) end
    end
  end
end
table.sort(rows)
for _, r in ipairs(rows) do print(r) end
