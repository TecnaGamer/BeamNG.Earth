-- Live air traffic: real aircraft in the sky round the player, from a public ADS-B network (extension
-- beamngEarth_airtraffic). Based on devo343's Live Air Traffic add-on (2.10.3), with permission (CREDITS.md): its
-- prediction, drawing, lights, contrails, sounds and cameras, re-plumbed into BeamNG.Earth.
--
-- OFF by default (settings airTraffic, with its consent text: turning it on says what is sent and to whom). While it
-- is on and the player is in the world, the mod asks the BeamNG.Earth app every POLL_S ('air_traffic' over the usual
-- connection; server/airtraffic.py asks adsb.lol, airplanes.live or adsb.fi about a ~28 km cell's point, never the
-- exact position, and earthfast.adsb_compact cuts the answer to the compact rows). Every frame each aircraft is moved
-- along its own ground speed, track, turn rate and climb rate from its position's own UTC time (dead reckoning), so
-- the sky moves smoothly between answers and two games reading the same answer (BeamMP) agree; a fresh position is
-- glided to over a few seconds instead of jumping.
--
-- While it is off NOTHING runs per frame: no onUpdate / onPreRender hook is registered at all (extensions.hookUpdate
-- takes them out), no request is sent, no object exists.
--
-- What is drawn (the add-on's, budgeted by the quality preset: BUDGETS):
--   * a 3D model per aircraft within modelKm (the nearest maxModels; shapes with detail levels, a livery overlay
--     tinted by instanceColor), banked in turns, pitched in climbs, gear on the ground and on final; spinning props
--   * lights (beacons, strobes, navigation, landing lights) only on aircraft overhead or close, by the drawn sun
--   * contrails behind jets above the contrail level (Schmidt-Appleman from the real weather's temperature, pressure
--     and the high cloud's humidity: predict.contrailFormation), lasting longer in humid air aloft, drifting with the
--     real wind aloft, hidden behind the cloud deck; at most trailSegments prisms a frame
--   * sounds of the aircraft near enough to hear (airtraffic/sound.lua), heard from where the aircraft was
--   * labels, off by default (Ctrl+Alt+K: flight number, full, type, off), on the nearest few aircraft and the one
--     looked at, never on parked ones; on the aircraft the camera follows, its painted flight number and registration
--   * a notice offering to switch devo343's own add-on off while it is on beside this (every aircraft twice)
--   * the aircraft on the 2D and 3D maps (mapItems: settings.uiAirTraffic)
--
-- Keys (Options > Controls > "BeamNG.Earth"; the add-on's bare F and Ctrl+Alt+P clash with the game's walking mode and
-- photo mode, and Ctrl+Alt+L the editor's lock selection, so they moved):
--   Ctrl+Alt+A  air traffic on / off (the first time: the settings page, for the consent)
--   Ctrl+Alt+K  labels: off -> flight number -> full -> aircraft type -> off
--   Ctrl+Alt+C  contrails on / off
--   Ctrl+Alt+I  what is that plane? (the one nearest the middle of the screen)
--   Ctrl+Alt+S  aircraft sounds on / off
--   Ctrl+Alt+F  follow the aircraft nearest the middle of the screen; again: sit inside it; a third time: camera back
--
-- Every position is kept in lat / lon and converted each frame through the current anchor (geodeticToWorld), so a
-- rebase or a teleport never misplaces anything.
local M = {}

local predict = require('beamngEarth/airtraffic/predict')
local DIMS = require('beamngEarth/airtraffic/dims')
-- An aircraft's airframe dimensions: its model's, else its class's, else a narrowbody's (the add-on's dimsOf called
-- itself here: a model without dims recursed until the stack overflowed).
local function dimsOf(it) return DIMS[it.model] or DIMS[it.kind] or DIMS.narrow end
M._dimsOf = dimsOf
local sound = require('beamngEarth/airtraffic/sound')
local livery = require('beamngEarth/airtraffic/livery')
local GLYPHS = require('beamngEarth/airtraffic/glyphs')
local okPal, PALETTES = pcall(require, 'beamngEarth/airtraffic/palettes')
if not okPal or type(PALETTES) ~= 'table' then PALETTES = {} end

M.SHAPES = '/levels/beamng_earth/art/shapes/airtraffic/'
-- airframes without a livery overlay file (the fighter's fin is in its body: tools/build_airtraffic_shapes.py; the
-- helicopters have dims liveries = 0). Every other overlay carries the fin and engine covers, so it always goes with
-- its body.
M.NO_LIVERY = { fighter = true, heli = true, heli_twin = true, heli_tandem = true }
local SHAPES = M.SHAPES
local FT = 0.3048
M.CONSENT_VERSION = 1      -- settings airTrafficConsent must be at least this (the consent text changed: ask again)

-- Settings (settings.lua airTraffic*; the first group's budgets come from the quality preset: BUDGETS)
M.cfg = {
  modelKm = 35,            -- 3D models within this distance (lights and contrails go further)
  maxModels = 40,          -- at most this many models at once, nearest first
  maxAircraft = 250,       -- at most this many aircraft tracked (the nearest; the server sends up to 400)
  trailSegments = 2000,    -- contrail prisms drawn a frame, at most
  maxVoices = 6,           -- aircraft heard at once
  lightsKm = 150,          -- lights drawn out to here (the game's view distance clips anyway)
  labelKm = 60,
  maxLabels = 5,           -- labels on the nearest this many (and the aircraft the camera looks at): no stacked column
  labelAimDeg = 12,        -- the aircraft nearest the look direction within this angle gets a label too
  contrailSeconds = 90,    -- how long a contrail stays in still, dry air before it has faded (x contrailLife)
  -- Aircraft lights (all of them: beacons, strobes, navigation and landing lights) show only on aircraft passing
  -- overhead or close by, as in real life: a plane low on the horizon 30 km off shows no lights by day.
  lightsNearKm = 3,        -- by day: any aircraft this close ...
  lightsOverheadDeg = 45,  -- ... or this high above the horizon ...
  lightsMaxKm = 14,        -- ... and no farther than this (a jet at 35,000 ft straight up is 10.7 km away)
  lightsNearKmNight = 8,   -- at night the same, the lights carrying farther in the dark
  lightsOverheadDegNight = 30,
  lightsMaxKmNight = 25,
  contrailClearKm = 18,    -- contrails at full strength out to here, then fading with distance (haze) ...
  contrailFarKm = 80,      -- ... to faint (contrailFarAlpha of full) from here on
  contrailFarAlpha = 0.15,
  cloudThickKm = 2.0,      -- the volumetric cloud deck's assumed depth above its base (the engine does not say)
  cloudHideModels = true,  -- also hide aircraft models (and lights) behind a thick deck
  sizeBoost = true,        -- models far away drawn up to 2.5x larger so they read as aircraft, not specks
  showGround = true,       -- parked and taxiing aircraft at airports
  soundVolume = 1.0,       -- aircraft sounds, 0..2 (Ctrl+Alt+S turns them on / off)
  textKm = 2.5,            -- the followed aircraft's flight number and registration painted on it nearer than this
  rangeNm = 60,            -- the search radius round the player (nautical miles)
  routes = false,          -- flight routes from adsbdb.com (their own switch)
}
-- Per quality preset (settings quality; the Steam Deck's Handheld runs Low or Medium): models, their reach, the
-- aircraft tracked, the contrail prisms a frame, the voices. 'custom' / unknown: high's.
M.BUDGETS = {
  low     = { maxModels = 12, modelKm = 15, maxAircraft = 120, trailSegments = 300,  maxVoices = 3 },
  medium  = { maxModels = 20, modelKm = 20, maxAircraft = 160, trailSegments = 600,  maxVoices = 4 },
  high    = { maxModels = 40, modelKm = 35, maxAircraft = 250, trailSegments = 2000, maxVoices = 6 },
  ultra   = { maxModels = 60, modelKm = 40, maxAircraft = 400, trailSegments = 4000, maxVoices = 6 },
  extreme = { maxModels = 60, modelKm = 40, maxAircraft = 400, trailSegments = 4000, maxVoices = 6 },
}
M.POLL_S = 15              -- the app is asked this often (its answer is shared per cell for 15 s)
M.RETRY_S = 5              -- ... sooner after an error (the app's own limiter, a teleport to a new cell)
M.REQUEST_TIMEOUT_S = 40   -- no answer this long: ask again
M.MISSING_KEEP_S = 45      -- an aircraft missing from the list is moved along this long before it is dropped
M.BLEND_S = 4              -- a new position is glided to over this long
M.SNAP_M = 6000            -- a correction larger than this is a jump, not a glide
M.CONTRAIL_SAMPLE_S = 1.5
M.MODEL_TICK_S = 0.5       -- models chosen this often ...
M.CREATE_PER_TICK = 3      -- ... at most this many new ones each time
M.LABEL_MODES = { 'flight', 'full', 'type', 'off' }   -- (Ctrl+Alt+K from off: flight numbers first)

-- The switches (settings.lua: airTraffic, airTrafficLabels, airTrafficContrails, airTrafficSounds)
M.enabled = false
M.labelMode = 'off'
M.contrails = true
M.sounds = true

local hooked = false
local clock = 0
local pollTimer = 0
local pending = nil        -- { id, t } of the request in flight
local reqId = 0
local items = {}           -- hex -> aircraft
local trails = {}          -- hex -> { pts = {...}, kind } (outlive their aircraft until they fade)
local trailsDirty = false  -- the world's origin moved: cached game positions of trail points are stale
local status = { connected = false, count = 0, error = nil, source = nil, credit = nil, answers = 0, at = nil }
local contrailFt = predict.CONTRAIL_DEFAULT_FT
local contrailLife = 1     -- contrailSeconds x this (humid air aloft: longer)
local windAloft = nil      -- vec3 m/s the contrails drift with (the real wind aloft), nil: not known yet
local weatherTimer = 0
local modelTimer = 0
local night = false
local dayK = 1             -- 1 by day, 0 by night on the ground, smooth through dusk and dawn
local sunElev = 30         -- the sun's elevation, degrees (by day until told otherwise)
local daylightSource = nil
local nightTimer = 0
local scaleOk = true
local wasInWorld = false
local frameData = nil
local anchor = nil         -- this frame's anchor (game frame): toGame through it
local group = nil          -- the SimGroup every object of ours is in

------------------------------------------------------------------------------------------ small helpers
local function logW(msg) if log then log('W', 'beamngEarth.airtraffic', msg) end end
local function logI(msg) if log then log('I', 'beamngEarth.airtraffic', msg) end end

local function message(text, seconds)
  if ui_message then pcall(ui_message, text, seconds or 4, 'beamngEarthAirTraffic') end
end

local function hash01(s)
  local h = 0
  for i = 1, #s do h = (h * 31 + s:byte(i)) % 1000003 end
  return h / 1000003
end

local function coreState()
  local c = rawget(_G, 'beamngEarth_core')
  if not (c and c._state) then return nil end
  local ok, st = pcall(c._state)
  return ok and type(st) == 'table' and st or nil
end

-- lat, lon, h (m over the ellipsoid) to game x, y, z through this frame's anchor; nil without one.
local function toGame(lat, lon, h)
  local a = anchor
  if not (a and a.geodeticToWorld and lat and lon) then return nil end
  return a:geodeticToWorld(lat, lon, h or 0)
end
M._toGame = function(...) return toGame(...) end

local function settingsMod()
  local ok, s = pcall(require, 'beamngEarth/settings')
  return ok and type(s) == 'table' and s or nil
end

local function setting(key, default)
  local s = settingsMod()
  if not (s and s.get) then return default end
  local ok, v = pcall(s.get, key)
  if ok and v ~= nil then return v end
  return default
end

local function saveSetting(key, v)
  local s = settingsMod()
  if s and s.set then pcall(s.set, key, v) end
end

-- The quality preset's budgets into cfg (settings quality, airTrafficMaxModels 0 = by the preset).
function M.applyBudgets(quality)
  local b = M.BUDGETS[quality or setting('quality', 'high')] or M.BUDGETS.high
  for k, v in pairs(b) do M.cfg[k] = v end
  local mm = tonumber(setting('airTrafficMaxModels', 0)) or 0
  if mm > 0 then M.cfg.maxModels = math.floor(mm) end
  sound.MAX_VOICES = M.cfg.maxVoices
end

-- The other settings into cfg and the switches (settings.lua APPLY calls this on every change).
function M.applySettings()
  M.labelMode = setting('airTrafficLabels', 'off')
  local okMode = false
  for _, m in ipairs(M.LABEL_MODES) do if m == M.labelMode then okMode = true end end
  if not okMode then M.labelMode = 'off' end
  M.contrails = setting('airTrafficContrails', true) ~= false
  M.sounds = setting('airTrafficSounds', true) ~= false
  M.cfg.soundVolume = math.max(0, math.min(2, (tonumber(setting('airTrafficVolume', 100)) or 100) / 100))
  M.cfg.showGround = setting('airTrafficGround', true) ~= false
  M.cfg.sizeBoost = setting('airTrafficSizeBoost', true) ~= false
  M.cfg.rangeNm = math.max(10, math.min(150, tonumber(setting('airTrafficRangeNm', 60)) or 60))
  M.cfg.routes = setting('airTrafficRoutes', false) == true
  M.applyBudgets()
  if not M.contrails then trails = {} end
  if not M.sounds then sound.clear() end
end

-- Consent given for the text now shown (settings airTrafficConsent)?
function M.consented()
  if M.testOn then return true end
  return (tonumber(setting('airTrafficConsent', 0)) or 0) >= M.CONSENT_VERSION
end

------------------------------------------------------------------------------------------ models (TSStatic)
local placed
local function deleteObj(id)
  if not id then return end
  if placed then placed[id] = nil end
  local o = scenetree and scenetree.findObjectById and scenetree.findObjectById(id)
  if o then pcall(o.delete, o) end
end

local function sceneGroup()
  if group and scenetree.findObjectById and scenetree.findObjectById(group) then return scenetree.findObjectById(group) end
  group = nil
  local ok, g = pcall(function()
    local x = createObject('SimGroup')
    x.canSave = false
    x:registerObject('')
    if scenetree.MissionGroup then scenetree.MissionGroup:addObject(x) end
    return x
  end)
  if ok and g then group = g:getID() return g end
  return scenetree.MissionGroup
end

-- tint: { r, g, b } for the livery overlay (its material lat_liv multiplies by the instance colour)
local function makeStatic(shape, tint)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, shape)
  o:setField('decalType', 0, 'None')
  o:setField('collisionType', 0, 'None')
  o:setField('dynamic', 0, 'true')      -- (a static add redraws the cached sun shadow map: shadow-cache-pulse)
  if tint then
    o.useInstanceRenderData = true
    o:setField('instanceColor', 0, string.format('%.3f %.3f %.3f 1', tint[1], tint[2], tint[3]))
  end
  o.canSave = false
  o:registerObject('')
  local g = sceneGroup()
  if g then g:addObject(o) end
  return o:getID()
end
M._makeStatic = makeStatic

local function dropModel(it)
  deleteObj(it.objId); deleteObj(it.gearId)
  for _, rt in ipairs(it.rotors or {}) do deleteObj(rt.id) end
  deleteObj(it.livId); deleteObj(it.cabId)
  it.cabId, it.cabScale = nil, nil
  for _, g in ipairs(it.glyphs or {}) do deleteObj(g.id) end
  it.objId, it.gearId, it.objScale, it.gearScale, it.cloudHidden, it.rotors = nil, nil, nil, nil, nil, nil
  it.livId, it.livScale, it.glyphs, it.textKey = nil, nil, nil, nil
end

local stretchNow = 1      -- the length factor of the aircraft being placed (livery.stretch: an A321 is a longer A320)

placed = {}   -- object id -> where it was put this frame { x, y, z, q } (the seat view re-locks to the camera)
local placing = false   -- the aircraft being placed is the one the camera sits in
local function placeObj(id, pos, q, scale, lastScale, stretch)
  local o = id and scenetree.findObjectById(id)
  if not o then return nil end
  o:setPosRot(pos.x, pos.y, pos.z, q.x, q.y, q.z, q.w)
  if placing then placed[id] = { pos.x, pos.y, pos.z, q } end     -- (only the seat view's aircraft: lockCabin)
  if scaleOk and (not lastScale or math.abs(scale - lastScale) > 0.04 * lastScale) then
    local sy = scale * (stretch or 1)
    local ok = pcall(function() o:setScale(vec3(scale, sy, scale)) end)
    if not ok then scaleOk = false end
    return scale
  end
  return lastScale
end

------------------------------------------------------------------------------------------ lettering
-- Flight number along the forward fuselage (where airline titles are painted) and registration on the rear one,
-- both sides, as real letters (tools/build_glyphs.py: one shape per character, cap height 1, face +X, writing +Y,
-- up +Z), each laid on the fuselage's curve where it sits and reading forward on the right side, aft on the left.
local GLYPH_CREATE_PER_FRAME = 24
local glyphBudget = 0

local function layoutTexts(it, d)
  local out = {}
  if d.layout == 'none' then return out end
  local lays = d.layout == 'heli' and livery.LAYOUT_HELI or livery.LAYOUT
  for _, t in ipairs(livery.texts(it.kind, it.raw.f, it.raw.r, GLYPHS)) do
    local lay = lays[t.role]
    if lay then
      local R, L = d.R, d.len
      local yc = (it.model == 'jumbo' and t.role == 'title') and 0.02 * L or lay.y * L
      local H, zc = lay.h * R, lay.z * R
      local phi = math.asin(math.max(-0.95, math.min(0.95, zc / R)))
      local xs = R * math.cos(phi) + 0.035
      local width = 0
      for ch in t.text:gmatch('.') do width = width + GLYPHS[ch].adv end
      width = width * H
      for _, side in ipairs({ 1, -1 }) do
        local c = 0
        for ch in t.text:gmatch('.') do
          local g = GLYPHS[ch]
          local y = side == 1 and (yc - width / 2 + c * H) or (yc + width / 2 - c * H)
          local tx, tz = -side * math.sin(phi), math.cos(phi)     -- "up" along the fuselage's curve
          out[#out + 1] = { shape = g.shape, side = side, H = H,
            bx = side * xs - tx * H * 0.5, by = y, bz = zc - tz * H * 0.5, tx = tx, tz = tz }
          c = c + g.adv
        end
      end
    end
  end
  return out
end

local function placeTexts(it, d, pos, f, r, u, s)
  if not it.glyphs then
    it.glyphs = layoutTexts(it, d)
    it.textKey = it.raw.f
  end
  for _, g in ipairs(it.glyphs) do
    if not g.id and glyphBudget > 0 then
      glyphBudget = glyphBudget - 1
      local ok, id = pcall(makeStatic, SHAPES .. g.shape .. '.dae')
      if ok then
        g.id = id
        if it.cloudHidden then
          local o = scenetree.findObjectById(id)
          if o and o.setHidden then pcall(o.setHidden, o, true) end
        end
      end
    end
    if g.id then
      local p = pos + r * (g.bx * s) + f * (g.by * s * stretchNow) + u * (g.bz * s)
      local q = quatFromDir(f * g.side, r * g.tx + u * g.tz)
      g.scale = placeObj(g.id, p, q, g.H * s, g.scale)
    end
  end
end

------------------------------------------------------------------------------------------ propellers and rotors
-- The blades are separate shapes (lat_rotor_<shape>.dae, blades round their +Y axis), turned every frame about the
-- propeller's axis (forward), the main rotor's (up) or the tail rotor's (sideways). Spin follows the engine: a
-- parked light aircraft's or helicopter's blades stand still, at idle they turn slower, at power fast. (Spin rates
-- are what reads as fast at 60 frames a second, not the real rpm, which would only strobe.)
-- turns per second at full power: each rotor's own (from dims.lua), or by its shape
M.ROTOR_SPIN = { turboprop = 11, light = 13, heli_main = 5.2, heli_tail = 21 }
M.ROTOR_SPINUP_S = 4

function M.rotorFrame(axis, f, r, u, angle)
  local ca, sa = math.cos(angle), math.sin(angle)
  if axis == 'up' then return u, f * ca + r * sa end
  if axis == 'right' then return r, u * ca + f * sa end
  return f, u * ca + r * sa                                -- 'fwd': a propeller
end

local function placeRotors(it, d, pos, f, r, u, s, dt)
  local rotorDefs = d.rotors
  if not rotorDefs or #rotorDefs == 0 then return end
  if not it.rotors then
    it.rotors = {}
    for i, def in ipairs(rotorDefs) do
      local ok, id = pcall(makeStatic, SHAPES .. 'lat_rotor_' .. def.shape .. '.dae')
      it.rotors[i] = { id = ok and id or nil, angle = hash01(it.id .. i) * 6.283, rate = 0 }
      if ok and it.cloudHidden then
        local o = scenetree.findObjectById(id)
        if o and o.setHidden then pcall(o.setHidden, o, true) end
      end
    end
  end
  local load = sound.engineLoad(it.kind, it.base.ground, it.base.gs, it.vs or it.base.vs, it.base.gsPrev, it.agl)
  for i, def in ipairs(rotorDefs) do
    local rt = it.rotors[i]
    if rt and rt.id then
      -- engines take a few seconds to bring the blades up to speed (or to wind down when stopped)
      local want = load and (def.spin or M.ROTOR_SPIN[def.shape] or 10) * (0.55 + 0.45 * load) or 0
      rt.rate = rt.rate + (want - rt.rate) * math.min(1, dt / M.ROTOR_SPINUP_S)
      rt.angle = (rt.angle + rt.rate * (def.dir or 1) * 2 * math.pi * dt) % (2 * math.pi)
      local p = pos + r * (def.pos[1] * s) + f * (def.pos[2] * s * stretchNow) + u * (def.pos[3] * s)
      local axis, spun = M.rotorFrame(def.axis, f, r, u, rt.angle)
      local q = quatFromDir(axis, spun)
      rt.scale = placeObj(rt.id, p, q, s, rt.scale)
    end
  end
end

local function dropAll()
  if M.stopFollow then M.stopFollow() end
  for _, it in pairs(items) do dropModel(it) end
  sound.clear()
  items, trails = {}, {}
  pending = nil
  local g = group and scenetree and scenetree.findObjectById and scenetree.findObjectById(group)
  if g then pcall(g.delete, g) end
  group = nil
end
M._dropAll = dropAll

------------------------------------------------------------------------------------------ merging the companion's list
local function labelBits(it)
  local a = it.raw
  local flight = (a.f ~= '' and a.f) or (a.r ~= '' and a.r) or a.h:upper()
  local typ = (a.t ~= '' and a.t) or it.kind
  return flight, typ
end

-- list: the app's rows (nearest first); srvNow: the app's clock as it answered (UTC ms): a row's position is
-- (srvNow - ts) / 1000 s old, so it is dated clock - that here.
local function merge(list, srvNow)
  local seen = {}
  srvNow = tonumber(srvNow)
  local n = 0
  for _, a in ipairs(list) do
    if n >= M.cfg.maxAircraft then break end
    if type(a) == 'table' and type(a.h) == 'string' and tonumber(a.lat) and tonumber(a.lon) then
      n = n + 1
      if srvNow and tonumber(a.ts) then a.age = math.max(0, math.min(predict.MAX_EXTRAP_S, (srvNow - a.ts) / 1000)) end
      a.f = type(a.f) == 'string' and a.f or ''
      a.t = type(a.t) == 'string' and a.t or ''
      a.r = type(a.r) == 'string' and a.r or ''
      seen[a.h] = true
      local b = predict.base(a, clock)
      local it = items[a.h]
      if not it then
        local kind = predict.classify(a.t, a.c) or predict.guessKind(b.altFt, b.gs)
        it = { id = a.h, kind = kind, model = predict.model(a.t, kind, a.h), phase = hash01(a.h), base = b, raw = a,
          seen = clock, born = clock,
          pal = livery.palette(a.f, a.r, a.h), stretch = livery.stretch(a.t) }
        if not b.hasTrk then b.trk = hash01(a.h .. 'h') * 360 end
        items[a.h] = it
      else
        -- glide from where it is drawn now to the new prediction
        local lat0, lon0, alt0, trk0 = it.lat, it.lon, it.altFt, it.trk
        if not b.hasTrk then b.trk = it.base.trk end
        b.gsPrev = it.base.gs                             -- (speeding up or slowing: take-off or landing)
        it.base, it.raw, it.seen, it.missing = b, a, clock, nil
        if lat0 then
          local lat1, lon1, alt1, trk1 = predict.predict(b, clock)
          local d = predict.distM(lat0, lon0, lat1, lon1)
          if d < M.SNAP_M and math.abs(alt0 - alt1) < 3000 then
            it.corr = { lat = lat0 - lat1, lon = lon0 - lon1, alt = alt0 - alt1,
              trk = predict.angleDiff(trk0, trk1), t = clock }
          else
            it.corr = nil
          end
        end
        local kind = predict.classify(a.t, a.c)
        if kind and kind ~= it.kind then it.kind = kind; dropModel(it) end
        local model = predict.model(a.t, it.kind, a.h)
        if model ~= it.model then it.model = model; dropModel(it) end
        -- (a callsign or type can arrive later: the colours, length and lettering follow)
        local pal, st = livery.palette(a.f, a.r, a.h), livery.stretch(a.t)
        if pal ~= it.pal or st ~= it.stretch or a.f ~= (it.textKey or a.f) then
          it.pal, it.stretch = pal, st
          dropModel(it)
        end
      end
    end
  end
  for id, it in pairs(items) do
    if not seen[id] and not it.missing then it.missing = clock end
  end
end

-- The app's 'air_traffic_result' (core.lua passes it on).
function M.onResult(msg)
  if type(msg) ~= 'table' then return end
  -- (only the answer to the request in flight: one for the place before a teleport, or a late one, is dropped)
  if not (pending and msg.id == pending.id) then return end
  pending = nil
  if not M.enabled then return end
  status.answers = status.answers + 1
  status.at = clock
  if msg.ok ~= true then
    status.connected, status.error = false, type(msg.error) == 'string' and msg.error or 'no answer'
    pollTimer = math.max(0, M.POLL_S - M.RETRY_S)           -- (sooner: the app's own limiter, every source resting)
    return
  end
  status.connected, status.error = true, nil
  status.source = type(msg.src) == 'string' and msg.src or nil
  if type(msg.credit) == 'string' and msg.credit ~= status.credit then
    status.credit = msg.credit
    local cr = rawget(_G, 'beamngEarth_core') and require('beamngEarth/credits')
    if cr and cr.setExtra then cr.setExtra('airtraffic', msg.credit) end
  end
  if type(msg.ac) == 'table' then merge(msg.ac, msg.srvNow) end
  local n = 0
  for _ in pairs(items) do n = n + 1 end
  status.count = n
end

local function paused()
  local sta = rawget(_G, 'simTimeAuthority')
  if not (sta and sta.getPause) then return false end
  local ok, p = pcall(sta.getPause)
  return ok and p == true
end

local function mapOpen()
  local m3 = rawget(_G, 'beamngEarth_map3d')
  if m3 and m3.isActive and m3.isActive() then return true end
  local s = settingsMod()
  if not (s and s.isMapOpen) then return false end
  local ok, on = pcall(s.isMapOpen)
  return ok and on == true
end

-- Ask the app every POLL_S (only from onUpdate: only while the feature is on and the player in the world; not while
-- the game is paused, unless a map is open to show them).
local function poll(dt, pl)
  if pending and clock - pending.t > M.REQUEST_TIMEOUT_S then pending = nil end
  pollTimer = pollTimer + dt
  if pending or pollTimer < M.POLL_S or not (pl and pl.lat and pl.lon) then return end
  if paused() and not mapOpen() then return end
  local okN, net = pcall(require, 'beamngEarth/net')
  if not (okN and net.isConnected and net.isConnected()) then return end
  pollTimer = 0
  reqId = reqId + 1
  pending = { id = reqId, t = clock }
  -- (the exact position goes only to the local app; it snaps it to its ~28 km grid before anything leaves the PC)
  net.send({ type = 'air_traffic', id = reqId, lat = pl.lat, lon = pl.lon, rangeNm = M.cfg.rangeNm,
    routes = M.cfg.routes == true })
end

------------------------------------------------------------------------------------------ per-frame state
local function camera()
  local ok, p = pcall(core_camera.getPosition)
  if not ok or not p then return nil end
  local okF, f = pcall(core_camera.getForward)
  local okV, fov = pcall(core_camera.getFovDeg)
  return p, (okF and f) or vec3(0, 1, 0), (okV and tonumber(fov)) or 60
end

-- The game's time of day (0 / 1 noon, 0.5 midnight) to the day factor: contrails fade out from 18:00 to 19:00 and
-- back in from 5:30 to 6:30.
function M.setTimeOfDay(t)
  local hour = (12 + (tonumber(t) or 0) % 1 * 24) % 24
  -- (no sun position known: a rough one from the clock, 0 at 6:00 and 18:00)
  M.setSunElevation(-50 * math.cos(2 * math.pi * hour / 24))
end

-- The sun's elevation decides day and night (BeamNG.Earth runs the real clock and the real sun, so a fixed clock
-- hour is wrong most of the year: in November the sun sets by 16:40 in New York). Contrails fade out as the sun goes
-- from 2 degrees above the horizon to 4 below (most of civil dusk) and come back the same way at dawn.
function M.setSunElevation(elev)
  elev = tonumber(elev)
  if not elev then return end
  sunElev = elev
  dayK = predict.smooth((elev + 4) / 6)
  night = elev < -0.833
end

-- Where the sun is, best source first: the sun the sky draws, BeamNG.Earth's sky (its sun for the game's clock and place),
-- the stock environment's day / night state, the game clock's hour.
-- The sun as the sky object draws it (ScatterSky 'elevation', degrees): right whatever sets the clock, the real-time
-- sky or a manual time of day (BeamNG.Earth's space clouds read it the same way). Torque keeps the angle going round:
-- over 90 is past the zenith, 180..360 below the horizon.
function M.normalizeElevation(el)
  el = tonumber(el)
  if not el then return nil end
  el = el % 360
  if el > 180 then el = el - 360 end
  if el > 90 then el = 180 - el elseif el < -90 then el = -180 - el end
  return el
end

local function drawnSunElevation()
  if not scenetree then return nil end
  local sky = scenetree.findObject and scenetree.findObject('sunsky')
  if not sky and scenetree.findClassObjects then
    local names = scenetree.findClassObjects('ScatterSky')
    sky = names and names[1] and scenetree.findObject(names[1])
  end
  if not sky or not sky.getField then return nil end
  local ok, v = pcall(sky.getField, sky, 'elevation', 0)
  if not ok then return nil end
  return M.normalizeElevation(v)
end

function M.refreshDaylight()
  local drawn = drawnSunElevation()
  if drawn then M.setSunElevation(drawn) daylightSource = 'drawn sky' return 'drawn sky' end
  local sky = rawget(_G, 'beamngEarth_sky')
  if sky and type(sky.status) == 'function' then
    local ok, st = pcall(sky.status)
    if ok and type(st) == 'table' and tonumber(st.sunElev) then M.setSunElevation(st.sunElev) daylightSource = 'sky' return 'sky' end
  end
  local env = rawget(_G, 'core_environment')
  if env and type(env.getTimeOfDay) == 'function' then
    local ok, tod = pcall(env.getTimeOfDay)
    if ok and type(tod) == 'table' and tonumber(tod.time) and tod.year and tod.latitude and sky
      and type(sky.sunElevationDeg) == 'function' then
      local h = ((tonumber(tod.time) or 0) + 0.5) % 1.0 * 24
      local ok2, e = pcall(sky.sunElevationDeg, tod.year, tod.month, tod.day, h, tod.utcOffset, tod.latitude,
        tod.longitude)
      if ok2 and tonumber(e) then M.setSunElevation(e) daylightSource = 'tod' return 'tod' end
    end
    if type(env.getLightState) == 'function' then
      local ok3, ls = pcall(env.getLightState)
      if ok3 and type(ls) == 'table' and ls.isNight ~= nil then
        M.setSunElevation(ls.isNight and -15 or 25)
        daylightSource = 'lightState'
        return 'lightState'
      end
    end
    if ok and type(tod) == 'table' and tonumber(tod.time) then M.setTimeOfDay(tod.time) daylightSource = 'hour' return 'hour' end
  end
  return nil
end

------------------------------------------------------------------------------------------ clouds
-- The game's volumetric clouds are part of the sky, drawn behind everything: a contrail, a light or a model 10 km
-- up shows through an overcast deck 2 km up as if it were not there. So the add-on hides them itself: what lies on
-- the far side of the cloud deck from the camera is faded by how much of the sky the deck covers, and more so
-- looking low through it (a long slant path crosses more cloud). Read from the drawn CloudLayer (coverage,
-- altitudeKm), so the real weather and a manual weather are both right.
local clouds = { frac = 0, base = nil, top = nil, timer = 0, hidden = false }

-- BeamNG.Earth's calibration (beamngEarth_weather.COVERAGE_CURVE: sky fraction -> CloudLayer.coverage), inverted.
local DEFAULT_CURVE = { { 0.00, 0.00 }, { 0.02, 0.70 }, { 0.07, 0.79 }, { 0.19, 0.90 }, { 0.50, 1.10 },
  { 0.80, 1.50 }, { 1.00, 2.00 } }
function M.coverFraction(coverage, curve)
  curve = curve or DEFAULT_CURVE
  coverage = tonumber(coverage) or 0
  if coverage <= curve[1][2] then return curve[1][1] end
  for i = 2, #curve do
    local a, b = curve[i - 1], curve[i]
    if coverage <= b[2] then
      if b[2] == a[2] then return b[1] end
      return a[1] + (b[1] - a[1]) * (coverage - a[2]) / (b[2] - a[2])
    end
  end
  return curve[#curve][1]
end

-- How much of something at height pz shows through the deck seen from camera height cz (0 hidden .. 1 clear).
-- sinElev: the sine of the line of sight's angle to the horizontal. Pure (cloud: { frac, base, top }).
function M.cloudTransmission(cz, pz, sinElev, cloud)
  cloud = cloud or clouds
  if not cloud.base or cloud.hidden or (cloud.frac or 0) <= 0.005 then return 1 end
  local lo, hi = math.min(cz, pz), math.max(cz, pz)
  local thick = math.max(1, cloud.top - cloud.base)
  local through = (math.min(hi, cloud.top) - math.max(lo, cloud.base)) / thick    -- share of the deck crossed
  if through <= 0 then return 1 end
  local m = math.min(6.7, 1 / math.max(math.abs(sinElev or 1), 0.15))              -- slant path, at most ~6.7x
  local blocked = 1 - (1 - math.min(1, cloud.frac)) ^ (m * through)                 -- chance the sight line is in cloud
  return 1 - predict.smooth((blocked - 0.08) / 0.62)
end

local function refreshClouds(dt)
  clouds.timer = clouds.timer - dt
  if clouds.timer > 0 then return end
  clouds.timer = 2
  clouds.base, clouds.frac = nil, 0
  if not scenetree or not scenetree.findClassObjects then return end
  local names = scenetree.findClassObjects('CloudLayer')
  local o = names and names[1] and scenetree.findObject(names[1])
  if not o then return end
  local okH, hid = pcall(function() return o.isHidden and o:isHidden() end)
  clouds.hidden = okH and hid == true
  local cov = tonumber(o.coverage)
  local env = rawget(_G, 'core_environment')
  if not cov and env and env.getCloudCover then
    local ok, c = pcall(env.getCloudCover)
    cov = ok and tonumber(c) or nil
  end
  local alt = tonumber(o.altitudeKm)
  if not cov or not alt then return end
  local w = rawget(_G, 'beamngEarth_weather')
  local curve = (w and type(w.COVERAGE_CURVE) == 'table' and #w.COVERAGE_CURVE >= 2) and w.COVERAGE_CURVE or nil
  clouds.frac = M.coverFraction(cov, curve)
  clouds.base = alt * 1000
  clouds.top = clouds.base + M.cfg.cloudThickKm * 1000
end

function M._clouds() return clouds end
function M._setClouds(c) for k, v in pairs(c) do clouds[k] = v end; clouds.timer = 1e9 end

-- The contrail level, its lifetime and the wind aloft from the real weather (weather.lua's last 'weather' message:
-- sea-level temperature, surface pressure, high cloud cover, the 700 hPa wind); the defaults without one.
function M.refreshWeather(msg)
  if msg == nil then
    local ok, w = pcall(require, 'beamngEarth/weather')
    msg = ok and type(w) == 'table' and type(w.current) == 'function' and w.current() or nil
  end
  if type(msg) ~= 'table' then
    contrailFt, contrailLife, windAloft = predict.CONTRAIL_DEFAULT_FT, 1, nil
    return
  end
  local cover = type(msg.cover) == 'table' and msg.cover or {}
  local rh = predict.humidityAloft(cover.high, cover.mid)
  contrailFt = predict.contrailFormation(msg.slTempC or msg.tempC, rh, msg.pressureHpa, msg.elevM)
  contrailLife = predict.contrailLife(cover.high, rh)
  local al = type(msg.aloft) == 'table' and msg.aloft or nil
  if al and tonumber(al.ms) and tonumber(al.fromDeg) then
    -- (to where it blows; cruise level winds run ~1.6 times the 700 hPa wind)
    local to = math.rad(tonumber(al.fromDeg) + 180)
    local spd = math.max(3, math.min(60, tonumber(al.ms) * 1.6))
    windAloft = vec3(math.sin(to) * spd, math.cos(to) * spd, 0)
  else
    windAloft = nil
  end
end
function M._contrail() return contrailFt, contrailLife, windAloft end

local function updateEnvironment(dt)
  refreshClouds(dt)
  weatherTimer = weatherTimer - dt
  if weatherTimer <= 0 then
    weatherTimer = 30
    M.refreshWeather()
  end
  nightTimer = nightTimer - dt
  if nightTimer <= 0 then
    nightTimer = 5
    M.refreshDaylight()
  end
end

-- The drawn state of an aircraft now: prediction plus the fading correction.
local function stateNow(it)
  local lat, lon, alt, trk, vs, tr = predict.predict(it.base, clock)
  local c = it.corr
  if c then
    local f = 1 - predict.smooth((clock - c.t) / M.BLEND_S)
    if f <= 0 then it.corr = nil else
      lat, lon, alt, trk = lat + c.lat * f, lon + c.lon * f, alt + c.alt * f, trk + c.trk * f
    end
  end
  it.lat, it.lon, it.altFt, it.trk, it.vs, it.tr = lat, lon, alt, predict.wrap360(trk), vs, tr
end

local UP = nil
-- Game position and frame (forward, right, up) of an aircraft; nil off the world.
M.GROUND_CLEAR = 0.15      -- m between the wheels and the surface (no z-fighting, never under it)
M.LEVEL_BELOW_M = 40       -- below this height over the ground (above the gear) attitude eases to level
M.ATTITUDE_S = 1.2         -- pitch and bank ease toward the data's over about this long
M.GROUND_BUILDING_M = 3.0  -- a sample this far above the centre's is a roof or a jet bridge, not the ground

-- The surface under an aircraft at game x, y (z: its height, a hint where to look from): the highest of the ground
-- under the centre, nose, tail and both main gears (a sloping taxiway or a hump must not swallow the nose or tail),
-- by the game's surface query and a ray against static collision, whichever is higher. Samples well above the
-- centre's are roofs (hangars, terminals, jet bridges) and are left out. nil where nothing is loaded.
local function surfaceAt(x, y, top)
  local best = nil
  if be and be.getSurfaceHeightBelow then
    local ok, gz = pcall(be.getSurfaceHeightBelow, be, vec3(x, y, top))
    if ok and tonumber(gz) and gz > -1e6 and gz < top then best = gz end
  end
  local ray = rawget(_G, 'castRayStatic')
  if ray then
    local ok, dd = pcall(ray, vec3(x, y, top), vec3(0, 0, -1), 20000)
    if ok and tonumber(dd) and dd < 20000 then
      local gz = top - dd
      if not best or gz > best then best = gz end
    end
  end
  return best
end

function M.groundUnder(x, y, z, fwd, d, stretch, centreOnly)
  local top = z + 9000
  local center = surfaceAt(x, y, top)
  if not center or centreOnly then return center end
  local right = vec3(fwd.y, -fwd.x, 0)
  local best = center
  -- nose and tail (of the length it is drawn at), and out at the engines / main gear
  local L, W = d.len * 0.45 * (stretch or 1), math.max(d.R * 1.2, d.trailW or d.span * 0.1)
  for _, o in ipairs({ { L, 0 }, { -L, 0 }, { 0, W }, { 0, -W } }) do
    local gz = surfaceAt(x + fwd.x * o[1] + right.x * o[2], y + fwd.y * o[1] + right.y * o[2], top)
    if gz and gz > best and gz - center < M.GROUND_BUILDING_M then best = gz end
  end
  return best
end

local function frameOf(it)
  local x, y, z = toGame(it.lat, it.lon, it.altFt * FT)
  if not x then return nil end
  local pos = vec3(x, y, z)
  local la, lo = predict.move(it.lat, it.lon, it.trk, 300)
  local x2, y2 = toGame(la, lo, it.altFt * FT)
  if not x2 then return nil end
  local fwd = vec3(x2 - x, y2 - y, 0)
  if fwd:length() < 1e-3 then fwd = vec3(0, 1, 0) end
  fwd = fwd:normalized()
  local d = dimsOf(it)
  -- on the ground (or below the ground: a bad altitude): set on the surface under it
  local onGround = it.base.ground
  local near = onGround or (it.agl or 99999) < 120
  -- (the ground under far aircraft is not loaded, nor needed: only within modelKm; the first sample of an answer's new
  -- aircraft spread over half a second, not all in one frame)
  local nearCam = (it.dist or 0) <= M.cfg.modelKm * 1000
  if nearCam and (onGround or it.altFt < 15000) then
    it.gTimer = (it.gTimer or hash01(it.id .. 'g') * 0.5) - (frameData and frameData.dt or 0)
    if not it.groundZ or it.gTimer <= 0 then
      -- often while on or close to the ground (a 70 m/s landing roll, terrain still streaming in), seldom up high
      -- (a parked aircraft does not move: seldom; one without a model only needs the point under it: Deck budget,
      -- each sample is a surface query and a static ray)
      local parked = onGround and (it.base.gs or 0) < 2
      it.gTimer = parked and (it.groundZ and 5.0 or 0.5) or (near and (it.objId and 0.25 or 1.0))
        or ((it.agl or 99999) < 1000 and 1.0 or 3.0)
      it.groundZ = M.groundUnder(x, y, z, fwd, d, it.stretch, not it.objId) or it.groundZ
    end
  else
    it.groundZ = nil
  end
  local agl = it.groundZ and (z - it.groundZ) or 99999
  if onGround then
    if not it.groundZ then return nil end            -- the ground there is not loaded: not drawn
    pos.z = it.groundZ + d.gearH + M.GROUND_CLEAR
    agl = 0
  elseif it.groundZ and agl < d.gearH + M.GROUND_CLEAR then
    pos.z = it.groundZ + d.gearH + M.GROUND_CLEAR      -- (a landing / take-off roll ADS-B still calls airborne)
    agl = d.gearH
  end
  it.agl = agl
  local gsMs = it.base.gs * predict.KT
  local pitch, bank = 0, 0
  if not onGround then
    pitch = math.atan2((it.vs or 0) * FT / 60, math.max(gsMs, 20))
    pitch = math.max(-0.25, math.min(0.3, pitch)) + (it.kind == 'heli' and 0.05 or 0.035)
    -- (new data every 15 s changes the climb and turn rates at once: the attitude eases over to them, so the
    -- aircraft, and a camera riding in it, does not jerk)
    local fdt = frameData and frameData.dt or 0
    it.pitchS = it.pitchS and it.pitchS + (pitch - it.pitchS) * math.min(1, fdt / M.ATTITUDE_S) or pitch
    pitch = it.pitchS
    -- the last few metres: wheels-level and wings-level (a pitched body would put the tail or nose into the runway,
    -- a banked one a wingtip or an engine)
    local lowK = predict.smooth((agl - d.gearH) / M.LEVEL_BELOW_M)
    pitch = pitch * lowK
    local omega = math.rad(it.tr or 0)
    bank = math.atan(gsMs * omega / 9.81)
    bank = math.max(-0.6, math.min(0.6, bank))
    it.bankS = it.bankS and it.bankS + (bank - it.bankS) * math.min(1, fdt / M.ATTITUDE_S) or bank
    bank = it.bankS * lowK
    if d.noPitch then pitch, bank = 0, 0 end         -- (a balloon hangs straight down)
  end
  UP = UP or vec3(0, 0, 1)
  local f = fwd * math.cos(pitch) + UP * math.sin(pitch)
  local r = f:cross(UP):normalized()
  local u0 = r:cross(f)
  local u = u0 * math.cos(bank) + r * math.sin(bank)
  r = f:cross(u):normalized()
  return pos, f, r, u, agl
end

------------------------------------------------------------------------------------------ lights
local function lightColor(r, g, b, a) return ColorF(r, g, b, a) end
local C_RED, C_GREEN, C_WHITE, C_STROBE, C_LAND

local function pxRadius(dist, px, tanHalf)
  -- a sphere px pixels wide at dist (1080 rows on screen; a little wider on bigger screens is fine)
  return math.max(0.11, dist * tanHalf * px / 1080)
end

local function local2world(pos, f, r, u, s, v)
  return pos + r * (v[1] * s) + f * (v[2] * s * stretchNow) + u * (v[3] * s)
end

-- How much an aircraft counts as passing overhead (0..1): high above the horizon as seen from the camera, or close.
-- Blinking fades in over 12 degrees of elevation and over the last 3 km, so it never pops on or off.
-- How dark it is for aircraft lights, 0 (day: none shown) .. 1 (night). From the sun the game's sky is drawing (the
-- real-time sky or a manual time of day alike): lights fade in from the sun 6 degrees up to 4 below, as in real life
-- they start to stand out in the dusk and are all you see at night. Dawn the other way round.
M.LIGHTS_FROM_SUN, M.LIGHTS_FULL_SUN = 6, -4
function M.darkFactor(elev)
  elev = elev or sunElev
  return 1 - predict.smooth((elev - M.LIGHTS_FULL_SUN) / (M.LIGHTS_FROM_SUN - M.LIGHTS_FULL_SUN))
end

function M.overheadFactor(pos, camPos, dist)
  local c = M.cfg
  local nearM = (night and c.lightsNearKmNight or c.lightsNearKm) * 1000
  local minElev = night and c.lightsOverheadDegNight or c.lightsOverheadDeg
  local maxM = (night and c.lightsMaxKmNight or c.lightsMaxKm) * 1000
  local dx, dy = pos.x - camPos.x, pos.y - camPos.y
  local elev = math.deg(math.atan2(pos.z - camPos.z, math.sqrt(dx * dx + dy * dy)))
  local high = predict.smooth((elev - (minElev - 10)) / 10) * (1 - predict.smooth((dist - maxM) / 3000))
  local close = 1 - predict.smooth((dist - nearM) / 1500)
  return math.max(high, close)
end

-- Distance fade of a contrail (1 near, contrailFarAlpha far away).
function M.contrailDistanceFade(dist)
  local k = predict.smooth((dist - M.cfg.contrailClearKm * 1000) /
    math.max(1000, (M.cfg.contrailFarKm - M.cfg.contrailClearKm) * 1000))
  return 1 - k * (1 - M.cfg.contrailFarAlpha)
end

local RGB_RED, RGB_STROBE = { 1.0, 0.12, 0.08 }, { 1, 1, 1 }
local RGB_GREEN, RGB_WHITE, RGB_LAND = { 0.1, 1.0, 0.3 }, { 1, 1, 0.95 }, { 1, 0.97, 0.88 }
local function faded(c, rgb, k)
  if k >= 0.999 then return c end
  return ColorF(rgb[1], rgb[2], rgb[3], k)
end

local function drawLights(it, pos, f, r, u, s, dist, tanHalf, camPos, blink)
  local d = dimsOf(it)
  if d.lights == false then return end      -- (gliders and balloons carry none)
  local t = clock + it.phase * 7
  local big = night and 1.5 or 1.0
  if blink <= 0.02 then return end        -- far off or low on the horizon: no lights at all
  -- by day lights are faint against the bright sky; they also dim a little with distance
  local vis = blink * (night and 1.0 or 0.75) * (1 - 0.4 * predict.smooth((dist - 3000) / 12000))
  blink = vis
  do
  local cRed, cStrobe = faded(C_RED, RGB_RED, blink), faded(C_STROBE, RGB_STROBE, blink)
  -- red anti-collision beacons (top and bottom alternating), about once a second
  local bp = (t % 1.05) / 1.05
  if bp < 0.14 then
    debugDrawer:drawSphere(local2world(pos, f, r, u, s, d.top), pxRadius(dist, 2.6 * big, tanHalf), cRed)
  elseif bp > 0.5 and bp < 0.64 then
    debugDrawer:drawSphere(local2world(pos, f, r, u, s, d.bottom), pxRadius(dist, 2.6 * big, tanHalf), cRed)
  end
  -- white strobes: double flash (jets), single (the rest)
  if not it.base.ground or it.base.gs > 25 then
    local sp = (t * 1.0 + 0.3) % 1.2
    local on = sp < 0.05 or (d.jet and sp > 0.14 and sp < 0.19)
    if on then
      local rad = pxRadius(dist, 3.0 * big, tanHalf)
      local tip = d.tip
      debugDrawer:drawSphere(local2world(pos, f, r, u, s, tip), rad, cStrobe)
      debugDrawer:drawSphere(local2world(pos, f, r, u, s, { -tip[1], tip[2], tip[3] }), rad, cStrobe)
      if it.kind ~= 'heli' then debugDrawer:drawSphere(local2world(pos, f, r, u, s, d.tail), rad * 0.8, cStrobe) end
    end
  end
  end
  -- steady navigation lights (left red, right green, tail white): small, so only nearer
  if dist < 12000 or night then
    local rad = pxRadius(dist, 1.4 * big, tanHalf)
    local tip = d.tip
    debugDrawer:drawSphere(local2world(pos, f, r, u, s, { -tip[1], tip[2], tip[3] }), rad, faded(C_RED, RGB_RED, vis))
    debugDrawer:drawSphere(local2world(pos, f, r, u, s, tip), rad, faded(C_GREEN, RGB_GREEN, vis))
    debugDrawer:drawSphere(local2world(pos, f, r, u, s, d.tail), rad * 0.8, faded(C_WHITE, RGB_WHITE, vis))
  end
  -- landing lights below 10,000 ft (and taxi lights on the ground while moving), brightest seen from ahead
  if (not it.base.ground and (it.base.baroFt or it.altFt) < 10000) or (it.base.ground and it.base.gs > 4) then
    local toCam = (camPos - pos):normalized()
    local facing = math.max(0, toCam:dot(f))
    if facing > 0.15 then
      local px = (1.3 + 1.6 * facing ^ 3) * big
      local p = local2world(pos, f, r, u, s, { 0, d.len * 0.33, -d.R * 0.9 })
      debugDrawer:drawSphere(p, pxRadius(dist, px, tanHalf), faded(C_LAND, RGB_LAND, vis))
    end
  end
end

------------------------------------------------------------------------------------------ contrails
local function trailSample(it, pos, f, r, s)
  local tr = trails[it.id]
  local d = dimsOf(it)
  if not tr then
    tr = { pts = {}, kind = it.kind, lastT = -1e9, spread = 0.7 + hash01(it.id .. 'c') * 0.6,
      -- engines half a wing-span-third apart: two trails that merge into one
      eng = d.trailW or d.span * 0.17 }
    trails[it.id] = tr
  end
  if clock - tr.lastT < M.CONTRAIL_SAMPLE_S then return end
  tr.lastT = clock
  local la, lo = predict.move(it.lat, it.lon, it.trk + 180, d.len * 0.5 * s * (it.stretch or 1))
  local x, y, z = toGame(la, lo, it.altFt * FT)
  if not x then return end
  tr.n = (tr.n or hash01(it.id) * 97) + 1
  local n = tr.n
  local c = hash01(it.id) * 6.28
  -- slowly varying along the trail (two slow waves each, so it never looks regular): thicker and thinner stretches,
  -- wider and narrower ones, and a gentle meander (wind shear and the wingtip vortices working on an older trail)
  local function slow(k1, k2, off) return 0.5 + 0.25 * math.sin(n * k1 + c + off) + 0.25 * math.sin(n * k2 + 2 * c + off) end
  table.insert(tr.pts, 1, { lat = la, lon = lo, h = it.altFt * FT, t = clock, x = x, y = y, z = z,
    rx = r.x, ry = r.y,
    na = 0.35 + 0.65 * slow(0.31, 0.83, 0), nw = 0.7 + 0.7 * slow(0.43, 1.07, 1.3), ph = n * 0.45 + c })
end

local function trailRefresh()
  for _, tr in pairs(trails) do
    for _, p in ipairs(tr.pts) do
      local x, y, z = toGame(p.lat, p.lon, p.h)
      if x then p.x, p.y, p.z = x, y, z end
    end
  end
  trailsDirty = false
end

local prismOk = true
local function drawSegment(a, b, w1, w2, col)
  if prismOk then
    -- a flat ribbon a little taller than wide reads softer than a square tube from below
    local ok = pcall(debugDrawer.drawSquarePrism, debugDrawer, a, b, Point2F(w1 * 0.7, w1), Point2F(w2 * 0.7, w2), col)
    if ok then return end
    prismOk = false
    logW('drawSquarePrism not available; contrails drawn as cylinders')
  end
  debugDrawer:drawCylinder(a, b, (w1 + w2) * 0.25, col)
end

-- One global wind aloft per session (direction from the session, speed from the surface wind): every contrail drifts
-- the same way, as real ones do.
local drift = nil
local function driftVec()
  if windAloft then return windAloft end
  if not drift then
    local ang = math.random() * 2 * math.pi
    drift = vec3(math.cos(ang) * 8, math.sin(ang) * 8, 0)
  end
  return drift
end


-- How a contrail at hM metres looks with the sun at elevDeg: visibility (0..1) and colour. High up the sun sets
-- later than on the ground (the horizon dips by acos(R / (R + h)): 3.3 degrees at 35,000 ft), so after sunset on the
-- ground a contrail still catches the sun, warm orange-pink; then it is in the Earth's shadow, a grey streak against
-- the twilight sky; in the dark night none is seen.
local R_EARTH = 6371000
function M.contrailLight(elevDeg, hM)
  elevDeg = tonumber(elevDeg) or 30
  local dip = math.deg(math.acos(R_EARTH / (R_EARTH + math.max(0, hM or 10000))))
  local above = elevDeg + dip                            -- the sun's height over the contrail's own horizon
  local lit = predict.smooth((above + 0.6) / 1.6)        -- in sunlight
  local sky = predict.smooth((elevDeg + 11) / 6)         -- the twilight sky behind it (gone by nautical dusk)
  local vis = lit + (1 - lit) * 0.45 * sky
  -- colour: white by day, golden then pink as the sun nears its horizon, grey-blue in the Earth's shadow
  local warm = 1 - predict.smooth((above - 0.5) / 7)
  local r = 0.97 + (1.00 - 0.97) * warm
  local g = 0.98 + (0.66 - 0.98) * warm
  local b = 1.00 + (0.50 - 1.00) * warm
  r = r * lit + 0.55 * (1 - lit)
  g = g * lit + 0.58 * (1 - lit)
  b = b * lit + 0.68 * (1 - lit)
  return vis, r, g, b
end

local function drawTrails(camPos, camFwd)
  if sunElev < -14 then trails = {} return end    -- full night: no contrail can be seen (none kept for the morning)
  local life = M.cfg.contrailSeconds * contrailLife
  local budget = M.cfg.trailSegments
  local wind = driftVec()
  local MERGE_S = 14                       -- the two engine trails run together after this long
  local GAP_S = 0.6                        -- clear air right behind the aircraft (the exhaust must freeze first)
  for id, tr in pairs(trails) do
    local pts = tr.pts
    while #pts > 0 and clock - pts[#pts].t > life do pts[#pts] = nil end
    local it = items[id]
    if #pts == 0 and (not it or not it.trailing) then
      trails[id] = nil
    elseif budget > 0 then
      local vis, cr, cg, cb = M.contrailLight(sunElev, pts[1] and pts[1].h or 10000)
      local live = it and it.trailing and it.trailPos
      local prev = live and { x = live.x, y = live.y, z = live.z, t = clock, rx = pts[1] and pts[1].rx or 1,
        ry = pts[1] and pts[1].ry or 0, nw = 1, na = 1, ph = pts[1] and pts[1].ph or 0 } or nil
      local first = pts[1]
      local dist0 = first and (vec3(first.x, first.y, first.z) - camPos):length() or 0
      local step = dist0 > 60000 and 3 or dist0 > 30000 and 2 or 1
      local layers = dist0 > 30000 and 2 or 3  -- (far off the middle layer is not missed)
      local i = 1
      while i <= #pts and budget > 0 do
        local p = pts[i]
        if prev then
          local ageA, ageB = clock - prev.t, clock - p.t
          local kA, kB = ageA / life, ageB / life
          local fadeIn = predict.smooth((ageB - GAP_S) / 1.6)
          local fadeOut = 1 - predict.smooth((kB - 0.5) / 0.5)
          -- older trails break up: density and width vary stretch by stretch, more and more with age
          local brkA, brkB = predict.smooth((ageA - 12) / 35), predict.smooth((ageB - 12) / 35)
          local base = fadeIn * fadeOut * vis * (1 - brkB * (1 - p.na))
          if base > 0.01 then
            local a = vec3(prev.x, prev.y, prev.z) + wind * ageA
            local b = vec3(p.x, p.y, p.z) + wind * ageB
            a.z, b.z = a.z - ageA * 0.35, b.z - ageB * 0.35         -- a slow sag
            local mid = (a + b) * 0.5
            local segDist = (mid - camPos):length()
            local distK = M.contrailDistanceFade(segDist)
            local cloudK = M.cloudTransmission(camPos.z, mid.z, (mid.z - camPos.z) / math.max(1, segDist))
            distK = distK * cloudK
            local visible = (a - camPos):dot(camFwd) > -3000 or (b - camPos):dot(camFwd) > -3000
            if visible and base * distK > 0.004 then
              -- spreading: a few metres behind the aircraft, a few hundred after a minute and a half
              local wA = (5 + 260 * tr.spread * kA ^ 0.7) * (1 + brkA * ((prev.nw or 1) - 1))
              local wB = (5 + 260 * tr.spread * kB ^ 0.7) * (1 + brkB * ((p.nw or 1) - 1))
              -- the slow waviness of an older trail (the wingtip vortices twisting it), sideways
              local rA, rB = vec3(prev.rx or 1, prev.ry or 0, 0), vec3(p.rx or 1, p.ry or 0, 0)
              local waveA = wA * 0.22 * predict.smooth((ageA - 10) / 30) * math.sin(prev.ph or 0)
              local waveB = wB * 0.22 * predict.smooth((ageB - 10) / 30) * math.sin(p.ph or 0)
              a, b = a + rA * waveA, b + rB * waveB
              -- dense near the aircraft, thinner as the same ice spreads over more air
              local alpha = base * distK * (0.9 / (1 + 0.7 * kB))
              -- soft edges: an outer haze, a middle layer, the core
              drawSegment(a, b, wA * 2.2, wB * 2.2, ColorF(cr * 0.93, cg * 0.95, cb * 0.98, alpha * 0.24))
              budget = budget - 1
              if ageB < MERGE_S and tr.eng then
                -- two engine trails, closing together inside a shared haze
                local oA = tr.eng * (1 - predict.smooth(ageA / MERGE_S))
                local oB = tr.eng * (1 - predict.smooth(ageB / MERGE_S))
                if layers == 3 then
                  drawSegment(a, b, wA * 1.1 + oA * 1.6, wB * 1.1 + oB * 1.6,
                    ColorF(cr * 0.97, cg * 0.98, cb, alpha * 0.28))
                  budget = budget - 1
                end
                local col = ColorF(cr, cg, cb, alpha)
                for _, sgn in ipairs({ -1, 1 }) do
                  drawSegment(a + rA * (oA * sgn), b + rB * (oB * sgn), wA * 0.42, wB * 0.42, col)
                end
                budget = budget - 2
              else
                if layers == 3 then
                  drawSegment(a, b, wA * 1.1, wB * 1.1, ColorF(cr * 0.97, cg * 0.98, cb, alpha * 0.5))
                  budget = budget - 1
                end
                drawSegment(a, b, wA * 0.5, wB * 0.5, ColorF(cr, cg, cb, alpha))
                budget = budget - 1
              end
            end
          end
        end
        prev = p
        i = i + step
      end
    end
  end
end

------------------------------------------------------------------------------------------ labels
local function labelText(it, mode)
  local flight, typ = labelBits(it)
  if mode == 'flight' then return flight end
  if mode == 'type' then return typ end
  local parts = { flight }
  if typ ~= flight then parts[#parts + 1] = typ end
  if it.base.ground then
    parts[#parts + 1] = it.base.gs > 4 and 'taxiing' or 'parked'
  else
    local alt = it.base.baroFt or it.altFt
    local now = alt + ((it.altFt or alt) - it.base.altFt)    -- (the extrapolated change)
    parts[#parts + 1] = now >= 18000 and string.format('FL%03d', math.floor(now / 100 + 0.5))
      or string.format('%d ft', math.floor(now / 100 + 0.5) * 100)
    parts[#parts + 1] = string.format('%d kt', math.floor(it.base.gs + 0.5))
    if (it.vs or 0) > 400 then parts[#parts + 1] = 'climbing' elseif (it.vs or 0) < -400 then
      parts[#parts + 1] = 'descending' end
  end
  local a = it.raw
  if a.d and a.d ~= '' then
    parts[#parts + 1] = (a.o and a.o ~= '' and (a.o .. ' > ') or '> ') .. a.d
  end
  return table.concat(parts, '  ')
end

local textOk = true
local function drawLabel(pos, text, alpha)
  local col = ColorF(1, 1, 1, alpha)
  if textOk then
    local ok = pcall(debugDrawer.drawTextAdvanced, debugDrawer, pos, String(text), col, true, false,
      ColorI(10, 16, 24, math.floor(150 * alpha)))
    if ok then return end
    textOk = false
  end
  pcall(debugDrawer.drawText, debugDrawer, pos, String(text), col)
end

-- Parked: on the ground and not moving (taxiing ones keep their label: they are the traffic on the airfield).
function M.parked(it)
  return it.base and it.base.ground == true and (it.base.gs or 0) <= 4
end

-- Which labels to draw (pure): the nearest n of the candidates { dist, dot (cosine of the angle to the look
-- direction) }, plus the one nearest the look direction within aimDeg (whatever its distance). Over a busy airport
-- this is a handful of labels instead of a column of them stacked over the ramp.
function M.pickLabels(cands, n, aimDeg)
  table.sort(cands, function(a, b) return a.dist < b.dist end)
  local out = {}
  for i = 1, math.min(#cands, n) do out[i] = cands[i] end
  local minDot, aimed = math.cos(math.rad(aimDeg or 12)), nil
  for _, c in ipairs(cands) do
    if (c.dot or -1) >= minDot and (not aimed or c.dot > aimed.dot) then aimed = c end
  end
  if aimed then
    local have = false
    for _, c in ipairs(out) do if c == aimed then have = true end end
    if not have then out[#out + 1] = aimed end
  end
  return out
end

------------------------------------------------------------------------------------------ devo343's add-on
-- The add-on this is built from (devo343's Live Air Traffic, a mod of its own) still on beside it draws every aircraft
-- a second time. While ours is on and it is too, a small notice offers to switch it off; nothing changes without the
-- player's click (the user, 2026-11-27). Its extension is unloaded first (its objects go with it), then the game's mod
-- manager deactivates its mod, as the mod manager page would.
M.OLD_ADDON_FILE = '/lua/ge/extensions/liveAirTraffic.lua'
local oldAddon = { checkT = -1e9, active = false, name = nil, hidden = false, result = nil }

-- Is it on (cached, checked every 10 s)? Its mod's name too (nil when the game cannot tell: not a zip mod).
function M.oldAddonActive()
  if clock - oldAddon.checkT < 10 then return oldAddon.active, oldAddon.name end
  oldAddon.checkT = clock
  local ext = rawget(_G, 'liveAirTraffic')
  local fsOk = FS and FS.fileExists and FS:fileExists(M.OLD_ADDON_FILE)
  oldAddon.active = (ext ~= nil) or fsOk == true
  oldAddon.name = nil
  if oldAddon.active then
    local mm = rawget(_G, 'core_modmanager')
    if mm and mm.getModForFilename then
      local ok, name = pcall(mm.getModForFilename, M.OLD_ADDON_FILE)
      -- (never BeamNG.Earth's own mod: should a copy of the old file ever sit inside it, the click must not take
      -- the whole mod off)
      local okOwn, own = pcall(mm.getModForFilename, '/lua/ge/extensions/beamngEarth/airtraffic.lua')
      if ok and type(name) == 'string' and not (okOwn and own == name) then oldAddon.name = name end
    end
  end
  return oldAddon.active, oldAddon.name
end

-- The click: off it goes. True, or false and why.
function M.disableOldAddon()
  local _, name = M.oldAddonActive()
  local ext = rawget(_G, 'extensions')
  if rawget(_G, 'liveAirTraffic') and ext and ext.unload then pcall(ext.unload, 'liveAirTraffic') end
  local mm = rawget(_G, 'core_modmanager')
  if not (name and mm and mm.deactivateMod) then
    return false, 'switch it off in the game\'s Mods page (Live Air Traffic)'
  end
  local ok = pcall(mm.deactivateMod, name)
  oldAddon.checkT = -1e9
  if not ok then return false, 'the game\'s mod manager refused: switch it off in the Mods page' end
  return true
end

function M.drawOldAddonNotice()
  if oldAddon.hidden then return end
  local active = M.oldAddonActive()
  if not active and not oldAddon.result then return end
  local im = rawget(_G, 'ui_imgui')
  if not im then return end
  local vp = im.GetMainViewport()
  if vp then im.SetNextWindowPos(im.ImVec2(vp.Pos.x + 12, vp.Pos.y + 120), im.Cond_Always, im.ImVec2(0, 0)) end
  local flags = im.WindowFlags_AlwaysAutoResize + im.WindowFlags_NoCollapse + im.WindowFlags_NoSavedSettings
    + im.WindowFlags_NoTitleBar
  if im.Begin('Live air traffic##beamngEarthOldAddon', nil, flags) then
    if oldAddon.result then
      im.TextWrapped(oldAddon.result)
      if im.Button('OK') then oldAddon.result, oldAddon.hidden = nil, true end
    else
      im.Text("devo343's Live Air Traffic add-on is also on: every aircraft is drawn twice.")
      im.Text('BeamNG.Earth has live air traffic built in now (based on that add-on).')
      if im.Button('Switch the old add-on off') then
        local ok, why = M.disableOldAddon()
        oldAddon.result = ok and 'The old Live Air Traffic add-on is off (turn it on again in the Mods page).'
          or ('Could not switch it off: ' .. tostring(why))
      end
      im.SameLine()
      if im.Button('Not now') then oldAddon.hidden = true end
    end
  end
  im.End()
end
function M._oldAddon() return oldAddon end

------------------------------------------------------------------------------------------ the frame
local function sizeFor(it, dist)
  if not M.cfg.sizeBoost or it.base.ground then return 1 end
  -- real size near the ground (an enlarged airliner on a runway would sink its belly into it), enlarged far up only
  local high = predict.smooth(((it.agl or 99999) - 300) / 1200)
  return 1 + (math.max(1, math.min(2.5, dist / 7000)) - 1) * high
end

local function chooseModels(camPos)
  -- nearest aircraft within modelKm get models; the rest lose theirs
  local list = {}
  for _, it in pairs(items) do
    if it.dist and it.dist <= M.cfg.modelKm * 1000 and it.visible then list[#list + 1] = it end
  end
  table.sort(list, function(a, b) return a.dist < b.dist end)
  local keep = {}
  for i = 1, math.min(#list, M.cfg.maxModels) do keep[list[i]] = true end
  for _, it in pairs(items) do
    if not keep[it] and it.objId then dropModel(it) end
  end
  -- new models, nearest first, at most CREATE_PER_TICK a tick: a shape's first load costs a frame (4-25 ms, the Deck
  -- ~2.5x: memory engine-shape-load-cost), so the first answer's dozens of aircraft come in over a few seconds
  local created = 0
  for _, it in ipairs(list) do
    if keep[it] and not it.objId and created < M.CREATE_PER_TICK then
      created = created + 1
      local d = dimsOf(it)
      local model = DIMS[it.model] and it.model or (DIMS[it.kind] and it.kind or 'narrow')
      local ok, id = pcall(makeStatic, SHAPES .. 'lat_' .. model .. '.dae')
      if ok then it.objId = id else logW('model: ' .. tostring(id)); M.cfg.maxModels = 0 end
      -- the livery: one overlay per airframe, tinted with the airline's colour (16 shapes per airframe before)
      local nLiv = M.NO_LIVERY[model] and 0 or (d.liveries or 16)
      if ok and nLiv > 0 then
        local pal = math.min(it.pal or 0, nLiv - 1)
        local okL, lid = pcall(makeStatic, SHAPES .. 'lat_' .. model .. '_liv.dae', PALETTES[pal] or { 1, 1, 1 })
        if okL then it.livId = lid end
      end
      it.shapeModel = model
    end
  end
  -- lettering (one object per letter, moved every frame): only on the aircraft the camera follows, near enough to read
  local want = {}
  local fi = M._followIt
  if fi and fi.objId and (fi.dist or 1e9) <= M.cfg.textKm * 1000 then want[fi] = true end
  for _, it in pairs(items) do
    it.wantText = want[it] or false
    if not it.wantText and it.glyphs then
      for _, g in ipairs(it.glyphs) do deleteObj(g.id) end
      it.glyphs = nil
    end
  end
end

local function update(dtReal)
  dtReal = math.min(tonumber(dtReal) or 0, 0.5)
  clock = clock + dtReal
  local st = coreState()
  local inWorld = st and st.inOurLevel == true and st.anchor ~= nil
  if not inWorld then
    if wasInWorld then dropAll() end
    wasInWorld, anchor = false, nil
    return
  end
  wasInWorld = true
  anchor = st.anchor
  poll(dtReal, st.player)
  if st.rebasing ~= nil then return end      -- positions settle with onBeamNGEarthRebased
  if trailsDirty then trailRefresh() end
  updateEnvironment(dtReal)

  C_RED = C_RED or lightColor(1.0, 0.12, 0.08, 1)
  C_GREEN = C_GREEN or lightColor(0.1, 1.0, 0.3, 1)
  C_WHITE = C_WHITE or lightColor(1, 1, 0.95, 1)
  C_STROBE = C_STROBE or lightColor(1, 1, 1, 1)
  C_LAND = C_LAND or lightColor(1, 0.97, 0.88, 1)

  local camPos, camFwd, fov = camera()
  if not camPos then return end
  local tanHalf = math.tan(math.rad(fov) * 0.5) * 2
  frameData = { dt = dtReal }
  local maxDist = M.cfg.lightsKm * 1000
  local darkK = M.darkFactor()                   -- (no aircraft lights by day)

  glyphBudget = GLYPH_CREATE_PER_FRAME
  -- move, place and light every aircraft
  local labels = {}
  local remove = {}
  for id, it in pairs(items) do
    if it.missing and clock - it.missing > M.MISSING_KEEP_S then
      remove[#remove + 1] = id
    elseif it.base.ground and not M.cfg.showGround then
      it.visible = false
      if it.objId then dropModel(it) end
    else
      stateNow(it)
      stretchNow = it.stretch or 1
      local pos, f, r, u, agl = frameOf(it)
      it.fPos, it.fF, it.fR, it.fU = pos, f, r, u
      if pos then
        local toA = pos - camPos
        local dist = toA:length()
        it.dist, it.visible = dist, dist <= maxDist
        local inFront = toA:dot(camFwd) > -dist * 0.2
        local s = sizeFor(it, dist)
        local d = dimsOf(it)
        local cloudK = M.cloudTransmission(camPos.z, pos.z, toA.z / math.max(1, dist))
        it.cloudK = cloudK
        local inCabin = M._cabinIt and M._cabinIt() == it and d.cabin
        placing = inCabin and true or false
        if it.objId then
          it.drawS = s
          local q0 = quatFromDir(f, u)
          if inCabin then
            -- the seat view: the interior added (the aircraft's windows are open and glazed already, so nothing
            -- is hidden or swapped: one fuselage, no two skins fighting in the same place)
            if not it.cabId then
              local okC, cid = pcall(makeStatic, SHAPES .. 'lat_' .. (it.shapeModel or it.model) .. '_cabin.dae')
              if okC then it.cabId = cid end
            end
            if it.cabId then it.cabScale = placeObj(it.cabId, pos, q0, s, it.cabScale, stretchNow) end
          elseif it.cabId then
            deleteObj(it.cabId)
            it.cabId, it.cabScale = nil, nil
          end
          local hide = M.cfg.cloudHideModels and cloudK < 0.25 and not inCabin
          if hide ~= (it.cloudHidden or false) then
            it.cloudHidden = hide
            local ids = { it.objId, it.gearId, it.livId }
            for _, rt in ipairs(it.rotors or {}) do ids[#ids + 1] = rt.id end
            for _, g in ipairs(it.glyphs or {}) do ids[#ids + 1] = g.id end
            for _, oid in pairs(ids) do
              local o = oid and scenetree.findObjectById(oid)
              if o and o.setHidden then pcall(o.setHidden, o, hide) end
            end
          end
          local q = quatFromDir(f, u)
          it.objScale = placeObj(it.objId, pos, q, s, it.objScale, stretchNow)
          if it.livId then it.livScale = placeObj(it.livId, pos, q, s, it.livScale, stretchNow) end
          -- the gear: on the ground and below 2,000 ft over the ground at approach speeds
          -- (and always when it is down at the runway, whatever ADS-B says its speed is)
          local wantGear = it.base.ground or (agl < 600 and it.base.gs < 220 and (it.vs or 0) < 300)
            or agl < d.gearH * 3
          if wantGear and not it.gearId and d.gear ~= false then
            local ok, gid = pcall(makeStatic, SHAPES .. 'lat_' .. (it.shapeModel or it.model) .. '_gear.dae')
            if ok then
              it.gearId, it.gearScale = gid, nil
              local go = it.cloudHidden and scenetree.findObjectById(gid)
              if go and go.setHidden then pcall(go.setHidden, go, true) end
            end
          elseif not wantGear and it.gearId then
            deleteObj(it.gearId); it.gearId = nil
          end
          if it.gearId then it.gearScale = placeObj(it.gearId, pos, q, s, it.gearScale, stretchNow) end
          placeRotors(it, d, pos, f, r, u, s, dtReal)
          if it.wantText then placeTexts(it, d, pos, f, r, u, s) end
        end
        if it.visible and inFront and not inCabin then
          drawLights(it, pos, f, r, u, s, dist, tanHalf, camPos, M.overheadFactor(pos, camPos, dist) * cloudK * darkK)
          if M.labelMode ~= 'off' and dist <= M.cfg.labelKm * 1000 and not M.parked(it) then
            labels[#labels + 1] = { it = it, dist = dist, pos = pos + u * ((d.finTop + 4) * s + dist * 0.004),
              dot = toA:dot(camFwd) / math.max(1, dist) }
          end
        end
        -- contrails: jets above the contrail altitude
        local makes = M.contrails and sunElev > -14 and d.jet and not it.base.ground and (it.base.baroFt or it.altFt) >= contrailFt
        it.trailing = makes
        if makes then
          it.trailPos = pos - f * (d.len * 0.5 * s * stretchNow)
          trailSample(it, pos, f, r, s)
        end
      else
        it.visible = false
      end
    end
  end
  for _, id in ipairs(remove) do
    local it = items[id]
    if it then dropModel(it); it.trailing = false end
    items[id] = nil
  end

  modelTimer = modelTimer - dtReal
  if modelTimer <= 0 then
    modelTimer = M.MODEL_TICK_S
    chooseModels(camPos)
  end

  if M.contrails then drawTrails(camPos, camFwd) end

  -- the camera riding along (first: the sounds are heard from where it is this frame)
  if M._updateFollow then M._updateFollow(dtReal) end
  -- sounds of the aircraft near enough to hear
  local following = M._following and M._following()
  local cabinIt = M._cabinIt and M._cabinIt()
  local camNow = following and camera() or camPos
  local okS, errS = pcall(sound.update, { items = items, clock = clock, toGame = toGame, camPos = camNow or camPos,
    dt = dtReal, volume = M.cfg.soundVolume, on = M.sounds, following = following and true or false,
    cabinId = cabinIt and cabinIt.id or nil })
  if not okS then logW('sound: ' .. tostring(errS)); M.sounds = false; sound.clear() end

  if #labels > 0 then
    for _, l in ipairs(M.pickLabels(labels, M.cfg.maxLabels, M.cfg.labelAimDeg)) do
      local alpha = 0.95 - 0.55 * math.min(1, l.dist / (M.cfg.labelKm * 1000))
      drawLabel(l.pos, labelText(l.it, M.labelMode), alpha)
    end
  end
  M.drawOldAddonNotice()
end

------------------------------------------------------------------------------------------ keys
function M.cycleLabels()
  local i = 1
  for k, m in ipairs(M.LABEL_MODES) do if m == M.labelMode then i = k end end
  M.labelMode = M.LABEL_MODES[i % #M.LABEL_MODES + 1]
  saveSetting('airTrafficLabels', M.labelMode)
  local names = { full = 'full (flight, type, altitude, speed, destination)', flight = 'flight numbers only',
    type = 'aircraft types only', off = 'off' }
  message('Live air traffic labels: ' .. names[M.labelMode], 3)
end

-- The per-frame hooks only while the feature is on (none at all while off; extensions.hookUpdate refreshes the
-- game's hook lists).
local function preRender() M.lockCabin() end
local function refreshHooks()
  local want = M.enabled == true
  if want == hooked then return end
  hooked = want
  M.onUpdate = want and update or nil
  M.onPreRender = want and preRender or nil
  local ext = rawget(_G, 'extensions')
  if ext and type(ext.hookUpdate) == 'function' then
    pcall(ext.hookUpdate, 'onUpdate')
    pcall(ext.hookUpdate, 'onPreRender')
  end
end
M._hooked = function() return hooked end

-- On or off (settings.lua's airTraffic, applied; the consent must be given first: M.consented).
function M.setEnabled(on)
  -- (a test server's session stays on whatever the saved setting says: settings.applyAll runs after its hello)
  on = (on == true or M.testOn == true) and M.consented()
  if on == M.enabled and hooked == on then return M.enabled end
  M.enabled = on
  if on then
    M.applySettings()
    pollTimer = M.POLL_S                -- (ask at once)
  else
    if M.stopFollow then M.stopFollow() end
    dropAll()
    status.connected, status.count, status.error = false, 0, nil
    if status.credit then
      status.credit = nil
      local okC, cr = pcall(require, 'beamngEarth/credits')
      if okC and cr.setExtra then cr.setExtra('airtraffic', nil) end
    end
  end
  refreshHooks()
  return M.enabled
end

-- Ctrl+Alt+A: on / off. The first time (no consent yet) it says where to turn it on: the settings page shows what is
-- sent and to whom.
function M.toggle()
  if not M.enabled and not M.consented() then
    message('Live air traffic: turn it on in BeamNG.Earth settings, Air traffic (it says what is sent '
      .. 'and to whom first)', 8)
    return
  end
  saveSetting('airTraffic', not M.enabled)
  M.setEnabled(not M.enabled)
  message('Live air traffic: ' .. (M.enabled and 'on' or 'off'), 3)
end

function M.toggleContrails()
  saveSetting('airTrafficContrails', not M.contrails)
  M.contrails = not M.contrails
  if not M.contrails then trails = {} end
  message('Live air traffic contrails: ' .. (M.contrails and 'on' or 'off'), 3)
end

function M.toggleSounds()
  saveSetting('airTrafficSounds', not M.sounds)
  M.sounds = not M.sounds
  if not M.sounds then sound.clear() end
  message('Live air traffic sounds: ' .. (M.sounds and 'on' or 'off'), 3)
end

-- One line on the sky as the add-on sees it (for "where are my contrails?").
function M.skyReport()
  local n, jets = 0, 0
  for _, tr in pairs(trails) do if #tr.pts > 0 then n = n + 1 end end
  for _, it in pairs(items) do if it.trailing then jets = jets + 1 end end
  local vis = M.contrailLight(sunElev, 10668)
  local snd = M.sounds and string.format(', %d aircraft heard', sound.count()) or ', sounds OFF (Ctrl+Alt+S)'
  local cl = clouds.base and string.format(', clouds %d%% from %.1f km%s', math.floor(clouds.frac * 100 + 0.5),
    clouds.base / 1000, clouds.hidden and ' (hidden)' or '') or ', no cloud layer'
  return string.format(' | sun %.1f deg (%s), contrails above %d ft, %d jets making them, %d trails, visibility %d%%%s%s',
    sunElev, daylightSource or 'default', math.floor(contrailFt / 100 + 0.5) * 100, jets, n,
    math.floor(vis * 100 + 0.5), cl, M.contrails and '' or ', contrails OFF (Ctrl+Alt+C)') .. snd
end

-- What is that plane? The aircraft nearest the middle of the screen.
function M.identify()
  if not M.enabled then message('Live air traffic is off (Ctrl+Alt+A, or the settings page)', 4) return end
  local camPos, camFwd = camera()
  if not camPos or not anchor then return end
  local best, bestA = nil, math.rad(12)
  for _, it in pairs(items) do
    if it.visible and it.dist then
      local pos = frameOf(it)
      if pos then
        local dir = (pos - camPos):normalized()
        local a = math.acos(math.max(-1, math.min(1, dir:dot(camFwd))))
        if a < bestA then best, bestA = it, a end
      end
    end
  end
  if not best then
    local why = status.connected and string.format('%d aircraft tracked; none near the middle of the screen',
      status.count) or ('no data: ' .. tostring(status.error or 'waiting for the first answer'))
    message('Live air traffic: ' .. why .. M.skyReport(), 7)
    return
  end
  local a = best.raw
  local flight, typ = labelBits(best)
  local txt = flight .. '  ' .. typ
  if a.r ~= '' and a.r ~= flight then txt = txt .. ' (' .. a.r .. ')' end
  if best.base.ground then
    txt = txt .. (best.base.gs > 4 and ', taxiing' or ', on the ground')
  else
    txt = txt .. string.format(', %d ft, %d kt, heading %03d', math.floor((best.base.baroFt or best.altFt) / 100 + 0.5)
      * 100, math.floor(best.base.gs + 0.5), math.floor(best.trk + 0.5) % 360)
  end
  if a.d and a.d ~= '' then
    txt = txt .. ', ' .. (a.o and a.o ~= '' and (a.o .. ' to ') or 'to ') .. a.d
    if a.dn and a.dn ~= '' then txt = txt .. ' (' .. a.dn .. ')' end
  end
  txt = txt .. string.format(', %.1f km away', best.dist / 1000)
  message(txt, 8)
end

------------------------------------------------------------------------------------------ follow camera (F)
-- F: the camera rides with the aircraft nearest the middle of the screen (or the nearest one), behind and above it.
-- It looks at the aircraft until you move the mouse; then the view is yours to turn while it keeps riding along.
-- F again (or the aircraft leaving the data) puts the camera back the way it was.
M.FOLLOW_PICK_DEG = 20
local follow = nil         -- { it, prevCam, offset, lastQ, freeLook, cabin = { yaw, pitch, lastQ, f, r, u } }

local function camName()
  local ok, n = pcall(function() return core_camera.getActiveCamName(0) end)
  return ok and n or nil
end

function M.stopFollow(why)
  if not follow then return end
  local prev = follow.prevCam
  follow = nil
  M._followIt = nil
  local done = false
  if prev and prev ~= 'free' then
    done = pcall(function() core_camera.setByName(0, prev) end)
  end
  if not done and prev ~= 'free' then
    local c = rawget(_G, 'commands')
    if c and c.setGameCamera then pcall(c.setGameCamera) end
  end
  if why then message('Live air traffic: ' .. why, 4) end
end

local function pickTarget()
  local camPos, camFwd = camera()
  if not camPos then return nil end
  local best, bestA = nil, math.rad(M.FOLLOW_PICK_DEG)
  local nearest, nearD = nil, 15000
  for _, it in pairs(items) do
    if it.visible and it.fPos and not it.missing then
      local toA = it.fPos - camPos
      local d = toA:length()
      if d < 30000 then
        local a = math.acos(math.max(-1, math.min(1, toA:normalized():dot(camFwd))))
        if a < bestA then best, bestA = it, a end
      end
      if d < nearD then nearest, nearD = it, d end
    end
  end
  return best or nearest
end

-- C while following: into the aircraft (a window seat by the wing, or the pilot's seat on small aircraft) and out.
function M.toggleCabin()
  if not follow then
    message('Live air traffic: press F on an aircraft first, then Ctrl+Alt+F again to sit inside it', 4)
    return
  end
  if follow.cabin then
    follow.cabin = nil
    follow.offset, follow.lastQ, follow.freeLook = nil, nil, false
    message('Live air traffic: back outside (Ctrl+Alt+F to stop following)', 3)
    return
  end
  local it = follow.it
  local d = dimsOf(it)
  if not d.seat then message('Live air traffic: no seat to sit in on this one', 3) return end
  follow.cabin = { yaw = d.seatYaw or 0, pitch = (d.layout == 'airliner' and (d.seatYaw or 0) > 30) and -12 or -8 }
  local flight, typ = labelBits(it)
  message(string.format('In a seat on %s (%s). Move the mouse to look around; Ctrl+Alt+F to put the camera back.',
    flight, typ), 6)
end
function M._cabinIt() return follow and follow.cabin and follow.it or nil end

-- rotate v by quaternion q (x, y, z, w)
local function qrot(q, v)
  local qx, qy, qz, qw = q.x, q.y, q.z, q.w
  local tx = 2 * (qy * v.z - qz * v.y)
  local ty = 2 * (qz * v.x - qx * v.z)
  local tz = 2 * (qx * v.y - qy * v.x)
  return vec3(v.x + qw * tx + (qy * tz - qz * ty), v.y + qw * ty + (qz * tx - qx * tz), v.z + qw * tz + (qx * ty - qy * tx))
end
M._qrot = qrot

local function updateCabin(dt, it, d)
  local cab = follow.cabin
  local pos, f, r, u = it.fPos, it.fF, it.fR, it.fU
  if not (pos and f and r and u) then return end
  local s, st, e = it.drawS or 1, it.stretch or 1, d.seat
  local eye = pos + r * (e[1] * s) + f * (e[2] * s * st) + u * (e[3] * s)
  -- The view: the mouse owns it (the free camera turns it as always); the mod only turns it by however much the
  -- aircraft's heading changed since last frame, so the view swings round with the aircraft in a turn. (Nothing is
  -- read back and re-derived: that fought the free camera's own rotation and made the view thrash.)
  local heading = math.atan2(f.x, f.y)
  local q
  if not cab.started then
    local yw, pt = math.rad(cab.yaw or 0), math.rad(cab.pitch or 0)
    local fh = vec3(f.x, f.y, 0)
    if fh:length() < 1e-3 then fh = vec3(0, 1, 0) end
    fh = fh:normalized()
    local rh = vec3(fh.y, -fh.x, 0)
    local dir = rh * (-math.sin(yw) * math.cos(pt)) + fh * (math.cos(yw) * math.cos(pt)) + vec3(0, 0, math.sin(pt))
    q = quatFromDir(dir:normalized(), vec3(0, 0, 1))
    cab.started = true
  else
    local ok, cq = pcall(core_camera.getQuat)
    if not ok or not cq or (cab.useLast or 0) > 0 then cq = cab.lastQ end   -- (just after a camera switch: ours)
    cab.useLast = math.max(0, (cab.useLast or 0) - 1)
    local dh = (cab.heading or heading) - heading      -- (a turn to the right is a negative turn about +Z)
    if dh > math.pi then dh = dh - 2 * math.pi elseif dh < -math.pi then dh = dh + 2 * math.pi end
    local hs, hc = math.sin(dh / 2), math.cos(dh / 2)
    -- (about world Z, applied on top of the camera's own rotation: qz * cq)
    q = { x = hc * cq.x - hs * cq.y, y = hc * cq.y + hs * cq.x, z = hc * cq.z + hs * cq.w, w = hc * cq.w - hs * cq.z }
  end
  cab.heading, cab.lastQ, cab.eye = heading, q, eye
  local ok = pcall(core_camera.setPosRot, 0, eye.x, eye.y, eye.z, q.x, q.y, q.z, q.w)
  if not ok then M.stopFollow('the camera could not be moved') end
end

-- Just before the frame is drawn: wherever the camera really is (the game may apply or smooth its move a moment
-- later), the aircraft you sit in is moved by the same amount, so the cabin is locked to your eye and does not
-- shake at 250 m/s.
function M.lockCabin()
  local it = M._cabinIt and M._cabinIt()
  local cab = follow and follow.cabin
  if not (it and cab and cab.eye) then return end
  local ok, cp = pcall(core_camera.getPosition)
  if not ok or not cp then return end
  local dx, dy, dz = cp.x - cab.eye.x, cp.y - cab.eye.y, cp.z - cab.eye.z
  local dd = dx * dx + dy * dy + dz * dz
  if dd < 1e-6 or dd > 2500 then return end
  local ids = { it.objId, it.cabId, it.livId, it.gearId }
  for _, rt in ipairs(it.rotors or {}) do ids[#ids + 1] = rt.id end
  for _, g in ipairs(it.glyphs or {}) do ids[#ids + 1] = g.id end
  for _, id in ipairs(ids) do
    local p, o = id and placed[id], id and scenetree.findObjectById(id)
    if p and o then
      o:setPosRot(p[1] + dx, p[2] + dy, p[3] + dz, p[4].x, p[4].y, p[4].z, p[4].w)
      placed[id] = { p[1] + dx, p[2] + dy, p[3] + dz, p[4] }
    end
  end
  cab.eye = cp
end

-- F: follow the aircraft in the middle of the screen; F again: into its seat; F a third time: the camera back.
function M.toggleFollow()
  if follow then
    if not follow.cabin and dimsOf(follow.it).seat then M.toggleCabin() return end
    M.stopFollow('camera back')
    return
  end
  if not M.enabled then message('Live air traffic is off (Ctrl+Alt+A, or the settings page)', 4) return end
  local it = pickTarget()
  if not it then message('Live air traffic: no aircraft near enough to follow', 4) return end
  follow = { it = it, prevCam = camName(), freeLook = false }
  M._followIt = it
  local c = rawget(_G, 'commands')
  if c and c.setFreeCamera then pcall(c.setFreeCamera) end
  local flight, typ = labelBits(it)
  message(string.format('Following %s (%s). Move the mouse to look around; Ctrl+Alt+F again to sit inside it.', flight, typ), 5)
end

-- Each frame, after the aircraft are placed.
local function updateFollow(dt)
  if not follow then return end
  local it = follow.it
  if items[it.id] ~= it or it.missing and clock - it.missing > 5 then
    M.stopFollow('the aircraft you followed is out of the data; camera back')
    return
  end
  -- (the game's own camera key can switch away from the free camera: back to it, and ignore the jump in view)
  if camName() ~= 'free' then
    local c = rawget(_G, 'commands')
    if c and c.setFreeCamera then pcall(c.setFreeCamera) end
    if follow.cabin then follow.cabin.useLast = 2 end
  end
  local pos, f = it.fPos, it.fF
  if not pos then return end
  local d = dimsOf(it)
  if follow.cabin and d.seat then return updateCabin(dt, it, d) end
  local size = d.len * (it.stretch or 1)
  -- behind and above, along its track (not its pitch: the camera stays level through climbs and turns)
  local fh = vec3(f.x, f.y, 0)
  if fh:length() < 1e-3 then fh = vec3(0, 1, 0) end
  fh = fh:normalized()
  local want = fh * -(size * 1.7 + 25) + vec3(0, 0, size * 0.35 + 7)
  follow.offset = follow.offset and (follow.offset + (want - follow.offset) * math.min(1, dt / 0.6)) or want
  local cam = pos + follow.offset
  local q
  -- free look once the mouse has turned the view away from where the mod last pointed it
  if follow.lastQ and not follow.freeLook then
    local ok, cq = pcall(core_camera.getQuat)
    if ok and cq then
      local dotq = math.abs(cq.x * follow.lastQ.x + cq.y * follow.lastQ.y + cq.z * follow.lastQ.z + cq.w * follow.lastQ.w)
      if dotq < 0.9995 then follow.freeLook = true end
    end
  end
  if follow.freeLook then
    local ok, cq = pcall(core_camera.getQuat)
    q = ok and cq or nil
  end
  if not q then
    local look = (pos - cam):normalized()
    q = quatFromDir(look, vec3(0, 0, 1))
    follow.lastQ = q
  end
  local ok = pcall(core_camera.setPosRot, 0, cam.x, cam.y, cam.z, q.x, q.y, q.z, q.w)
  if not ok then M.stopFollow('the camera could not be moved') end
end
M._updateFollow = updateFollow
function M._following() return follow and follow.it or nil end


------------------------------------------------------------------------------------------ the maps
-- The aircraft for the 2D / 3D map's layer (settings.uiAirTraffic, polled every few seconds while a map is open):
-- { h, lat, lon, trk (deg), alt (ft, nil on the ground), gs (kt), f (flight), t (type), g (on the ground), o, d }
-- for each aircraft tracked, where it is drawn now. Empty while off.
function M.mapItems()
  local out = {}
  if not M.enabled then return out end
  for id, it in pairs(items) do
    if not it.missing and it.lat then
      local a = it.raw
      out[#out + 1] = { h = id, lat = it.lat, lon = it.lon, trk = math.floor((it.trk or 0) + 0.5) % 360,
        alt = (not it.base.ground) and math.floor((it.base.baroFt or it.altFt or 0) + 0.5) or nil,
        gs = math.floor((it.base.gs or 0) + 0.5), f = a.f ~= '' and a.f or (a.r ~= '' and a.r or id:upper()),
        t = a.t ~= '' and a.t or it.kind, g = it.base.ground == true, o = a.o, d = a.d }
    end
  end
  table.sort(out, function(x, y) return x.h < y.h end)
  return out
end

-- The 3D map's labels with the aircraft added (map3d.drawLabels, every frame while it is open; maplabels3d rows: a
-- place-like label at each aircraft's drawn position, its flight number). Rebuilt at most every 0.5 s.
local rows3d = { base = nil, t = -1, rows = nil }
function M.mapRows3d(base)
  if not M.enabled or setting('airTrafficMap', true) == false then return base end
  if rows3d.base == base and clock - rows3d.t < 0.5 and rows3d.rows then return rows3d.rows end
  local out = {}
  for i, r in ipairs(base or {}) do out[i] = r end
  for id, it in pairs(items) do
    if not it.missing and it.lat then
      local x, y, z = toGame(it.lat, it.lon, (it.altFt or 0) * FT)
      if x then
        local a = it.raw
        out[#out + 1] = { id = 'air:' .. id, k = 'poi', e = 'transit', t = a.f ~= '' and a.f or (a.r ~= '' and a.r or id:upper()),
          x = x, y = y, z = z, mz = -10, xz = 40, r = 40 }
      end
    end
  end
  rows3d.base, rows3d.t, rows3d.rows = base, clock, out
  return out
end

-- The status for the settings page and the map: { on, consented, connected, count, error, source, credit }.
function M.status()
  return { on = M.enabled, consented = M.consented(), connected = status.connected, count = status.count,
    error = status.error, source = status.source, credit = status.credit }
end

------------------------------------------------------------------------------------------ lifecycle
-- The app's hello: a test server (BNE_AIRTRAFFIC_TEST=on, set by whoever started it) turns the feature on for this
-- session without the page (nothing saved); the player's own setting rules otherwise.
M.testOn = false
function M.onHello(msg)
  local test = type(msg) == 'table' and msg.airTrafficTest == true
  if test == M.testOn then return end
  M.testOn = test
  if test then
    logI('live air traffic on for this session (the test server\'s BNE_AIRTRAFFIC_TEST)')
    M.setEnabled(true)
  else
    M.setEnabled(setting('airTraffic', false) == true)
  end
end

function M.onBeamNGEarthRebased()
  trailsDirty = true
  for _, it in pairs(items) do it.groundZ, it.gTimer = nil, 0 end     -- (game-frame heights: sampled again)
end
function M.onBeamNGEarthTeleported() dropAll(); pollTimer = M.POLL_S end
function M.onClientEndMission() dropAll(); wasInWorld = false; anchor = nil end

function M.onExtensionLoaded()
  M.applySettings()
  M.setEnabled(setting('airTraffic', false) == true)
  logI('live air traffic loaded (' .. (M.enabled and 'on' or 'off') .. ')')
end

function M.onExtensionUnloaded()
  dropAll()
  M.enabled = false
  refreshHooks()
end

-- for the tests and the console
function M._items() return items end
function M._trails() return trails end
function M._status() return status end
function M._merge(list, srvNow) return merge(list, srvNow) end
M._update = function(dt) return update(dt) end
function M._setClock(t) clock = t end
M._labelText = labelText
function M._dayK() return dayK end
function M._sunElev() return sunElev end

return M
