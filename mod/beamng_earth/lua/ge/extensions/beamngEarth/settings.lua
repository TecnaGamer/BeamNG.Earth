-- BeamNG.Earth settings: GE extension 'beamngEarth_settings' and the module the other parts read
-- (docs/DEVTOOLS.md "Pause-menu integration"). The pause menu has a "BeamNG.Earth" tab with a button
-- per settings page (ui/ui-vue/mods/beamngEarth/index.js, EarthSettingsPage.vue); the pages read and
-- writes everything here through the bridge (bngApi.engineLua, as the user's taa_standalone mod):
--   beamngEarth_settings.uiState()             -> { settings, schema, status, dev }
--   beamngEarth_settings.uiSet(key, value)     -> { ok, value, error } (validated, saved, applied)
--   beamngEarth_settings.uiReset()             every setting back to its default
--   beamngEarth_settings.uiDevSet(name, on)    beamngEarth_settings.uiDevPreset(name)  (dev tools)
--   beamngEarth_settings.uiCacheClear()         -> { ok, error } (the server's download cache)
--   beamngEarth_settings.uiPathSet(what, pick, path) -> { ok, error } (the cache folder, the BeamMP launcher)
--   beamngEarth_settings.setPageExists(true)   (index.js, once the button is registered)
--   beamngEarth_settings.uiPlaces() / uiPlaceAddHere(name) / uiPlaceAdd(name, "lat, lon") /
--     uiPlaceRename(id, name) / uiPlaceDelete(id) / uiPlaceGo(id) / uiPlaceGoText("lat, lon") /
--     uiPlaceGoRecent(i, lat, lon)  -> { ok, error, places, recent, cannotGo, here, warning }  (places.lua;
--     saved in places.FILE, apart from the settings)
-- Values persist in FILE (jsonReadFile / jsonWriteFile, as taa_standalone). Every default is
-- today's behaviour, so a missing or broken file changes nothing. The dev overlays themselves are
-- per session (DEVTOOLS.md "State persists per session only"); only their parameters persist here.
--
-- The imgui window (ui.lua) becomes a compact status HUD once the page exists, hidden at level
-- start unless `hudVisible` is on (the page itself is the place for settings then). Whether the
-- page exists: index.js reports it, and the pause menu's registry is asked too (load order of the
-- UI and the Lua extensions does not matter). Without the page the window stays as before.

local M = {}

local rebase = require('beamngEarth/rebase')
local ui = require('beamngEarth/ui')
local net = require('beamngEarth/net')
local credits = require('beamngEarth/credits')
local places = require('beamngEarth/places')

M.FILE = '/settings/beamngEarth.json'
M.VERSION = 1
M.BUTTON_ID = 'beamngEarth-settings' -- index.js: the pause tab's first page button (earthSettingsStore.js pageId)
M.LEVEL_ID = 'beamng_earth'

-- key -> { kind = 'number'|'bool'|'enum', default, min, max, values, label }. Order = ORDER.
M.SCHEMA = {
  -- the quality presets (M.PRESETS; the user, 2026-10-28: "proper quality presets", picked from the hardware on the
  -- first run: M.detectQuality); 'custom' once a knob is changed away from every preset
  quality = { kind = 'enum', default = 'auto', values = { 'auto', 'low', 'medium', 'high', 'ultra', 'extreme', 'custom' },
    label = 'Quality' },
  -- tree density (trees.lua QUALITY_LEVELS: ranks and tile caps) and draw distance (DISTANCES: the rings and the
  -- server's tree tile reach), split 2026-10-28 (the user: "give player much more control in the graphics settings")
  trees = { kind = 'enum', default = 'normal', values = { 'low', 'medium', 'normal', 'high', 'ultra', 'extreme' },
    label = 'Tree density' },
  treeDistance = { kind = 'enum', default = 'normal', values = { 'near', 'medium', 'normal', 'far', 'farther', 'max' },
    label = 'Tree draw distance' },
  nightLights = { kind = 'enum', default = 'normal', values = { 'few', 'normal', 'many' }, label = 'Street lamp lights' },
  -- the far field's terrain detail: the server's K table times M.TERRAIN_DETAIL (the 'graphics' message's lodScale)
  terrainDetail = { kind = 'enum', default = 'normal', values = { 'low', 'normal', 'high', 'ultra', 'extreme' },
    label = 'Terrain detail' },
  -- how far the far buildings, the skyline and the far bridges reach (M.BUILDING_DISTANCE: buildingScale)
  buildingDistance = { kind = 'enum', default = 'normal', values = { 'near', 'normal', 'far', 'max' },
    label = 'Building draw distance' },
  -- the drawn terrain block's aerial photo (px over the 1 km block; terrain.setPhotoSize, the server's photoPx):
  -- from the next level load
  groundPhoto = { kind = 'enum', default = '2048', values = { '1024', '2048', '4096' }, label = 'Ground photo resolution' },
  -- the grass's reach (groundcover.lua DISTANCE)
  grassDistance = { kind = 'enum', default = 'normal', values = { 'near', 'normal', 'far' }, label = 'Grass draw distance' },
  -- the engine's shape detail in our level (enginepref.lua: $pref::TS::detailAdjust; 'game': the game's Mesh Quality)
  objectDetail = { kind = 'enum', default = 'game', values = { 'game', 'low', 'normal', 'high', 'ultra', 'extreme' },
    label = 'Object detail' },
  -- not on the page: what the first run's detection found ("Steam Deck: Medium")
  qualityDetected = { kind = 'string', default = '', maxLen = 120, label = 'Detected hardware' },
  rebaseDistance = { kind = 'number', default = rebase.DEFAULT_DISTANCE, min = rebase.MIN_DISTANCE,
    max = rebase.MAX_DISTANCE, label = 'Rebase distance (m)' },
  viewDistanceMode = { kind = 'enum', default = 'horizon', values = { 'fixed', 'horizon' }, label = 'View distance' },
  viewDistanceKm = { kind = 'number', default = 100, min = 20, max = 1000, label = 'Fixed view distance (km)' },
  hazeStrength = { kind = 'number', default = 0, min = 0, max = 1, label = 'Distance haze (placeholder)' },
  skyMode = { kind = 'enum', default = 'real', values = { 'real', 'manual' }, label = 'Sky' },
  clouds = { kind = 'bool', default = true, label = 'Clouds' },
  weatherMode = { kind = 'enum', default = 'real', values = { 'real', 'manual' }, label = 'Weather' },
  realGravity = { kind = 'bool', default = true, label = 'Real gravity' },
  -- grass and shrubs on the surface layers (groundcover.lua; needs the server's --surface on)
  groundCover = { kind = 'bool', default = true, label = 'Grass and shrubs' },
  groundCoverDensity = { kind = 'enum', default = 'normal', values = { 'low', 'normal', 'high' }, label = 'Grass density' },
  -- the vegetation tiles' rocks and boulders (trees.lua groups rock, desert_rock; GitHub #5, after PR #321)
  rocks = { kind = 'bool', default = true, label = 'Rocks' },
  -- roads near the player (roads.lua; the server's --roads on)
  roads = { kind = 'bool', default = true, label = 'Roads' },
  -- exit, overhead and merge signs and Interstate mile markers along highways (roads.lua sign tiles; server
  -- signstream.py, from cody196's PR #363); needs Roads
  highwaySigns = { kind = 'bool', default = true, label = 'Highway signs' },
  -- Earth imagery on supported vehicle dashboard screens (dashboard.lua)
  dashboardSatellite = { kind = 'bool', default = true, label = 'Satellite on dashboard screens' },
  hudVisible = { kind = 'bool', default = false, label = 'Status HUD at level start' },
  -- the status bar at the top (statusbar.lua): street, town / state / country, loading, fps
  statusBar = { kind = 'bool', default = true, label = 'Status bar' },
  -- town / state / country names from Photon (komoot), sent the position rounded to ~1 km
  placeNamesOnline = { kind = 'bool', default = true, label = 'Place names in the status bar (online)' },
  -- BeamMP started by the BeamNG.Earth app with the game (launcher.py reads this file: bmplaunch.on_launch; its
  -- "don't ask again" writes it off)
  beammpOnLaunch = { kind = 'bool', default = true, label = 'Start BeamMP with BeamNG.Earth' },
  -- the on-screen data credits (credits.lua): on by default, CC BY licences ask for visible attribution
  creditsVisible = { kind = 'bool', default = true, label = 'Data credits on screen' },
  creditsCorner = { kind = 'enum', default = 'bottom-left', values = credits.CORNERS, label = 'Data credits corner' },
  creditsSize = { kind = 'enum', default = 'small', values = { 'small', 'normal' }, label = 'Data credits size' },
  -- the server's download cache (server cache.py): its size cap in GB, sent on every hello and change
  -- (typed on the page since 2026-10-29, the user: "change the cache limit setting to be a text box"; 0: unlimited.
  -- The old choices '5' .. '50' read as numbers, 'unlimited' as 0: MIGRATE)
  cacheCapGb = { kind = 'number', default = 20, min = 0, max = 100000, label = 'Download cache size (GB, 0: unlimited)' },
  -- even frame pacing (framepacer.lua): frames shorter than the recent ones wait, so the game's time runs steadily
  framePacing = { kind = 'bool', default = true, label = 'Even frame pacing' },
  -- the GPS route's data fetched ahead (the server's route_download; sent with each route): none, the next 12 miles
  -- (20 km), or the whole route (up to 300 miles)
  routeDownload = { kind = 'enum', default = 'ahead', values = { 'off', 'ahead', 'route' },
    label = 'Download along the GPS route' },
  devOutlineKm = { kind = 'number', default = 3, min = 0.5, max = 20, label = 'Dev outlines radius (km)' },
  devMapKm = { kind = 'number', default = 10, min = 1, max = 80, label = 'Dev mini-map radius (km)' },
  -- not on the page (not in ORDER): the welcome version last shown (EarthWelcome.vue)
  welcomeSeen = { kind = 'number', default = 0, min = 0, max = 1000, label = 'Welcome page seen (version)' },
  -- not on the page: the newer version whose update notice the player dismissed (core.lua drawUpdateNotice)
  updateDismissed = { kind = 'string', default = '', maxLen = 40, label = 'Update notice dismissed (version)' },
}
M.ORDER = { 'quality', 'framePacing', 'terrainDetail', 'groundPhoto', 'trees', 'treeDistance', 'buildingDistance', 'objectDetail',
  'grassDistance', 'nightLights', 'rebaseDistance', 'viewDistanceMode', 'viewDistanceKm', 'hazeStrength', 'skyMode', 'clouds',
  'weatherMode', 'realGravity', 'groundCover', 'groundCoverDensity', 'rocks', 'roads', 'highwaySigns', 'dashboardSatellite',
  'hudVisible', 'statusBar',
  'placeNamesOnline', 'creditsVisible', 'beammpOnLaunch',
  'creditsCorner', 'creditsSize', 'cacheCapGb', 'routeDownload', 'devOutlineKm', 'devMapKm' }

-- Stored but not on the settings page
M.HIDDEN = { welcomeSeen = true, updateDismissed = true, qualityDetected = true }

-- The quality presets: each sets these knobs at once ('high' = the defaults before presets existed, 2026-10-28). A
-- Steam Deck on Handheld in a forest (Patapsco, 2026-10-28): low 37.0 fps, high 31.5, ultra 30.9; 'medium' (its first-run
-- pick) thins the trees too: the same trees as 'high' gave it only 32.2. 'extreme' (the user, 2026-10-28: "an extreme
-- quality setting that puts everything at max quality to the real horizon"): every knob at its most, never picked by
-- the first run (M.detectQuality).
M.PRESET_KEYS = { 'trees', 'treeDistance', 'groundCover', 'groundCoverDensity', 'grassDistance', 'rocks', 'nightLights',
  'viewDistanceMode', 'viewDistanceKm', 'terrainDetail', 'buildingDistance', 'groundPhoto', 'objectDetail' }
M.PRESETS = {
  low = { trees = 'low', treeDistance = 'near', groundCover = false, groundCoverDensity = 'low', grassDistance = 'near',
    rocks = true, nightLights = 'few', viewDistanceMode = 'fixed', viewDistanceKm = 40, terrainDetail = 'low',
    buildingDistance = 'near', groundPhoto = '1024', objectDetail = 'game' },
  medium = { trees = 'medium', treeDistance = 'medium', groundCover = true, groundCoverDensity = 'low',
    grassDistance = 'normal', rocks = true, nightLights = 'few', viewDistanceMode = 'horizon', viewDistanceKm = 100,
    terrainDetail = 'normal', buildingDistance = 'normal', groundPhoto = '2048', objectDetail = 'game' },
  high = { trees = 'normal', treeDistance = 'normal', groundCover = true, groundCoverDensity = 'normal',
    grassDistance = 'normal', rocks = true, nightLights = 'normal', viewDistanceMode = 'horizon', viewDistanceKm = 100,
    terrainDetail = 'normal', buildingDistance = 'normal', groundPhoto = '2048', objectDetail = 'game' },
  ultra = { trees = 'high', treeDistance = 'far', groundCover = true, groundCoverDensity = 'high', grassDistance = 'normal',
    rocks = true, nightLights = 'many', viewDistanceMode = 'horizon', viewDistanceKm = 100, terrainDetail = 'high',
    buildingDistance = 'far', groundPhoto = '2048', objectDetail = 'game' },
  extreme = { trees = 'extreme', treeDistance = 'max', groundCover = true, groundCoverDensity = 'high',
    grassDistance = 'far', rocks = true, nightLights = 'many', viewDistanceMode = 'horizon', viewDistanceKm = 100,
    terrainDetail = 'extreme', buildingDistance = 'max', groundPhoto = '4096', objectDetail = 'extreme' },
}
M.PRESET_ORDER = { 'low', 'medium', 'high', 'ultra', 'extreme' }

-- The server's knobs (core.sendGraphics, the 'graphics' message): the far field's K table times TERRAIN_DETAIL (64
-- cells, Baltimore's first selection, 2026-10-28: 0.75 -> 0.78x the far tiles, 1.3 -> ~1.3x, 1.6 -> ~1.6x, 2 -> 2.1x;
-- L1 splits at 288 / 384 / 499 / 614 / 768 m), the far buildings' / skyline's / far bridges' reach times
-- BUILDING_DISTANCE (1.6 / 4.8 / 4.8 km at 1).
M.TERRAIN_DETAIL = { low = 0.75, normal = 1.0, high = 1.3, ultra = 1.6, extreme = 2.0 }
M.BUILDING_DISTANCE = { near = 0.6, normal = 1.0, far = 1.5, max = 2.0 }
-- A file saved before the tree distance had its own setting: the distance its tree level had (trees.lua, before)
M.TREE_DISTANCE_OF = { low = 'near', medium = 'medium', normal = 'normal', high = 'far', ultra = 'farther' }

-- Pure: the preset a first run picks for this hardware (Engine.Platform.getGPUInfo: { name, memoryMB }) and why.
-- A Steam Deck (its APU: "AMD Custom GPU 0405 / 0932 (RADV VANGOGH)") or a GPU with under 3 GB: medium / low.
function M.detectQuality(gpu)
  local name = type(gpu) == 'table' and tostring(gpu.name or '') or ''
  local mem = type(gpu) == 'table' and tonumber(gpu.memoryMB) or nil
  local up = name:upper()
  if up:find('VANGOGH', 1, true) or up:find('CUSTOM GPU 0405', 1, true) or up:find('CUSTOM GPU 0932', 1, true) then
    return 'medium', 'Steam Deck'
  end
  if mem and mem > 0 and mem < 3072 then return 'low', string.format('%s, %d MB', name, mem) end
  if mem and mem > 0 and mem < 6144 then return 'medium', string.format('%s, %d MB', name, mem) end
  return 'high', name ~= '' and name or 'this PC'
end

-- Old values that still load (and set) as their replacement, without a warning: 'auto' weather
-- (real unless BWM_Weather was changing the weather) is Real now, which switches itself to Manual
-- when anyone changes the weather.
M.MIGRATE = { weatherMode = { auto = 'real' }, cacheCapGb = { unlimited = 0 } }

local values = {}       -- the settings in use (always complete and valid)
local cachePolls = 0    -- uiState calls (the cache usage is asked every CACHE_POLL-th)
M.CACHE_POLL = 5
local pageFlag = false  -- index.js said the page exists
local appliedView = nil -- last view distance written to LevelInfo (m)

local function defaults()
  local t = {}
  for k, s in pairs(M.SCHEMA) do t[k] = s.default end
  return t
end
M.defaults = defaults
values = defaults() -- until load(): the defaults (= today's behaviour)

-- Settings no longer on the page, left in older files: ignored without a warning (gasStations: gas stations are
-- always on since the user's 2026-10-25 "make gas station always enabled and remove the toggle")
M.RETIRED = { gasStations = true }

-- A valid value for key, or nil + error. Numbers are clamped to the range; strings that read as a
-- number or boolean are accepted (the page sends what its controls hold).
function M.validate(key, v)
  local s = M.SCHEMA[key]
  if not s then return nil, 'unknown setting ' .. tostring(key) end
  if s.kind == 'number' then
    if M.MIGRATE[key] and M.MIGRATE[key][v] ~= nil then v = M.MIGRATE[key][v] end
    local n = tonumber(v)
    if not n or n ~= n or n == math.huge or n == -math.huge then return nil, key .. ': not a number' end
    return math.max(s.min, math.min(s.max, n))
  elseif s.kind == 'string' then
    if type(v) ~= 'string' and type(v) ~= 'number' then return nil, key .. ': not a string' end
    return tostring(v):sub(1, s.maxLen or 200)
  elseif s.kind == 'bool' then
    if v == true or v == 'true' or v == 1 then return true end
    if v == false or v == 'false' or v == 0 then return false end
    return nil, key .. ': not a boolean'
  else
    if M.MIGRATE[key] and M.MIGRATE[key][v] ~= nil then v = M.MIGRATE[key][v] end
    for _, ok in ipairs(s.values) do if v == ok then return v end end
    return nil, key .. ': must be one of ' .. table.concat(s.values, ', ')
  end
end

-- Read the file (path: FILE): every valid known value is taken, the rest stays at its default.
-- Returns the values and a list of warnings (unknown keys, invalid values, an unreadable file).
function M.load(path)
  values = defaults()
  local warnings = {}
  local read = rawget(_G, 'jsonReadFile')
  local ok, data = false, nil
  if read and (not FS or not FS.fileExists or FS:fileExists(path or M.FILE)) then ok, data = pcall(read, path or M.FILE) end
  if ok and type(data) == 'table' then
    for k, v in pairs(data) do
      if k ~= 'version' and not M.RETIRED[k] then
        local val, err = M.validate(k, v)
        if val ~= nil then values[k] = val else warnings[#warnings + 1] = err end
      end
    end
    M.fillNewKnobs(data)
  elseif ok and data ~= nil then
    warnings[#warnings + 1] = 'settings file is not an object'
  elseif not ok and data ~= nil then
    warnings[#warnings + 1] = 'settings file unreadable: ' .. tostring(data)
  end
  for _, w in ipairs(warnings) do log('W', 'beamngEarth', 'settings: ' .. w) end
  if values.quality == 'auto' then M.resolveAuto() end
  return values, warnings
end

-- A file from before some preset knobs existed (the graphics knobs of 2026-10-28): each knob it lacks follows its
-- quality preset (a player on Ultra gets Ultra's terrain detail), on Custom its default, the tree distance the one
-- its tree level had then (TREE_DISTANCE_OF). Nothing for a first run (quality 'auto': resolveAuto).
function M.fillNewKnobs(data)
  local preset = M.PRESETS[values.quality]
  for _, k in ipairs(M.PRESET_KEYS) do
    if data[k] == nil then
      if preset then values[k] = preset[k]
      elseif k == 'treeDistance' and data.trees ~= nil then values[k] = M.TREE_DISTANCE_OF[values.trees] or values[k] end
    end
  end
end

-- Pure-ish: the preset whose knobs all equal the current values, or nil.
function M.matchingPreset()
  for _, p in ipairs(M.PRESET_ORDER) do
    local all = true
    for _, k in ipairs(M.PRESET_KEYS) do
      if M.PRESETS[p][k] ~= values[k] then all = false; break end
    end
    if all then return p end
  end
  return nil
end

-- The first run (quality 'auto'): the hardware's preset, unless the player changed one of its knobs before presets
-- existed (then 'custom', theirs kept). Saved by the caller's next save (and here, when the file can be written).
function M.resolveAuto(gpu)
  if gpu == nil then
    local P = rawget(_G, 'Engine') and Engine.Platform
    if P and P.getGPUInfo then
      local ok, g = pcall(P.getGPUInfo)
      if ok then gpu = g end
    end
  end
  local pick, why = M.detectQuality(gpu)
  values.qualityDetected = why .. ': ' .. pick
  local changed = false
  for _, k in ipairs(M.PRESET_KEYS) do
    if values[k] ~= M.SCHEMA[k].default then changed = true end
  end
  if changed then
    values.quality = M.matchingPreset() or 'custom'
  else
    for _, k in ipairs(M.PRESET_KEYS) do values[k] = M.PRESETS[pick][k] end
    values.quality = pick
  end
  log('I', 'beamngEarth', 'settings: quality ' .. values.quality .. ' (detected ' .. values.qualityDetected .. ')')
  if rawget(_G, 'jsonWriteFile') then M.save() end
end

-- Pick a preset: its knobs set, saved and applied. Returns { ok, settings, error }.
function M.setQuality(p)
  if not M.PRESETS[p] then return { ok = false, error = 'unknown preset ' .. tostring(p) } end
  for _, k in ipairs(M.PRESET_KEYS) do values[k] = M.PRESETS[p][k] end
  values.quality = p
  M.save()
  for _, k in ipairs(M.PRESET_KEYS) do M.apply(k) end
  return { ok = true, settings = M.getAll() }
end

-- Write every value (and the version) to the file. Returns true on success.
function M.save(path)
  local write = rawget(_G, 'jsonWriteFile')
  if not write then return false end
  local out = { version = M.VERSION }
  for k, v in pairs(values) do out[k] = v end
  local ok, err = pcall(write, path or M.FILE, out, true)
  if not ok then log('W', 'beamngEarth', 'settings: write failed: ' .. tostring(err)) end
  return ok and err ~= false
end

function M.get(key) return values[key] end

-- The download cache's cap for the server: GB, 0 = unlimited.
function M.cacheCapGb() return math.max(0, tonumber(values.cacheCapGb) or 0) end

-- bytes as the page shows them: "350 MB", "1.2 GB", "20 GB" (decimal units, as the cap)
local function size(b, whole)
  if b < 0.1e9 then return string.format('%d MB', math.floor(b / 1e6 + 0.5)) end
  if whole then return string.format('%g GB', math.floor(b / 1e8 + 0.5) / 10) end
  return string.format('%.1f GB', b / 1e9)
end

-- The page's "Download cache" line from the server's 'cache' message (core.cacheState()).
function M.cacheLine(c)
  if type(c) ~= 'table' then return 'no information from the server' end
  local used = size(c.usedBytes or 0)
  if not c.ready then return 'counting files... (' .. used .. ' so far)' end
  local files = string.format('%d files', c.files or 0)
  if not c.managed then return used .. ', ' .. files .. ' (a newer version\'s cache: left alone)' end
  local kept = (c.pinnedBytes or 0) > 0 and (', ' .. size(c.pinnedBytes) .. ' in saved areas (Clear cache keeps them)')
    or ''
  if (c.capBytes or 0) <= 0 then return used .. ', ' .. files .. ' (no limit)' .. kept end
  return used .. ' of ' .. size(c.capBytes, true) .. ', ' .. files .. kept
end

function M.getAll()
  local t = {}
  for k, v in pairs(values) do t[k] = v end
  return t
end

------------------------------------------------------------------------------------------
-- applying values to the running mod (each through the module that owns the behaviour)

local function ext(name) return rawget(_G, name) end

-- LevelInfo.visibleDistance only takes effect after postApply (in-game, 2026-10-10). Only in our
-- level, and the default (the level file's 100 km) never touches the engine: nothing changes until
-- the player picks another distance.
local function applyViewDistance()
  local li = scenetree and scenetree.theLevelInfo
  local level = rawget(_G, 'getCurrentLevelIdentifier') and getCurrentLevelIdentifier()
  if values.viewDistanceMode ~= 'fixed' then
    -- 'horizon' (the default): v1.4 M1h drives it. Coming back from a fixed distance we set, the
    -- level's own distance is put back once (M1h's next status sets its own).
    if li and level == M.LEVEL_ID and appliedView ~= nil then
      li.visibleDistance = M.SCHEMA.viewDistanceKm.default * 1000
      if li.postApply then li:postApply() end
      appliedView = nil
      local core = ext('beamngEarth_core')
      if core and core.refreshHorizon then core.refreshHorizon() end    -- (the horizon's own distance at once)
    end
    return
  end
  local m = values.viewDistanceKm * 1000
  if not li or level ~= M.LEVEL_ID or appliedView == m then return end
  if appliedView == nil and m == M.SCHEMA.viewDistanceKm.default * 1000 then return end
  li.visibleDistance = m
  if li.postApply then li:postApply() end
  appliedView = m
end

-- The server's graphics knobs now ({ lodScale, treeScale, buildingScale, photoPx, viewM }: core.sendGraphics). photoPx: the
-- ground photo size of the block in the level now, or of the next one (terrain.photoSize).
function M.serverGraphics()
  local okT, trees = pcall(require, 'beamngEarth/trees')
  local okR, terrain = pcall(require, 'beamngEarth/terrain')
  return {
    lodScale = M.TERRAIN_DETAIL[values.terrainDetail] or 1.0,
    treeScale = okT and trees.distanceScale and trees.distanceScale(values.treeDistance) or 1.0,
    buildingScale = M.BUILDING_DISTANCE[values.buildingDistance] or 1.0,
    photoPx = okR and terrain.photoSize and terrain.photoSize() or tonumber(values.groundPhoto),
    -- a fixed view distance: the far field's radius at most (nothing past it is drawn); 0 = the real horizon
    viewM = values.viewDistanceMode == 'fixed' and values.viewDistanceKm * 1000 or 0,
  }
end

local function sendGraphics()
  local core = ext('beamngEarth_core')
  if core and core.sendGraphics then core.sendGraphics() end
end

local function inOurLevel()
  local core = ext('beamngEarth_core')
  local st = core and core._state and core._state() or nil
  return st and st.inOurLevel or false
end

M.APPLY = {
  rebaseDistance = function(v)
    local core = ext('beamngEarth_core')
    if core and core.setRebaseDistance then core.setRebaseDistance(v) else rebase.setDistance(v) end
  end,
  viewDistanceMode = function() applyViewDistance(); sendGraphics() end,
  viewDistanceKm = function() applyViewDistance(); sendGraphics() end,
  skyMode = function(v)
    local sky = ext('beamngEarth_sky')
    if sky and sky.setMode and sky.getMode and sky.getMode() ~= v then sky.setMode(v) end
  end,
  clouds = function(v)
    local sky = ext('beamngEarth_sky')
    if sky and sky.setClouds and sky.CLOUDS_ENABLED ~= v then sky.setClouds(v) end
  end,
  -- real weather and gravity (weather.lua; the server's --weather / --gravity decide whether data come).
  -- weather.lua and sky.lua save 'manual' here themselves when the weather / time is changed in the
  -- game; the mode is then already set, so it is not applied again. 'real' always is: it also clears
  -- a gravity change made in the game (weather.lua keeps that until setMode('real')).
  weatherMode = function(v)
    local w = ext('beamngEarth_weather')
    if w and w.setMode and not (v == 'manual' and w.getMode and w.getMode() == v) then w.setMode(v) end
  end,
  realGravity = function(v)
    local w = ext('beamngEarth_weather')
    if w and w.setGravityEnabled and w.GRAVITY_ENABLED ~= v then w.setGravityEnabled(v) end
  end,
  trees = function(v) require('beamngEarth/trees').setQuality(v) end,
  treeDistance = function(v)
    require('beamngEarth/trees').setDistance(v)
    sendGraphics()          -- (the server's tree tiles reach as far)
  end,
  terrainDetail = sendGraphics,
  buildingDistance = sendGraphics,
  groundPhoto = function(v)
    require('beamngEarth/terrain').setPhotoSize(tonumber(v))
    sendGraphics()
  end,
  grassDistance = function(v) require('beamngEarth/groundcover').setDistance(v) end,
  objectDetail = function(v) require('beamngEarth/enginepref').setLevel(v, inOurLevel()) end,
  nightLights = function(v) require('beamngEarth/nightlights').setLightCount(v) end,
  groundCover = function(v) require('beamngEarth/groundcover').setEnabled(v) end,
  groundCoverDensity = function(v) require('beamngEarth/groundcover').setDensity(v) end,
  rocks = function(v) require('beamngEarth/trees').setGroupEnabled({ 'rock', 'desert_rock' }, v) end,
  highwaySigns = function(v)
    local core = ext('beamngEarth_core')
    if core and core.setHighwaySigns then core.setHighwaySigns(v) else require('beamngEarth/roads').setSignsEnabled(v) end
  end,
  roads = function(v)
    local core = ext('beamngEarth_core')
    if core and core.setRoadsEnabled then core.setRoadsEnabled(v) else require('beamngEarth/roads').setEnabled(v) end
  end,
  dashboardSatellite = function(v)
    local dashboard = ext('beamngEarth_dashboard')
    if dashboard and dashboard.setEnabled then dashboard.setEnabled(v) end
  end,
  hudVisible = function(v)
    if M.pageExists() then ui.visible = v end
  end,
  creditsVisible = function(v) credits.setEnabled(v) end,
  statusBar = function(v) require('beamngEarth/statusbar').enabled = v and true or false end,
  creditsCorner = function(v) credits.setCorner(v) end,
  creditsSize = function(v) credits.setSize(v) end,
  cacheCapGb = function()
    local core = ext('beamngEarth_core')
    if core and core.cacheSetCap then core.cacheSetCap(M.cacheCapGb()) end
  end,
  devOutlineKm = function(v)
    local dev = ext('beamngEarth_dev')
    if dev and dev.setParam then dev.setParam('outlineKm', v) end
  end,
  devMapKm = function(v)
    local dev = ext('beamngEarth_dev')
    if dev and dev.setParam then dev.setParam('mapKm', v) end
  end,
  -- hazeStrength: a placeholder until the haze exists (v1.4 M1h)
}

function M.apply(key)
  local f = M.APPLY[key]
  if not f then return end
  local ok, err = pcall(f, values[key])
  if not ok then log('W', 'beamngEarth', 'settings: applying ' .. key .. ' failed: ' .. tostring(err)) end
end

function M.applyAll()
  for _, k in ipairs(M.ORDER) do if k ~= 'hudVisible' then M.apply(k) end end
end

-- Validate, store, save and apply one value. Returns { ok, value, error }.
function M.set(key, v)
  local val, err = M.validate(key, v)
  if val == nil then return { ok = false, error = err, value = values[key] } end
  if key == 'quality' then
    if M.PRESETS[val] then return M.setQuality(val) end
    if val == 'auto' then M.resolveAuto(); M.applyAll(); return { ok = true, value = values.quality, settings = M.getAll() } end
  end
  values[key] = val
  local follows = false
  for _, k in ipairs(M.PRESET_KEYS) do if k == key then follows = true end end
  if follows then values.quality = M.matchingPreset() or 'custom' end     -- (a knob moved: Custom, or the preset it now is)
  M.save()
  M.apply(key)
  return { ok = true, value = val, quality = values.quality }
end

------------------------------------------------------------------------------------------
-- the settings page

-- Does the pause-menu page exist? index.js says so, or the pause menu lists our button.
function M.pageExists()
  if pageFlag then return true end
  local pa = ext('ui_pause_actions')
  if not pa or not pa.getVisibleModTabs then return false end
  local ok, tabs = pcall(pa.getVisibleModTabs)
  if not ok or type(tabs) ~= 'table' then return false end
  for _, tab in ipairs(tabs) do
    for _, b in ipairs(tab.buttons or {}) do
      if b.id == M.BUTTON_ID then pageFlag = true; return true end
    end
  end
  return false
end

-- The window becomes the compact HUD once the page exists; hidden unless hudVisible.
local function applyHud()
  ui.compact = true
  ui.hudHidden = not values.hudVisible
  ui.visible = values.hudVisible
end

function M.setPageExists(on)
  pageFlag = on and true or false
  if pageFlag then applyHud() end
  return pageFlag
end

-- Called by core at level start: is the window hidden there? (Nothing changes without the page.)
function M.hudHiddenAtStart()
  if not M.pageExists() then return false end
  applyHud()
  return not values.hudVisible
end

function M.uiState()
  local core = ext('beamngEarth_core')
  local st = core and core._state and core._state() or nil
  local dev = ext('beamngEarth_dev')
  local status = {
    connection = net.state(), lastError = (not net.isConnected()) and net.lastError or nil,
    session = st and st.sessionId, anchorId = st and st.anchorId,
    anchorLat = st and st.anchor and st.anchor.lat, anchorLon = st and st.anchor and st.anchor.lon,
    playerLat = st and st.player and st.player.lat, playerLon = st and st.player and st.player.lon,
    dist = st and st.player and st.player.dist, rebaseDistance = rebase.distance,
    status = st and st.status or '', inOurLevel = st and st.inOurLevel or false,
    attribution = core and core.attribution and core.attribution() or '',
    credits = core and core.credits and core.credits() or nil, -- the on-screen data credits now
    -- the connection banner's state line { id, level, text, guard } (docs/ROADMAP.md "Robustness" 1)
    banner = core and core.banner and core.banner() or nil,
    update = core and core.updateInfo and core.updateInfo() or nil,  -- a newer version on GitHub
  }
  -- server features (status only): weather, gravity, imagery de-shadowing and colour seams
  local w = ext('beamngEarth_weather')
  status.weather = w and w.statusLine and w.statusLine() or nil
  status.gravity = w and w.gravityLine and w.gravityLine() or nil
  status.deshadow = st and st.deshadow or 'off'
  status.colormatch = st and st.colormatch or 'off'
  -- the download cache: the server's last numbers, fresh ones asked every CACHE_POLL-th call (the page
  -- polls every 2 s while it is open)
  cachePolls = cachePolls + 1
  if core and core.cacheQuery and cachePolls % M.CACHE_POLL == 1 then core.cacheQuery() end
  local c = core and core.cacheState and core.cacheState() or nil
  status.cache = { line = M.cacheLine(c), clears = c and c.clears or 0, clearedBytes = c and c.clearedBytes or nil,
    clearedText = c and c.clearedBytes and size(c.clearedBytes) or nil }
  -- the cache folder and the BeamMP launcher (the server's app_paths), asked with the cache numbers
  if core and core.appPath and cachePolls % M.CACHE_POLL == 1 then core.appPath() end
  status.paths = core and core.appPathState and core.appPathState() or nil
  local schema = {}
  for _, k in ipairs(M.ORDER) do
    local s = M.SCHEMA[k]
    schema[#schema + 1] = { id = k, kind = s.kind, default = s.default, min = s.min, max = s.max, values = s.values,
      label = s.label }
  end
  return { settings = M.getAll(), schema = schema, status = status,
    dev = dev and dev.state and dev.state() or nil, hudVisible = ui.visible }
end

function M.uiSet(key, v) return M.set(key, v) end

function M.uiReset()
  values = defaults()
  M.resolveAuto()                               -- (quality 'auto': the hardware's preset again)
  M.save()
  M.applyAll()
  return M.getAll()
end

-- "Clear cache" (the page asked for confirmation): the server removes every download but this
-- session's and the gravity model; its answer (core.cacheState().clears + 1) carries the bytes freed.
function M.uiCacheClear()
  local core = ext('beamngEarth_core')
  if not (core and core.cacheClear) then return { ok = false, error = 'mod not loaded' } end
  local sent, why = core.cacheClear()
  if not sent then return { ok = false, error = why or 'server not connected' } end
  return { ok = true }
end

-- "Change..." (pick: the system's file window, on the server's computer) or a typed path ("Use") for the cache folder
-- (what 'cache') or the BeamMP launcher ('beammp'); the answer comes in status.paths (answers + 1).
function M.uiPathSet(what, pick, path)
  if what ~= 'cache' and what ~= 'beammp' then return { ok = false, error = 'unknown path' } end
  local core = ext('beamngEarth_core')
  if not (core and core.appPath) then return { ok = false, error = 'mod not loaded' } end
  local sent, why = core.appPath(what, pick == true, path)
  if not sent then return { ok = false, error = why or 'server not connected' } end
  return { ok = true }
end

function M.uiDevSet(name, on)
  local dev = ext('beamngEarth_dev')
  if dev and dev.set then dev.set(name, on and true or false) end
  return dev and dev.state and dev.state() or nil
end

function M.uiDevPreset(name)
  local dev = ext('beamngEarth_dev')
  if dev and dev.preset then dev.preset(name) end
  return dev and dev.state and dev.state() or nil
end

-- Show / hide the HUD now (the page's "Show status HUD" switch also sets the level-start default).
function M.uiShowHud(on)
  ui.visible = on and true or false
  return ui.visible
end

-- Saved places (places.lua): the page's "Places" section. Each answers with the whole list again;
-- an error inside answers { ok = false, error } (the page would otherwise get nothing back).
local function placesCall(f, ...)
  local ok, res = pcall(f, ...)
  if ok then return res end
  log('W', 'beamngEarth', 'places: ' .. tostring(res))
  return { ok = false, error = 'Places: ' .. tostring(res), places = {}, recent = {} }
end
function M.uiPlaces() return placesCall(places.uiState) end
function M.uiPlaceAddHere(name) return placesCall(places.uiAddHere, name) end
function M.uiPlaceAdd(name, text) return placesCall(places.uiAdd, name, text) end
function M.uiPlaceRename(id, name) return placesCall(places.uiRename, id, name) end
function M.uiPlaceDelete(id) return placesCall(places.uiDelete, id) end
function M.uiPlaceGo(id) return placesCall(places.uiGo, id) end
function M.uiPlaceGoText(text) return placesCall(places.uiGoText, text) end
function M.uiPlaceGoRecent(i, lat, lon) return placesCall(places.uiGoRecent, i, lat, lon) end

------------------------------------------------------------------------------------------
-- The world map (ui/ui-vue/mods/beamngEarth/EarthMap.vue: a route of its own, and a pause-menu
-- button; tiles from the server's 127.0.0.1 endpoint, server/maptiles.py, port in hello.mapPort).

M.MAP_ROUTE = 'beamngEarthMap' -- index.js registers the route under this name
M.SETTINGS_ROUTE = 'beamngEarthSettings' -- the full settings page (index.js; the pause menu's card opens it)

function M.openSettings()
  return M.navigate(M.SETTINGS_ROUTE)
end

-- The page's Back (and Esc): the menu it was opened from (the pause menu in a level, the main menu outside one)
function M.closeSettings()
  return M.navigate('menu')
end
local mapOpen = false

-- The player's heading in degrees clockwise from north (the bubble's +y is north, +x east), or nil.
local function playerHeading()
  local gpv = rawget(_G, 'getPlayerVehicle')
  local veh = gpv and gpv(0)
  if not veh then return nil end
  local ok, dir = pcall(function() return veh:getDirectionVector() end)
  if not ok or not dir then return nil end
  if math.abs(dir.x) + math.abs(dir.y) < 1e-6 then return nil end
  local h = math.deg(math.atan2(dir.x, dir.y))
  -- from true north (the map's up): the frame's +y is north only at the anchor
  local core = ext('beamngEarth_core')
  local st = core and core._state and core._state() or nil
  local a = st and st.anchor
  if a and a.northAt then
    local okP, p = pcall(function() return veh:getPosition() end)
    local okN, nx, ny = pcall(a.northAt, a, okP and p and p.x or 0, okP and p and p.y or 0)
    if okN and tonumber(nx) and tonumber(ny) then h = h - math.deg(math.atan2(nx, ny)) end
  end
  return (h + 360) % 360
end

-- The map's tiles as files (the game's UI browser cancels every http request, even to 127.0.0.1): list
-- {{z, x, y, layer}, ...} the map shows, the most wanted first (layer 'sat' when left out, 'map', 'labels'); returns
-- the keys "layer/z/x/y" whose file is there (mapTilePath) and asks the server for the others (map_tiles): the
-- whole missing list whenever it changes, and again every MAP_ASK_S while some are missing. The server builds the
-- newest list first and drops what it no longer has (server/maptiles.py TileFiles), so a list that became empty
-- goes out too (it cancels).
M.MAP_ASK_S = 4
M.MAP_TILES_DIR = '/temp/beamngEarth/maptiles/'
M.MAP_LAYERS = { sat = '.jpg', map = '.jpg', labels = '.png' }   -- server maptiles.LAYERS
function M.mapTilePath(layer, z, x, y)
  local zxy = string.format('%d/%d/%d', z, x, y)
  if layer == 'sat' then return M.MAP_TILES_DIR .. zxy .. '.jpg' end   -- (where it always was)
  return M.MAP_TILES_DIR .. layer .. '/' .. zxy .. M.MAP_LAYERS[layer]
end
local mapSent, mapSentAt = '', nil
function M.mapTiles(list)
  local ready, missing, keys = {}, {}, {}
  for _, t in ipairs(type(list) == 'table' and list or {}) do
    local z, x, y = tonumber(t[1]), tonumber(t[2]), tonumber(t[3])
    local layer = t[4] == nil and 'sat' or t[4]
    if z and x and y and type(layer) == 'string' and M.MAP_LAYERS[layer] then
      local key = string.format('%s/%d/%d/%d', layer, z, x, y)
      if FS:fileExists(M.mapTilePath(layer, z, x, y)) then
        ready[#ready + 1] = key
      else
        missing[#missing + 1] = { z, x, y, layer }
        keys[#keys + 1] = key
      end
    end
  end
  local sig = table.concat(keys, ',')
  local now = os.clock()
  if sig ~= mapSent or (#missing > 0 and (not mapSentAt or now - mapSentAt > M.MAP_ASK_S)) then
    local core = ext('beamngEarth_core')
    if core and core.requestMapTiles and core.requestMapTiles(missing) then
      mapSent, mapSentAt = sig, now
    end
  end
  return ready
end

-- What the map polls every second: the player, the places, the tile endpoint, the credits.
-- The other players of a BeamMP session for the map's arrows and its Players list ({ { pid, name, lat, lon, h } },
-- mpsession.others()), nil outside one.
local function otherPlayers()
  local okB, inMP = pcall(function() return require('beamngEarth/beammp').inSession() end)
  if not okB or not inMP then return nil end
  local ok, list = pcall(function() return require('beamngEarth/mpsession').others() end)
  return ok and list or nil
end

-- The map's weather + local-time badge (the player's spot): sky.lua's real local clock for the lat/lon, and the
-- latest 'weather' message's code/temperature (weather.current(), a plain table; weather.status() is a dumps()
-- string for the console, too slow to parse every second). nil when neither is up yet.
local function localWeatherBadge()
  local okS, sky = pcall(require, 'beamngEarth/sky')
  local sst = okS and sky.status and sky.status()
  local okW, weather = pcall(require, 'beamngEarth/weather')
  local m = okW and weather.current and weather.current()
  if not (sst and sst.localTime) and not m then return nil end
  return {
    localTime = sst and sst.localTime or nil,
    code = m and m.code or nil,
    tempC = m and (m.tempC or m.slTempC) or nil,
    metric = require('beamngEarth/gps').metric(),      -- (the game's unit: the badge's °C or °F)
  }
end

local mapPolledAt = nil    -- when the map last asked for its state (MAP_LOST_S)
function M.uiMapState()
  mapPolledAt = os.clock()
  local core = ext('beamngEarth_core')
  local st = core and core._state and core._state() or nil
  local pl = st and st.player
  local ok, ps = pcall(places.uiState)
  if not ok then ps = { places = {}, recent = {} } end
  return {
    connected = net.isConnected(), connection = net.state(),
    mapPort = net.isConnected() and st and st.mapPort or nil,
    player = (pl and pl.lat) and { lat = pl.lat, lon = pl.lon, heading = playerHeading(), speed = pl.speed } or nil,
    others = otherPlayers(),
    places = ps.places, recent = ps.recent, cannotGo = ps.cannotGo,
    attribution = core and core.attribution and core.attribution() or '',
    creditsByArea = core and core.creditsByArea and core.creditsByArea() or nil,
    gps = require('beamngEarth/gps').status(),
    metric = require('beamngEarth/gps').metric(),     -- (the game's length unit with no GPS route: the Players list)
    weather = localWeatherBadge(),
  }
end

-- Teleport from the map (the page asked "Teleport here?" first); recorded in the recent places.
function M.uiMapTeleport(lat, lon, name)
  local ok, why = places.goTo(lat, lon, name)
  return { ok = ok and true or false, error = (not ok) and why or nil }
end

-- The map's right-click "Set GPS destination" / "Clear GPS" (gps.lua: the game's own route arrows).
function M.uiMapGps(lat, lon, name)
  local gps = require('beamngEarth/gps')
  if lat == nil then gps.clear() return { ok = true } end
  return { ok = gps.setDestination(lat, lon, name) }
end

-- The map's GPS "Autodrive": the player's car drives itself along the route (gps.lua) / stops doing so.
function M.uiGpsAutodrive(on)
  return { ok = true, autodrive = require('beamngEarth/gps').setAutodrive(on) }
end

-- The GPS's planned route for the map's line: { version, points = {{lat, lon}...}, dist, dur } or nil.
function M.uiGpsRoute()
  return require('beamngEarth/gps').route()
end

-- The map's search-as-you-type: send (q -> { id } or { error }) and poll (id -> nil while waiting, else
-- { results = {{name, detail, kind, lat, lon}...}, error }). Biased toward the player.
local geocodeId = 0
function M.uiGeocode(q)
  local core = ext('beamngEarth_core')
  if not core or not core.requestGeocode then return { error = 'BeamNG.Earth is not loaded' } end
  local st = core._state and core._state() or nil
  local pl = st and st.player
  geocodeId = geocodeId + 1
  if not core.requestGeocode(geocodeId, tostring(q or ''), pl and pl.lat, pl and pl.lon) then
    return { error = 'Companion server not connected' }
  end
  return { id = geocodeId }
end
function M.uiGeocodeResult(id)
  local core = ext('beamngEarth_core')
  return core and core.takeGeocode and core.takeGeocode(tonumber(id)) or nil
end

-- The map's cache view (server covermap.py): ask (z, x0, y0, x1, y1, mode -> { id } or { error }) and poll (id -> nil
-- while waiting, else { z, x0, y0, w, h, cells (base64 levels), gen, ready, error }).
local cacheMapId = 0
function M.uiCacheMap(z, x0, y0, x1, y1, mode)
  local core = ext('beamngEarth_core')
  if not core or not core.requestCacheMap then return { error = 'BeamNG.Earth is not loaded' } end
  cacheMapId = cacheMapId + 1
  if not core.requestCacheMap(cacheMapId, tonumber(z), tonumber(x0), tonumber(y0), tonumber(x1), tonumber(y1),
      tostring(mode or 'summary')) then
    return { error = 'Companion server not connected' }
  end
  return { id = cacheMapId }
end
function M.uiCacheMapResult(id)
  local core = ext('beamngEarth_core')
  return core and core.takeCacheMap and core.takeCacheMap(tonumber(id)) or nil
end

-- Saved areas (server areas.py): a picked box's estimate (send -> { id }, poll -> nil or the estimate), a new area and
-- pause / resume / delete (send -> { id }, poll uiAreaResult -> nil or { ok, error }), the list (uiAreas).
local areaId = 0
local function areaSend(fn, ...)
  local core = ext('beamngEarth_core')
  if not core or not core[fn] then return { error = 'BeamNG.Earth is not loaded' } end
  areaId = areaId + 1
  if not core[fn](areaId, ...) then return { error = 'Companion server not connected' } end
  return { id = areaId }
end
-- mode: 'data' (the downloads) or 'world' (them, then the roads, buildings etc. built ahead: server areas.py)
local function areaMode(mode) return mode == 'world' and 'world' or 'data' end
function M.uiAreaPlan(s, w, n, e, mode)
  return areaSend('requestAreaPlan', tonumber(s), tonumber(w), tonumber(n), tonumber(e), areaMode(mode))
end
function M.uiAreaPlanResult(id)
  local core = ext('beamngEarth_core')
  return core and core.takeAreaPlan and core.takeAreaPlan(tonumber(id)) or nil
end
function M.uiAreaAdd(s, w, n, e, name, mode)
  return areaSend('areaAdd', tonumber(s), tonumber(w), tonumber(n), tonumber(e), tostring(name or ''), areaMode(mode))
end
-- A saved route: the GPS's planned route with half m each side (the estimate, then the download, as an area's).
function M.uiRoutePlan(half, mode)
  return areaSend('requestRoutePlan', tonumber(half), areaMode(mode))
end
function M.uiRouteAdd(half, name, mode)
  return areaSend('routeAdd', tonumber(half), tostring(name or ''), areaMode(mode))
end
function M.uiAreaCmd(area, op)
  return areaSend('areaCmd', tostring(area or ''), tostring(op or ''))
end
function M.uiAreaResult(id)
  local core = ext('beamngEarth_core')
  return core and core.takeAreaResult and core.takeAreaResult(tonumber(id)) or nil
end
function M.uiAreas()
  local core = ext('beamngEarth_core')
  local a = core and core.areasState and core.areasState()
  return { areas = a and a.areas or {}, pinnedBytes = a and a.pinnedBytes or nil }
end

-- "lat, lon" typed in the map's search box -> { lat, lon } or { error }.
function M.uiMapParse(text)
  local lat, lon = places.parseLatLon(text)
  if not lat then return { error = lon } end
  return { lat = lat, lon = lon }
end

-- A controller on the map (the user, 2026-10-20: "make sure our map is controller compatible; see how the default map
-- handles controllers"): as the stock big map does (freeroam/bigMapMode.lua), its own action map "BigMap" is on while
-- the map is open, with the player's bindings for it (stock pads: the right stick pans, the triggers zoom, A selects;
-- X and Y come to EarthMap.vue as the menus' own 'UINavigation' events). Its
-- actions call freeroam_bigMapMode's functions by name; while our map is open those go to EarthMap.vue
-- (guihooks 'BeamNGEarthMapInput'). The stock map pauses the game; ours does not (the GPS autodrive drives on), so the
-- player's own driving controls are held off instead (the triggers would rev and brake the car), the car's inputs let
-- go first (a pedal held when the map opened would stay down), with the stock map's blocked groups.
M.MAP_INPUT_EVENT = 'BeamNGEarthMapInput'
M.MAP_BLOCKED = { 'gameCam', 'vehicleTeleporting', 'funStuff', 'freeCam', 'physicsControls',
  'vehicleSwitching', 'walkingMode', 'vehicleMenues', 'aiControls', 'photoMode', 'couplers', 'missionPopup',
  'resetPhysics', 'miniMap' }
-- ... and every driving action by name: the action filter's groups have none ('vehicle' named no group, so the sticks
-- and triggers still drove the car behind the map, the user 2026-10-21): the actions of these categories
-- (core/input/actions/vehicle.json, the vehicles' own input_actions), else this list
M.MAP_BLOCKED_CATS = { vehicle = true, vehicle_specific = true }
M.MAP_BLOCKED_DRIVING = { 'accelerate', 'brake', 'accelerate_brake', 'steering', 'steer_left', 'steer_right', 'clutch',
  'parkingbrake', 'parkingbrake_temporary', 'parkingbrake_toggle', 'shiftUp', 'shiftDown', 'gearR', 'gearN', 'gear1',
  'gear2', 'gear3', 'gear4', 'gear5', 'gear6', 'gear7', 'gear8', 'horn', 'activateStarterMotor', 'toggleShifterMode' }

-- The actions the map holds off: the stock map's groups and the driving actions (M.MAP_BLOCKED_CATS).
function M.mapBlockedActions(filter, actions)
  local out = filter.createActionTemplate(M.MAP_BLOCKED)
  local seen = {}
  for _, a in ipairs(out) do seen[a] = true end
  local all = actions and actions.getActiveActions and actions.getActiveActions()
  local n = 0
  for name, a in pairs(type(all) == 'table' and all or {}) do
    if type(a) == 'table' and M.MAP_BLOCKED_CATS[a.cat] and not seen[name] then
      out[#out + 1], seen[name], n = name, true, n + 1
    end
  end
  if n == 0 then
    for _, name in ipairs(M.MAP_BLOCKED_DRIVING) do
      if not seen[name] then out[#out + 1], seen[name] = name, true end
    end
  end
  return out
end
local mapCtl = nil      -- the stock map's functions while ours has its controls
-- The map asks for its state every second while it is open (uiMapState). With our controls on and no ask for MAP_LOST_S
-- the map is gone without its close reaching us (a level load, a UI reload): the car's controls are given back. Left
-- on, every driving action stayed blocked: the car could not drive, steer or be recovered while the menus and the free
-- camera still worked (GitHub #252).
M.MAP_LOST_S = 5

local function mapInput(kind, a, b)
  local gh = rawget(_G, 'guihooks')
  if gh then gh.trigger(M.MAP_INPUT_EVENT, { kind = kind, a = a, b = b }) end
end

function M.mapControls(on)
  local bm = rawget(_G, 'freeroam_bigMapMode')
  local filter = rawget(_G, 'core_input_actionFilter')
  if on and not mapCtl and bm then
    mapCtl = { bm = bm }
    mapPolledAt = os.clock()
    for _, f in ipairs({ 'camMoveController', 'camMoveKey', 'zoomInOut', 'controllerZoom', 'zoom', 'onControllerSelect',
        'onChangeUiFilter' }) do mapCtl[f] = bm[f] end
    bm.camMoveController = function(upDown, v) mapInput(upDown and 'move_ud' or 'move_lr', v) end
    bm.camMoveKey = function(v, dir) mapInput('move_key', v, dir) end
    bm.zoomInOut = function(v, zoomIn) mapInput('zoom_trigger', v, zoomIn) end
    bm.controllerZoom = function(v) mapInput('zoom_axis', v) end
    bm.zoom = function(v) mapInput('zoom_axis', v) end
    bm.onControllerSelect = function() mapInput('select') end
    bm.onChangeUiFilter = function(v, dir) mapInput('filter', v, dir) end
    local veh = rawget(_G, 'getPlayerVehicle') and getPlayerVehicle(0)
    if veh then
      -- the pedals let go (blocked below, they would hold their last value), the steering left as it is: written
      -- here with the direct filter and no wheel angle, a force-feedback wheel locked fully right after the map until
      -- the car was respawned, G920 / G29 / Fanatec / Moza (GitHub #284); the car stands still without it
      pcall(veh.queueLuaCommand, veh, "input.event('throttle', 0, 2) input.event('brake', 0, 2) "
        .. "input.event('clutch', 0, 2)")
    end
    if filter and filter.createActionTemplate then
      pcall(function()
        filter.setGroup('beamngEarthMapBlocked', M.mapBlockedActions(filter, rawget(_G, 'core_input_actions')))
        filter.addAction(0, 'beamngEarthMapBlocked', true)
      end)
    end
    -- X and Y: the stock pads bind them to the vehicle triggers too (triggerAction1 / 2, gameplay.json), whose action map
    -- takes the press before the menus' (cui_action_2 / cui_context never came on a Steam Deck, 2026-10-21): while
    -- the map is open those two go to the map as X and Y, any other trigger as before
    local vt = rawget(_G, 'core_vehicleTriggers') or (rawget(_G, 'extensions') and extensions.core_vehicleTriggers)
    if vt and vt.onActionEvent then
      mapCtl.vt, mapCtl.vtOrig = vt, vt.onActionEvent
      vt.onActionEvent = function(id, value, ...)
        if id == 1 then mapInput('pad_x', value) return end
        if id == 2 then mapInput('pad_y', value) return end
        return mapCtl and mapCtl.vtOrig and mapCtl.vtOrig(id, value, ...)
      end
    end
    pcall(pushActionMap, 'BigMap')
    local am = scenetree.findObject('BigMapActionMap')
    if am then am.trapHandledEvents = false end      -- (as the stock map: nothing else gets stuck behind it)
  elseif not on and mapCtl then
    if mapCtl.vt then mapCtl.vt.onActionEvent = mapCtl.vtOrig end
    for f, fn in pairs(mapCtl) do if f ~= 'bm' and f ~= 'vt' and f ~= 'vtOrig' then mapCtl.bm[f] = fn end end
    mapCtl = nil
    pcall(popActionMap, 'BigMap')
    if filter and filter.addAction then pcall(filter.addAction, 0, 'beamngEarthMapBlocked', false) end
  end
end

function M.mapOpened(on)
  mapOpen = on and true or false
  M.mapControls(mapOpen)
end
function M.isMapOpen() return mapOpen end

-- Go to a UI screen (route): extensions.ui_router.navigate, BeamNG 0.40's way (its patch notes: "Use
-- extensions.ui_router.navigate(routeName, params) instead of guihooks.trigger('ChangeState', ...)"; it exists in 0.39.4
-- too); the old ChangeState only without it.
function M.navigate(state)
  local r = rawget(_G, 'ui_router')
  if r and r.navigate then
    local ok = pcall(r.navigate, state)
    if ok then return true end
  end
  local gh = rawget(_G, 'guihooks')
  if gh then gh.trigger('ChangeState', { state = state }) return true end
  return false
end

-- The key binding (actions/beamngEarth.json; M and the pause menu Map via redirectBigMap): open the map, or close it back to driving.
function M.toggleMap()
  if mapOpen then
    M.navigate('play')
    mapOpen = false
  else
    M.navigate(M.MAP_ROUTE)
  end
end

------------------------------------------------------------------------------------------
-- extension hooks

-- The game's own map (the user, 2026-10-14: "replace the default map, via the keybind or the pause menu"): the M key
-- (toggleBigMap) and the pause menu's Map entry (enterBigMap) both go through freeroam_bigMapMode; in our level they
-- open this map instead (the stock big map is a 3D overhead view of a stock level). Career keeps its own; leaving
-- the level puts the stock functions back.
M.replaceBigMap = true
local bigMapOrig = nil
function M.redirectBigMap(inOurLevel)
  local bm = rawget(_G, 'freeroam_bigMapMode')
  local career = rawget(_G, 'career_career')
  local want = M.replaceBigMap and inOurLevel and bm ~= nil and not (career and career.isActive and career.isActive())
  if want and not bigMapOrig then
    bigMapOrig = { bm = bm, enter = bm.enterBigMap, toggle = bm.toggleBigMap }
    bm.toggleBigMap = function() M.toggleMap() end
    bm.enterBigMap = function() if not mapOpen then M.toggleMap() end end
  elseif not want and bigMapOrig then
    bigMapOrig.bm.enterBigMap, bigMapOrig.bm.toggleBigMap = bigMapOrig.enter, bigMapOrig.toggle
    bigMapOrig = nil
  end
end

function M.onExtensionLoaded()
  M.load()
  M.applyAll()
end

-- The one-time welcome (the user, 2026-10-15: "add a 1 time popup with info about this project"): shown a moment after
-- our level starts, once per WELCOME_VERSION (raise it when the page has news worth showing again).
M.WELCOME_VERSION = 1
M.WELCOME_ROUTE = 'beamngEarthWelcome' -- index.js registers the route under this name
M.WELCOME_DELAY_S = 2
local welcomeAt = nil

function M.showWelcome()
  M.navigate(M.WELCOME_ROUTE)
end

-- EarthWelcome.vue's buttons: seen.
function M.uiWelcomeDone()
  if (values.welcomeSeen or 0) < M.WELCOME_VERSION then M.set('welcomeSeen', M.WELCOME_VERSION) end
  return { ok = true }
end

-- Automation (tools/benchmark.py, autodrive_perf.py: mcp.quiet_ui) keeps the page away for this game session only; the
-- player still gets it on their next start.
M.welcomeSuppressed = false

-- The settings page's Feedback buttons ('report': the report window, Ctrl+Shift+E; 'profiler': Ctrl+Shift+Y): the pause
-- menu closes first, and the report runs FEEDBACK_FRAMES frames later, so its screenshot is the world, not the menu.
M.FEEDBACK_FRAMES = 6
local feedbackQ = nil      -- { what, frames } waiting for the menu to go

function M.feedback(what)
  if what ~= 'report' and what ~= 'profiler' then return { error = 'unknown' } end
  M.navigate('play')
  feedbackQ = { what = what, frames = M.FEEDBACK_FRAMES }
  return { ok = true }
end

-- X and Y on the map are the menus' cui_action_2 / cui_context; the UI turns each menu action's map on only for an
-- element that says it takes it, and off again on a route or scope change (with the map open both were off on a Steam
-- Deck, 2026-10-21; turned on, X worked once, then the reopened map had them off). Kept on while the map is open.
M.MAP_MENU_ACTIONS = { cui_action_2 = true, cui_context = true }
M.MAP_MENU_CHECK_S = 0.25
local menuCheckAt = 0
local function keepMapMenuActions()
  local b = rawget(_G, 'core_input_bindings')
  if not (b and b.getMenuActionStates and b.setMenuActionsEnabled) or os.clock() < menuCheckAt then return end
  menuCheckAt = os.clock() + M.MAP_MENU_CHECK_S
  local states = b.getMenuActionStates() or {}
  for name in pairs(M.MAP_MENU_ACTIONS) do
    if states[name] == false then pcall(b.setMenuActionsEnabled, M.MAP_MENU_ACTIONS) return end
  end
end

-- Our map's hold on the controls let go (the map is not open any more, or its close never came: MAP_LOST_S).
local function releaseMap(why)
  if not mapCtl then return end
  log('W', 'beamngEarth', 'map controls given back: ' .. why)
  mapOpen = false
  M.mapControls(false)
end

function M.onUpdate()
  if mapOpen then keepMapMenuActions() end
  if mapCtl and mapPolledAt and os.clock() - mapPolledAt > M.MAP_LOST_S then
    releaseMap(string.format('the map has not asked for %.0f s', os.clock() - mapPolledAt))
  end
  if feedbackQ then
    feedbackQ.frames = feedbackQ.frames - 1
    if feedbackQ.frames <= 0 then
      local what, rep = feedbackQ.what, ext('beamngEarth_report')
      feedbackQ = nil
      if rep then if what == 'report' then rep.note() else rep.profiler() end end
    end
  end
  if welcomeAt and os.clock() >= welcomeAt then
    welcomeAt = nil
    if not M.welcomeSuppressed then M.showWelcome() end
  end
end

-- other extensions (sky, dev) may load after this one: apply again once the level runs
function M.onClientPostStartMission()
  releaseMap('a level started')
  appliedView = nil
  M.applyAll()
  local level = rawget(_G, 'getCurrentLevelIdentifier') and getCurrentLevelIdentifier()
  if level == M.LEVEL_ID and (values.welcomeSeen or 0) < M.WELCOME_VERSION then
    welcomeAt = os.clock() + M.WELCOME_DELAY_S
  end
end

return M
