-- Saved places (docs/ROADMAP.md "Scheduled quick wins" 1): bookmarks, recent places and "go there",
-- for the pause-menu settings page (settings.lua's uiPlace* bridge, EarthSettings.vue "Places").
-- Engine-light module: the file through jsonReadFile / jsonWriteFile, the teleport through
-- beamngEarth_core.teleport, nothing per frame.
--   places   { id, name, lat, lon }   the player's list, in their order (new ones at the end)
--   recent   { lat, lon, name?, kind } newest first: every teleport target ('teleport') and the
--            place of each level start ('start'), at most RECENT_MAX, one per DEDUP_M
-- Persisted in FILE (apart from the settings file). No file yet: the list starts with SEED (the
-- docs/PLACES.md benchmarks and a few well-known drives; data only, the player can edit or delete
-- every one) and is written at once. A file that cannot be read (or is not a places file): one
-- warning, a copy of its text in FILE .. '.bak', and an empty list. Places skipped on load (no
-- name, no valid lat/lon) are copied the same way first. The file is never overwritten with less
-- than it held unless that copy exists: when it cannot be made, nothing is saved this session.

local M = {}

local net = require('beamngEarth/net')

M.FILE = '/settings/beamngEarth_places.json'
M.VERSION = 1
M.RECENT_MAX = 10
M.DEDUP_M = 200   -- a recent place this close to a newer one is the same place
M.NAME_MAX = 60
M.ID_MAX = 2 ^ 31 -- ids (and nextId) from the file at or above this are renumbered

-- The first run's list (docs/PLACES.md first table, then well-known drives): each on a public road, on the ground
-- (2026-10-20, the user: "some teleport the player in a building or on a hill": Fells Point's point was inside a
-- building, Echo Point's on the cliff edge at 74 %, Mount Whitney's summit and the Kern River ravine 3 km from any
-- road; each was moved to the nearest road, server randomplace.road_spot, or to a road nearby with the same view).
M.SEED = {
  { name = 'Fells Point, Baltimore MD', lat = 39.283309, lon = -76.593484 },
  { name = 'Mount Whitney, Whitney Portal Road', lat = 36.596718, lon = -118.169991 },
  { name = 'Kern River Canyon, CA-178', lat = 35.460552, lon = -118.771223 },
  { name = 'Monument Valley mesas', lat = 36.78624, lon = -110.139875 },
  { name = 'Arizona / Utah state line, US-163', lat = 36.99664, lon = -110.176367 },
  { name = 'El Capitan Meadow, Yosemite', lat = 37.72098, lon = -119.638981 },
  { name = 'Pikes Peak summit, Colorado', lat = 38.840612, lon = -105.044051 },
  { name = 'Mount Fuji, Subaru Line 5th station', lat = 35.395301, lon = 138.732839 },
  { name = 'Everest North Base Camp, Tibet', lat = 28.14141, lon = 86.851111 },
  { name = 'Stelvio Pass, Italy', lat = 46.528634, lon = 10.453422 },
  { name = 'Blue Mountains, Echo Point NSW', lat = -33.731908, lon = 150.312007 },
  -- (the user, 2026-10-21: "remove that solar panel farm from the default places and add my current location")
  { name = 'Entz Béla út, Pécs, Hungary', lat = 46.099176, lon = 18.218798 },
}

-- The earlier first-run list's places as they were (a player's list keeps them until they edit them): one still at
-- its old name and position moves to its new entry (`to`: the name of its entry in M.SEED; false: left as it is,
-- the place no longer in the first-run list) when the file is read.
M.OLD_SEED = {
  { name = 'Fells Point, Baltimore MD', lat = 39.2834, lon = -76.5934, to = 'Fells Point, Baltimore MD' },
  { name = 'Mount Whitney summit', lat = 36.5785, lon = -118.2923, to = 'Mount Whitney, Whitney Portal Road' },
  { name = 'Kern River ravine (Sierra Nevada)', lat = 36.506422, lon = -118.402437, to = 'Kern River Canyon, CA-178' },
  { name = 'Monument Valley mesas', lat = 36.7818, lon = -110.1393, to = 'Monument Valley mesas' },
  { name = 'Arizona / Utah state line, Monument Valley', lat = 36.998218, lon = -110.102293,
    to = 'Arizona / Utah state line, US-163' },
  { name = 'Kayenta PV Solar Plant, Arizona', lat = 36.7874, lon = -110.2382, to = false },
  { name = 'El Capitan Meadow, Yosemite', lat = 37.7225, lon = -119.6385, to = 'El Capitan Meadow, Yosemite' },
  { name = 'Pikes Peak summit, Colorado', lat = 38.8405, lon = -105.0442, to = 'Pikes Peak summit, Colorado' },
  { name = 'Mount Fuji, Subaru Line 5th station', lat = 35.3953, lon = 138.7330,
    to = 'Mount Fuji, Subaru Line 5th station' },
  { name = 'Everest North Base Camp, Tibet', lat = 28.1414, lon = 86.8514, to = 'Everest North Base Camp, Tibet' },
  { name = 'Stelvio Pass, Italy', lat = 46.5286, lon = 10.4532, to = 'Stelvio Pass, Italy' },
  { name = 'Blue Mountains, Echo Point NSW', lat = -33.7320, lon = 150.3115, to = 'Blue Mountains, Echo Point NSW' },
}

local function seedNamed(name)
  for _, s in ipairs(M.SEED) do if s.name == name then return s end end
end

local places, recent, nextId = {}, {}, 1
local loaded = false
local warning = nil -- the last load's problem, shown on the page
local noSave = false -- the file lost something on load and could not be copied: never overwrite it

-- great-circle distance in m (mean Earth radius; the dedup needs no ellipsoid)
local function distance(lat1, lon1, lat2, lon2)
  local r = math.pi / 180
  local a = math.sin((lat2 - lat1) * r / 2) ^ 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ^ 2
  return 2 * 6371008.8 * math.asin(math.min(1, math.sqrt(a)))
end
M.distance = distance

local function round6(x) return math.floor(x * 1e6 + 0.5) / 1e6 end -- ~0.1 m; keeps the file tidy

local function validLatLon(lat, lon)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon or lat ~= lat or lon ~= lon then return nil end
  if lat < -90 or lat > 90 or lon < -180 or lon > 180 then return nil end
  return lat, lon
end

local function cleanName(name)
  if type(name) ~= 'string' and type(name) ~= 'number' then return nil end
  local s = tostring(name):gsub('[%c]', ' '):gsub('^%s+', ''):gsub('%s+$', '')
  if s == '' then return nil end
  if #s > M.NAME_MAX then s = s:sub(1, M.NAME_MAX):gsub('[\128-\191]+$', ''):gsub('[\192-\255]$', '') end
  return s
end

function M.coordText(lat, lon) return string.format('%.5f, %.5f', lat, lon) end

------------------------------------------------------------------------------------------
-- lat / lon text: "36.5785, -118.2923" (Google Maps' copy), "36.5785 -118.2923",
-- "36.5785 N, 118.2923 W", "N 36.5785 W 118.2923", "36°34'42.6"N 118°17'32.3"W" (degrees, minutes,
-- seconds). Returns lat, lon or nil + error.

local SKIP = { ['°'] = true, ['º'] = true, ["'"] = true, ['"'] = true, ['′'] = true, ['″'] = true,
  ['’'] = true, ['”'] = true, ['\194\160'] = true } -- \194\160: a no-break space (pasted text)

local function tokens(text)
  local out, i, n = {}, 1, #text
  while i <= n do
    local c = text:sub(i, i)
    if c:match('%s') then
      i = i + 1
    else
      local num = text:match('^[%-+]?%d+%.?%d*', i) or text:match('^[%-+]?%.%d+', i)
      if num then
        out[#out + 1] = { num = tonumber(num), text = num }
        i = i + #num
      elseif c:match('[NSEWnsew]') and not text:sub(i + 1, i + 1):match('%a') then
        out[#out + 1] = { hemi = c:upper() }
        i = i + 1
      elseif c == ',' or c == ';' then
        out[#out + 1] = { sep = true }
        i = i + 1
      else
        local skipped = false
        for s in pairs(SKIP) do
          if text:sub(i, i + #s - 1) == s then i, skipped = i + #s, true; break end
        end
        if not skipped then return nil end
      end
    end
  end
  return out
end

-- one coordinate: 1-3 numbers (degrees [minutes [seconds]]) and an optional hemisphere letter
local function component(g)
  local nums = g.nums
  if #nums < 1 or #nums > 3 then return nil end
  local d = nums[1]
  local neg = d.num < 0 or d.text:sub(1, 1) == '-'
  local v = math.abs(d.num)
  for k = 2, #nums do
    local prev, x = nums[k - 1], nums[k]
    if prev.num ~= math.floor(prev.num) or x.num < 0 or x.num >= 60 or x.text:match('^[%-+]') then return nil end
    v = v + x.num / (k == 2 and 60 or 3600)
  end
  if g.hemi then
    if neg then return nil end -- "-36 N": ambiguous
    neg = g.hemi == 'S' or g.hemi == 'W'
  end
  return neg and -v or v
end

function M.parseLatLon(text)
  if type(text) ~= 'string' then return nil, 'type a latitude and longitude' end
  local toks = tokens(text)
  if not toks or #toks == 0 then return nil, 'not a latitude, longitude' end
  -- group the tokens into coordinates: a separator, a hemisphere letter after numbers (suffix) or
  -- a second letter (prefix form) ends one
  local groups, cur = {}, { nums = {} }
  local seps = 0
  local function close()
    if #cur.nums > 0 or cur.hemi then groups[#groups + 1] = cur end
    cur = { nums = {} }
  end
  for _, t in ipairs(toks) do
    if t.sep then
      seps = seps + 1
      close()
    elseif t.hemi then
      if #cur.nums > 0 and not cur.hemi then
        cur.hemi = t.hemi
        close()
      else
        if cur.hemi or #cur.nums > 0 then close() end
        cur.hemi = t.hemi
      end
    else
      cur.nums[#cur.nums + 1] = t
    end
  end
  close()
  -- only numbers, no separator: split them evenly ("36.5 -118.2", "36 34 42 -118 17 32")
  if #groups == 1 and not groups[1].hemi and seps == 0 and (#groups[1].nums == 2 or #groups[1].nums == 4 or #groups[1].nums == 6) then
    local all, h = groups[1].nums, #groups[1].nums / 2
    local a, b = { nums = {} }, { nums = {} }
    for k = 1, h do a.nums[k], b.nums[k] = all[k], all[k + h] end
    groups = { a, b }
  end
  if #groups ~= 2 then return nil, 'not a latitude, longitude' end
  local g1, g2 = groups[1], groups[2]
  local isLat = function(g) return g.hemi == 'N' or g.hemi == 'S' end
  local isLon = function(g) return g.hemi == 'E' or g.hemi == 'W' end
  if isLon(g1) or isLat(g2) then g1, g2 = g2, g1 end -- "118.2 W, 36.5 N"
  if isLon(g1) or isLat(g2) then return nil, 'two latitudes or two longitudes' end
  local lat, lon = component(g1), component(g2)
  if not lat or not lon then return nil, 'not a latitude, longitude' end
  if lat < -90 or lat > 90 then return nil, 'latitude must be between -90 and 90' end
  if lon < -180 or lon > 180 then return nil, 'longitude must be between -180 and 180' end
  return lat, lon
end

------------------------------------------------------------------------------------------
-- the file

local function save(path)
  local write = rawget(_G, 'jsonWriteFile')
  if not write or noSave then return false end
  local out = { version = M.VERSION, nextId = nextId, places = {}, recent = {} }
  for i, p in ipairs(places) do out.places[i] = { id = p.id, name = p.name, lat = p.lat, lon = p.lon } end
  for i, r in ipairs(recent) do out.recent[i] = { lat = r.lat, lon = r.lon, name = r.name, kind = r.kind } end
  local ok, err = pcall(write, path or M.FILE, out, true)
  if not ok then log('W', 'beamngEarth', 'places: write failed: ' .. tostring(err)) end
  if ok and err ~= false and (path or M.FILE) == M.FILE then M.syncLevel() end
  return ok and err ~= false
end
M.save = save

-- The level select's spawn points follow the list (levelinfo.lua): after each save, at load and when the file
-- changes on disk (core's onFileChanged).
function M.syncLevel()
  local ok, li = pcall(require, 'beamngEarth/levelinfo')
  if ok and li and li.write then
    local ok2, err = pcall(li.write, places)
    if not ok2 then log('W', 'beamngEarth', 'places: level info: ' .. tostring(err)) end
    -- each place's picture, made by the server (placepreview.py): its answer runs this again (core)
    if net.isConnected() then
      local miss = li.missing(places)
      if #miss > 0 then net.send({ type = 'place_previews', places = miss }) end
    end
  end
end

local function seed()
  places, recent, nextId = {}, {}, 1
  for _, s in ipairs(M.SEED) do
    places[#places + 1] = { id = nextId, name = s.name, lat = s.lat, lon = s.lon }
    nextId = nextId + 1
  end
end

-- Keep a copy of a file that could not be read before it is ever overwritten.
local function backup(path)
  local read, write = rawget(_G, 'readFile'), rawget(_G, 'writeFile')
  if not read or not write then return false end
  local ok, text = pcall(read, path)
  if not ok or type(text) ~= 'string' then return false end
  local ok2, res = pcall(write, path .. '.bak', text)
  return ok2 and res and true or false
end

-- Read the file (path: FILE). Returns the places, the recent places and a list of warnings.
function M.load(path)
  path = path or M.FILE
  loaded, warning, noSave = true, nil, false
  places, recent, nextId = {}, {}, 1
  local read = rawget(_G, 'jsonReadFile')
  local exists = FS and FS.fileExists and FS:fileExists(path)
  local ok, data = false, nil
  if read and exists ~= false then ok, data = pcall(read, path) end
  local warnings = {}
  if (not ok and data == nil) or (ok and data == nil and not exists) then
    -- no file yet (or no file API): the first run
    seed()
    save(path)
    return places, recent, warnings
  end
  -- every places file this module writes has a places list (empty: {})
  if not ok or type(data) ~= 'table' or type(data.places) ~= 'table' then
    local why = not ok and ('unreadable: ' .. tostring(data)) or 'not a places file'
    local copied = backup(path)
    noSave = not copied
    warning = 'Places file ' .. why .. (copied and ' (a copy is in ' .. path .. '.bak)'
      or ' (no copy could be kept, so changes are not saved this session)') .. '; starting empty'
    log('W', 'beamngEarth', 'places: ' .. warning)
    return places, recent, { warning }
  end
  local used = {}
  for _, p in ipairs(data.places or {}) do
    local lat, lon = validLatLon(type(p) == 'table' and p.lat, type(p) == 'table' and p.lon)
    local name = type(p) == 'table' and cleanName(p.name)
    if lat and name then
      local id = tonumber(type(p) == 'table' and p.id)
      if not id or used[id] or id ~= math.floor(id) or id < 1 or id >= M.ID_MAX then id = nil end
      places[#places + 1] = { id = id, name = name, lat = lat, lon = lon }
      if id then used[id] = true; nextId = math.max(nextId, id + 1) end
    else
      warnings[#warnings + 1] = 'a place without a name or a valid lat/lon was skipped'
    end
  end
  local n = tonumber(data.nextId)
  if n and n >= 1 and n < M.ID_MAX then nextId = math.max(nextId, math.floor(n)) end
  for _, p in ipairs(places) do
    if not p.id then p.id = nextId; nextId = nextId + 1 end
  end
  -- an earlier first-run place still as it was: to its new spot (M.OLD_SEED)
  local moved = false
  for _, p in ipairs(places) do
    for _, o in ipairs(M.OLD_SEED) do
      local s = o.to and seedNamed(o.to)
      if s and p.name == o.name and math.abs(p.lat - o.lat) < 1e-6 and math.abs(p.lon - o.lon) < 1e-6 then
        p.name, p.lat, p.lon = s.name, s.lat, s.lon
        moved = true
      end
    end
  end
  for _, r in ipairs(type(data.recent) == 'table' and data.recent or {}) do
    local lat, lon = validLatLon(type(r) == 'table' and r.lat, type(r) == 'table' and r.lon)
    if lat and #recent < M.RECENT_MAX then
      recent[#recent + 1] = { lat = lat, lon = lon, name = cleanName(r.name),
        kind = r.kind == 'start' and 'start' or 'teleport' }
    end
  end
  if moved and #warnings == 0 then save(path) end
  if #warnings > 0 then
    -- the next save drops them: keep the file as it was first
    local copied = backup(path)
    noSave = not copied
    warning = warnings[1] .. (#warnings > 1 and (' (' .. #warnings .. ' entries)') or '')
      .. (copied and '; the file as it was is in ' .. path .. '.bak'
        or '; no copy could be kept, so changes are not saved this session')
    log('W', 'beamngEarth', 'places: ' .. warning)
  end
  return places, recent, warnings
end

local function ensure() if not loaded then M.load() end end

------------------------------------------------------------------------------------------
-- the list

function M.list() ensure(); return places end
function M.recentList() ensure(); return recent end

function M.find(id)
  ensure()
  id = tonumber(id)
  for i, p in ipairs(places) do if p.id == id then return p, i end end
  return nil
end

function M.add(name, lat, lon)
  ensure()
  lat, lon = validLatLon(lat, lon)
  if not lat then return nil, 'latitude must be -90..90 and longitude -180..180' end
  name = cleanName(name) or M.coordText(lat, lon)
  local p = { id = nextId, name = name, lat = round6(lat), lon = round6(lon) }
  nextId = nextId + 1
  places[#places + 1] = p
  save()
  return p
end

function M.rename(id, name)
  local p = M.find(id)
  if not p then return nil, 'no such place' end
  name = cleanName(name)
  if not name then return nil, 'type a name' end
  p.name = name
  save()
  return p
end

function M.remove(id)
  local p, i = M.find(id)
  if not p then return nil, 'no such place' end
  table.remove(places, i)
  save()
  return p
end

-- Recorded by beamngEarth_core.teleport for every teleport it sends (kind 'teleport') and for the
-- level start's own (kind 'start'). The newest is first; an older one within DEDUP_M goes (its
-- name stays when the new one has none).
function M.noteTeleport(lat, lon, name, kind)
  ensure()
  lat, lon = validLatLon(lat, lon)
  if not lat then return end
  local e = { lat = round6(lat), lon = round6(lon), name = cleanName(name), kind = kind == 'start' and 'start' or 'teleport' }
  for i = #recent, 1, -1 do
    local r = recent[i]
    if distance(r.lat, r.lon, e.lat, e.lon) <= M.DEDUP_M then
      e.name = e.name or r.name
      table.remove(recent, i)
    end
  end
  table.insert(recent, 1, e)
  while #recent > M.RECENT_MAX do recent[#recent] = nil end
  save()
  M.notePosition(e.lat, e.lon, nil, true)      -- (where the car goes now)
end

------------------------------------------------------------------------------------------
-- where the car was last ("Where you left off": the user, 2026-10-25, "make sure it saves on game close and save
-- periodicly in case the game crashes or force closes"). The recent places are where teleports went; driving 30 miles
-- from there and restarting put the car back at the teleport. Its own small file (the places file's save also redoes
-- the level select's spawn points): written every POS_EVERY_S at most, once the car has moved POS_MOVE_M, and at once
-- on a teleport, at level end and game close (force).
M.POS_FILE = '/settings/beamngEarth_lastpos.json'
M.POS_EVERY_S = 60
M.POS_MOVE_M = 100
local lastPos = nil         -- { lat, lon } as last read or written
local posRead = false
local posWrittenAt = -math.huge

function M.lastPosition()
  if not posRead then
    posRead = true
    local read = rawget(_G, 'jsonReadFile')
    local ok, data = pcall(function() return read and read(M.POS_FILE) end)
    if ok and type(data) == 'table' then
      local lat, lon = validLatLon(data.lat, data.lon)
      if lat then lastPos = { lat = lat, lon = lon } end
    end
  end
  return lastPos
end

-- The car at (lat, lon) now (clock: seconds, any monotonic clock); force: write whatever the time and distance.
-- Returns whether it wrote.
function M.notePosition(lat, lon, clock, force)
  lat, lon = validLatLon(lat, lon)
  if not lat then return false end
  local prev = M.lastPosition()
  if not force then
    if (clock or 0) - posWrittenAt < M.POS_EVERY_S then return false end
    if prev and distance(prev.lat, prev.lon, lat, lon) < M.POS_MOVE_M then return false end
  end
  local write = rawget(_G, 'jsonWriteFile')
  if not write then return false end
  local e = { lat = round6(lat), lon = round6(lon) }
  local ok, err = pcall(write, M.POS_FILE, e, true)
  if not ok or err == false then return false end
  lastPos, posWrittenAt = e, clock or posWrittenAt
  return true
end

-- The name shown for a recent place: its own, else a saved place's within DEDUP_M, else nil.
function M.recentName(r)
  if r.name then return r.name end
  for _, p in ipairs(M.list()) do
    if distance(p.lat, p.lon, r.lat, r.lon) <= M.DEDUP_M then return p.name end
  end
  return nil
end

------------------------------------------------------------------------------------------
-- going there

-- Why a teleport cannot be sent now (nil: it can).
function M.cannotGo()
  local core = rawget(_G, 'beamngEarth_core')
  if not core or not core.teleport then return 'BeamNG.Earth is not loaded' end
  if not net.isConnected() then return 'Companion server not connected' end
  -- (no car needed: with none the free camera goes there, core.placePlayer; the user, 2026-10-28: "teleport to
  -- locations without being in a vehicle". The car stood in for a level being loaded)
  local st = core._state and core._state() or nil
  if not (st and st.inOurLevel) then return 'Load the BeamNG.Earth level first' end
  -- (the ground where the player is loaded first: a teleport under way, being set down; core.groundReady)
  if core.groundReady and not core.groundReady() then return 'Loading the ground: wait a moment' end
  return nil
end

function M.goTo(lat, lon, name)
  lat, lon = validLatLon(lat, lon)
  if not lat then return false, 'latitude must be -90..90 and longitude -180..180' end
  local why = M.cannotGo()
  if why then return false, why end
  local core = rawget(_G, 'beamngEarth_core')
  if not core.teleport(lat, lon, cleanName(name)) then return false, 'teleport refused (see the status line)' end
  return true
end

function M.go(id)
  local p = M.find(id)
  if not p then return false, 'no such place' end
  return M.goTo(p.lat, p.lon, p.name)
end

-- The player's position now (core's last pose), or nil.
function M.here()
  local core = rawget(_G, 'beamngEarth_core')
  local st = core and core._state and core._state() or nil
  local pl = st and st.player
  if pl and pl.lat and pl.lon then return pl.lat, pl.lon end
  return nil
end

------------------------------------------------------------------------------------------
-- the settings page (through settings.lua's uiPlace* functions)

function M.uiState(res)
  ensure()
  local out = res or { ok = true }
  out.places, out.recent = {}, {}
  for i, p in ipairs(places) do
    out.places[i] = { id = p.id, name = p.name, lat = p.lat, lon = p.lon, text = M.coordText(p.lat, p.lon) }
  end
  for i, r in ipairs(recent) do
    out.recent[i] = { index = i, lat = r.lat, lon = r.lon, name = M.recentName(r), kind = r.kind,
      text = M.coordText(r.lat, r.lon) }
  end
  out.cannotGo = M.cannotGo()
  local lat, lon = M.here()
  out.here = lat and M.coordText(lat, lon) or nil
  out.warning = warning
  return out
end

local function result(ok, err) return M.uiState({ ok = ok and true or false, error = (not ok) and err or nil }) end

function M.uiAddHere(name)
  local lat, lon = M.here()
  if not lat then return result(false, 'Position unknown (not in a BeamNG.Earth world yet)') end
  return result(M.add(name, lat, lon))
end

function M.uiAdd(name, text)
  local lat, lon = M.parseLatLon(text)
  if not lat then return result(false, lon) end
  return result(M.add(name, lat, lon))
end

function M.uiRename(id, name) return result(M.rename(id, name)) end
function M.uiDelete(id) return result(M.remove(id)) end
function M.uiGo(id) return result(M.go(id)) end

function M.uiGoText(text)
  local lat, lon = M.parseLatLon(text)
  if not lat then return result(false, lon) end
  return result(M.goTo(lat, lon))
end

-- lat, lon: the row the page showed. Its index is only a hint: a Go moves its place to the front
-- (a double click, or a level start between the poll and the click, would go elsewhere).
function M.uiGoRecent(index, lat, lon)
  ensure()
  local r = recent[tonumber(index) or 0]
  lat, lon = validLatLon(lat, lon)
  if lat then
    if not (r and distance(r.lat, r.lon, lat, lon) < 1) then
      r = nil
      for _, x in ipairs(recent) do
        if distance(x.lat, x.lon, lat, lon) < 1 then r = x; break end
      end
    end
    if not r then return result(M.goTo(lat, lon)) end -- gone from the list since: still the row's place
  end
  if not r then return result(false, 'no such recent place') end
  return result(M.goTo(r.lat, r.lon, r.name))
end

function M._reset() places, recent, nextId, loaded, warning, noSave = {}, {}, 1, false, nil, false end -- tests

return M
