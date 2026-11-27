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

-- The region the player is in, as the game's traffic reads a level's (2026-11-27 mod-compatibility study): its
-- vehicle selection (gameplay/traffic/trafficUtils getLevelInfo) takes info.json's region (northAmerica, europe,
-- asiaPacific: the Region filter with Options > Gameplay > Traffic's smart selections) and the last word of its country
-- (usa, germany, italy, france, japan: the police's Country filter). Ours spans the Earth: the region of the road
-- graph's country (navgraph.syncLevelRegion), kept in the text as 'Every country (USA)' so the level select still
-- says what the level is. nil: none (outside the three regions). Until the session's first graph says (syncRegion)
-- the file's own region is kept (M.diskRegion): the level start's write does not take it out only for the graph to
-- put it back seconds later (two rebuilt level lists a session).
M.COUNTRY_WORDS = { US = 'USA', DE = 'Germany', IT = 'Italy', FR = 'France', JP = 'Japan' }
M.region = nil          -- { region = 'europe', cc = 'DE' }
M.diskRegion = nil      -- { region, country } of the file before this session's syncRegion; false: none; nil: unread
local regionKnown = false

-- The info table for places ({ id, name, lat, lon }); hasPreview(key) -> true when that place's picture exists.
function M.build(placeList, hasPreview)
  local info = copy(M.BASE)
  if M.region then
    info.region = M.region.region
    local word = M.COUNTRY_WORDS[M.region.cc]
    if word then info.country = M.BASE.country .. ' (' .. word .. ')' end
  elseif not regionKnown and M.diskRegion then
    info.region = M.diskRegion.region
    if type(M.diskRegion.country) == 'string' then info.country = M.diskRegion.country end
  end
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
local lastList = nil -- the places of the last write (syncRegion writes the info again with them)
M.writes = 0          -- info.json writes this session

-- Write the info for placeList when it differs from the file. Returns true when it wrote.
function M.write(placeList)
  lastList = placeList
  local enc, write, read = rawget(_G, 'jsonEncodePretty') or rawget(_G, 'jsonEncode'), rawget(_G, 'writeFile'),
    rawget(_G, 'readFile')
  if not enc or not write then return false end
  if not regionKnown and M.diskRegion == nil then
    local jr = rawget(_G, 'jsonReadFile')
    local okR, d = false, nil
    if jr and FS and FS.fileExists and FS:fileExists(M.PATH) then okR, d = pcall(jr, M.PATH) end
    M.diskRegion = okR and type(d) == 'table' and type(d.region) == 'string' and { region = d.region, country = d.country }
      or false
  end
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
  M.writes = M.writes + 1
  return true
end

-- The level's police vehicles (police.vehGroup.json, as the stock levels have one: the game's police role reads it
-- with smart selections on, gameplay/police.lua -> trafficUtils.getTrafficGroupFromFile, and so does any mod that asks
-- for the level's group by name), of the region the player is in. Ours: its "bngEarth" field is the stamp it was made
-- for (region | country | mods); a file without it (some other mod's) is left alone.
M.POLICE_PATH = '/levels/beamng_earth/police.vehGroup.json'
local policeStamp = nil   -- the police file's stamp on disk: a string, false (none of ours), nil (not read yet)
local foreignPolice = false
local regionStamp = nil   -- the region / country / mods syncRegion last saw
M.regionWrites = 0        -- (probes: the region changes written)

local function readPolice()
  local read = rawget(_G, 'jsonReadFile')
  local exists = FS and FS.fileExists and FS:fileExists(M.POLICE_PATH)
  if not exists then return false end
  local ok, d = pcall(read or function() return nil end, M.POLICE_PATH)
  if ok and type(d) == 'table' and type(d.bngEarth) == 'string' then return d.bngEarth end
  foreignPolice = true
  return false
end

-- The player's region changed (navgraph: the road graph's rules): region ('northAmerica', 'europe', 'asiaPacific' or
-- nil), cc (ISO country code), makePolice() -> the group's { { model, config }... } (only called when the file is
-- written; nil: no police file), allMods (the traffic's mods setting: a group with or without mods' vehicles). Writes
-- info.json's region / country and the police file only when their region, country or mods changed (each write is a
-- file event under /levels/: the game rebuilds its level list, so both go in the same frame); outside the regions the
-- file goes (the game's own police group then). Returns true when it wrote or removed something.
function M.syncRegion(region, cc, makePolice, allMods)
  cc = type(cc) == 'string' and string.upper(cc) or nil
  local stamp = region and (region .. '|' .. tostring(M.COUNTRY_WORDS[cc or ''] or '') .. (allMods and '|mods' or '')) or false
  if stamp == regionStamp then return false end
  regionStamp, regionKnown = stamp, true
  local did = false
  -- info.json: the region and the country word (written only when its text changes)
  M.region = region and { region = region, cc = cc } or nil
  local w0 = M.writes
  if lastList == nil then          -- (no write this session yet: places.lua's, with the places list)
    local okP, places = pcall(require, 'beamngEarth/places')
    if okP and places and places.syncLevel then pcall(places.syncLevel) end
  else
    pcall(M.write, lastList)
  end
  did = M.writes ~= w0
  -- the police file
  if policeStamp == nil then policeStamp = readPolice() end
  if not foreignPolice and policeStamp ~= stamp then
    local write, enc = rawget(_G, 'writeFile'), rawget(_G, 'jsonEncodePretty') or rawget(_G, 'jsonEncode')
    local data = nil
    if stamp and makePolice then
      local okG, g = pcall(makePolice)
      if okG and type(g) == 'table' and g[1] then data = g end
    end
    if data and write and enc then
      local group = { type = 'custom', name = 'BeamNG.Earth - Police (' .. region .. ')', tags = { 'beamng_earth', 'police', region },
        data = data, bngEarth = stamp }
      local okE, text = pcall(enc, group)
      local okF, res = false, nil
      if okE and type(text) == 'string' then okF, res = pcall(write, M.POLICE_PATH, text) end
      if okF and res ~= false then policeStamp, did = stamp, true
      else log('W', 'beamngEarth', 'level info: police group write failed: ' .. tostring(res)) end
    elseif policeStamp and FS and FS.removeFile then   -- (no region, or no group made: the game's own)
      pcall(FS.removeFile, FS, M.POLICE_PATH)
      policeStamp, did = false, true
    end
  end
  if did then M.regionWrites = M.regionWrites + 1 end
  return did
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

function M._reset()      -- tests
  last, asked, lastList, policeStamp, foreignPolice, regionStamp, M.region, M.regionWrites = nil, {}, nil, nil, false,
    nil, nil, 0
  M.diskRegion, regionKnown = nil, false
end

return M
