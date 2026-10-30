-- luajit mod/tests/test_settings.lua  (the pause-menu settings, settings.lua; docs/DEVTOOLS.md)
-- Defaults (= today's behaviour), load / save round trip through jsonReadFile / jsonWriteFile,
-- validation (clamping, types, enums, unknown keys, broken files), applying values through the
-- module that owns them, the "page exists" rule that turns the imgui window into a hidden HUD, and
-- the page's bridge calls.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local rebase = require('beamngEarth/rebase')
local ui = require('beamngEarth/ui')
local settings = require('beamngEarth/settings')

-- in-memory settings files
local files, writes = {}, 0
function jsonReadFile(path)
  local f = files[path]
  if f == 'THROW' then error('broken json') end
  return f
end
function jsonWriteFile(path, data, pretty)
  writes = writes + 1
  local copy = {}
  for k, v in pairs(data) do copy[k] = v end
  files[path] = copy
  return true
end
FS = { fileExists = function(_, p) return files[p] ~= nil end }

local dist0, vis0, compact0 = rebase.distance, ui.visible, ui.compact

------------------------------------------------------------------------------------------
-- defaults

local vals, warn = settings.load()
local d = settings.defaults()
T.check(#warn == 0, 'no file: no warnings')
local same = true
for k, v in pairs(d) do
  if vals[k] ~= v and k ~= 'quality' and k ~= 'qualityDetected' then same = false end
end
T.check(same, 'no file: every value is its default (but the quality, picked from the hardware)')
T.check(vals.quality == 'high', 'no file, no GPU info: the High preset (= the old defaults)')

-- the quality presets
T.check(settings.detectQuality({ name = 'AMD Custom GPU 0932 (RADV VANGOGH)', memoryMB = 6144 }) == 'medium',
  'a Steam Deck gets Medium')
T.check(settings.detectQuality({ name = 'Intel UHD 620', memoryMB = 1024 }) == 'low', 'a small GPU gets Low')
T.check(settings.detectQuality({ name = 'AMD Radeon RX 7900 XTX', memoryMB = 24560 }) == 'high', 'a big GPU gets High')
for _, p in ipairs(settings.PRESET_ORDER) do
  for _, k in ipairs(settings.PRESET_KEYS) do
    T.check(settings.validate(k, settings.PRESETS[p][k]) == settings.PRESETS[p][k], p .. ' preset: valid ' .. k)
  end
end
for _, k in ipairs(settings.PRESET_KEYS) do
  T.check(settings.PRESETS.high[k] == d[k], 'High is the old default of ' .. k)
end
T.check(d.rebaseDistance == rebase.DEFAULT_DISTANCE and d.skyMode == 'real' and d.clouds == true
  and d.viewDistanceMode == 'horizon' and d.viewDistanceKm == 100 and d.hudVisible == false,
  'defaults (15 km rebase, real sky, clouds on, the real horizon (2026-10-11), 100 km fixed, HUD hidden with the page)')
for _, k in ipairs(settings.ORDER) do T.check(settings.SCHEMA[k] ~= nil, 'ORDER key in SCHEMA: ' .. k) end
local n = 0
for k in pairs(settings.SCHEMA) do if not settings.HIDDEN[k] then n = n + 1 end end
T.check(n == #settings.ORDER, 'every SCHEMA key is in ORDER (or HIDDEN)')
-- the one-time welcome: due in our level until seen, then not again
T.check(settings.get('welcomeSeen') == 0, 'welcome not seen yet')
T.check(settings.uiWelcomeDone().ok and settings.get('welcomeSeen') == settings.WELCOME_VERSION, 'seen: stored')

------------------------------------------------------------------------------------------
-- validation

T.check(settings.validate('rebaseDistance', '12000') == 12000, 'number from a string')
T.check(settings.validate('rebaseDistance', 99999) == rebase.MAX_DISTANCE, 'number clamped to max')
T.check(settings.validate('rebaseDistance', -5) == rebase.MIN_DISTANCE, 'number clamped to min')
T.check(settings.validate('rebaseDistance', 'abc') == nil, 'not a number refused')
T.check(settings.validate('rebaseDistance', 0 / 0) == nil, 'NaN refused')
T.check(settings.validate('rebaseDistance', math.huge) == nil, 'inf refused')
T.check(settings.validate('clouds', 'false') == false and settings.validate('clouds', 1) == true, 'booleans from strings / 0-1')
T.check(settings.validate('clouds', 'maybe') == nil, 'non-boolean refused')
T.check(settings.validate('skyMode', 'manual') == 'manual' and settings.validate('skyMode', 'night') == nil, 'enum values')
local v, e = settings.validate('nope', 1)
T.check(v == nil and e:match('unknown'), 'unknown key refused')

------------------------------------------------------------------------------------------
-- load: valid values taken, the rest default with a warning

files[settings.FILE] = { version = 1, rebaseDistance = 30000, skyMode = 'night', clouds = false, extra = 3,
  viewDistanceKm = '250' }
vals, warn = settings.load()
T.check(vals.rebaseDistance == rebase.MAX_DISTANCE and vals.clouds == false and vals.viewDistanceKm == 250,
  'load: valid values taken (clamped, from strings)')
T.check(vals.skyMode == 'real', 'load: an invalid enum keeps the default')
T.check(#warn == 2, 'load: warnings for the invalid enum and the unknown key (' .. #warn .. ')')
files[settings.FILE] = 'THROW'
vals, warn = settings.load()
T.check(vals.clouds == true and #warn == 1 and warn[1]:match('unreadable'), 'load: a broken file -> defaults + warning')
files[settings.FILE] = 42
vals, warn = settings.load()
T.check(vals.clouds == true and #warn == 1, 'load: a non-object file -> defaults + warning')
files[settings.FILE] = nil

------------------------------------------------------------------------------------------
-- set: validated, saved, applied through the owner

local coreCalls, skyCalls = {}, {}
beamngEarth_core = { setRebaseDistance = function(m) coreCalls[#coreCalls + 1] = m; return rebase.setDistance(m) end }
local skyMode, clouds = 'real', true
beamngEarth_sky = { getMode = function() return skyMode end,
  setMode = function(m) skyCalls[#skyCalls + 1] = 'mode ' .. m; skyMode = m end,
  setClouds = function(on) skyCalls[#skyCalls + 1] = 'clouds ' .. tostring(on); clouds = on; beamngEarth_sky.CLOUDS_ENABLED = on end,
  CLOUDS_ENABLED = true }
settings.load()
writes = 0
local r = settings.set('rebaseDistance', 9000)
T.check(r.ok and r.value == 9000 and rebase.distance == 9000 and coreCalls[#coreCalls] == 9000,
  'set rebaseDistance: applied through beamngEarth_core.setRebaseDistance')
T.check(writes == 1 and files[settings.FILE].rebaseDistance == 9000 and files[settings.FILE].version == settings.VERSION,
  'set: saved with the version')
r = settings.set('rebaseDistance', 'x')
T.check(not r.ok and r.value == 9000 and writes == 1, 'set invalid: refused, nothing saved')
settings.set('skyMode', 'manual')
settings.set('clouds', false)
T.check(skyMode == 'manual' and clouds == false and #skyCalls == 2, 'sky mode and clouds applied through beamngEarth_sky')
settings.set('skyMode', 'manual')
T.check(#skyCalls == 2, 'an unchanged sky mode is not re-applied')
-- round trip
local saved = files[settings.FILE]
vals = settings.load()
T.check(vals.rebaseDistance == 9000 and vals.skyMode == 'manual' and vals.clouds == false, 'save -> load round trip')
T.check(saved.hudVisible == false and saved.devOutlineKm == 3, 'every value is written')
-- real weather and gravity: through beamngEarth_weather (defaults = its own: real, gravity on)
T.check(settings.get('weatherMode') == 'real' and settings.get('realGravity') == true, 'weather defaults: real, real gravity on')
local wCalls, wMode = {}, 'real'
beamngEarth_weather = { GRAVITY_ENABLED = true,
  setMode = function(m) wCalls[#wCalls + 1] = 'mode ' .. m; wMode = m end, getMode = function() return wMode end,
  setGravityEnabled = function(on) wCalls[#wCalls + 1] = 'gravity ' .. tostring(on); beamngEarth_weather.GRAVITY_ENABLED = on end,
  statusLine = function() return 'Weather (real): 20 C' end, gravityLine = function() return '9.78676 m/s2, EGM2008 (server)' end }
settings.set('weatherMode', 'manual')
settings.set('realGravity', false)
T.check(wCalls[1] == 'mode manual' and wCalls[2] == 'gravity false', 'weather mode and gravity applied through beamngEarth_weather')
settings.set('realGravity', false)
T.check(#wCalls == 2, 'an unchanged gravity switch is not re-applied')
T.check(not settings.set('weatherMode', 'stormy').ok, 'weather mode: only real / manual')
-- 'auto' (the old default) is real now: set and loaded as 'real', without a warning
r = settings.set('weatherMode', 'auto')
T.check(r.ok and r.value == 'real' and wMode == 'real' and files[settings.FILE].weatherMode == 'real', "set('auto') = real")
files[settings.FILE].weatherMode = 'auto'
vals, warn = settings.load()
T.check(vals.weatherMode == 'real' and #warn == 0, "a saved 'auto' loads as 'real' without a warning (" .. #warn .. ')')
T.check(#settings.SCHEMA.weatherMode.values == 2 and settings.validate('weatherMode', 'auto') == 'real', 'weather values: real, manual')
-- the switch to manual when the weather (time) is changed in the game: weather.lua / sky.lua set
-- their own mode, then save it here; applying it again is not needed (and not done)
local nCalls, nSky = #wCalls, #skyCalls
wMode, skyMode = 'manual', 'manual'
local rw, rs = settings.set('weatherMode', 'manual'), settings.set('skyMode', 'manual')
T.check(rw.ok and rs.ok and files[settings.FILE].weatherMode == 'manual' and files[settings.FILE].skyMode == 'manual',
  'switch to manual: saved')
T.check(#wCalls == nCalls and #skyCalls == nSky, 'switch to manual: the mode is not applied again')
T.check(settings.uiState().settings.weatherMode == 'manual' and settings.uiState().settings.skyMode == 'manual',
  'switch to manual: the page reads it (uiState, polled every 2 s)')
T.check(settings.load().weatherMode == 'manual', 'switch to manual: kept for the next start')
settings.set('weatherMode', 'real')
settings.set('skyMode', 'real')
T.check(wCalls[#wCalls] == 'mode real' and skyCalls[#skyCalls] == 'mode real', 'Real again from the page: applied')
-- Real chosen while already real (the dropdown, a reset to defaults): applied as before, which clears
-- a gravity change made in the game (weather.lua keeps it until setMode('real'))
nCalls = #wCalls
settings.set('weatherMode', 'real')
T.check(#wCalls == nCalls + 1 and wCalls[#wCalls] == 'mode real', 'Real while real: applied again (the gravity override)')
settings.set('realGravity', true)
-- without the core extension: straight to rebase.lua
beamngEarth_core = nil
settings.set('rebaseDistance', 7000)
T.check(rebase.distance == 7000, 'rebaseDistance without the core extension: rebase.setDistance')

------------------------------------------------------------------------------------------
-- view distance: only in our level, the default never touches the engine

local post = 0
scenetree.theLevelInfo = { visibleDistance = 100000, postApply = function() post = post + 1 end }
local level = 'west_coast_usa'
function getCurrentLevelIdentifier() return level end
settings.set('viewDistanceKm', 300)
T.check(scenetree.theLevelInfo.visibleDistance == 100000 and post == 0, 'view distance: the horizon default never touches it')
settings.set('viewDistanceMode', 'fixed')
T.check(scenetree.theLevelInfo.visibleDistance == 100000 and post == 0, 'view distance: other levels untouched')
level = 'beamng_earth'
settings.onClientPostStartMission()
T.check(scenetree.theLevelInfo.visibleDistance == 300000 and post == 1, 'view distance: applied in our level, with postApply')
settings.set('viewDistanceMode', 'horizon')
settings.set('viewDistanceKm', 200)

T.check(scenetree.theLevelInfo.visibleDistance == 100000 and post == 2, 'horizon after fixed: the level\'s 100 km back once')
settings.set('viewDistanceMode', 'fixed')
T.check(scenetree.theLevelInfo.visibleDistance == 200000 and post == 3, 'fixed again: 200 km')
settings.uiReset()
T.check(scenetree.theLevelInfo.visibleDistance == 100000 and post == 4, 'reset (horizon): back to 100 km (applied, it had changed)')
settings.onClientPostStartMission()
T.check(post == 4, 'the default at level start: the engine is not touched')

------------------------------------------------------------------------------------------
-- the page: compact HUD, hidden unless hudVisible

ui.visible, ui.compact = true, false
T.check(not settings.pageExists() and not settings.hudHiddenAtStart() and ui.compact == false,
  'no page: the window stays as before')
ui_pause_actions = { getVisibleModTabs = function()
  return { { tabId = 'mod:mods', buttons = { { id = 'taa-standalone-settings' }, { id = settings.BUTTON_ID } } } }
end }
T.check(settings.pageExists(), 'page: found in the pause menu registry')
T.check(settings.hudHiddenAtStart() == true and ui.compact and ui.visible == false, 'page: compact HUD, hidden at start')
settings.set('hudVisible', true)
T.check(ui.visible == true, 'hudVisible on: shown at once')
T.check(settings.hudHiddenAtStart() == false and ui.visible == true, 'hudVisible on: shown at level start')
settings.set('hudVisible', false)
T.check(ui.visible == false, 'hudVisible off: hidden at once')
ui_pause_actions = nil
T.check(settings.setPageExists(true) and settings.pageExists(), 'index.js can say the page exists')
T.check(settings.uiShowHud(true) == true and ui.visible, 'uiShowHud')

------------------------------------------------------------------------------------------
-- bridge calls

local devCalls = {}
beamngEarth_dev = { set = function(nm, on) devCalls[#devCalls + 1] = nm .. '=' .. tostring(on) end,
  preset = function(nm) devCalls[#devCalls + 1] = 'preset ' .. nm end,
  setParam = function(nm, v) devCalls[#devCalls + 1] = nm .. ':' .. v end,
  state = function() return { active = true } end }
local stTbl = { sessionId = 'abc', anchorId = 3, anchor = { lat = 1, lon = 2 }, status = 'ok', inOurLevel = true,
  player = { lat = 1.1, lon = 2.2, dist = 500 } }
beamngEarth_core = { _state = function() return stTbl end, attribution = function() return 'credits' end }
local s = settings.uiState()
T.check(s.settings.rebaseDistance == settings.get('rebaseDistance') and #s.schema == #settings.ORDER, 'uiState: settings + schema')
T.check(s.status.connection == 'disconnected' and s.status.session == 'abc' and s.status.anchorId == 3
  and s.status.attribution == 'credits' and s.status.dist == 500, 'uiState: status from the core')
T.check(s.dev and s.dev.active, 'uiState: dev state')
T.check(s.status.weather == 'Weather (real): 20 C' and s.status.gravity:match('EGM2008') and s.status.deshadow == 'off',
  'uiState: server features (weather, gravity, de-shadowing off)')
stTbl.deshadow = 'full'
T.check(settings.uiState().status.deshadow == 'full', 'uiState: de-shadowing mode from the hello')
T.check(s.status.colormatch == 'off', 'uiState: colour seams off without hello.colormatch')
stTbl.colormatch = 's2'
T.check(settings.uiState().status.colormatch == 's2', 'uiState: colour-seam mode from the hello')
T.check(settings.uiDevSet('tint', true).active and devCalls[1] == 'tint=true', 'uiDevSet forwards')
settings.uiDevPreset('lod')
T.check(devCalls[2] == 'preset lod', 'uiDevPreset forwards')
settings.uiSet('devOutlineKm', 5)
T.check(devCalls[3] == 'outlineKm:5', 'dev radius applied to the dev module')
local res = settings.uiReset()
T.check(res.rebaseDistance == rebase.DEFAULT_DISTANCE and files[settings.FILE].rebaseDistance == rebase.DEFAULT_DISTANCE,
  'uiReset: defaults saved')

------------------------------------------------------------------------------------------
-- the download cache (server cache.py): the cap setting, the usage line, the clear button

T.check(settings.defaults().cacheCapGb == 20 and settings.cacheCapGb() == 20, 'cache cap: 20 GB by default')
T.check(settings.validate('cacheCapGb', '7') == 7 and settings.validate('cacheCapGb', 12.5) == 12.5
  and settings.validate('cacheCapGb', 'unlimited') == 0 and settings.validate('cacheCapGb', -3) == 0
  and settings.validate('cacheCapGb', 'lots') == nil,
  'cache cap: any number of GB typed (the old choices and "unlimited" (0) still read)')
local capsSent, clears, queries, connected = {}, 0, 0, true
local cacheInfo = nil
beamngEarth_core = { _state = function() return stTbl end,
  cacheSetCap = function(gb) capsSent[#capsSent + 1] = gb; return connected end,
  cacheClear = function() clears = clears + 1; return connected end,
  cacheQuery = function() queries = queries + 1; return connected end,
  cacheState = function() return cacheInfo end }
settings.uiSet('cacheCapGb', '50')
settings.uiSet('cacheCapGb', 'unlimited')
T.check(capsSent[1] == 50 and capsSent[2] == 0 and files[settings.FILE].cacheCapGb == 0,
  'cache cap: saved, sent to the server in GB (unlimited = 0)')
settings.applyAll()
T.check(capsSent[3] == 0, 'cache cap: applied with the rest (the level start)')
T.check(settings.cacheLine(nil) == 'no information from the server', 'cache line: no server (or one without a cache)')
T.check(settings.cacheLine({ usedBytes = 3e9, capBytes = 20e9, files = 10, ready = true, managed = true,
  pinnedBytes = 1.5e9 }) == '3.0 GB of 20 GB, 10 files, 1.5 GB in saved areas (Clear cache keeps them)',
  'cache line: the saved areas\' share')
T.check(settings.cacheLine({ usedBytes = 1.23e9, capBytes = 20e9, files = 34877, ready = true, managed = true })
  == '1.2 GB of 20 GB, 34877 files', 'cache line: used of cap')
T.check(settings.cacheLine({ usedBytes = 45e6, capBytes = 0, files = 9, ready = true, managed = true })
  == '45 MB, 9 files (no limit)', 'cache line: unlimited, small')
T.check(settings.cacheLine({ usedBytes = 3e9, capBytes = 5e9, files = 1, ready = false, managed = true })
  == 'counting files... (3.0 GB so far)', 'cache line: the server is still counting')
T.check(settings.cacheLine({ usedBytes = 3e9, capBytes = 5e9, files = 2, ready = true, managed = false })
  :match('newer version'), 'cache line: a cache from a newer version is left alone')
cacheInfo = { usedBytes = 1.23e9, capBytes = 20e9, files = 10, ready = true, managed = true, clears = 1, clearedBytes = 5e8 }
local q0 = queries
local st1 = settings.uiState().status.cache
T.check(st1.line == '1.2 GB of 20 GB, 10 files' and st1.clears == 1 and st1.clearedText == '0.5 GB',
  'uiState: the cache line and the last clear')
for _ = 1, settings.CACHE_POLL - 1 do settings.uiState() end
T.check(queries - q0 == 1, 'uiState: fresh numbers asked once per CACHE_POLL polls (' .. (queries - q0) .. ')')
local r = settings.uiCacheClear()
T.check(r.ok and clears == 1, 'uiCacheClear: sent')
connected = false
r = settings.uiCacheClear()
T.check(not r.ok and r.error == 'server not connected', 'uiCacheClear: not connected')
beamngEarth_core.cacheClear = function() return false, 'the server has no download cache' end
r = settings.uiCacheClear()
T.check(not r.ok and r.error == 'the server has no download cache', 'uiCacheClear: core\'s reason shown')
beamngEarth_core = nil
r = settings.uiCacheClear()
T.check(not r.ok and r.error == 'mod not loaded', 'uiCacheClear: no core')
settings.uiReset()
T.check(settings.get('cacheCapGb') == 20, 'uiReset: the cap back to 20 GB')

------------------------------------------------------------------------------------------
-- the compact HUD draws (permissive imgui stub)

local drawnText = {}
ui_imgui = setmetatable({
  BoolPtr = function(v) return { [0] = v } end, DoublePtr = function(v) return { [0] = v } end,
  FloatPtr = function(v) return { [0] = v } end, Begin = function() return true end,
  Button = function() return false end, TextUnformatted = function(t) drawnText[#drawnText + 1] = t end,
}, { __index = function() return function() end end })
beamngEarth_dev = { isActive = function() return false end, toggleLast = function() end }
ui.visible, ui.compact = true, true
local view = { connection = 'connected', anchorId = 2, dist = 1200, toRebase = 13800, farText = 'Far tiles: 300 live',
  status = 'ok', defaultLat = 1, defaultLon = 2, rebaseDistance = 15000, rebaseMin = 2000, rebaseMax = 16000, attribution = 'c' }
local okDraw, errDraw = pcall(ui.draw, view, { teleport = function() end, setRebaseDistance = function(m) return m end })
local joined = table.concat(drawnText, '\n')
T.check(okDraw and joined:match('rebase in 13.8 km') and joined:match('pause menu > BeamNG.Earth'),
  'compact HUD: status line and the settings hint (' .. tostring(errDraw) .. ')')
ui.compact, drawnText = false, {}
okDraw = pcall(ui.draw, view, { teleport = function() end, setRebaseDistance = function(m) return m end })
T.check(okDraw and table.concat(drawnText, '\n'):match('Anchor: #2'), 'full window still draws')

------------------------------------------------------------------------------------------
-- the page's own copy of the defaults (earthSettingsStore.js settingsSchema: its reset buttons and
-- "default" labels) agrees with SCHEMA for every persisted setting it lists

do
  local f = io.open(dir .. '/../beamng_earth/ui/ui-vue/mods/beamngEarth/earthSettingsStore.js', 'r')
  local page = f and f:read('*a') or ''
  if f then f:close() end
  local defs, seen = settings.defaults(), 0
  for id, val in page:gmatch('{ id: "(%w+)", type: "%w+",[^\n]-default: ([^,]+),') do
    if settings.SCHEMA[id] then
      seen = seen + 1
      local v = val:match('^"(.*)"$')
      if v == nil then
        if val == 'true' then v = true elseif val == 'false' then v = false else v = tonumber(val) end
      end
      T.check(v == defs[id], 'page default of ' .. id .. ' (' .. tostring(val) .. ') = settings.lua\'s (' .. tostring(defs[id]) .. ')')
    end
  end
  T.check(seen >= 10, 'page defaults compared (' .. seen .. ')')
end

------------------------------------------------------------------------------------------
-- the world map's tile files (settings.mapTiles -> core.requestMapTiles -> server map_tiles)
do
  local sent = {}
  local core0, fs0 = beamngEarth_core, FS
  beamngEarth_core = { requestMapTiles = function(list) sent[#sent + 1] = list; return true end }
  local have = {}
  FS = { fileExists = function(_, p) return have[p] == true end }
  T.check(settings.mapTilePath('sat', 3, 2, 1) == '/temp/beamngEarth/maptiles/3/2/1.jpg', 'sat tiles where they were')
  T.check(settings.mapTilePath('map', 3, 2, 1) == '/temp/beamngEarth/maptiles/map/3/2/1.jpg', 'map layer path')
  T.check(settings.mapTilePath('labels', 3, 2, 1) == '/temp/beamngEarth/maptiles/labels/3/2/1.png', 'labels layer path')
  have[settings.mapTilePath('sat', 5, 1, 1)] = true
  local list = { { 5, 1, 1 }, { 5, 2, 1, 'map' }, { 5, 2, 1, 'labels' }, { 5, 3, 1, 'osm' }, { 'x', 1, 1 } }
  local ready = settings.mapTiles(list)
  T.check(#ready == 1 and ready[1] == 'sat/5/1/1', 'ready keys carry the layer')
  T.check(#sent == 1 and #sent[1] == 2 and sent[1][1][4] == 'map' and sent[1][2][4] == 'labels',
    'the missing ones asked, in order, unknown layers and bad tiles left out')
  settings.mapTiles(list)
  T.check(#sent == 1, 'the same missing list is not sent again at once')
  have[settings.mapTilePath('map', 5, 2, 1)] = true
  ready = settings.mapTiles(list)
  T.check(#ready == 2 and #sent == 2 and #sent[2] == 1 and sent[2][1][4] == 'labels', 'a changed list goes out at once')
  have[settings.mapTilePath('labels', 5, 2, 1)] = true
  settings.mapTiles(list)
  T.check(#sent == 3 and #sent[3] == 0, 'nothing missing any more: an empty list cancels the last one')
  settings.mapTiles(list)
  T.check(#sent == 3, 'and only once')
  beamngEarth_core, FS = core0, fs0
end

-- the Feedback buttons: the menu goes to play first, the report runs FEEDBACK_FRAMES frames later
do
  local states, calls = {}, {}
  local gh0, rep0 = rawget(_G, 'guihooks'), rawget(_G, 'beamngEarth_report')
  _G.guihooks = { trigger = function(name, d) if name == 'ChangeState' then states[#states + 1] = d.state end end }
  _G.beamngEarth_report = { note = function() calls[#calls + 1] = 'note' end,
                            profiler = function() calls[#calls + 1] = 'profiler' end }
  T.check(settings.feedback('nope').error ~= nil and #states == 0, 'feedback: an unknown kind does nothing')
  T.check(settings.feedback('report').ok and states[1] == 'play', 'feedback: the menu closes at once')
  for _ = 1, settings.FEEDBACK_FRAMES - 1 do settings.onUpdate() end
  T.check(#calls == 0, 'feedback: not while the menu may still show')
  settings.onUpdate()
  T.check(calls[1] == 'note' and #calls == 1, 'feedback: the report window after the frames')
  settings.feedback('profiler')
  for _ = 1, settings.FEEDBACK_FRAMES + 2 do settings.onUpdate() end
  T.check(calls[2] == 'profiler' and #calls == 2, 'feedback: the profiler, once')
  _G.guihooks, _G.beamngEarth_report = gh0, rep0
end

-- leave the shared modules as found
rebase.distance, ui.visible, ui.compact, ui.hudHidden = dist0, vis0, compact0, false
-- a text setting: the dismissed update notice's version (core.lua drawUpdateNotice), not on the page
do
  local st = require('beamngEarth/settings')
  T.check(st.validate('updateDismissed', '1.5.1') == '1.5.1', 'updateDismissed: a version string')
  T.check(st.validate('updateDismissed', {}) == nil, 'updateDismissed: a table is refused')
  T.check(#st.validate('updateDismissed', string.rep('x', 100)) == 40, 'updateDismissed: at most maxLen characters')
  T.check(st.HIDDEN.updateDismissed, 'updateDismissed: not on the settings page')
end

-- the map holds off the car's controls (the user, 2026-10-21: the controller drove the car behind the map): the stock
-- groups and every driving action by name
do
  local filter = { createActionTemplate = function(groups) return { 'camera_1', 'funBoom' } end }
  local actions = { getActiveActions = function() return {
    accelerate = { cat = 'vehicle' }, steering = { cat = 'vehicle' }, pickup__toggleBed = { cat = 'vehicle_specific' },
    bigMapZoom = { cat = 'gameplay' }, funBoom = { cat = 'vehicle' } } end }
  local got = {}
  for _, a in ipairs(settings.mapBlockedActions(filter, actions)) do got[a] = (got[a] or 0) + 1 end
  T.check(got.accelerate == 1 and got.steering == 1 and got.pickup__toggleBed == 1, 'map: driving actions blocked')
  T.check(got.camera_1 == 1 and got.funBoom == 1 and not got.bigMapZoom, 'map: the stock groups, once each; its own kept')
  got = {}
  for _, a in ipairs(settings.mapBlockedActions(filter, nil)) do got[a] = true end
  T.check(got.accelerate and got.steering and got.brake, 'map: the fallback list without the action list')
end

-- X and Y on the map: the vehicle triggers they are bound to go to the map while it is open, as before after
do
  local sent, orig = {}, {}
  local vt = { onActionEvent = function(id, v) orig[#orig + 1] = id end }
  local saved = { core_vehicleTriggers = rawget(_G, 'core_vehicleTriggers'), freeroam_bigMapMode = rawget(_G, 'freeroam_bigMapMode'),
    guihooks = rawget(_G, 'guihooks'), pushActionMap = rawget(_G, 'pushActionMap'), popActionMap = rawget(_G, 'popActionMap') }
  core_vehicleTriggers, freeroam_bigMapMode = vt, {}
  guihooks = { trigger = function(name, d) if name == settings.MAP_INPUT_EVENT then sent[#sent + 1] = d.kind end end }
  pushActionMap, popActionMap = function() end, function() end
  settings.mapControls(true)
  vt.onActionEvent(1, 1) vt.onActionEvent(2, 1) vt.onActionEvent(0, 1)
  T.check(sent[1] == 'pad_x' and sent[2] == 'pad_y' and #orig == 1 and orig[1] == 0, 'map open: X / Y to the map, others as before')
  settings.mapControls(false)
  vt.onActionEvent(1, 1)
  T.check(#sent == 2 and orig[2] == 1, 'map closed: the triggers as before')
  for k, v in pairs(saved) do rawset(_G, k, v) end
end

-- The map's hold on the controls goes when the map stops asking for its state (its close lost, GitHub #252) or a level
-- starts, never while it asks
do
  local pops, popped = 0, {}
  local filter = { createActionTemplate = function() return {} end, setGroup = function() end,
    addAction = function(_, group, on) popped[#popped + 1] = on end }
  local saved = { core_input_actionFilter = rawget(_G, 'core_input_actionFilter'), freeroam_bigMapMode = rawget(_G, 'freeroam_bigMapMode'),
    pushActionMap = rawget(_G, 'pushActionMap'), popActionMap = rawget(_G, 'popActionMap'), clock = os.clock }
  local now = 100
  os.clock = function() return now end
  core_input_actionFilter, freeroam_bigMapMode = filter, {}
  pushActionMap, popActionMap = function() end, function() pops = pops + 1 end
  settings.mapOpened(true)
  T.check(popped[1] == true, 'map open: the driving actions blocked')
  for _ = 1, 8 do now = now + 1; pcall(settings.uiMapState); settings.onUpdate() end
  T.check(pops == 0 and settings.isMapOpen(), 'map asking every second: held (pops ' .. pops .. ', open ' .. tostring(settings.isMapOpen()) .. ')')
  now = now + settings.MAP_LOST_S + 0.5
  settings.onUpdate()
  T.check(pops == 1 and popped[#popped] == false and not settings.isMapOpen(), 'map silent: the controls given back')
  settings.mapOpened(true)
  settings.onClientPostStartMission()
  T.check(pops == 2 and popped[#popped] == false, 'a level start: given back')
  os.clock = saved.clock
  saved.clock = nil
  for k, v in pairs(saved) do rawset(_G, k, v) end
end

-- #44: dashboard visibility is a persisted player setting.
do
  local dashboard0, settingsFile0 = rawget(_G, 'beamngEarth_dashboard'), files[settings.FILE]
  beamngEarth_dashboard = nil
  settings.load()
  settings.set('dashboardSatellite', false)
  T.check(files[settings.FILE].dashboardSatellite == false, '#44: dashboard visibility off is persisted')
  T.check(settings.load().dashboardSatellite == false, '#44: dashboard visibility off survives reload')
  settings.set('dashboardSatellite', true)
  T.check(files[settings.FILE].dashboardSatellite == true, '#44: dashboard visibility on is persisted')
  beamngEarth_dashboard = dashboard0
  files[settings.FILE] = settingsFile0
  settings.load()
end


-- presets: picking one sets its knobs; moving a knob makes it Custom (or the preset it now matches)
local r = settings.setQuality('low')
T.check(r.ok and settings.get('quality') == 'low' and settings.get('trees') == 'low' and settings.get('groundCover') == false,
  'Low sets its knobs')
settings.set('rocks', false)
T.check(settings.get('quality') == 'custom', 'a knob moved off the preset: Custom')
settings.set('rocks', true)
T.check(settings.get('quality') == 'low', 'moved back: the preset again')
settings.set('quality', 'ultra')
T.check(settings.get('trees') == 'high' and settings.get('nightLights') == 'many', 'Ultra through set()')
T.check(settings.validate('quality', 'bogus') == nil, 'an unknown preset is refused')

-- an older file with a knob changed and no quality: kept, Custom (the first run never overrides the player)
files[settings.FILE] = { version = 1, groundCover = false }
local v2 = settings.load()
T.check(v2.groundCover == false and v2.quality == 'custom', 'an old changed knob: kept, quality Custom')
files[settings.FILE] = { version = 1, groundCover = false, rocks = false, trees = 'low', nightLights = 'few',
  groundCoverDensity = 'low', viewDistanceMode = 'fixed', viewDistanceKm = 40 }
local v3 = settings.load()
T.check(v3.quality == 'custom' or v3.quality == 'low', 'old knobs that match no preset stay theirs')
files[settings.FILE] = nil
T.done('test_settings')
