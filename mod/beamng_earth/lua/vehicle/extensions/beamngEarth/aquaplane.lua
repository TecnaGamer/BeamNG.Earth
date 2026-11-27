-- Aquaplaning (the user, 2026-11-26: "yes, plus deep-water aquaplaning"): standing water under a tyre cuts its grip as
-- the speed nears the hydroplaning speed. The game side (ge beamngEarth/aquaplane.lua) sends the player's own car the
-- puddles shown near it (the skins' puddle quads, server roadwear.puddle_spots, in world coordinates), how full they
-- are (wetlook's alpha threshold) and the rain's water film on the road (mm); here each frame every tyre's contact
-- point is looked up in them and its tread nodes' friction scaled (obj:setNodeFrictionSlidingCoefs, the call the stock
-- tyre deflation makes: beamstate.lua), at a change of the quantised factor only. Ground models are per material, not
-- per spot, and the puddles have no collision, so a per-tyre factor is the way (a separate collision mesh would cost
-- collision rebuilds and could not follow the speed).
--
-- Physics:
--   onset (full dynamic hydroplaning) in water deeper than the tread drains: Horne (NASA TN D-2056, 1963), for car
--   tyres v = 6.35 sqrt(p kPa) km/h = 10.35 sqrt(p psi) mph (35 psi: 61 mph); shallower water raises it as Gallaway
--   (1979) does, ~ depth^-0.06 (2 mm: +15 %, 70 mph);
--   below it the tyre's dry contact shrinks as the water wedge's lift grows with v^2: grip x (1 - (v / vp)^2), never
--   under F_MIN (a hydroplaning tyre keeps ~0.05-0.1 of its grip);
--   a film thinner than D_MIN (the texture and tread clear it) does nothing, the effect grows to full at D_FULL.
-- Nothing runs while the game side sends no water (dry, the weather Off, the setting off: the extension not loaded).
local M = {}

M.D_MIN_MM = 0.5          -- water under this: no effect (cleared by the road texture and the tread)
M.D_FULL_MM = 3.0         -- the full effect from this depth (about a summer tyre's groove capacity)
M.D_REF_MM = 20           -- Horne's speed at and beyond this depth
M.F_MIN = 0.1             -- the share of grip left when fully hydroplaning
M.STEPS = 20              -- the factor's quanta per unit (0.05): the nodes are written at its changes only
M.Z_TOL = 0.6             -- m: a puddle counts within this of the tyre's contact height (not the road under a bridge)
M.P_DEFAULT_KPA = 241     -- a tyre without a pressure group: 35 psi
-- the puddle atlas (native roadwear.rs puddle_atlas): per row (sag, gutter, rut, shallow rut) the depth peak's alpha
-- share and the water depth at full soak in the pool's middle (mm: a road sag holds ~1-2 cm, ruts a few mm)
M.PEAK = { 1.0, 0.85, 0.72, 0.6 }
M.DMAX_MM = { 15, 10, 6, 4 }
M.SHAPE = { { 0.80, 0.92 }, { 0.62, 0.95 }, { 0.88, 0.88 }, { 0.70, 0.95 } }   -- per column: (across, along) radii
M.RIM = 0.86              -- the pool's rim radius in its cell (the atlas's mean rim)
M.REF_FULL = 15           -- wetlook PUDDLE_REF soaked: the alpha threshold at which a pool is full
M.STRIDE = 8              -- a spot: x, y, z, ux, uy (along), a (half along), b (half across), atlas cell

local abs, floor, max, min, sqrt = math.abs, math.floor, math.max, math.min, math.sqrt

-- Pure: the hydroplaning speed (m/s) in water depthMm deep, the tyre at pKpa (gauge).
function M.onsetSpeed(depthMm, pKpa)
  local p = max(20, tonumber(pKpa) or M.P_DEFAULT_KPA)
  local d = min(M.D_REF_MM, max(1, tonumber(depthMm) or 0))
  return 6.35 * sqrt(p) / 3.6 * (M.D_REF_MM / d) ^ 0.06
end

-- Pure: the grip factor (F_MIN..1) of a tyre at speed (m/s) in water depthMm deep at pKpa.
function M.gripFactor(speed, depthMm, pKpa)
  depthMm = tonumber(depthMm) or 0
  if depthMm <= M.D_MIN_MM then return 1 end
  local w = min(1, (depthMm - M.D_MIN_MM) / (M.D_FULL_MM - M.D_MIN_MM))
  local x = (tonumber(speed) or 0) / M.onsetSpeed(depthMm, pKpa)
  local mu = M.F_MIN + (1 - M.F_MIN) * max(0, 1 - x * x)
  return 1 - w * (1 - mu)
end

-- Pure: the factor rounded to 1 / STEPS.
function M.quantise(f)
  return floor(f * M.STEPS + 0.5) / M.STEPS
end

-- Pure: the water depth (mm) at (x, y, z) in the spot at index i of the flat list s, the pools filled to alpha
-- threshold ref (255: empty). The atlas's pool: an ellipse in its cell, the depth flat over its inner half, falling to
-- the rim.
function M.depthAt(s, i, ref, x, y, z)
  local cz = s[i + 2]
  if abs(z - cz) > M.Z_TOL then return 0 end
  local dx, dy = x - s[i], y - s[i + 1]
  local ux, uy = s[i + 3], s[i + 4]
  local al = (dx * ux + dy * uy) / s[i + 5]          -- (-1..1 along the pool, and across it)
  if abs(al) >= 1 then return 0 end
  local ac = (dy * ux - dx * uy) / s[i + 6]
  if abs(ac) >= 1 then return 0 end
  local cell = floor(s[i + 7] + 0.5)
  local row, col = min(3, max(0, floor(cell / 4))), cell % 4
  local sh = M.SHAPE[col + 1]
  local ax, ay = sh[1], sh[2]
  if col == 3 then ax = ax * (1 - 0.35 * (1 - min(1, abs(al * 2.2)))) end   -- (two pools: the waist between them)
  local r = sqrt((ac / ax) ^ 2 + (al / ay) ^ 2)
  local peak = M.PEAK[row + 1] * 255
  local alpha = peak * min(1, max(0, 1 - r / M.RIM) / 0.5)
  if alpha <= ref then return 0 end
  return M.DMAX_MM[row + 1] * (alpha - ref) / max(1, peak - M.REF_FULL)
end

-- Pure: the deepest water (mm) at a point: the film, or a pool's.
function M.waterAt(s, n, ref, film, x, y, z)
  local d = film or 0
  for i = 1, n, M.STRIDE do
    local w = M.depthAt(s, i, ref, x, y, z)
    if w > d then d = w end
  end
  return d
end

------------------------------------------------------------------------------------------
-- runtime (the player's car)

local spots, nSpots, fillRef, film = {}, 0, 255, 0
local active = false
local applied = {}          -- wheel index -> factor written (absent: stock)
M.writes = 0
M.last = {}                 -- wheel index -> { depth mm, factor } of the last frame (the probes)

local function tyrePressureKpa(wd)
  local id = wd.pressureGroupId
  if id and obj.getGroupPressure then
    local ok, p = pcall(obj.getGroupPressure, obj, id)
    if ok and type(p) == 'number' and p > 101325 then return (p - 101325) / 1000 end
  end
  return M.P_DEFAULT_KPA
end

-- Write wheel i's tread nodes at factor f (1: their own values).
local function setWheel(i, wd, f)
  local nodes = v and v.data and v.data.nodes
  if not nodes then return end
  for _, cid in pairs(wd.treadNodes) do
    local n = nodes[cid]
    local fc = n and n.frictionCoef
    if fc then
      obj:setNodeFrictionSlidingCoefs(cid, fc * f, (n.slidingFrictionCoef or fc) * f)
    end
  end
  applied[i] = f < 1 and f or nil
  M.writes = M.writes + 1
end

local function restoreAll()
  if not (wheels and wheels.wheels) then applied = {} return end
  for i, f in pairs(applied) do
    local wd = wheels.wheels[i]
    -- (a tyre deflated since keeps the stock deflation's values)
    if wd and wd.treadNodes and not wd.isTireDeflated and f then setWheel(i, wd, 1) end
  end
  applied = {}
end

-- The game side: ref (the pools' alpha threshold, 255 none), filmMm, list (flat, STRIDE a spot, world coordinates).
function M.set(ref, filmMm, list)
  spots = type(list) == 'table' and list or {}
  nSpots = #spots - (#spots % M.STRIDE)
  fillRef, film = tonumber(ref) or 255, max(0, tonumber(filmMm) or 0)
  active = (nSpots > 0 and fillRef < 255) or film > M.D_MIN_MM
  if not active then restoreAll() end
end

function M.updateGFX(dt)
  if not active or not (wheels and wheels.wheels) then return end
  local pos = obj:getPosition()
  local speed = obj:getVelocity():length()
  local n = fillRef < 255 and nSpots or 0
  for i = 0, (wheels.wheelCount or 0) - 1 do
    local wd = wheels.wheels[i]
    if wd and wd.treadNodes and wd.node1 and wd.node2 and not wd.isTireDeflated then
      local p1, p2 = obj:getNodePosition(wd.node1), obj:getNodePosition(wd.node2)
      local x = pos.x + (p1.x + p2.x) * 0.5
      local y = pos.y + (p1.y + p2.y) * 0.5
      local z = pos.z + (p1.z + p2.z) * 0.5 - (wd.radius or 0.3)
      local d = M.waterAt(spots, n, fillRef, film, x, y, z)
      local f = d > M.D_MIN_MM and M.quantise(M.gripFactor(speed, d, tyrePressureKpa(wd))) or 1
      if f ~= (applied[i] or 1) then setWheel(i, wd, f) end
      M.last[i] = { d, f }
    end
  end
end

-- A reset or a new vehicle reloads this Lua (the jbeam's own friction back); the game side sends again.
function M.onReset()
  applied = {}
end

function M.status()
  local w = {}
  for i, f in pairs(applied) do w[#w + 1] = i .. '=' .. f end
  table.sort(w)
  return string.format('active=%s spots=%d ref=%d film=%.2f writes=%d applied=%s', tostring(active),
    nSpots / M.STRIDE, fillRef, film, M.writes, table.concat(w, ','))
end

return M
