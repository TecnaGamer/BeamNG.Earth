-- luajit mod/tests/bench_farglobal_place.lua: what placing N global far tiles into a new anchor costs (the rebase
-- frame's share: farfield.transform -> placeGlobal per kept global tile). Not a test (bench_*: not run by the suite).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local farfield = require('beamngEarth/farfield')
local geodesy = require('beamngEarth/geodesy')
local geotile = require('beamngEarth/geotile')
local N = tonumber(arg[1]) or 250
local a = geodesy.newAnchor(35.46, -118.77)
local insts = {}
for k = 1, N do
  local z = 10 + k % 5
  local x, y = geotile.tileOf(35.46 + (k % 17) * 0.05, -118.77 + (k % 13) * 0.05, z)
  insts[k] = { global = { z = z, x = x, y = y, frame = geodesy.newAnchor(geotile.centre(z, x, y)), pz = 900 } }
end
local t0 = os.clock()
for _, inst in ipairs(insts) do farfield._placeGlobal(inst, a) end
print(string.format('%d global tiles placed in %.2f ms (%.1f us each)', N, (os.clock() - t0) * 1000,
  (os.clock() - t0) * 1e6 / N))
