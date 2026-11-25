-- BeamNG.Earth: the wet look of our meshes (the weather plan 4.4 item 6; weather phase B): sidewalks, curbs, markings,
-- mesh roads and bridge decks, buildings and roofs go darker and glossier while the ground is wet (Lagarde's wet
-- surfaces: a porous surface darkens as water fills its pores, every surface's roughness falls as a water film forms;
-- screen-space reflections, where the player has them, then show the lamps and the sky in it).
--
-- How wet: winter W1's road surface state when the server sends one (beamngEarth/winter's snowstate: the road class
-- under the car, its condition dry / damp / wet / salt_wet / slush ...; BNE_WET_ROADS), else this module's own model
-- from the rain the weather shows (wetness rises within minutes in rain and dries over DRY_S after it). The level is
-- quantised to LEVELS steps, and only a step change writes materials: one batch, at most BATCH per frame (setField and
-- reload each), never per frame otherwise (SkyCycle's stutters came from refreshing materials all the time, the plan's
-- research note 2). Dry or the weather Off: every material back to its own values (only those still holding ours).
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
  asphalt = { 0.55, 0.75 },   -- mesh carriageways, decks, asphalt walks: dark and shiny wet
  concrete = { 0.6, 0.6 },    -- sidewalks, curbs, slabs, setts, concrete decks
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
M.WET_DARK = 0.7          -- a porosity-1 surface's colour at full wetness: x (1 - WET_DARK) (Lagarde's 0.2..0.5); at 0.6 the
                          -- Tokyo sidewalks hardly showed it in the broker shots (wb9), at 0.95 clearly (wb11)
M.WET_ROUGH = 0.85        -- a gloss-1 surface's roughness falls by this share at full wetness
M.ROUGH_MIN = 0.06

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

-- Pure: the wetness from W1's road row ({ condition, ... }), or nil.
function M.wetFromCondition(cond)
  return cond and M.CONDITION_WET[cond] or nil
end

------------------------------------------------------------------------------------------
-- runtime

local own = 0             -- the own model's wetness
local level = 0           -- the level applied (or being applied)
local queue = nil         -- names left to write for the level
local orig = {}           -- name -> { class, color = {...} or nil, rough = number or nil, ours = { color, rough } }
local known = nil         -- name -> class of the materials seen (nil: not scanned)
local timer, rescan = 0, 0
M.writes, M.source = 0, 'none'

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
  queue = {}
  if k == 0 then
    for name in pairs(orig) do queue[#queue + 1] = name end
  else
    for name in pairs(known) do queue[#queue + 1] = name end
  end
  table.sort(queue)
end

-- W1's wetness of the road under the car, or nil without a road surface state.
local function winterWet()
  local wm = package.loaded['beamngEarth/winter']
  if not (wm and wm._state) then return nil end
  local ok, state, cls = pcall(wm._state)
  if not ok or type(state) ~= 'table' or type(state.roads) ~= 'table' then return nil end
  local row = state.roads[(tonumber(cls) or 2) + 1]
  return type(row) == 'table' and M.wetFromCondition(row[1]) or nil
end

-- Every frame from weather.lua while the weather is on or a wet look is applied: rainMmH = the rain shown (mm/h, its
-- intensity included; 0 with the weather off), on = the setting. Returns true while it has work or a look applied.
function M.update(dt, rainMmH, on)
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
  if timer < M.CHECK_S then return level > 0 or own > 0 end
  own = on and M.wetStep(own, rainMmH, timer) or 0      -- (off: no memory of the rain kept)
  timer = 0
  local w = on and winterWet() or nil
  M.source = w and 'road state' or (on and 'rain' or 'off')
  w = on and (w or own) or 0
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
  return level > 0 or own > 0 or queue ~= nil
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
  return string.format('wet lvl=%d own=%.2f src=%s writes=%d %s | %s', level, own, tostring(M.source), M.writes,
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

-- The level goes: nothing to restore (the materials load again with the next level); state cleared.
function M.reset()
  own, level, queue, orig, known, timer, rescan = 0, 0, nil, {}, nil, 0, 0
end

-- Off now (the setting, weather Off): every material back at once.
function M.restoreAll()
  for name in pairs(orig) do pcall(writeOne, name, 0) end
  orig, queue, level = {}, nil, 0
end

M._setOwn = function(w) own = w end      -- (tests)

return M
