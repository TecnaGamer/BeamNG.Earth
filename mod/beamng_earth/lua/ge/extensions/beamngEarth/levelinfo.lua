-- The level's info.json for the freeroam level select (the user, 2026-10-20: "have the location selector
-- automatically match the places list"): a spawn point per saved place (places.lua), "Random location" and the
-- default start, and the level's details as the stock levels have them, for the whole Earth.
--
-- Written to /levels/beamng_earth/info.json in the user folder, where it stands over the mod's own copy (checked in
-- game 2026-10-20: the level list reads the user folder's; deleting it brings the mod's back). core_levels drops its
-- level list when a file under /levels/ changes, so the menu shows the new list by itself. Written only when its text
-- changes (each write is a file event and a rebuilt level list).
--
-- The spawn point the player picks is saved by the game (setSpawnpoint.setDefaultSP, settings/cloud/game-state.json
-- levels.beamng_earth.defaultSpawnPointName); our level has no SpawnSphere of those names, the car starts at the
-- default one, and core's start teleport goes to the place it names (M.chosen).

local M = {}

M.PATH = '/levels/beamng_earth/info.json'
M.GAME_STATE = 'settings/cloud/game-state.json'
M.LEVEL = 'beamng_earth'
M.DEFAULT = 'spawn_default'
M.RANDOM = 'bne_random'
M.PREFIX = 'bne_place_'
M.PREVIEW = 'beamng_earth_preview.jpg'
M.PLACE_PREVIEWS = 'places/'           -- per place: <PLACE_PREVIEWS><previewKey>.jpg (the server writes them)

-- Earth (stock levels' fields; levelTiles.lua shows size as width x height (area): the equator's 40,075 km by
-- 12,728 km, whose product is the Earth's surface, 510,072,000 km2)
M.BASE = {
  title = 'BeamNG.Earth',
  description = 'The whole Earth, streamed around you by the BeamNG.Earth companion server. Start the server, then '
    .. 'pick a place below or teleport from the BeamNG.Earth window. Preview: Sentinel-2 cloudless 2016 by EOX IT '
    .. 'Services GmbH (CC BY 4.0).',
  previews = { M.PREVIEW },
  size = { 40075017, 12727986 },
  biome = 'Every biome on Earth: oceans, deserts, rainforests, forests, grasslands, tundra, mountains and ice',
  roads = 'About 64 million km of roads in every country: motorways, highways, city streets, rural and dirt '
    .. 'roads',
  suitablefor = 'Freeroam, road trips, exploring, off-roading',
  features = 'The real planet from open data: terrain, roads, bridges, tunnels, buildings, forests, lakes, seas, '
    .. 'airports and live weather',
  country = 'Every country',
  authors = 'BeamNG.Earth',
  defaultSpawnPointName = M.DEFAULT,
  supportsTraffic = true,
  supportsTimeOfDay = true,
}

-- A place's preview file name: its rounded position (a rename keeps the picture; a moved place gets a new one).
function M.previewKey(lat, lon) return string.format('%.4f_%.4f', lat, lon):gsub('-', 'm'):gsub('%.', 'p') end

local function copy(t)
  local o = {}
  for k, v in pairs(t) do o[k] = type(v) == 'table' and copy(v) or v end
  return o
end

-- The info table for places ({ id, name, lat, lon }); hasPreview(key) -> true when that place's picture exists.
function M.build(placeList, hasPreview)
  local info = copy(M.BASE)
  local sp = {
    { objectname = M.DEFAULT, translationId = 'Where you left off', preview = M.PREVIEW,
      description = 'The last place you teleported to.' },
    { objectname = M.RANDOM, translationId = 'Random location', preview = M.PREVIEW,
      description = 'A road somewhere in the world, picked at random each time.' },
  }
  for _, p in ipairs(placeList or {}) do
    local key = M.previewKey(p.lat, p.lon)
    sp[#sp + 1] = { objectname = M.PREFIX .. tostring(p.id), translationId = p.name,
      description = string.format('%.5f, %.5f', p.lat, p.lon),
      preview = (hasPreview and hasPreview(key)) and (M.PLACE_PREVIEWS .. key .. '.jpg') or M.PREVIEW }
  end
  info.spawnPoints = sp
  return info
end

local last = nil -- the text written last (or read back)

-- Write the info for placeList when it differs from the file. Returns true when it wrote.
function M.write(placeList)
  local enc, write, read = rawget(_G, 'jsonEncodePretty') or rawget(_G, 'jsonEncode'), rawget(_G, 'writeFile'),
    rawget(_G, 'readFile')
  if not enc or not write then return false end
  local function hasPreview(key)
    return FS and FS.fileExists and FS:fileExists('/levels/beamng_earth/' .. M.PLACE_PREVIEWS .. key .. '.jpg') or false
  end
  local ok, text = pcall(enc, M.build(placeList, hasPreview))
  if not ok or type(text) ~= 'string' then return false end
  if last == nil and read then
    local ok2, cur = pcall(read, M.PATH)
    last = ok2 and cur or ''
  end
  if text == last then return false end
  local ok3, res = pcall(write, M.PATH, text)
  if not ok3 or res == false then
    log('W', 'beamngEarth', 'level info: write failed: ' .. tostring(res))
    return false
  end
  last = text
  return true
end

-- The places whose picture is not there yet and not asked for this session: { { key, lat, lon } }, each marked asked.
local asked = {}
function M.missing(placeList)
  local out = {}
  for _, p in ipairs(placeList or {}) do
    local key = M.previewKey(p.lat, p.lon)
    if not asked[key] and not (FS and FS.fileExists
        and FS:fileExists('/levels/beamng_earth/' .. M.PLACE_PREVIEWS .. key .. '.jpg')) then
      asked[key] = true
      out[#out + 1] = { key = key, lat = p.lat, lon = p.lon }
    end
  end
  return out
end

-- The spawn point the player picked for our level: M.DEFAULT, M.RANDOM, M.PREFIX .. id, or nil.
function M.chosen()
  local read = rawget(_G, 'jsonReadFile')
  if not read then return nil end
  local ok, data = pcall(read, M.GAME_STATE)
  local lv = ok and type(data) == 'table' and type(data.levels) == 'table' and data.levels[M.LEVEL] or nil
  local name = type(lv) == 'table' and lv.defaultSpawnPointName or nil
  return type(name) == 'string' and name or nil
end

-- The place id of a spawn point name, or nil.
function M.placeId(name)
  local id = type(name) == 'string' and name:match('^' .. M.PREFIX .. '(%d+)$')
  return id and tonumber(id) or nil
end

-- True for a spawn point name of ours that has no SpawnSphere in the level (M.RANDOM, M.PREFIX .. id).
function M.isOurs(name)
  return name == M.RANDOM or (type(name) == 'string' and name:sub(1, #M.PREFIX) == M.PREFIX)
end

-- The big map's POI list (gameplay_rawPois): core_levels' onGetRawPoiListForLevel lists the level's spawn points that
-- have a scene object and logs an error for each one without. Ours have none, so each gathering logged one per place
-- (663 errors in a 33 min drive on BeamNG 0.40.4, 2026-10-30; gasstations.lua gathers it again as stations come and
-- go). While in our level the stock function is called with our level's spawn points less ours, put back after.
-- extensions.hook keeps the functions it calls: hookUpdate makes it take the wrap (and the stock one back).
local poiOrig, poiWrap = nil, nil
function M.hookSpawnPois()
  local levels, ext = rawget(_G, 'core_levels'), rawget(_G, 'extensions')
  if not levels or type(levels.onGetRawPoiListForLevel) ~= 'function' or type(levels.getLevelByName) ~= 'function'
      or not ext or type(ext.hookUpdate) ~= 'function' then return false end
  if levels.onGetRawPoiListForLevel == poiWrap then return true end
  local orig = levels.onGetRawPoiListForLevel
  poiOrig = orig
  poiWrap = function(levelIdentifier, ...)
    local info = type(levelIdentifier) == 'string' and levelIdentifier:lower() == M.LEVEL
      and levels.getLevelByName(levelIdentifier) or nil
    local all = type(info) == 'table' and info.spawnPoints or nil
    if type(all) ~= 'table' then return orig(levelIdentifier, ...) end
    local kept = {}
    for _, sp in ipairs(all) do
      if not M.isOurs(type(sp) == 'table' and sp.objectname) then kept[#kept + 1] = sp end
    end
    info.spawnPoints = kept
    local ok, err = pcall(orig, levelIdentifier, ...)
    info.spawnPoints = all
    if not ok then error(err, 0) end
  end
  levels.onGetRawPoiListForLevel = poiWrap
  ext.hookUpdate('onGetRawPoiListForLevel')
  return true
end

function M.unhookSpawnPois()
  local levels, ext = rawget(_G, 'core_levels'), rawget(_G, 'extensions')
  if levels and poiWrap and levels.onGetRawPoiListForLevel == poiWrap then
    levels.onGetRawPoiListForLevel = poiOrig
    if ext and type(ext.hookUpdate) == 'function' then ext.hookUpdate('onGetRawPoiListForLevel') end
  end
  poiOrig, poiWrap = nil, nil
end

function M._reset() last, asked = nil, {} end -- tests

return M
