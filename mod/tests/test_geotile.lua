-- luajit mod/tests/test_geotile.lua
-- geotile.lua: the global tile grid and the placement of a tile frame's content into an anchor's world. Agreement
-- with the server's Rust (keys and corners bit for bit) is server/tests/test_geotiles.py's (it runs this module).
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local g = require('beamngEarth/geodesy')
local gt = require('beamngEarth/geotile')

local LAT, LON = 39.2834, -76.5934

-- the grid: a point lies in its tile; the centre too; keys round trip
do
  local x, y = gt.tileOf(LAT, LON)
  local s, w, n, e = gt.bounds(17, x, y)
  T.check(s <= LAT and LAT < n and w <= LON and LON < e, 'the point in its tile')
  local cl, cn = gt.centre(17, x, y)
  T.check(s < cl and cl < n and w < cn and cn < e, 'the centre in the tile')
  T.check(gt.key(x, y) == 'g17_' .. x .. '_' .. y, 'key')
  local z, px, py = gt.parse(gt.key(x, y))
  T.check(z == 17 and px == x and py == y, 'parse')
  T.check(gt.parse('3_4') == nil and not gt.isKey('12_34'), 'an anchor tile key is no geotile key')
  local ox, oy = gt.tileOf(s + 1e-9, w + 1e-9)
  T.check(ox == x and oy == y, 'the SW corner inside')
  local ex, _ = gt.tileOf(cl, e + 1e-9)
  T.check(ex == x + 1, 'the east edge is the next tile')
end

-- the quad in an anchor 15 km away: ~237 m square, the point inside, quadDist 0 there, positive outside
do
  local a = g.newAnchor(LAT, LON + 15000 / (111320 * math.cos(math.rad(LAT))))
  local x, y = gt.tileOf(LAT, LON)
  local q = gt.quad(a, 17, x, y)
  local side = math.sqrt((q[2][1] - q[1][1]) ^ 2 + (q[2][2] - q[1][2]) ^ 2)
  T.check(side > 200 and side < 260, 'side ' .. side)
  local px, py = a:geodeticToWorld(LAT, LON, 0)
  T.near(gt.quadDist(q, px, py), 0, 0, 'inside')
  T.check(gt.quadDist(q, px + 1000, py) > 700, 'outside')
end

-- placement: exact at the pivot, within half a mm over the tile and 200 m of relief; the shear matters
do
  local x, y = gt.tileOf(LAT, LON)
  local tf = gt.frameOf({ key = gt.key(x, y) })
  local a = g.newAnchor(LAT, LON + 15000 / (111320 * math.cos(math.rad(LAT))))
  local pl = gt.placement(tf, a, 0, 0, 1650)
  local worst, rigidWorst = 0, 0
  for _, p in ipairs({ { 100, -80, 1700 }, { -120, 110, 1550 }, { 0, 0, 1650 }, { 5, 7, 1750 } }) do
    local wx, wy, wz = gt.worldMap(tf, a, p[1], p[2], p[3])
    local qx, qy, qz = gt.apply(pl, p[1], p[2], p[3])
    worst = math.max(worst, math.abs(qx - wx), math.abs(qy - wy), math.abs(qz - wz))
    local s = p[3] - pl.pz
    rigidWorst = math.max(rigidWorst, math.abs(qx - pl.c[1] * s - wx), math.abs(qy - pl.c[2] * s - wy))
  end
  T.check(worst < 5e-4, 'placed within half a mm: ' .. worst)
  T.check(rigidWorst > 0.1, 'without the shear: ' .. rigidWorst)
  -- between: the rigid move from one anchor's placement to another's
  local b = g.newAnchor(LAT + 0.05, LON)
  local pb = gt.placement(tf, b, 0, 0, 1650)
  local d = gt.between(pl, pb)
  local ax, ay, az = gt.apply(pl, 50, 60, 1650)
  local bx, by, bz = gt.apply(pb, 50, 60, 1650)
  local mx, my, mz = d:apply(ax, ay, az)
  T.check(math.abs(mx - bx) + math.abs(my - by) + math.abs(mz - bz) < 1e-3, 'between at the pivot height')
  T.check(math.abs(gt.yawOf(pl)) < 1, 'a small turn')
end

-- worldToGeodetic is exact (h too): round trips at 1650 m 15 km out
do
  local a = g.newAnchor(LAT, LON)
  local wx, wy, wz = a:geodeticToWorld(LAT + 0.01, LON + 0.15, 1650)
  local la, lo, h = a:worldToGeodetic(wx, wy, wz)
  T.near(la, LAT + 0.01, 1e-11, 'lat')
  T.near(lo, LON + 0.15, 1e-11, 'lon')
  T.near(h, 1650, 1e-6, 'h')
end

T.done('test_geotile')
