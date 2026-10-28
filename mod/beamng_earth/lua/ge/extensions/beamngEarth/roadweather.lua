-- The weather on the road (the user, 2026-10-25: grip, a wet look and snow on the roads). Two amounts follow the weather
-- the player sees (weather.roadInputs: real or manual): wet (0..1) rises in rain and dries slowly after it, snow (0..1)
-- settles while it snows below freezing and melts above it (rain melts it faster). From them:
-- - grip: every ground model's static and sliding friction scaled (be:setGroundModel, as core_environment's own
--   submitGroundModel), wet roads ~25 % less, snow up to 50 %, wet roads freezing below 0 C worse still;
-- - the look: the road and marking materials darker and glossier when wet, paler under snow (as seasonlook tints the
--   leaves: setField, flush, reload).
-- Both are put back as they were when our level ends (restore). Applied only when they changed enough, not every frame.
local M = {}

M.WET_UP_S = 180            -- s of full rain to soak a dry road
M.DRY_S = 1500              -- s for a soaked road to dry (no rain, 15 C; faster when warm)
M.SNOW_UP_S = 900           -- s of full snow to cover a road
M.MELT_S = 1800             -- s for full snow to melt at 5 C above freezing (faster warmer, and in rain)
M.APPLY_S = 5               -- grip and look written at most this often ...
M.GRIP_STEP = 0.02          -- ... and only when the grip changed this much
M.LOOK_STEP = 0.04          -- ... or the look this much
M.ROAD_MATS = { 'road_asphalt_2lane', 'm_gm_concrete_plain', 'italy_cobblestone', 'm_dirt_road', 'line_white',
  'line_dashed_long', 'gutter1', 'sidewalk_concrete' }
M.SNOW_TINT = 3.2           -- the colour factor under full snow (a dark road texture toward white)

local wet, snow = 0, 0
local applied = { grip = 1, wet = 0, snow = 0 }
local lastApply = -math.huge
local baseGm = nil          -- ground model name -> { static, sliding } as loaded
local baseMat = {}          -- material name -> { field, colour = { r, g, b, a }, rough = { field, v } }

-- Pure: the amounts after dt s of rain / snow (0..100) at tempC.
function M.step(w, s, dt, rainPct, snowPct, tempC)
  local rain, sn = (rainPct or 0) / 100, (snowPct or 0) / 100
  tempC = tempC or 15
  if rain > 0.02 then
    w = w + dt * rain / M.WET_UP_S
  else
    w = w - dt * math.max(0.3, (tempC + 5) / 20) / M.DRY_S
  end
  if sn > 0.02 and tempC <= 1.5 then
    s = s + dt * sn / M.SNOW_UP_S
    w = math.max(w, 0.4 * s)                 -- slush under it
  elseif tempC > 0 then
    s = s - dt * (tempC / 5 + rain * 3) / M.MELT_S
    if s > 0 then w = math.max(w, 0.6) end   -- melting snow wets the road
  end
  return math.max(0, math.min(1, w)), math.max(0, math.min(1, s))
end

-- Pure: the friction factor of the road surface.
function M.grip(w, s, tempC)
  local f = 1 - 0.25 * w - 0.5 * s
  if (tempC or 15) < 0 and w > 0.2 then f = f - 0.25 * w end          -- a wet road freezing: ice
  return math.max(0.25, math.min(1, f))
end

-- Pure: a material colour factor c (r, g, b, a) and roughness r for the amounts (wet: darker, glossier; snow: paler,
-- matte).
function M.look(c, r, w, s)
  local k = (1 - 0.35 * w) * (1 - s) + M.SNOW_TINT * s
  local out = { c[1] * k, c[2] * k, c[3] * (k + 0.05 * s), c[4] }
  local rr = r and (r * (1 - 0.6 * w * (1 - s)) * (1 - s) + 0.85 * s) or nil
  return out, rr
end

local function gms()
  local env = rawget(_G, 'core_environment')
  return env and env.groundModels
end

local function setGrip(f)
  local list, be_, ctor = gms(), rawget(_G, 'be'), rawget(_G, 'ground_model')
  if not (list and be_ and be_.setGroundModel and ctor) then return false end
  if not baseGm then
    baseGm = {}
    for name, g in pairs(list) do
      local cd = g.cdata
      if cd then baseGm[name] = { cd.staticFrictionCoefficient, cd.slidingFrictionCoefficient } end
    end
  end
  for name, g in pairs(list) do
    local b, cd = baseGm[name], g.cdata
    if b and cd and not tostring(name):find('WATER', 1, true) then
      local gm = ctor()
      for _, k in ipairs({ 'roughnessCoefficient', 'defaultDepth', 'hydrodynamicFriction', 'stribeckVelocity',
        'strength', 'collisiontype', 'fluidDensity', 'flowConsistencyIndex', 'flowBehaviorIndex', 'dragAnisotropy',
        'skidMarks', 'shearStrength' }) do
        gm[k] = cd[k]
      end
      gm.staticFrictionCoefficient = b[1] * f
      gm.slidingFrictionCoefficient = b[2] * f
      pcall(be_.setGroundModel, be_, name, gm)
    end
  end
  return true
end

local function parse4(v)
  local t = {}
  for n in tostring(v or ''):gmatch('[-%d%.eE]+') do t[#t + 1] = tonumber(n) end
  return #t >= 3 and { t[1], t[2], t[3], t[4] or 1 } or nil
end

local function setLook(w, s)
  local st = rawget(_G, 'scenetree')
  if not st then return end
  for _, name in ipairs(M.ROAD_MATS) do
    local m = st.findObject(name)
    if m then
      local b = baseMat[name]
      if not b then
        local c = parse4(m:getField('baseColorFactor', 0))
        local field = c and 'baseColorFactor' or 'diffuseColor'
        c = c or parse4(m:getField('diffuseColor', 0)) or { 1, 1, 1, 1 }
        local r = tonumber(m:getField('roughnessFactor', 0))
        b = { field = field, colour = c, rough = r }
        baseMat[name] = b
      end
      local c, r = M.look(b.colour, b.rough, w, s)
      m:setField(b.field, 0, string.format('%.3f %.3f %.3f %.3f', c[1], c[2], c[3], c[4]))
      if r then m:setField('roughnessFactor', 0, string.format('%.3f', r)) end
      m:flush()
      m:reload()
    end
  end
end

-- Every frame in our level (core): dt s since the last call.
local lastT = nil
function M.update(dt, now)
  now = now or os.clock()
  if dt == nil then dt = lastT and (now - lastT) or 0 end
  lastT = now
  local ok, w = pcall(require, 'beamngEarth/weather')
  local rain, sn, temp
  if ok and w.roadInputs then rain, sn, temp = w.roadInputs() end
  if not rain then return end
  wet, snow = M.step(wet, snow, math.min(dt or 0, 1), rain, sn, temp)
  if now - lastApply < M.APPLY_S then return end
  local g = M.grip(wet, snow, temp)
  local gripChanged = math.abs(g - applied.grip) >= M.GRIP_STEP or (g == 1 and applied.grip ~= 1)
  local lookChanged = math.abs(wet - applied.wet) >= M.LOOK_STEP or math.abs(snow - applied.snow) >= M.LOOK_STEP
    or ((wet == 0 and snow == 0) and (applied.wet ~= 0 or applied.snow ~= 0))
  if not (gripChanged or lookChanged) then return end
  lastApply = now
  if gripChanged and setGrip(g) then applied.grip = g end
  if lookChanged then
    pcall(setLook, wet, snow)
    applied.wet, applied.snow = wet, snow
  end
end

-- Our level ended: grip and look as they were.
function M.restore()
  if applied.grip ~= 1 then pcall(setGrip, 1) end
  if applied.wet ~= 0 or applied.snow ~= 0 then pcall(setLook, 0, 0) end
  wet, snow, applied, lastApply, lastT = 0, 0, { grip = 1, wet = 0, snow = 0 }, -math.huge, nil
end

function M.status() return { wet = wet, snow = snow, grip = applied.grip } end
function M._set(w, s) wet, snow = w, s end      -- (tests)

return M
