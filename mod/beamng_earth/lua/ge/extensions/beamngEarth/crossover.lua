-- LOD swap transitions done with transforms only (docs/V1_2_DESIGN.md 2.5 C1/C2, 12 "P2").
-- Pure math, runs under plain luajit.
--
-- C2, the two-phase vertical crossover (default): with Delta >= max|new - old| over the swapped area,
--   spawn   the new tiles at z - Delta (completely under the old surface: no visible change);
--   phase 1 (0.25 s, smootherstep) raise the new tiles from -Delta to 0: the visible surface
--           max(new(t), old) rises continuously to max(new, old);
--   phase 2 (0.25 s, smootherstep) sink the old tiles from 0 to -Delta: max(new, old(t)) falls
--           continuously to new;
--   end     delete the old tiles.
-- Every surface point thus moves monotonically and continuously from its old height to its new one.
-- Delta = 1.2 * (err_new + err_old) + 0.2 m, err = the per-tile geometric error the server sends.
-- C1 (instant swap) when the whole group is outside the camera frustum (+10 deg margin), or within
-- INSTANT_M (800 m, expanded from 350 m where skirted .cdae meshes match seamlessly).
-- At most MAX_GROUPS (8, down from 24 to reduce per-frame setPosition() calls by 3x) crossovers run
-- at once: beyond that a group in view within NEAR_POP_M waits for a free slot (farfield.lua), a
-- farther one swaps instantly.
-- A translucent instance-alpha fade was probed and does not work on TSStatic (P2), hence transforms.

local M = {}

local max, min, sqrt, atan, tan, rad, deg, acos, asin, floor = math.max, math.min, math.sqrt, math.atan, math.tan,
  math.rad, math.deg, math.acos, math.asin, math.floor

M.PHASE1_S = 0.25          -- new tiles rise
M.PHASE2_S = 0.25          -- old tiles sink
M.COVER_SINK_S = 0.3       -- far tiles newly covered by the terrain block sink this long (phase 2 only)
M.INSTANT_M = 800          -- instant group swap radius (m, expanded from 350 m)
M.NEAR_INSTANT_M = 800     -- alias for near-player instant group swap radius (m)
M.MAX_GROUPS = 8           -- concurrent crossovers (transition cap, down from 24 to cut setPosition() calls by 3x)
M.MARGIN_DEG = 10          -- frustum margin for the off-screen test
M.NEAR_POP_M = 5000        -- at the cap, an in-view group nearer than this waits instead of popping
                           -- (a farther one swaps instantly: counted in farfield stats().pops)
M.ASPECT_MAX = 2.4         -- widest screen assumed by the frustum cone (21:9)
M.CELLS = 32               -- far tile grid (cells per side) of a FORMAT 3 server (no hello.cells)

-- The connected server's tile grid: hello.cells (docs/V1_4_SPEC.md 3.1 M1: 64 with FORMAT 4), set by
-- core.lua on every hello; M.CELLS when the server sends none.
local cells = M.CELLS

-- 6t^5 - 15t^4 + 10t^3 on [0, 1], clamped.
function M.smootherstep(t)
  if t <= 0 then return 0 end
  if t >= 1 then return 1 end
  return t * t * t * (t * (t * 6 - 15) + 10)
end

-- The server's cells per tile edge (hello.cells; nil or invalid: M.CELLS). Returns the value in use.
function M.setCells(n)
  n = tonumber(n)
  cells = (n and n >= 1 and n == floor(n)) and n or M.CELLS
  return cells
end

function M.cells() return cells end

-- Fallback geometric error for a tile without 'err': a quarter of its cell size.
function M.defaultErr(level)
  return 256 * 2 ^ (level or 0) / cells / 4
end

-- Crossover amplitude for a group (max err of its visible adds / removes).
function M.delta(errNew, errOld)
  return 1.2 * ((errNew or 0) + (errOld or 0)) + 0.2
end

-- z offset of a new (added) tile, t seconds after the crossover started: -Delta -> 0 in phase 1.
function M.addOffset(delta, t)
  return -delta * (1 - M.smootherstep(t / M.PHASE1_S))
end

-- z offset of an old (removed) tile: 0 during phase 1, then 0 -> -Delta in phase 2.
function M.removeOffset(delta, t)
  return -delta * M.smootherstep((t - M.PHASE1_S) / M.PHASE2_S)
end

-- Total length of a crossover (rise only when there is nothing to sink).
function M.duration(withRemoves)
  return withRemoves and (M.PHASE1_S + M.PHASE2_S) or M.PHASE1_S
end

-- from -> to over dur seconds (smootherstep), t seconds after the start.
function M.ramp(from, to, t, dur)
  if dur <= 0 then return to end
  return from + (to - from) * M.smootherstep(t / dur)
end

-- Half-angle (deg) of a cone that contains the view frustum. core_camera's fovDeg is taken as the
-- vertical fov and the screen as up to ASPECT_MAX wide, so the cone holds the frustum's corners
-- (if the engine's fov is horizontal, the cone is just wider than needed: still safe).
function M.coneHalfDeg(fovDeg)
  local f = rad(min(max(fovDeg or 60, 1), 179) / 2)
  return deg(atan(tan(f) * sqrt(1 + M.ASPECT_MAX * M.ASPECT_MAX)))
end

-- Sphere (cx, cy, cz, r) against the camera cone (+ MARGIN_DEG). cam = {x,y,z,fx,fy,fz,fovDeg}.
-- Returns inView, distance from the camera to the sphere's surface (>= 0).
function M.sphereInView(cam, cx, cy, cz, r)
  local dx, dy, dz = cx - cam.x, cy - cam.y, cz - cam.z
  local d = sqrt(dx * dx + dy * dy + dz * dz)
  if d <= r then return true, 0 end
  local fl = sqrt(cam.fx * cam.fx + cam.fy * cam.fy + cam.fz * cam.fz)
  if fl < 1e-9 then return true, d - r end
  local c = (dx * cam.fx + dy * cam.fy + dz * cam.fz) / (d * fl)
  local angle = deg(acos(max(-1, min(1, c))))
  local spread = deg(asin(min(1, r / d)))
  return angle - spread <= M.coneHalfDeg(cam.fovDeg) + M.MARGIN_DEG, d - r
end

-- Bounding sphere of a far tile from its footprint (x0, y0, x1, y1), mean height z and the vertical
-- motion amplitude: half the diagonal, plus relief (unknown here: max(50 m, 10 % of the size)).
function M.tileSphere(x0, y0, x1, y1, z, delta)
  local s = max(x1 - x0, y1 - y0)
  return 0.5 * (x0 + x1), 0.5 * (y0 + y1), z, 0.7072 * s + max(50, 0.1 * s) + (delta or 0)
end

return M
