-- Winter grip (winter W1, the server's BNE_WINTER; ~/bnge-tests/winterplan/PLAN.md section 4): the server's snow kernel
-- (server/snow.py, native snow.rs) sends a 'snowstate' per UTC hour / place; here its road and ground conditions become
-- live ground-model values. Loaded by core.lua only when the first snowstate arrives: with winter off nothing here
-- runs, and no ground model is touched.
--
-- Ground models are retuned by NAME (W0 probes P1/P5: be:setGroundModel reaches built terrain and mesh collision, no
-- reloadCollision, ~1 us a call). Several names share a collision id, and aliases share one cdata in
-- core_environment.groundModels while the engine keeps a copy per name: so each target name gets a ground_model() of
-- its own (a copy of its stock values with the condition's), the stock cdata is never written, and a restore hands the
-- stock cdata back. A level load reloads every ground model from the files (environment.lua reloadGroundModels): the
-- once-a-second check sees the new table and applies again.
--   ROAD   terrain carriageways: the layer bngEarthGroundAsphalt's ASPHALT_OLD (+ its alias), terrain cobbles
--   DECK   meshes: bridge decks (bne_bridge_asphalt) and the other asphalt meshes, ASPHALT and two of its aliases
--          (not 'concrete' / 'concrete2', aliases of ASPHALT: tunnel floors keep their grip)
--   OPEN   the open ground's terrain layers (grass, dirt, sand, mud, the snow layer, gravel, rock)
-- The terrain takes one ground model per layer, and every terrain road is one layer (W3 adds a layer per plow class),
-- so ROAD follows the plow class of the road under the player's car (navgraph: drivability and posted speed), with a
-- dwell; DECK follows the same class's bridge row (decks freeze first).
-- Depth (defaultDepth, deep snow) only where it holds: a heightfield keeps a car above 0.20 m of it, a mesh drops the
-- car through it beyond ~0.15 m (probe P6 on Windows): models any mesh uses stay at MESH_DEPTH_MAX.
local M = {}

M.INTERVAL_S = 1.0          -- the check (class, reload, apply): once a second
M.CLASS_DWELL_S = 5.0       -- a new road class holds this long before the slots follow it
M.STALE_S = 60.0            -- after a new server hello, no snowstate this long: back to stock grip
M.MESH_DEPTH_MAX = 0.12     -- m (0.15 held, 0.20 fell through on a mesh: P6)
M.TERRAIN_DEPTH_MAX = 0.20  -- m (held on terrain: P6)

M.ROAD = { 'ASPHALT_OLD', 'GROUNDMODEL_ASPHALT_OLD', 'COBBLESTONE' }
M.DECK = { 'ASPHALT', 'GROUNDMODEL_ASPHALT1', 'GRID' }
M.OPEN = { 'GRASS', 'DIRT', 'DIRT_DUSTY', 'DIRT_DUSTY_LOOSE', 'SAND', 'MUD', 'SNOW', 'GRAVEL', 'ROCK' }
-- open-ground models only the terrain uses (our materials: art/terrains/main.materials.json; none of the level's mesh
-- materials names them): deeper snow allowed
M.TERRAIN_ONLY = { DIRT_DUSTY = true, DIRT_DUSTY_LOOSE = true, SAND = true, MUD = true, SNOW = true }

-- Road condition slots (the plan's 4.1 starting values [R2], calibrated by stop distances: see CALIBRATION below):
-- static / sliding friction, roughness, stribeck velocity, hydrodynamic friction, the stock collision type (particles,
-- tyre sounds), and whether its depth counts (loose snow, slush).
M.SLOTS = {
  dry        = nil,                                                         -- stock values
  damp       = { 0.90, 0.63, 0.1, 4.5, 0.0006, 'ASPHALT_WET' },
  wet        = { 0.80, 0.55, 0.2, 5.0, 0.0012, 'ASPHALT_WET' },
  salt_wet   = { 0.72, 0.50, 0.2, 5.0, 0.0012, 'ASPHALT_WET' },
  frost      = { 0.35, 0.28, 0.1, 1.5, 0, 'ICE' },
  black_ice  = { 0.15, 0.11, 0.1, 1.0, 0, 'ICE' },
  ice        = { 0.12, 0.10, 0.1, 1.0, 0, 'ICE' },          -- (by temperature: M.iceFriction)
  wet_ice    = { 0.07, 0.05, 0.1, 1.0, 0, 'ICE' },
  slush      = { 0.40, 0.33, 0.6, 2.5, 0.002, 'SNOW', true },
  dusting    = { 0.42, 0.40, 0.4, 2.5, 0, 'SNOW' },
  loose      = { 0.38, 0.33, 0.6, 2.5, 0, 'SNOW', true },
  packed     = { 0.30, 0.27, 0.55, 2.0, 0, 'SNOW' },
  packed_icy = { 0.17, 0.13, 0.3, 1.0, 0, 'ICE' },
}
M.GRIT = { 0.25, 0.22 }      -- gritted ice / packed ice: at least this (Nordic minor roads)
M.ICE_WARM, M.ICE_COLD = { -1, 0.08 }, { -15, 0.22 }    -- ice by pavement temperature: most slippery near 0 degC
-- Open ground by depth bin D1..D6 (static, sliding, roughness): thin cover over grass to deep snow
M.GROUND = { { 0.55, 0.45, 0.4 }, { 0.45, 0.38, 0.6 }, { 0.40, 0.35, 0.65 }, { 0.38, 0.33, 0.7 }, { 0.36, 0.32, 0.7 },
  { 0.35, 0.31, 0.7 } }
M.FLUID = { density = 300, k = 800, n = 0.45, shear = 3000 }   -- the snow layer's resistance (P6 used 400 / 1200 / 7000)
-- The AI's speed in our speed cap (speeding.lua) per condition (plan 4.4: ~0.8 packed snow, 0.65 ice)
M.SPEED = { dry = 1, damp = 1, wet = 0.95, salt_wet = 0.92, frost = 0.8, black_ice = 0.65, ice = 0.65, wet_ice = 0.6,
  slush = 0.75, dusting = 0.85, loose = 0.75, packed = 0.8, packed_icy = 0.7 }
M.LABEL = { dry = 'Dry road', damp = 'Damp road', wet = 'Wet road', salt_wet = 'Wet, salted', frost = 'Frost',
  black_ice = 'Black ice', ice = 'Ice', wet_ice = 'Wet ice', slush = 'Slush', dusting = 'Dusting of snow',
  loose = 'Loose snow', packed = 'Packed snow', packed_icy = 'Icy packed snow' }

local state = nil            -- the last snowstate
local forced = nil           -- M.force's condition (calibration / the presets on the mod side), nil: the state's
local applied = {}           -- name -> signature of the values set ('stock' after a restore)
local keep = {}              -- name -> the ground_model() handed to the engine (kept alive)
local gmTable, gmProbe = nil, nil
local timer, clock = 0, 0
local cls, clsCand, clsSince = 2, nil, 0
local pendingSince = nil
local readoutText = ''
M.calls, M.applies = 0, 0

-- Pure: the plow class 0..4 of a road from navgraph's edge (drivability, posted speed m/s): motorways and trunks P0,
-- the other full-drivability roads (primary, secondary) P1, tertiary P2, residential / unclassified / living streets
-- P3, service roads and tracks P4 (server/navgraph.py DRIVE).
function M.classOf(drivability, posted)
  local d = tonumber(drivability)
  if not d then return 2 end
  if d >= 1.0 then return (tonumber(posted) or 0) >= 24.5 and 0 or 1 end
  if d >= 0.85 then return 2 end
  if d >= 0.5 then return 3 end
  return 4
end

-- Pure: ice friction (static, sliding) at pavement temperature tc (degC).
function M.iceFriction(tc)
  local w, c = M.ICE_WARM, M.ICE_COLD
  local t = math.max(c[1], math.min(w[1], tonumber(tc) or w[1]))
  local s = w[2] + (c[2] - w[2]) * (t - w[1]) / (c[1] - w[1])
  return s, 0.8 * s
end

-- Pure: the slot values {static, sliding, rough, stribeck, hydro, ctype, depth} of a road row
-- {cond, depthMm, tempC, edgeCond, edgeMm, grit, track}, the depth capped at cap (m); nil = stock.
function M.roadValues(row, cap)
  if type(row) ~= 'table' then return nil end
  local cond = row[1]
  local s = M.SLOTS[cond]
  if not s then return nil end
  local st, sl = s[1], s[2]
  if cond == 'ice' then st, sl = M.iceFriction(row[3]) end
  if row[6] == 1 and (cond == 'ice' or cond == 'black_ice' or cond == 'packed_icy' or cond == 'packed') then
    st, sl = math.max(st, M.GRIT[1]), math.max(sl, M.GRIT[2])
  end
  local depth = s[7] and math.min(cap, (tonumber(row[2]) or 0) / 1000) or 0
  return { st, sl, s[3], s[4], s[5], s[6], depth }
end

-- Pure: the open ground's values from the snowstate ground [depthMm, bin, cover, wet, crust, swe, age]; nil = stock.
-- Sinkage, not depth (plan 4.2): fresh snow lets a wheel down ~80 % of it, a crust halves that.
function M.groundValues(g, cap)
  if type(g) ~= 'table' then return nil end
  local bin = tonumber(g[2]) or 0
  if bin < 1 then return nil end
  local v = M.GROUND[math.min(bin, #M.GROUND)]
  local depth = 0
  if bin >= 3 then
    depth = (tonumber(g[1]) or 0) / 1000 * 0.8
    if g[5] == 1 then depth = depth * 0.5 end
    depth = math.min(cap, depth)
  end
  local st, sl = v[1], v[2]
  if g[5] == 1 then st, sl = st * 0.85, sl * 0.85 end    -- (an icy crust)
  return { st, sl, v[3], 2.5, 0, 'SNOW', depth }
end

local function sig(v)
  if not v then return 'stock' end
  return string.format('%.3f/%.3f/%.2f/%.2f/%.4f/%s/%.3f', v[1], v[2], v[3], v[4], v[5], v[6], v[7])
end

local function ctypeId(name)
  local ok, particles = pcall(require, 'particles')
  if not ok or not particles then return nil end
  local ok2, id = pcall(function()
    return particles.getOrAddMaterialIDByName(particles.getMaterialsParticlesTable(), name)
  end)
  return ok2 and id or nil
end

-- Set one name's values (v nil: its stock cdata back). Returns true when the engine was called.
local function setModel(gms, name, v)
  local h = gms[name]
  if not h or not h.cdata then return false end
  local s = sig(v)
  if applied[name] == s then return false end
  local ok = pcall(function()
    if not v then
      be:setGroundModel(name, h.cdata)
      keep[name] = nil
    else
      local src, gm = h.cdata, ground_model()
      for _, f in ipairs({ 'hydrodynamicFriction', 'strength', 'fluidDensity', 'flowConsistencyIndex',
          'flowBehaviorIndex', 'dragAnisotropy', 'skidMarks', 'shearStrength', 'collisiontype' }) do
        gm[f] = src[f]
      end
      gm.staticFrictionCoefficient, gm.slidingFrictionCoefficient = v[1], v[2]
      gm.roughnessCoefficient, gm.stribeckVelocity, gm.hydrodynamicFriction = v[3], v[4], v[5]
      local id = ctypeId(v[6])
      if id then gm.collisiontype = id end
      gm.defaultDepth = v[7] or 0
      if (v[7] or 0) > 0 then
        gm.fluidDensity, gm.flowConsistencyIndex = M.FLUID.density, M.FLUID.k
        gm.flowBehaviorIndex, gm.shearStrength = M.FLUID.n, M.FLUID.shear
      end
      gm.skidMarks = false
      be:setGroundModel(name, gm)
      keep[name] = gm
    end
  end)
  M.calls = M.calls + 1
  if ok then applied[name] = s end
  return ok
end

local function envModels()
  local env = rawget(_G, 'core_environment')
  return env and env.groundModels or nil
end

-- The road row the slots follow now: { road row, bridge row } of the class under the car, or the forced condition's.
local function rows()
  if forced then
    local r = { forced, 30, -5, forced, 30, 0, 1 }
    return r, r
  end
  if not state or type(state.roads) ~= 'table' then return nil, nil end
  return state.roads[cls + 1], type(state.bridges) == 'table' and state.bridges[cls + 1] or nil
end

-- Apply the current targets to every name (only the changed ones reach the engine).
function M.apply()
  local gms = envModels()
  if not gms then return 0 end
  if gmTable == nil then gmTable, gmProbe = gms, gms.ASPHALT_OLD and gms.ASPHALT_OLD.cdata or nil end
  local road, deck = rows()
  local n = 0
  local rv = M.roadValues(road, M.TERRAIN_DEPTH_MAX)
  local mv = M.roadValues(road, M.MESH_DEPTH_MAX)
  for _, name in ipairs(M.ROAD) do
    -- (the terrain road layer is terrain only; COBBLESTONE likewise: terrain cobbles. ASPHALT_OLD can also sit on
    -- stock meshes (the gridmap floor, W0 Windows probe): mesh cap there)
    if setModel(gms, name, name == 'COBBLESTONE' and rv or mv) then n = n + 1 end
  end
  local dv = M.roadValues(deck or road, M.MESH_DEPTH_MAX)
  for _, name in ipairs(M.DECK) do
    if setModel(gms, name, dv) then n = n + 1 end
  end
  local g = (not forced) and state and state.ground or nil
  for _, name in ipairs(M.OPEN) do
    local cap = M.TERRAIN_ONLY[name] and M.TERRAIN_DEPTH_MAX or M.MESH_DEPTH_MAX
    if setModel(gms, name, M.groundValues(g, cap)) then n = n + 1 end
  end
  if n > 0 then M.applies = M.applies + 1 end
  return n
end

-- Every touched name back to its stock values.
function M.restore()
  local gms = envModels()
  if gms then
    for name, s in pairs(applied) do
      if s ~= 'stock' then setModel(gms, name, nil) end
    end
  end
  applied, keep = {}, {}
end

local function tempText(c)
  local stg = rawget(_G, 'settings')
  local ok, unit = pcall(function() return stg and stg.getValue and stg.getValue('uiUnitTemperature') end)
  if ok and (unit == 'f' or unit == 'F') then return string.format('%d\194\176F', math.floor(c * 9 / 5 + 32.5)) end
  return string.format('%d\194\176C', math.floor(c + 0.5))
end

-- Pure-ish: the readout for the status bar ("Packed snow, -6 degC  bridges: ice"), '' on a dry road with no snow.
function M.composeReadout(road, deck, ground)
  if type(road) ~= 'table' then return '' end
  local cond = road[1]
  local snowy = type(ground) == 'table' and (tonumber(ground[2]) or 0) >= 1
  local deckBad = type(deck) == 'table' and deck[1] ~= cond and (M.SPEED[deck[1]] or 1) < (M.SPEED[cond] or 1)
  if cond == 'dry' and not snowy and not deckBad then return '' end
  local s = (M.LABEL[cond] or cond) .. ', ' .. tempText(tonumber(road[3]) or 0)
  if deckBad then s = s .. ', bridges: ' .. string.lower(M.LABEL[deck[1]] or deck[1]) end
  return s
end

function M.readout() return readoutText end

-- The AI's speed factor (speeding.lua) for the road under the player.
function M.speedFactor()
  local road = rows()
  return type(road) == 'table' and M.SPEED[road[1]] or 1
end

local function refreshReadout()
  local road, deck = rows()
  readoutText = M.composeReadout(road, deck, state and state.ground)
end

-- The other modules' hooks (only once winter runs): the AI's speed factor, the status bar's readout.
local function hook(on)
  local sp, sb = package.loaded['beamngEarth/speeding'], package.loaded['beamngEarth/statusbar']
  if sp then sp.winter = on and M or nil end
  if sb then sb.extra = on and M.readout or nil end
end

-- core.lua: a snowstate from the server.
function M.onState(msg)
  if type(msg) ~= 'table' or type(msg.roads) ~= 'table' then return end
  state, pendingSince = msg, nil
  hook(true)
  M.apply()
  refreshReadout()
end

-- core.lua: a new server hello. Its snowstate follows the first pose; none within STALE_S (a server without winter):
-- stock grip again.
function M.onHello()
  pendingSince = clock
end

-- Calibration and testing (console: require('beamngEarth/winter').force('ice')): every road and deck at one
-- condition, open ground stock; nil: the server's state again.
function M.force(cond)
  forced = (cond ~= nil and M.SLOTS[cond] ~= nil or cond == 'dry') and cond or nil
  if forced then hook(true) end
  M.apply()
  refreshReadout()
  return forced
end

local function roadClassUnderCar()
  local ng = package.loaded['beamngEarth/navgraph']
  local id = be and be.getPlayerVehicleID and be:getPlayerVehicleID(0) or -1
  local obj = id and id >= 0 and getObjectByID(id) or nil
  if not (ng and ng.speedProfileAt and obj) then return nil end
  local p = obj:getPosition()
  local ok, r = pcall(ng.speedProfileAt, p.x, p.y, p.z)
  if not ok or type(r) ~= 'table' or (r.distance or 99) > 12 then return nil end
  return M.classOf(r.drivability, r.posted or r.target)
end

-- core.lua, every frame while loaded: the once-a-second check.
function M.update(dt)
  clock = clock + (dt or 0)
  timer = timer - (dt or 0)
  if timer > 0 then return end
  timer = M.INTERVAL_S
  if pendingSince and clock - pendingSince > M.STALE_S then
    pendingSince, state = nil, nil
    M.restore()
    readoutText = ''
    if not forced then hook(false) end
    return
  end
  local gms = envModels()
  if gms ~= gmTable or (gms and gms.ASPHALT_OLD and gms.ASPHALT_OLD.cdata) ~= gmProbe then
    -- (a level load reloaded every ground model: our values are gone, the stock ones back)
    gmTable, gmProbe = gms, gms and gms.ASPHALT_OLD and gms.ASPHALT_OLD.cdata or nil
    applied, keep = {}, {}
  end
  local c = roadClassUnderCar()
  if c and c ~= cls then
    if c ~= clsCand then clsCand, clsSince = c, clock end
    if clock - clsSince >= M.CLASS_DWELL_S then cls, clsCand = c, nil end
  elseif c == cls then
    clsCand = nil
  end
  if state or forced then
    M.apply()
    refreshReadout()
  end
end

function M.reset()
  M.restore()
  hook(false)
  state, forced, pendingSince, readoutText = nil, nil, nil, ''
  gmTable, gmProbe, cls, clsCand = nil, nil, 2, nil
end

function M._state() return state, cls, applied end     -- (tests)
function M._setClass(c) cls = c end                     -- (tests)

return M
