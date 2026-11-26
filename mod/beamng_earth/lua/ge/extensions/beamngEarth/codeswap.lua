-- Another copy of BeamNG.Earth mounted under the running one: its Lua swapped in (2026-11-11, BeamMP's 1-2 fps).
--
-- A BeamMP server sends its own copy of the mod (mods/multiplayer/beamng_earth.zip) and BeamMP turns the player's off
-- (mods/unpacked/beamng_earth) when they join, and back on when they leave. The game swaps the files, not the Lua that
-- runs: beamngEarth_core and the other extensions are 'manual' (modScript.lua) and stay loaded, and every module they
-- required stays in package.loaded. So the player's own copy kept running on the server's files, and when the two were
-- not the same version a module only one of them has was required again on every frame: parking.lua's
-- require('beamngEarth/busparked') against a 1.9.1 server's copy failed in 1.4 s a frame on the Steam Deck (the game's
-- search of every package path in every mounted mod), the players' "1 fps on a BeamMP server, fine alone" (Discord
-- 2026-11-06 to 11-10; "1 fps until I close the earth cmd window": with the app gone that path never ran). Any other
-- difference between the copies (a function one has, a file the other reads) broke the same way.
--
-- The rule: the running code remembers the copy it was loaded from (its fingerprint: every Lua file of
-- lua/ge/extensions/beamngEarth with its size). When a mod is turned on or off (core.lua's onModActivated /
-- onModDeactivated) or a copy's modScript runs (modScript.lua: a newer copy taking over from an older one that has none
-- of this), and the copy now mounted is another one, our extensions are unloaded and our modules forgotten; the mod
-- manager then loads the mounted copy's extensions (loadManualUnloadExtensions), or this does after a hook. Nothing is
-- swapped while no copy is mounted (between BeamMP turning one off and the other on, a stock-map server), nor when the
-- mounted copy is the same. The player's copy is switched back on after the session by a job core.lua's
-- onModDeactivated starts before this runs (beammp.lua): the swap doesn't touch it, whatever version the server sent.
-- BeamMP turns the server's copy on before it turns the player's off, and the mod manager runs a modScript by its
-- virtual path, so the player's copy's modScript usually runs then: the swap comes from the running code's hooks (the
-- player's app), and a server's newer copy can take over an older app's code only when its files win (measured on the
-- Steam Deck 2026-11-11: dev app on a 1.9.1 server swapped at the player's copy going off; a 1.9.1 app on a dev
-- server kept its own code, which ran fine on the newer files, ~35 fps).
--
-- Self-contained (modScript.lua runs it with dofile: a require would return the old copy's module). Rollback: an empty
-- file settings/beamngEarth_nocodeswap in the game's user folder turns it off.
local M = {}

M.DIR = '/lua/ge/extensions/beamngEarth/'
M.OFF_FILE = '/settings/beamngEarth_nocodeswap'
M.GLOBAL = 'beamngEarthCodeFingerprint'   -- (a global: it outlives the modules, so a newer copy's modScript can read it)
M.CORE = 'beamngEarth_core'
-- The GPS's destination and the waypoints still ahead, carried across a swap: gps.lua keeps them in its module, which
-- the swap forgets, so a route set before a BeamMP join was gone after it. The mounted copy's gps.lua takes them once
-- at load (gps.lua M.CARRY).
M.GPS = 'beamngEarthGpsCarry'
-- Not code: release.lua names the release, written only into packaged copies (tools/package.py), so a BeamMP server's
-- copy of the player's own version would look like another one (the Deck, 2026-11-11: two needless swaps a join)
M.SKIP = { ['release.lua'] = true }

local function fs() return rawget(_G, 'FS') end

-- The mounted copy's fingerprint: its Lua files and sizes, sorted ("name:size;..."). nil when no copy is mounted.
function M.fingerprint()
  local F = fs()
  if not F or not F.findFiles then return nil end
  local ok, files = pcall(F.findFiles, F, M.DIR, '*.lua', 0, false, false)
  if not ok or type(files) ~= 'table' or #files == 0 then return nil end
  local parts = {}
  for _, p in ipairs(files) do
    local name = type(p) == 'string' and (p:match('([^/]+)$') or p):lower()
    if name and not M.SKIP[name] then
      local okS, n = pcall(F.fileSize, F, p)
      parts[#parts + 1] = name .. ':' .. tostring(okS and n or '?')
    end
  end
  if #parts == 0 then return nil end
  table.sort(parts)
  return table.concat(parts, ';')
end

function M.enabled()
  local F = fs()
  if not F or not F.fileExists then return true end
  local ok, off = pcall(F.fileExists, F, M.OFF_FILE)
  return not (ok and off)
end

-- The running code's copy (core.lua at load: the files it was loaded from).
function M.remember(fp)
  rawset(_G, M.GLOBAL, fp or M.fingerprint())
end

function M.remembered() return rawget(_G, M.GLOBAL) end

-- Pure: swap? running (is our core loaded), had (the fingerprint it remembers, nil for a copy older than this module),
-- now (the mounted copy's, nil for none).
function M.due(running, had, now)
  if not running or now == nil then return false end
  return had ~= now
end

-- Our extensions (loaded, named beamngEarth_*).
local function ours()
  local ext = rawget(_G, 'extensions')
  local out = {}
  if not ext or type(ext.getLoadedExtensionsNames) ~= 'function' then return out end
  local ok, names = pcall(ext.getLoadedExtensionsNames, true)
  for _, n in ipairs(ok and type(names) == 'table' and names or {}) do
    if type(n) == 'string' and n:match('^beamngEarth_') then out[#out + 1] = n end
  end
  return out
end
M.ours = ours

-- Is k a module of ours (package.loaded key)?
function M.ownModule(k)
  if type(k) ~= 'string' then return false end
  local s = k:gsub('%.', '/')
  return s:match('^beamngEarth/') ~= nil or s:match('^lua/ge/extensions/beamngEarth/') ~= nil
    or s == 'core/cameraModes/earthGlobe'
end

-- The running GPS's route as M.GPS: { { lat, lon, name }... }, the waypoints ahead and the destination last; nil
-- without a destination (a route cleared before the swap stays cleared).
function M.keepGps()
  rawset(_G, M.GPS, nil)
  local gps = package.loaded['beamngEarth/gps']
  if type(gps) ~= 'table' or type(gps.status) ~= 'function' then return end
  local ok, st = pcall(gps.status)
  if not ok or type(st) ~= 'table' or tonumber(st.lat) == nil or tonumber(st.lon) == nil then return end
  local stops = {}
  for _, w in ipairs(type(st.waypoints) == 'table' and st.waypoints or {}) do
    stops[#stops + 1] = { lat = w.lat, lon = w.lon, name = w.name }
  end
  stops[#stops + 1] = { lat = st.lat, lon = st.lon, name = st.name }
  rawset(_G, M.GPS, stops)
end

-- Unload our extensions, forget our modules; load the mounted copy's when reload. -> the extensions unloaded.
function M.swap(why, reload)
  pcall(M.keepGps)                -- (before the unload: our extensions' unload may clear the GPS)
  local names = ours()
  local ext = rawget(_G, 'extensions')
  local log = rawget(_G, 'log')
  if log then
    pcall(log, 'W', 'beamngEarth', string.format('another copy of BeamNG.Earth is mounted (%s): its code loaded in place '
      .. 'of the running one (%d extensions)', tostring(why), #names))
  end
  if ext and type(ext.unload) == 'function' then
    for _, n in ipairs(names) do pcall(ext.unload, n) end
  end
  for k in pairs(package.loaded) do
    if M.ownModule(k) then package.loaded[k] = nil end
  end
  rawset(_G, M.GLOBAL, nil)
  if reload and ext and type(ext.load) == 'function' and #names > 0 then
    pcall(ext.load, names)
  end
  return names
end

-- The check (a mod turned on or off, a copy's modScript). reload: load the mounted copy's extensions here (a hook; the
-- mod manager does it after a modScript). -> true when swapped.
function M.check(why, reload)
  if not M.enabled() then return false end
  local now = M.fingerprint()
  if not M.due(rawget(_G, M.CORE) ~= nil, M.remembered(), now) then return false end
  M.swap(why, reload)
  return true
end

return M
