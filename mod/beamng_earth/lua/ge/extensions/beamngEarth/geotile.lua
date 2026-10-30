-- Origin-independent tiles ("geotiles", the server's geotiles.py / earthfast geotiles.rs): content made once per tile of
-- a global Web-Mercator grid (zoom 17: ~240 m at 39 deg) in the tile's own frame (the ENU plane at its centre, in the
-- world convention: x, y of a place at h = 0, z at its height). The tile's key ('g17_x_y') names no anchor, so the
-- mod keeps a tile across rebases and places it into the current anchor's world itself:
--   p' = R p + t + c (p.z - pivot.z)
-- (placement: the rigid move between the frames made exact at the tile's pivot, and the shear c that carries a point's
-- height difference from it: 23 cm per 100 m of relief at 15 km). The grid's arithmetic is the server's, operation for
-- operation (keys and corners agree bit for bit: server/tests/test_geotiles.py runs both).
-- Pure Lua (geodesy.lua only), so it runs under plain luajit for tests.
local M = {}

local geodesy = require('beamngEarth/geodesy')

local floor, pi, log, tan, cos, atan, sinh = math.floor, math.pi, math.log, math.tan, math.cos, math.atan, math.sinh
M.Z = 17
M.MAX_LAT = 85.0511287798066
M.SHEAR_DZ = 50.0       -- m either side of the pivot the shear is measured over (geotiles.rs placement_of)

-- The tile (x, y) of lat / lon at zoom z (geotiles.rs tile_of).
function M.tileOf(lat, lon, z)
  z = z or M.Z
  local n = 2 ^ z
  local la = math.max(-M.MAX_LAT, math.min(M.MAX_LAT, lat)) * pi / 180.0
  local xf = (lon + 180.0) / 360.0 * n
  local yf = (1.0 - log(tan(la) + 1.0 / cos(la)) / pi) / 2.0 * n
  local top = n - 1
  return math.max(0, math.min(top, floor(xf))), math.max(0, math.min(top, floor(yf)))
end

function M.latOf(yf, z)
  local n = 2 ^ (z or M.Z)
  return atan(sinh(pi * (1.0 - 2.0 * yf / n))) * 180.0 / pi
end

function M.lonOf(xf, z)
  local n = 2 ^ (z or M.Z)
  return xf / n * 360.0 - 180.0
end

-- (lat, lon) of a tile's centre: its frame's origin.
function M.centre(z, x, y)
  return M.latOf(y + 0.5, z), M.lonOf(x + 0.5, z)
end

-- (south, west, north, east) of a tile.
function M.bounds(z, x, y)
  return M.latOf(y + 1.0, z), M.lonOf(x, z), M.latOf(y, z), M.lonOf(x + 1.0, z)
end

function M.key(x, y, z)
  return string.format('g%d_%d_%d', z or M.Z, x, y)
end

-- (z, x, y) of a key, or nil.
function M.parse(key)
  local z, x, y = tostring(key):match('^g(%d+)_(%-?%d+)_(%-?%d+)$')
  if not z then return nil end
  return tonumber(z), tonumber(x), tonumber(y)
end

function M.isKey(key)
  return M.parse(key) ~= nil
end

-- The tile's corners in anchor a's world at h = 0: { {x, y} SW, SE, NE, NW } (geotiles.rs quad).
function M.quad(a, z, x, y)
  local s, w, n, e = M.bounds(z, x, y)
  local q = {}
  for k, ll in ipairs({ { s, w }, { s, e }, { n, e }, { n, w } }) do
    local px, py = a:geodeticToWorld(ll[1], ll[2], 0)
    q[k] = { px, py }
  end
  return q
end

-- m from (px, py) to a convex quad (0 inside).
function M.quadDist(q, px, py)
  local inside, best, sign = true, math.huge, 0
  for k = 1, 4 do
    local a, b = q[k], q[k % 4 + 1]
    local dx, dy = b[1] - a[1], b[2] - a[2]
    local cr = dx * (py - a[2]) - dy * (px - a[1])
    if cr ~= 0 then
      local sg = cr > 0 and 1 or -1
      if sign == 0 then sign = sg elseif sg ~= sign then inside = false end
    end
    local l2 = math.max(dx * dx + dy * dy, 1e-12)
    local t = math.max(0, math.min(1, ((px - a[1]) * dx + (py - a[2]) * dy) / l2))
    local ex, ey = px - (a[1] + t * dx), py - (a[2] + t * dy)
    best = math.min(best, math.sqrt(ex * ex + ey * ey))
  end
  return inside and 0 or best
end

-- One point of src's world in dst's world (anchors; exact: world -> lat / lon / h -> world).
function M.worldMap(src, dst, x, y, z)
  return dst:geodeticToWorld(src:worldToGeodetic(x, y, z))
end

-- The placement of src's (a tile frame's) content in dst's world at pivot (src world): { r (9, row-major), t (3),
-- c (3), pz } with p' = R p + t + c (p.z - pz) (geotiles.rs placement_of).
function M.placement(src, dst, px, py, pz)
  px, py, pz = px or 0, py or 0, pz or 0
  local xf = src:worldTransformTo(dst, px, py, pz)
  local r, t = xf.r, xf.t
  local d = M.SHEAR_DZ
  local ux, uy, uz = M.worldMap(src, dst, px, py, pz + d)
  local dx, dy, dz = M.worldMap(src, dst, px, py, pz - d)
  local c = { (ux - dx) / (2 * d) - r[3], (uy - dy) / (2 * d) - r[6], (uz - dz) / (2 * d) - r[9] }
  return { r = r, t = t, c = c, pz = pz }
end

-- A point placed.
function M.apply(pl, x, y, z)
  local r, t, c = pl.r, pl.t, pl.c
  local s = z - pl.pz
  return r[1] * x + r[2] * y + r[3] * z + t[1] + c[1] * s,
         r[4] * x + r[5] * y + r[6] * z + t[2] + c[2] * s,
         r[7] * x + r[8] * y + r[9] * z + t[3] + c[3] * s
end

-- The heading change (degrees, counter-clockwise) a placement turns a yaw by (the rotation's x axis in the plane).
function M.yawOf(pl)
  return math.deg(math.atan2(pl.r[4], pl.r[1]))
end

-- The rigid move from placement a's world to placement b's (both of one tile into two anchors): b * a^-1 without the
-- shears ({ r, t } as geodesy's Transform: :apply, :beamngQuat): what a Forest made with a takes to show as b.
function M.between(a, b)
  local ra, rb = a.r, b.r
  local r = {}
  for i = 0, 2 do
    for j = 0, 2 do
      -- (b.r * a.r^T)[i][j] = sum_k b[i][k] a[j][k]
      r[i * 3 + j + 1] = rb[i * 3 + 1] * ra[j * 3 + 1] + rb[i * 3 + 2] * ra[j * 3 + 2] + rb[i * 3 + 3] * ra[j * 3 + 3]
    end
  end
  local ta, tb = a.t, b.t
  local t = {}
  for i = 0, 2 do
    t[i + 1] = tb[i + 1] - (r[i * 3 + 1] * ta[1] + r[i * 3 + 2] * ta[2] + r[i * 3 + 3] * ta[3])
  end
  return geodesy.transformOf(r, t)
end

-- The frame (an anchor) of a tile from the server's message (frame = { lat, lon }), or of its key.
function M.frameOf(msg)
  local f = type(msg) == 'table' and msg.frame
  if type(f) == 'table' and tonumber(f[1]) and tonumber(f[2]) then
    return geodesy.newAnchor(tonumber(f[1]), tonumber(f[2]))
  end
  local z, x, y = M.parse(type(msg) == 'table' and msg.key or msg)
  if not z then return nil end
  return geodesy.newAnchor(M.centre(z, x, y))
end

return M
