-- BeamNG.Earth: the wet look of our meshes (the weather plan 4.4 item 6; weather phase B): sidewalks, curbs, markings,
-- mesh roads and bridge decks, buildings and roofs go darker and glossier while the ground is wet (Lagarde's wet
-- surfaces: a porous surface darkens as water fills its pores, every surface's roughness falls as a water film forms;
-- screen-space reflections, where the player has them, then show the lamps and the sky in it).
--
-- How wet: the wetter of winter W1's road surface state when the server sends one (beamngEarth/winter's snowstate: the
-- road class under the car, its condition dry / damp / wet / salt_wet / slush ...; BNE_WET_ROADS: the REAL road) and this
-- module's own model from the rain the weather shows (wetness rises within minutes in rain and dries over DRY_S after
-- it; a preset storm over a dry real road is wet: M.wetOf). The level is
-- quantised to LEVELS steps, and only a step change writes materials: one batch, at most BATCH per frame (setField and
-- reload each), never per frame otherwise (SkyCycle's stutters came from refreshing materials all the time, the plan's
-- research note 2). Dry or the weather Off: every material back to its own values (only those still holding ours).
--
-- Grip (the user, 2026-11-26: "grip should follow the weather shown in game", "yes, plus deep-water aquaplaning"):
-- the own model's wetness (the rain shown, not the snowstate, which winter.lua has itself) as a road condition, dry /
-- damp / wet (M.gripCond), handed to winter.lua at its changes only (setShown: its ground-model retune, the wetter of it
-- and the real road, its AI margins); with the setting 'Wet roads' (weather.lua WET_GRIP) on, whether or not the wet
-- look is. The puddles shown (their alpha step) and the rain's water film go to aquaplane.lua (the player's car).
--
-- Not here: the city carriageways are terrain under the aerial photo (the layer bngEarthGroundAsphalt; winter
-- PROBES.md P10) and a terrain material change costs ~1 s (updateMaterial). Their wet look needs the block-commit route
-- (the asphalt layer's roughness set before a block's own updateMaterial) or the terrain shader route (W9): a later job.
local M = {}

M.LEVELS = 4              -- wetness steps (0 dry .. LEVELS soaked)
M.BATCH = 12              -- materials written per frame during a change
M.CHECK_S = 2             -- the wetness looked at this often
M.WET_TAU_S = 240         -- own model: wetting time constant in rain (to ~63 %)
M.DRY_S = 2700            -- own model: drying from soaked to dry after the rain (45 min; faster in sun: later)
M.RAIN_SOAK_MMH = 2       -- own model: this rain soaks surfaces fully
M.RESCAN_S = 30           -- while wet, new materials (tiles loaded since) looked for this often

-- Surface classes: { porosity (0 none .. 1 most: how much darker wet), gloss (0..1: how much the roughness falls) }.
M.CLASSES = {
  -- (phase C, broker wc19 / wc21 against the rain photos: a wet surface reads darker only when its colour falls well
  -- below dry, the sky's sheen outweighing a smaller drop, as on the terrain's streets; the gloss of concrete lower so
  -- the sheen does not turn it milky)
  asphalt = { 0.75, 0.75 },   -- mesh carriageways, decks, asphalt walks: dark and shiny wet
  concrete = { 0.9, 0.45 },   -- sidewalks, curbs, slabs, setts, concrete decks
  marking = { 0.1, 0.7 },     -- paint: hardly darker, glossy
  wall = { 0.25, 0.35 },      -- facades: rain wets them in streaks; a little
  roof = { 0.5, 0.6 },
  metal = { 0.0, 0.5 },
  soil = { 0.5, 0.3 },
}
-- Material names (exact) and name prefixes (patterns anchored at the start) per class.
M.NAMES = {
  road_asphalt_2lane = 'asphalt', bne_bridge_asphalt = 'asphalt', bne_eu_asphalt_walk = 'asphalt',
  repair1_west_coast_usa = 'asphalt', road_crack = 'asphalt',
  sidewalk_concrete = 'concrete', sidewalk1 = 'concrete', curbs = 'concrete', gutter1 = 'concrete',
  m_gm_concrete_plain = 'concrete', italy_sidewalk_tiles = 'concrete', italy_cobblestone = 'concrete',
  bne_highway_concrete = 'concrete', bne_bridge_concrete = 'concrete', bne_eu_curb = 'concrete',
  tactile_indicator = 'marking', bne_eu_tactile = 'marking',
  line_white = 'marking', line_yellow = 'marking', line_dashed_long = 'marking', crossing_white = 'marking',
  roadmarkings1 = 'marking', bne_rail_xing_rubber = 'asphalt',
  m_dirt_road = 'soil', bne_dirt_gravel = 'soil', m_dirty_ground = 'soil',
  bne_truss_steel = 'metal', bne_wire = 'metal',
}
M.PREFIXES = {
  { '^bne_bld_roof', 'roof' }, { '^bne_bld_', 'wall' }, { '^bne_sidewalk_', 'concrete' }, { '^bne_eu_walk_', 'concrete' },
  { '^bne_steel_', 'metal' }, { '^bne_wear_', 'asphalt' }, { '^bne_spot_', 'asphalt' }, { '^bne_crack_', 'asphalt' },
  { '^line_', 'marking' }, { '^bne_rr_concrete', 'concrete' }, { '^bne_eui_concrete', 'concrete' },
}
-- ... and, for the materials of other sets the road tiles borrow (stock levels' sidewalks, curbs, pavers), words in the
-- name (lower case; not our terrain layers bngEarth*, not glass, lights or signs)
M.WORDS = { { 'sidewalk', 'concrete' }, { 'curb', 'concrete' }, { 'kerb', 'concrete' }, { 'pavement', 'concrete' },
  { 'paver', 'concrete' }, { 'cobble', 'concrete' }, { 'concrete', 'concrete' }, { 'asphalt', 'asphalt' },
  { 'crossing', 'marking' }, { 'marking', 'marking' } }
M.WORDS_NOT = { 'glass', 'light', 'lamp', 'sign', 'glow', 'decal_' }
M.NOT = { bne_bld_sky = true, bne_bld_skywin = true, bne_bld_glasshouse = true, bne_bld_lobby = true }   -- (lit windows)
M.WET_DARK = 0.85         -- a porosity-1 surface's colour at full wetness: x (1 - WET_DARK) (Lagarde's 0.2..0.5); at 0.6 the
                          -- Tokyo sidewalks hardly showed it in the broker shots (wb9), at 0.95 clearly (wb11); 0.7 with
                          -- concrete 0.6 still read pale under the sheen (wc19, wc21): concrete x 0.24, asphalt x 0.36 now
M.WET_ROUGH = 0.85        -- a gloss-1 surface's roughness falls by this share at full wetness
M.ROUGH_MIN = 0.06

-- Puddles (the skins' puddle meshes, server roadwear.puddle_chunks: the road's sags, gutters and ruts; roads.lua
-- setPuddles): drawn from PUDDLE_MIN wetness on, hidden below it (no draw at all). Their one material (bne_puddle: a
-- smooth water film, roughness 0.06, coloured as the overcast sky it mirrors: the engine gave our static meshes no sky
-- reflection at Normal, broker r9, and a dark film vanished on the wet asphalt) masks them by the atlas's depth: its alpha
-- test threshold falls as the rain fills them (the deep sag pools first, the shallow ruts last) and rises as they dry,
-- in PUDDLE_STEPS steps (one material write a step, never per frame).
-- APPROXIMATION: the sky-grey base colour (puddles.materials.json, 0.36 0.39 0.44) stands in for the sky reflection
-- the engine does not give our static meshes; physically the film is dark (~0.03) and its brightness is all
-- reflection. With a real reflection source (a later job: an environment probe, or SSR where the preset has it) the
-- base goes back to dark water. At night the grey reads a little light under the lamps.
M.PUDDLE_MIN = 0.45
M.PUDDLE_STEPS = 6
M.PUDDLE_REF = { 250, 15 }  -- the alpha threshold just shown, soaked
M.PUDDLE_MAT = 'bne_puddle'

-- Pure: the puddles' step (0 none .. PUDDLE_STEPS full) at wetness w (0..1).
function M.puddleStep(w)
  w = tonumber(w) or 0
  if w < M.PUDDLE_MIN then return 0 end
  local f = (w - M.PUDDLE_MIN) / (1 - M.PUDDLE_MIN)
  return math.max(1, math.min(M.PUDDLE_STEPS, math.floor(f * M.PUDDLE_STEPS + 0.5)))
end

-- Pure: the puddle material's alpha test threshold at a step (255: nothing drawn).
function M.puddleRef(step)
  step = tonumber(step) or 0
  if step <= 0 then return 255 end
  local a, b = M.PUDDLE_REF[1], M.PUDDLE_REF[2]
  return math.floor(a + (b - a) * (step - 1) / math.max(1, M.PUDDLE_STEPS - 1) + 0.5)
end

-- The new puddle look (the puddle plan R1, ~/bnge-tests/puddles/PLAN_FINAL.md; the setting "New puddle look",
-- weather.lua setPuddleLook, default off: M.v2). On: the wet state is wetstate.lua's integer one (the film for the look
-- and the grip, the standing water for the puddles: filled by how much it rained, BeamMP-consistent) instead of
-- wetStep / puddleStep; the level's environment map becomes a cubemap of the sky over a built-up or an open horizon
-- while wet (V2_CUBES, normalized by the scene's own sky light so it follows the time of day; R0 probe 1), and the
-- puddle material a very smooth lit sky-grey (V2_LOOK). Its alpha threshold has V2 steps (wetstate's
-- H_STEPS_UM), one material write a step and at most a step a check. Off: everything as before, nothing of it loaded.
M.v2 = false
M.V2_REF = { 250, 15 }       -- the alpha threshold at the first and the last step
-- (R1 broker shots, 2026-11-27: a dark base read as black holes by day at Normal: the engine's reflections do not reach
-- the alpha-tested puddle material, whatever the cubemap. The mirrored overcast sky as a lit grey instead: bright by
-- day like the reference photos' puddles, dark at night and dusk with the scene's light; the cubemaps still go on the
-- level's environment map for every other wet surface)
M.V2_LOOK = { base = '0.30 0.31 0.33 1', rough = '0.03', metal = '0' }
M.V2_SKY_FILE = '/levels/beamng_earth/art/shapes/beamngEarth/wetsky.materials.json'
M.V2_CUBES = true            -- (false: the dark water without a cubemap: the engine's own reflections only)
-- Where the sky cubemap goes: 'env' the level's environment map (LevelInfo globalEnviromentMap: every wet surface,
-- the streets, sidewalks and cars too, while the ground is wet; R0 probe 1, a 0.5 ms write), 'material' the puddle
-- material's own (R0 probe 2, ~3.5 ms a write), or 'both'.
M.V2_ROUTE = 'both'
M.HORIZON_S = 10             -- the horizon (built-up or open) looked at this often while puddles show ...
M.HORIZON_RAYS, M.HORIZON_RAY_M, M.HORIZON_CITY = 8, 150, 4    -- ... rays round the camera; this many hits: built-up
M.SKY_OVERCAST = 0.6         -- the deck (max(low, mid) cloud share) from which the sky mirrored is the overcast one

-- Pure: the new look's alpha threshold at a step (255: nothing drawn).
function M.puddleRefV2(step, n)
  step = tonumber(step) or 0
  n = n or #require('beamngEarth/wetstate').H_STEPS_UM
  if step <= 0 then return 255 end
  local a, b = M.V2_REF[1], M.V2_REF[2]
  return math.floor(a + (b - a) * (step - 1) / math.max(1, n - 1) + 0.5)
end

-- Pure: the cubemap mirrored (bne_wetsky_<sky>_<horizon>) for a shown message (its cloud deck, its rain) and a horizon.
function M.skyCube(msg, city)
  local c = type(msg) == 'table' and type(msg.cover) == 'table' and msg.cover or {}
  local deck = math.max(tonumber(c.low) or 0, tonumber(c.mid) or 0) / 100
  local wet = type(msg) == 'table' and (tonumber(msg.rainMmH) or 0) > 0
  local sky = (wet or deck >= M.SKY_OVERCAST) and 'overcast' or 'clear'
  return 'bne_wetsky_' .. sky .. '_' .. (city and 'city' or 'open')
end

-- The own wetness as a road condition for the grip (winter.lua's slots): damp from GRIP_DAMP, wet from GRIP_WET (the
-- midpoints of CONDITION_WET's damp 0.35 and wet 0.8 around dry 0), GRIP_HYST either side of a change.
M.GRIP_DAMP = 0.18
M.GRIP_WET = 0.57
M.GRIP_HYST = 0.03

-- Pure: the grip condition (nil dry, 'damp', 'wet') of own wetness w, the last one prev.
function M.gripCond(w, prev)
  w = tonumber(w) or 0
  local h = M.GRIP_HYST
  local rank = prev == 'wet' and 2 or prev == 'damp' and 1 or 0
  -- (a step above the last condition needs h more, one at or below it h less)
  if w >= M.GRIP_WET + (rank >= 2 and -h or h) then return 'wet' end
  if w >= M.GRIP_DAMP + (rank >= 1 and -h or h) then return 'damp' end
  return nil
end

-- W1's road conditions -> wetness (0..1)
M.CONDITION_WET = { dry = 0, damp = 0.35, wet = 0.8, salt_wet = 0.8, slush = 1, wet_ice = 0.9, frost = 0.15,
  black_ice = 0.5, ice = 0.4, dusting = 0.3, loose = 0.2, packed = 0.2, packed_icy = 0.3 }

------------------------------------------------------------------------------------------
-- pure

-- Pure: the class of a material name, or nil.
function M.classOf(name)
  name = tostring(name or '')
  if M.NOT[name] then return nil end
  if M.NAMES[name] then return M.NAMES[name] end
  for _, p in ipairs(M.PREFIXES) do
    if name:find(p[1]) then return p[2] end
  end
  local l = name:lower()
  if l:find('^bngearth') then return nil end
  for _, w in ipairs(M.WORDS_NOT) do if l:find(w, 1, true) then return nil end end
  for _, w in ipairs(M.WORDS) do if l:find(w[1], 1, true) then return w[2] end end
  return nil
end

-- Pure: the wetness level (0..LEVELS) of a wetness w (0..1).
function M.levelOf(w)
  return math.max(0, math.min(M.LEVELS, math.floor((tonumber(w) or 0) * M.LEVELS + 0.5)))
end

-- Pure: one step of the own wetness model: w (0..1) after dt s of rain mmh (mm/h).
function M.wetStep(w, mmh, dt)
  w, mmh, dt = tonumber(w) or 0, tonumber(mmh) or 0, math.max(0, tonumber(dt) or 0)
  if mmh > 0.02 then
    local target = math.min(1, 0.35 + 0.65 * mmh / M.RAIN_SOAK_MMH)
    if w < target then return w + (target - w) * (1 - math.exp(-dt / M.WET_TAU_S)) end
    return w
  end
  return math.max(0, w - dt / M.DRY_S)
end

-- Pure: the wet values of a material from its own (colour {r, g, b, a} or nil, roughness or nil) at wetness level k:
-- colour string, roughness string (nil: that field is not written).
function M.wetValues(class, color, rough, k)
  local c = M.CLASSES[class]
  if not c then return nil, nil end
  local w = (tonumber(k) or 0) / M.LEVELS
  local col, ro = nil, nil
  if color and c[1] > 0 then
    local f = 1 - M.WET_DARK * c[1] * w
    col = string.format('%.4f %.4f %.4f %.4f', color[1] * f, color[2] * f, color[3] * f, color[4] or 1)
  end
  if c[2] > 0 then
    local r = tonumber(rough) or 1
    ro = string.format('%.4f', math.max(M.ROUGH_MIN, r * (1 - M.WET_ROUGH * c[2] * w)))
  end
  return col, ro
end

-- Pure: the terrain's wet step (0 dry, 1 damp, 2 wet) for a wetness level.
function M.terrainStep(k)
  k = tonumber(k) or 0
  if k >= 3 then return 2 elseif k >= 1 then return 1 end
  return 0
end

-- Pure: the wetness from W1's road row ({ condition, ... }), or nil.
function M.wetFromCondition(cond)
  return cond and M.CONDITION_WET[cond] or nil
end

-- Pure: the wetness shown (0..1) and its source from the setting (on), W1's road state (road: 0..1 or nil) and the own
-- rain model (own): the wetter of the two. W1's state is the REAL road at the place (Phoenix dry) while the weather shown
-- may be a preset storm: the road state alone kept the streets dry under it (the 2026-11-26 playtest: 'wet lvl=0
-- own=0.97 src=road state'); a real wet road with the sky shown clear stays wet.
function M.wetOf(on, road, own_)
  if not on then return 0, 'off' end
  own_ = tonumber(own_) or 0
  road = tonumber(road)
  if road and road >= own_ then return road, 'road state' end
  return own_, 'rain'
end

------------------------------------------------------------------------------------------
-- runtime

local own = 0             -- the own model's wetness
local level = 0           -- the level applied (or being applied)
local queue = nil         -- names left to write for the level
local orig = {}           -- name -> { class, color = {...} or nil, rough = number or nil, ours = { color, rough } }
local known = nil         -- name -> class of the materials seen (nil: not scanned)
local timer, rescan = 0, 0
local puddle = 0          -- the puddles' step applied
local grip = nil          -- the grip condition handed to winter.lua (nil dry)
local road, roadPlace = nil, nil    -- W1's road wetness shown (M.roadStep) and winter.place's place it is for
M.writes, M.source = 0, 'none'
local ws = nil            -- wetstate.lua, loaded with the new look
local look = nil          -- the new look on the puddle material: { orig = its own fields, cube = the cubemap set }
local horizon = { timer = 0, city = nil, pending = nil }

-- The grip condition to winter.lua, at its changes (loading it with the first wet road).
local function setGrip(cond)
  if cond == grip then return end
  grip = cond
  local wm = package.loaded['beamngEarth/winter']
  if cond and not wm then wm = require('beamngEarth/winter') end
  if wm and wm.setShown then pcall(wm.setShown, cond) end
end

local function refOf(k) return M.v2 and M.puddleRefV2(k) or M.puddleRef(k) end

-- The level's environment map: ours (cube) while wet with the new look, the level's own back otherwise (only where ours
-- is still set: someone else's change is theirs).
local env = nil           -- { orig = the level's own map, set = ours }
local function setEnv(cube)
  local li = scenetree and scenetree.findObject('theLevelInfo')
  if not li then env = nil return end
  if cube and M.V2_CUBES and M.V2_ROUTE ~= 'material' then
    if env and env.set == cube then return end
    if not scenetree.findObject(cube) then return end
    env = env or { orig = li:getField('globalEnviromentMap', 0) }
    pcall(function()
      li:setField('globalEnviromentMap', 0, cube)
      if li.postApply then li:postApply() end
    end)
    env.set = cube
    M.writes = M.writes + 1
  elseif env then
    if li:getField('globalEnviromentMap', 0) == env.set then
      pcall(function()
        li:setField('globalEnviromentMap', 0, env.orig or '')
        if li.postApply then li:postApply() end
      end)
      M.writes = M.writes + 1
    end
    env = nil
  end
end

-- The new look on the puddle material (its own fields kept to put back), once the material is loaded; the sky file's
-- cubemaps loaded with it (never with the look off).
local LOOK_FIELDS = { { 'baseColorFactor', 'base' }, { 'roughnessFactor', 'rough' }, { 'metallicFactor', 'metal' } }
local function applyLook(cube)
  if look then return true end
  local m = scenetree and scenetree.findObject(M.PUDDLE_MAT)
  if not m then return false end
  look = { orig = { cubemap = m:getField('cubemap', 0) } }
  for _, f in ipairs(LOOK_FIELDS) do look.orig[f[1]] = m:getField(f[1], 0) end
  if M.V2_CUBES and cube and not scenetree.findObject(cube) and loadJsonMaterialsFile then
    pcall(loadJsonMaterialsFile, M.V2_SKY_FILE)
  end
  pcall(function()
    for _, f in ipairs(LOOK_FIELDS) do m:setField(f[1], 0, M.V2_LOOK[f[2]]) end
    if M.V2_CUBES and M.V2_ROUTE ~= 'env' and cube and scenetree.findObject(cube) then
      m:setField('cubemap', 0, cube)
      look.cube = cube
    end
    m:setField('alphaRef', 0, tostring(refOf(puddle)))
    m:flush()
    m:reload()
  end)
  M.writes = M.writes + 1
  return true
end

local function restoreLook()
  puddle = math.min(puddle, M.PUDDLE_STEPS)      -- (the old steps again)
  setEnv(nil)
  if not look then return end
  local m = scenetree and scenetree.findObject(M.PUDDLE_MAT)
  if m then
    pcall(function()
      for _, f in ipairs(LOOK_FIELDS) do m:setField(f[1], 0, look.orig[f[1]] or '') end
      m:setField('cubemap', 0, look.orig.cubemap or '')
      m:setField('alphaRef', 0, tostring(M.puddleRef(puddle)))
      m:flush()
      m:reload()
    end)
    M.writes = M.writes + 1
  end
  look = nil
end

local function setCube(cube)
  if M.V2_ROUTE == 'env' then return end
  if not (look and M.V2_CUBES and cube) or look.cube == cube then return end
  local m = scenetree.findObject(M.PUDDLE_MAT)
  if not (m and scenetree.findObject(cube)) then return end
  pcall(function() m:setField('cubemap', 0, cube); m:flush(); m:reload() end)
  look.cube = cube
  M.writes = M.writes + 1
end

-- Is the camera among buildings? HORIZON_RAYS level rays from 2 m over it, HORIZON_CITY of them hitting within
-- HORIZON_RAY_M.
local function cityAround()
  local cam = rawget(_G, 'core_camera')
  local p = cam and cam.getPosition and cam.getPosition()
  if not (p and type(castRayStatic) == 'function') then return nil end
  local hits = 0
  for k = 0, M.HORIZON_RAYS - 1 do
    local a = 2 * math.pi * k / M.HORIZON_RAYS
    local d = castRayStatic(vec3(p.x, p.y, p.z + 2), vec3(math.cos(a), math.sin(a), 0), M.HORIZON_RAY_M)
    if d and d < M.HORIZON_RAY_M then hits = hits + 1 end
  end
  return hits >= M.HORIZON_CITY
end

-- The puddles to step k: the material's threshold, then shown or hidden (roads.lua setPuddles).
local function setPuddles(k)
  if k == puddle then return end
  puddle = k
  local m = scenetree and scenetree.findObject(M.PUDDLE_MAT)
  if m then
    pcall(function()
      m:setField('alphaRef', 0, tostring(refOf(k)))
      m:flush()
      m:reload()
    end)
    M.writes = M.writes + 1
  end
  local roads = package.loaded['beamngEarth/roads']
  if roads and roads.setPuddles then pcall(roads.setPuddles, k > 0) end
end

local function parse(s)
  local t = {}
  for v in tostring(s or ''):gmatch('[-%d%.eE]+') do t[#t + 1] = tonumber(v) end
  return #t >= 3 and t or nil
end

local function scan()
  known = {}
  for _, nm in ipairs((scenetree.findClassObjects and scenetree.findClassObjects('Material')) or {}) do
    local cls = M.classOf(nm)
    if cls then known[nm] = cls end
  end
end

local function writeOne(name, k)
  local m = scenetree.findObject(name)
  if not m then orig[name] = nil return end
  local o = orig[name]
  if not o then
    if k == 0 then return end
    local rough = tonumber(m:getField('roughnessFactor', 0))
    -- (no factor set: the engine's default white, so the colour map is darkened too)
    o = { class = known[name], color = parse(m:getField('baseColorFactor', 0)) or { 1, 1, 1, 1 }, rough = rough, ours = {} }
    orig[name] = o
  end
  local changed = false
  if k == 0 then
    -- back to its own values, only where it still holds ours (someone else's change is theirs)
    if o.ours.color and m:getField('baseColorFactor', 0) == o.ours.color and o.color then
      m:setField('baseColorFactor', 0, string.format('%.4f %.4f %.4f %.4f', o.color[1], o.color[2], o.color[3], o.color[4] or 1))
      changed = true
    end
    if o.ours.rough and m:getField('roughnessFactor', 0) == o.ours.rough then
      m:setField('roughnessFactor', 0, string.format('%.4f', o.rough or 1))
      changed = true
    end
    orig[name] = nil
  else
    local col, ro = M.wetValues(o.class, o.color, o.rough, k)
    if col then m:setField('baseColorFactor', 0, col); o.ours.color = m:getField('baseColorFactor', 0); changed = true end
    if ro then m:setField('roughnessFactor', 0, ro); o.ours.rough = m:getField('roughnessFactor', 0); changed = true end
  end
  if changed then
    m:flush()          -- (seasonlook.lua's order: flush, then reload)
    m:reload()
    M.writes = M.writes + 1
  end
end

local function startLevel(k)
  if not known or k > 0 then scan() end
  level = k
  -- the terrain's paved layers (the city carriageways) follow at the next block swap (terrain.lua setWet: 0 dry, 1 damp,
  -- 2 wet)
  local ter = package.loaded['beamngEarth/terrain']
  if ter and ter.setWet then pcall(ter.setWet, M.terrainStep(k)) end
  queue = {}
  if k == 0 then
    for name in pairs(orig) do queue[#queue + 1] = name end
  else
    for name in pairs(known) do queue[#queue + 1] = name end
  end
  table.sort(queue)
end

-- W1's road condition under the car ('dry', 'wet', 'packed' ...), or nil without a road surface state: winter.lua's
-- stableRow (a wet film the same on every plow class: no flip of the whole scene at a turn onto another street).
local function winterCond()
  local wm = package.loaded['beamngEarth/winter']
  if not (wm and wm._state) then return nil end
  local ok, state, cls = pcall(wm._state)
  if not ok or type(state) ~= 'table' or type(state.roads) ~= 'table' then return nil end
  local row
  if wm.stableRow then row = wm.stableRow(state.roads, tonumber(cls) or 2)
  else row = state.roads[(tonumber(cls) or 2) + 1] end
  return type(row) == 'table' and row[1] or nil
end

-- W1's wetness of the road under the car (nil without a road surface state) and the place it is for (winter.place).
local function winterWet()
  local wm = package.loaded['beamngEarth/winter']
  local ok, place = pcall(function() return wm and wm.place and wm.place() or 0 end)
  return M.wetFromCondition(winterCond()), ok and place or 0
end

-- Pure: W1's road wetness as shown after dt s, w moving toward the state's (target): wetting as the rain model does
-- (WET_TAU_S), drying at DRY_S. A new state each UTC hour (wet, then damp, then dry as the film dries) used to switch
-- every wet material and the terrain's carriageways at once; now they follow over minutes.
function M.roadStep(w, target, dt)
  w, target, dt = tonumber(w) or 0, tonumber(target) or 0, math.max(0, tonumber(dt) or 0)
  if target > w then return w + (target - w) * (1 - math.exp(-dt / M.WET_TAU_S)) end
  return math.max(target, w - dt / M.DRY_S)
end

-- The new look's check (every CHECK_S): the wet state synced to the weather shown (src(): weather.lua's wetInputs),
-- the puddles' step from its standing water (none under snow and ice), the cubemap by the sky and the horizon.
local function checkV2(on, gripOn, src, dt)
  ws = ws or require('beamngEarth/wetstate')
  local ctx = (on or gripOn) and src and src() or nil
  if ctx then
    ws.sync(ctx.msg, ctx.src, ctx.key, ctx.hist, ctx.lat, ctx.lon, ctx.snap)
  else
    ws.reset()           -- (both off, or the weather off: no memory of the rain kept, as the own model)
  end
  own = ws.wetness()
  local step = on and select(1, ws.puddleStep(winterCond(), ctx and ctx.tC)) or 0
  if on and (own > 0 or step > 0 or puddle > 0) then
    -- the horizon: looked at every HORIZON_S while wet; the cubemap follows a class seen twice in a row
    horizon.timer = horizon.timer + dt
    if horizon.city == nil or horizon.timer >= M.HORIZON_S then
      horizon.timer = 0
      local c = cityAround()
      if c ~= nil then
        if horizon.city == nil then horizon.city = c
        elseif c ~= horizon.city then
          if horizon.pending == c then horizon.city, horizon.pending = c, nil else horizon.pending = c end
        else horizon.pending = nil end
      end
    end
    local cube = M.skyCube(ctx and ctx.msg, horizon.city ~= false)
    if applyLook(cube) then setCube(cube) end
    setEnv(cube)
  else
    setEnv(nil)
  end
  return step
end

-- Every frame from weather.lua while the weather is on or a wet look is applied: rainMmH = the rain shown (mm/h, its
-- intensity included; 0 with the weather off), on = the wet look's setting, gripOn = the grip's (weather.lua
-- WET_GRIP), src = with the new look, a function giving the weather shown for wetstate (weather.lua's wetInputs).
-- Returns true while it has work, a look applied or a wet grip.
function M.update(dt, rainMmH, on, gripOn, src)
  dt = tonumber(dt) or 0
  if queue then
    for _ = 1, M.BATCH do
      local name = table.remove(queue)
      if not name then break end
      pcall(writeOne, name, level)
    end
    if #queue == 0 then queue = nil end
    return true
  end
  timer, rescan = timer + dt, rescan + dt
  if timer < M.CHECK_S then return level > 0 or own > 0 or grip ~= nil or puddle > 0 end
  local step = nil
  local dtCheck = timer
  if M.v2 then
    step = checkV2(on, gripOn, src, timer)
  else
    own = (on or gripOn) and M.wetStep(own, rainMmH, timer) or 0      -- (both off: no memory of the rain kept)
  end
  timer = 0
  setGrip(gripOn and M.gripCond(own, grip) or nil)
  -- W1's road wetness: a new place's at once (a teleport: not the last place's drying), one place's over minutes
  local target, at = winterWet()
  if target == nil then
    road, roadPlace = nil, nil
  elseif road == nil or at ~= roadPlace then
    road, roadPlace = target, at
  else
    road = M.roadStep(road, target, dtCheck)
  end
  local w, wsrc = M.wetOf(on, road, own)
  M.source = wsrc
  setPuddles(step or M.puddleStep(w))
  local k = M.levelOf(w)
  if k ~= level then
    startLevel(k)
  elseif k > 0 and rescan >= M.RESCAN_S then
    rescan = 0
    local before = known
    scan()
    queue = {}
    for name in pairs(known) do if not (before and before[name]) or not orig[name] then queue[#queue + 1] = name end end
    if #queue == 0 then queue = nil end
  end
  return level > 0 or own > 0 or queue ~= nil or grip ~= nil or puddle > 0 or (M.v2 and ws ~= nil and ws.active())
    or (road or 0) > 0
end

-- The new puddle look on or off (weather.lua, the setting): off puts the material's own values back at once and the
-- old model takes over at the next check (from the film the new one had reached).
function M.setV2(on)
  on = on and true or false
  if on == M.v2 then return end
  M.v2 = on
  if not on then
    restoreLook()
    if ws then ws.reset() end
  else
    horizon = { timer = 0, city = nil, pending = nil }
    local m = scenetree and scenetree.findObject(M.PUDDLE_MAT)     -- (the threshold on the new steps at the next change)
    if m and puddle > 0 then pcall(function() m:setField('alphaRef', 0, tostring(refOf(puddle))); m:flush(); m:reload() end) end
  end
  timer = M.CHECK_S          -- (checked at the next update)
end

-- One line on what is wet (the probes' overlay, the console): materials written per class and a few names.
function M.debugLine()
  local per, names = {}, {}
  for name, o in pairs(orig) do
    per[o.class or '?'] = (per[o.class or '?'] or 0) + 1
    if #names < 6 then names[#names + 1] = name end
  end
  local parts = {}
  for k, n in pairs(per) do parts[#parts + 1] = k .. '=' .. n end
  table.sort(parts)
  table.sort(names)
  return string.format('wet lvl=%d own=%.2f src=%s grip=%s puddles=%d writes=%d %s | %s', level, own, tostring(M.source),
    tostring(grip), puddle, M.writes,
    table.concat(parts, ' '), table.concat(names, ' '))
end

-- The names written of one class, sorted (the probes).
function M.debugNames(class)
  local out = {}
  for name, o in pairs(orig) do if o.class == class then out[#out + 1] = name end end
  table.sort(out)
  return table.concat(out, ' ')
end

function M.level() return level end
function M.wetness() return own end
function M.roadWetness() return road end      -- (W1's road wetness shown, nil: none)
function M.grip() return grip end
-- The puddles' alpha threshold shown now (255: none; aquaplane.lua's water depth in them).
function M.puddleRefShown() return refOf(puddle) end

-- The level goes: nothing to restore (the materials load again with the next level); state cleared.
function M.reset()
  own, level, queue, orig, known, timer, rescan, puddle = 0, 0, nil, {}, nil, 0, 0, 0
  road, roadPlace = nil, nil
  look, horizon, env = nil, { timer = 0, city = nil, pending = nil }, nil   -- (the material and the level info load again)
  if ws then ws.reset() end
  setGrip(nil)
  local roads = package.loaded['beamngEarth/roads']
  if roads and roads.setPuddles then pcall(roads.setPuddles, false) end      -- (the next level's puddles made hidden)
end

-- A teleport: the rain's memory stays behind (the user, 2026-11-27: "roads stay wet if you teleport to a place with
-- different weather"); the next check (now) dries the look, the puddles and the grip unless the new place's real road
-- state says wet, and its own rain wets them again within minutes (WET_TAU_S).
function M.onTeleport()
  own = 0
  road, roadPlace = nil, nil   -- (the road state's wetness: the state's own at once, not drying from the last place's)
  timer = M.CHECK_S
  -- (the new puddle look: wetstate's real-weather memory too, the same way; a preset's or the BeamMP host's stays)
  if ws then ws.onTeleport() end
end

-- Off now (the setting, weather Off): every material back at once.
function M.restoreAll()
  for name in pairs(orig) do pcall(writeOne, name, 0) end
  orig, queue, level = {}, nil, 0
  setPuddles(0)
  restoreLook()
  if ws then ws.reset() end
  local ter = package.loaded['beamngEarth/terrain']
  if ter and ter.setWet then pcall(ter.setWet, 0) end
end

M._setOwn = function(w) own = w end      -- (tests)

return M
