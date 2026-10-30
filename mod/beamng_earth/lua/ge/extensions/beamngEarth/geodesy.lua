-- WGS84 geodesy in Lua doubles: geodetic <-> ECEF <-> local ENU of an anchor.
-- Pure Lua (no engine globals), so it runs under plain luajit for tests.
-- World frame (docs/V1_SPEC.md "World frame"): ENU of the anchor (lat0, lon0, h=0),
-- +x east, +y north, +z up along the ellipsoid normal, metres.

local M = {}

local sin, cos, sqrt, atan2, rad, deg = math.sin, math.cos, math.sqrt, math.atan2, math.rad, math.deg

local A = 6378137.0                 -- semi-major axis
local F = 1 / 298.257223563         -- flattening
local E2 = F * (2 - F)              -- first eccentricity squared
M.A, M.F, M.E2 = A, F, E2

function M.geodeticToEcef(latDeg, lonDeg, h)
  local lat, lon = rad(latDeg), rad(lonDeg)
  local sl, cl = sin(lat), cos(lat)
  local n = A / sqrt(1 - E2 * sl * sl)
  local r = (n + (h or 0)) * cl
  return r * cos(lon), r * sin(lon), (n * (1 - E2) + (h or 0)) * sl
end

-- Fixed-point iteration on latitude. The height formula
-- h = p*cos(lat) + z*sin(lat) - a*sqrt(1 - e2*sin^2(lat)) stays well-conditioned at the
-- poles (unlike p/cos(lat) - N). Six iterations give sub-millimetre accuracy near the surface.
function M.ecefToGeodetic(x, y, z)
  local p = sqrt(x * x + y * y)
  local lon = atan2(y, x)
  local lat = atan2(z, p * (1 - E2))
  for _ = 1, 6 do
    local sl = sin(lat)
    local n = A / sqrt(1 - E2 * sl * sl)
    lat = atan2(z + E2 * n * sl, p)
  end
  local sl, cl = sin(lat), cos(lat)
  local h = p * cl + z * sl - A * sqrt(1 - E2 * sl * sl)
  return deg(lat), deg(lon), h
end

-- An anchor = the ENU frame at (lat0, lon0, 0). Methods take/return plain numbers.
local Anchor = {}
Anchor.__index = Anchor

function M.newAnchor(latDeg, lonDeg)
  local lat, lon = rad(latDeg), rad(lonDeg)
  local sl, cl, so, co = sin(lat), cos(lat), sin(lon), cos(lon)
  local ox, oy, oz = M.geodeticToEcef(latDeg, lonDeg, 0)
  return setmetatable({
    lat = latDeg, lon = lonDeg,
    ox = ox, oy = oy, oz = oz,
    -- rows of the ECEF->ENU rotation (east, north, up unit vectors in ECEF)
    ex = -so,      ey = co,       ez = 0,
    nx = -sl * co, ny = -sl * so, nz = cl,
    ux = cl * co,  uy = cl * so,  uz = sl,
  }, Anchor)
end

function Anchor:ecefToEnu(x, y, z)
  local dx, dy, dz = x - self.ox, y - self.oy, z - self.oz
  return self.ex * dx + self.ey * dy + self.ez * dz,
         self.nx * dx + self.ny * dy + self.nz * dz,
         self.ux * dx + self.uy * dy + self.uz * dz
end

function Anchor:enuToEcef(e, n, u)
  return self.ox + self.ex * e + self.nx * n + self.ux * u,
         self.oy + self.ey * e + self.ny * n + self.uy * u,
         self.oz + self.ez * e + self.nz * n + self.uz * u
end

function Anchor:enuToGeodetic(e, n, u)
  return M.ecefToGeodetic(self:enuToEcef(e, n, u))
end

function Anchor:geodeticToEnu(latDeg, lonDeg, h)
  return self:ecefToEnu(M.geodeticToEcef(latDeg, lonDeg, h))
end

-- The world's convention (the server's ground, roads and outlines): a place's x, y are those of its point at h = 0, its
-- z that of the point at its height. At its height a point lands h x distance / earth radius farther out (2-3 m at
-- 1650 m, 13 km from the anchor), so a lat / lon read from (x, y, z) with z in it missed the road it stood on.
function Anchor:worldToGeodetic(x, y, z)
  -- (the place whose h = 0 point has these x, y: the plane point (x, y, 0) lies d^2 / 2R over the ellipsoid, its
  -- lat / lon cm off at 15 km; two corrections make it exact to well under a mm)
  local qx, qy = x, y
  local lat, lon = self:enuToGeodetic(qx, qy, 0)
  for _ = 1, 2 do
    local ex, ey = self:geodeticToEnu(lat, lon, 0)
    qx, qy = qx + (x - ex), qy + (y - ey)
    lat, lon = self:enuToGeodetic(qx, qy, 0)
  end
  -- h whose up is z (Newton on the up, whose rate is the cosine between this frame's up and the place's normal; the
  -- plane point's own height was h x d^2 / R^2 off: 9 mm at 1650 m and 15 km)
  local _, _, h = self:enuToGeodetic(x, y, z or 0)
  local sl, cl = sin(rad(lat)), cos(rad(lat))
  local rate = self.ux * cl * cos(rad(lon)) + self.uy * cl * sin(rad(lon)) + self.uz * sl
  for _ = 1, 2 do
    local _, _, u = self:geodeticToEnu(lat, lon, h)
    h = h + ((z or 0) - u) / rate
  end
  return lat, lon, h
end

function Anchor:geodeticToWorld(latDeg, lonDeg, h)
  local x, y = self:geodeticToEnu(latDeg, lonDeg, 0)
  local _, _, z = self:geodeticToEnu(latDeg, lonDeg, h or 0)
  return x, y, z
end

-- True north at a point (e, n) of this frame, as a unit direction (x, y) of the frame's ground plane. The frame's +y
-- is north only at the anchor; elsewhere the meridian turns by about d * tan(lat) / R (0.1 deg 15 km off at 39 deg,
-- 1.5 deg at 85 deg, any direction near a pole, where north is toward the pole).
function Anchor:northAt(e, n)
  local lat, lon = M.ecefToGeodetic(self:enuToEcef(e, n, 0))
  local sl, cl = sin(rad(lat)), cos(rad(lat))
  local so, co = sin(rad(lon)), cos(rad(lon))
  local nx, ny, nz = -sl * co, -sl * so, cl
  local x = self.ex * nx + self.ey * ny + self.ez * nz
  local y = self.nx * nx + self.ny * ny + self.nz * nz
  local len = sqrt(x * x + y * y)
  if len < 1e-9 then return 0, 1 end
  return x / len, y / len
end

-- Coordinates of a point given in `self`'s ENU frame, expressed in `other`'s ENU frame
-- (ENU_old -> ECEF -> ENU_new, docs/V1_SPEC.md "Floating origin / rebase").
function Anchor:toFrame(other, e, n, u)
  return other:ecefToEnu(self:enuToEcef(e, n, u))
end

-- Rigid transform from `self`'s ENU frame into `other`'s: p_new = R*p_old + t, exact in doubles
-- (ENU_old -> ECEF -> ENU_new, docs/V1_1_SPEC.md 1b). With E = the ECEF->ENU rotation (rows e, n, u)
-- and O = the anchor origin in ECEF: R = E_new * E_old^T and t = E_new * (O_old - O_new).
-- R is a rotation by ~d/R_earth (0.09 deg at 10 km), which must not be dropped: far tiles reach 80 km.
local Transform = {}
Transform.__index = Transform

function Anchor:transformTo(other)
  local a, b = self, other
  local ea = { { a.ex, a.ey, a.ez }, { a.nx, a.ny, a.nz }, { a.ux, a.uy, a.uz } }
  local eb = { { b.ex, b.ey, b.ez }, { b.nx, b.ny, b.nz }, { b.ux, b.uy, b.uz } }
  local r = {}
  for i = 1, 3 do
    for j = 1, 3 do
      r[(i - 1) * 3 + j] = eb[i][1] * ea[j][1] + eb[i][2] * ea[j][2] + eb[i][3] * ea[j][3]
    end
  end
  local dx, dy, dz = a.ox - b.ox, a.oy - b.oy, a.oz - b.oz
  local t = {
    eb[1][1] * dx + eb[1][2] * dy + eb[1][3] * dz,
    eb[2][1] * dx + eb[2][2] * dy + eb[2][3] * dz,
    eb[3][1] * dx + eb[3][2] * dy + eb[3][3] * dz,
  }
  return setmetatable({ r = r, t = t }, Transform)
end

-- transformTo, its translation corrected at the place (x, y, z) of this frame so that place lands where the other
-- frame's world puts it (worldToGeodetic / geodeticToWorld: x, y at h = 0). The worlds of two anchors differ by more
-- than a rigid move: a place at height h sits h x distance / earth radius from its rigid image (4 m at 1650 m and
-- 15 km, a rebase on a high plateau); what is near (x, y, z) moves with it, as the rigid transform moves it.
function Anchor:worldTransformTo(other, x, y, z)
  local xf = self:transformTo(other)
  if self.lat == other.lat and self.lon == other.lon then return xf end    -- (the same world: as it is)
  local rx, ry, rz = xf:apply(x, y, z)
  local wx, wy, wz = other:geodeticToWorld(self:worldToGeodetic(x, y, z))
  xf.t[1], xf.t[2], xf.t[3] = xf.t[1] + wx - rx, xf.t[2] + wy - ry, xf.t[3] + wz - rz
  return xf
end

-- A Transform from a rotation r (9 numbers, row-major) and a translation t (3).
function M.transformOf(r, t)
  return setmetatable({ r = r, t = t }, Transform)
end

-- A point of the old frame in the new frame.
function Transform:apply(x, y, z)
  local r, t = self.r, self.t
  return r[1] * x + r[2] * y + r[3] * z + t[1],
         r[4] * x + r[5] * y + r[6] * z + t[2],
         r[7] * x + r[8] * y + r[9] * z + t[3]
end

-- A direction (velocity, axis) of the old frame in the new frame: rotation only.
function Transform:rotate(x, y, z)
  local r = self.r
  return r[1] * x + r[2] * y + r[3] * z, r[4] * x + r[5] * y + r[6] * z, r[7] * x + r[8] * y + r[9] * z
end

-- Inverse transform (new frame -> old frame): R^T, -R^T t.
function Transform:inverse()
  local r, t = self.r, self.t
  local ri = { r[1], r[4], r[7], r[2], r[5], r[8], r[3], r[6], r[9] }
  return setmetatable({ r = ri, t = {
    -(ri[1] * t[1] + ri[2] * t[2] + ri[3] * t[3]),
    -(ri[4] * t[1] + ri[5] * t[2] + ri[6] * t[3]),
    -(ri[7] * t[1] + ri[8] * t[2] + ri[9] * t[3]),
  } }, Transform)
end

-- Unit quaternion (x, y, z, w) of R in the standard (Hamilton, active) convention:
-- v' = q v q*. Shepperd's method, stable for every rotation.
function M.matToQuat(r)
  local m00, m01, m02, m10, m11, m12, m20, m21, m22 = r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9]
  local tr = m00 + m11 + m22
  local x, y, z, w
  if tr > 0 then
    local s = sqrt(tr + 1) * 2
    w, x, y, z = 0.25 * s, (m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s
  elseif m00 > m11 and m00 > m22 then
    local s = sqrt(1 + m00 - m11 - m22) * 2
    w, x, y, z = (m21 - m12) / s, 0.25 * s, (m01 + m10) / s, (m02 + m20) / s
  elseif m11 > m22 then
    local s = sqrt(1 + m11 - m00 - m22) * 2
    w, x, y, z = (m02 - m20) / s, (m01 + m10) / s, 0.25 * s, (m12 + m21) / s
  else
    local s = sqrt(1 + m22 - m00 - m11) * 2
    w, x, y, z = (m10 - m01) / s, (m02 + m20) / s, (m12 + m21) / s, 0.25 * s
  end
  local n = sqrt(x * x + y * y + z * z + w * w)
  return x / n, y / n, z / n, w / n
end

function Transform:quat() return M.matToQuat(self.r) end

-- The same rotation as a BeamNG Lua/engine quaternion. BeamNG's `quat * vec3`
-- (common/mathlib.lua LuaQuat.__mul: v - 2w(q x v) + 2 q x (q x v)) rotates by the CONJUGATE of
-- the standard formula, e.g. setRotationFromTo stores -w (mathlib.lua:1038-1049). So the BeamNG
-- quaternion that maps v to R*v is the conjugate of the standard one. This is the form that
-- setClusterPosRelRot / setPosRot / setOriginalTransform take (spawn.lua:520-535 applies the same
-- diffRot to a vec3 in Lua and hands it to setClusterPosRelRot).
function Transform:beamngQuat()
  local x, y, z, w = self:quat()
  return -x, -y, -z, w
end

-- Rotation angle of R in radians (~ d / R_earth for two anchors d apart).
function Transform:angle()
  local x, y, z, w = self:quat()
  return 2 * atan2(sqrt(x * x + y * y + z * z), math.abs(w))
end

-- Hamilton product a*b of two (x, y, z, w) quaternions; the same arithmetic as LuaQuat.__mul.
-- With BeamNG quaternions, (a*b)*v == b*(a*v): a is applied first.
function M.quatMul(ax, ay, az, aw, bx, by, bz, bw)
  return aw * bx + ax * bw + ay * bz - az * by,
         aw * by + ay * bw + az * bx - ax * bz,
         aw * bz + az * bw + ax * by - ay * bx,
         aw * bw - ax * bx - ay * by - az * bz
end

-- BeamNG's quat * vec3 (mathlib.lua LuaQuat.__mul, vec3 branch), for tests and plain-number code.
function M.beamngRotate(qx, qy, qz, qw, x, y, z)
  local tx, ty, tz = 2 * (qy * z - qz * y), 2 * (qz * x - qx * z), 2 * (qx * y - qy * x)
  return x - tx * qw + (qy * tz - qz * ty),
         y - ty * qw + (qz * tx - qx * tz),
         z - tz * qw + (qx * ty - qy * tx)
end

-- Teleport anchors on a grid (core.teleportTo): the same place gets the same frame every time, so the server can keep
-- what it built there (its revisit cache: a game started where it was left reuses the last session's tiles). SNAP_DEG
-- of latitude (~1.1 km); longitude in steps that double toward the poles to stay about as long. Past SNAP_MAX_LAT no
-- snapping (the polar frames). The car lands at its real spot, at most ~0.8 km from the origin.
M.SNAP_DEG = 0.01
M.SNAP_MAX_LAT = 85.0

function M.snapAnchor(latDeg, lonDeg)
  if math.abs(latDeg) > M.SNAP_MAX_LAT then return latDeg, lonDeg end
  local lat = math.floor(latDeg / M.SNAP_DEG + 0.5) * M.SNAP_DEG
  local c = math.cos(math.rad(lat))
  local k = 1                   -- (k * cos in (0.5, 1]: a step of 0.55-1.1 km)
  while c * k * 2 <= 1.0 + 1e-12 and k < 128 do k = k * 2 end
  local step = M.SNAP_DEG * k
  local lon = math.floor(lonDeg / step + 0.5) * step
  if lon >= 180 then lon = lon - 360 elseif lon < -180 then lon = lon + 360 end
  return tonumber(string.format('%.6f', lat)), tonumber(string.format('%.6f', lon))
end

M.Anchor = Anchor
M.Transform = Transform

return M
