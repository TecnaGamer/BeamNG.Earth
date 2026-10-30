-- luajit mod/tests/test_places.lua  (saved and recent places, places.lua; docs/ROADMAP.md quick win 1)
-- The seed on the first run only, the file round trip, a corrupt file (warned, kept as .bak, empty
-- list), add / rename / delete, recent places (newest first, 200 m dedup, cap), lat/lon parsing
-- (Google Maps' "lat, lon", N/S/E/W, degrees-minutes-seconds), "go" through beamngEarth_core.teleport
-- (only with a connected server, in the BeamNG.Earth level; no car needed), core recording its teleports, and the
-- settings page's bridge.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local net = require('beamngEarth/net')
local places = require('beamngEarth/places')
local settings = require('beamngEarth/settings')

-- in-memory files (jsonReadFile / jsonWriteFile, and readFile / writeFile for the .bak copy)
local files, raw, writes = {}, {}, 0
local function deepcopy(v)
  if type(v) ~= 'table' then return v end
  local c = {}
  for k, x in pairs(v) do c[k] = deepcopy(x) end
  return c
end
function jsonReadFile(path)
  local f = files[path]
  if f == 'THROW' then error('broken json') end
  if f == 'NIL' then return nil end -- the engine's jsonReadFile on a file it cannot parse
  return deepcopy(f)
end
function jsonWriteFile(path, data, pretty)
  writes = writes + 1
  files[path] = deepcopy(data)
  return true
end
function readFile(path) return raw[path] end
function writeFile(path, text) raw[path] = text; return true end
FS = { fileExists = function(_, p) return files[p] ~= nil end }
local warnings = {}
function log(level, _, msg) if level == 'W' then warnings[#warnings + 1] = msg end end

local F = places.FILE

------------------------------------------------------------------------------------------
-- first run: the seed, written at once; not again once the file exists

local list = places.list() -- lazy load
T.check(#list == #places.SEED and #list >= 12, 'first run: the seed list (' .. #list .. ')')
T.check(list[1].name:match('Fells Point') and list[2].name:match('Whitney') and list[2].lat == 36.596718
  and list[2].lon == -118.169991, 'seed: Fells Point, then Mount Whitney on its road (not the summit)')
do
  local seedNames, ok = {}, true
  for _, s in ipairs(places.SEED) do seedNames[s.name] = true end
  for _, o in ipairs(places.OLD_SEED) do if o.to ~= false and not seedNames[o.to] then ok = false end end
  T.check(ok, 'every earlier seed place has its new spot (or none: left as it is)')
end
local names = {}
for _, p in ipairs(list) do names[#names + 1] = p.name end
names = table.concat(names, '|')
for _, want in ipairs({ 'Kern River', 'Monument Valley mesas', 'Utah state line', 'El Capitan',
  'Pikes Peak', 'Fuji', 'Everest', 'Blue Mountains', 'Pécs' }) do
  T.check(names:find(want, 1, true) ~= nil, 'seed has ' .. want)
end
T.check(not names:find('Kayenta', 1, true), 'seed: no solar plant (the user, 2026-10-21)')
local ids = {}
for _, p in ipairs(list) do ids[p.id] = (ids[p.id] or 0) + 1 end
local unique = true
for _, n in pairs(ids) do if n > 1 then unique = false end end
T.check(unique, 'seed: unique ids')
T.check(type(files[F]) == 'table' and #files[F].places == #list and files[F].version == places.VERSION,
  'first run: the seed is written to the places file')
T.check(#places.recentList() == 0 and #warnings == 0, 'first run: no recent places, no warnings')
-- the player deletes everything: no re-seed on the next start
for i = #list, 1, -1 do places.remove(list[i].id) end
T.check(#places.list() == 0, 'delete every place')
places.load()
T.check(#places.list() == 0, 'an emptied list stays empty (seed on the first run only)')
-- an empty list from the engine's encoder comes back as {} (an object): still empty, no warning
files[F] = { version = 1, places = {}, recent = {}, nextId = 20 }
places.load()
T.check(#places.list() == 0 and #warnings == 0, 'empty lists load without a warning')

------------------------------------------------------------------------------------------
-- add / rename / delete and the round trip

places._reset()
files[F] = nil
places.load()
local n0 = #places.list()
local p = places.add('  My  spot ', 36.1234567, -118.7654321)
T.check(p and p.name == 'My  spot' and p.lat == 36.123457 and p.lon == -118.765432, 'add: name trimmed, lat/lon rounded to 1e-6')
local q = places.add('', 10, 20)
T.check(q and q.name == '10.00000, 20.00000', 'add without a name: named by its lat/lon')
T.check(q.id ~= p.id and q.id > p.id, 'add: a new id')
T.check(places.add('bad', 91, 0) == nil and places.add('bad', 0, 181) == nil and places.add('bad', 'x', 0) == nil,
  'add: invalid lat/lon refused')
local long = places.add(string.rep('x', 100), 1, 1)
T.check(#long.name == places.NAME_MAX, 'add: long names cut to NAME_MAX')
local utf = places.add(string.rep('a', 59) .. 'é', 1, 1)
T.check(utf.name == string.rep('a', 59), 'add: a cut never leaves half a UTF-8 character')
places.remove(long.id); places.remove(utf.id)
T.check(places.rename(p.id, 'Renamed') and places.find(p.id).name == 'Renamed', 'rename')
T.check(places.rename(p.id, '   ') == nil and places.find(p.id).name == 'Renamed', 'rename to blank refused')
T.check(places.rename(99999, 'x') == nil, 'rename an unknown id refused')
T.check(places.find(tostring(p.id)) == places.find(p.id), 'ids from the page may come as strings')
places.noteTeleport(36.5785, -118.2923, nil, 'start')
local savedFile = deepcopy(files[F])
places._reset()
places.load()
local l2 = places.list()
T.check(#l2 == n0 + 2 and places.find(p.id).name == 'Renamed' and places.find(q.id).lat == 10, 'round trip: places')
T.check(#places.recentList() == 1 and places.recentList()[1].kind == 'start' and places.recentList()[1].lat == 36.5785,
  'round trip: recent places')
local r = places.add('After reload', 1, 2)
T.check(r.id > q.id, 'round trip: ids keep counting after a reload')
T.check(places.remove(q.id) and places.find(q.id) == nil and #places.list() == n0 + 2, 'delete')
T.check(places.remove(q.id) == nil, 'delete twice: refused')
-- broken entries are skipped (one warning), the rest loads; duplicate / missing ids get new ones
warnings = {}
files[F] = { version = 1, nextId = 3, places = {
  { id = 1, name = 'A', lat = 1, lon = 2 }, { id = 1, name = 'B', lat = 3, lon = 4 }, { name = 'C', lat = 5, lon = 6 },
  { id = 7, name = 'no lat', lon = 1 }, { id = 8, name = '', lat = 1, lon = 1 }, 'junk', { id = 9, name = 'far', lat = 95, lon = 0 } },
  recent = { { lat = 1, lon = 2, kind = 'start' }, { lat = 'x' }, 7 } }
local pl, rc, w = places.load()
T.check(#pl == 3 and pl[1].id == 1 and pl[2].id ~= 1 and pl[3].id ~= pl[2].id and pl[2].id >= 3, 'load: duplicate / missing ids renumbered')
T.check(#w == 4 and #warnings == 1, 'load: 4 broken places skipped with one warning (' .. #w .. ', ' .. #warnings .. ')')
T.check(#rc == 1 and rc[1].kind == 'start', 'load: broken recent places skipped')
T.check(places.uiState().warning ~= nil, 'the warning reaches the page')

------------------------------------------------------------------------------------------
-- a corrupt file: one warning, a copy, an empty list (never the seed)

for _, bad in ipairs({ 'THROW', 'NIL', 42, { places = 'nope' } }) do
  places._reset()
  warnings, raw = {}, { [F] = '{ "places": [ broken' }
  files[F] = bad
  local pl2 = places.load()
  T.check(#pl2 == 0 and #places.recentList() == 0, 'corrupt file (' .. tostring(bad) .. '): starts empty, no seed')
  T.check(#warnings == 1 and warnings[1]:match('starting empty'), 'corrupt file: one warning')
  T.check(raw[F .. '.bak'] == '{ "places": [ broken', 'corrupt file: its text kept in .bak')
  places.list(); places.recentList(); places.uiState()
  T.check(#warnings == 1, 'corrupt file: warned once (not on every call)')
end
places.add('New', 1, 1)
T.check(#files[F].places == 1, 'after a corrupt file: adding writes a valid file')

------------------------------------------------------------------------------------------
-- recent places: newest first, one per 200 m, at most RECENT_MAX

places._reset()
files[F] = { version = 1, places = { { id = 1, name = 'Whitney', lat = 36.5785, lon = -118.2923 } }, recent = {} }
places.load()
places.noteTeleport(36.5785, -118.2923, nil, 'start')
places.noteTeleport(39.2834, -76.5934, 'Fells', 'teleport')
local rl = places.recentList()
T.check(#rl == 2 and rl[1].name == 'Fells' and rl[2].kind == 'start', 'recent: newest first')
places.noteTeleport(36.5785 + 0.0015, -118.2923, nil, 'teleport') -- ~167 m north
rl = places.recentList()
T.check(#rl == 2 and rl[1].lat == 36.58 and rl[1].kind == 'teleport' and rl[2].name == 'Fells',
  'recent: within 200 m replaces the older one and moves to the front')
places.noteTeleport(39.2834 + 0.0019, -76.5934, nil, 'teleport') -- ~211 m north
T.check(#places.recentList() == 3, 'recent: beyond 200 m is another place')
places.noteTeleport(39.2834 + 0.0001, -76.5934, nil, 'teleport')
rl = places.recentList()
T.check(rl[1].name == 'Fells', 'recent: a nameless revisit keeps the old name')
local ui0 = places.uiState()
T.check(ui0.recent[3].name == 'Whitney' and ui0.recent[3].text == '36.58000, -118.29230',
  'recent: an unnamed entry shows the saved place within 200 m')
for i = 1, 15 do places.noteTeleport(-40 + i, 100 + i, nil, 'teleport') end
rl = places.recentList()
T.check(#rl == places.RECENT_MAX and places.RECENT_MAX == 10, 'recent: capped at 10')
T.check(rl[1].lat == -25 and rl[10].lat == -34, 'recent: the oldest dropped')
T.check(#files[F].recent == 10, 'recent: saved')
places.noteTeleport(100, 0)
T.check(places.recentList()[1].lat == -25, 'recent: an invalid lat/lon is not recorded')
T.check(math.abs(places.distance(0, 0, 0, 1) - 111195) < 5, 'distance: 1 degree of the equator')

------------------------------------------------------------------------------------------
-- lat / lon parsing

local function parses(text, lat, lon)
  local a, b = places.parseLatLon(text)
  T.check(a and b and math.abs(a - lat) < 1e-6 and math.abs(b - lon) < 1e-6,
    string.format('parse %q -> %s, %s (got %s, %s)', text, lat, lon, tostring(a), tostring(b)))
end
local function refuses(text)
  local a, err = places.parseLatLon(text)
  T.check(a == nil and type(err) == 'string', string.format('parse %q refused (%s)', tostring(text), tostring(err)))
end
parses('36.5785, -118.2923', 36.5785, -118.2923)
parses('36.5785,-118.2923', 36.5785, -118.2923)
parses('  36.5785   -118.2923  ', 36.5785, -118.2923)
parses('-33.732, 150.3115', -33.732, 150.3115)
parses('+36.5, +118.2', 36.5, 118.2)
parses('36.5785 N, 118.2923 W', 36.5785, -118.2923)
parses('36.5785N 118.2923W', 36.5785, -118.2923)
parses('N 36.5785 W 118.2923', 36.5785, -118.2923)
parses('N36.5785, W118.2923', 36.5785, -118.2923)
parses('33.732 s 150.3115 e', -33.732, 150.3115)
parses('36.5785° N, 118.2923° W', 36.5785, -118.2923)
parses('118.2923 W, 36.5785 N', 36.5785, -118.2923)
parses('36°34\'42.6"N 118°17\'32.3"W', 36 + 34 / 60 + 42.6 / 3600, -(118 + 17 / 60 + 32.3 / 3600))
parses('36° 34′ 42.6″ N, 118° 17′ 32.3″ W', 36 + 34 / 60 + 42.6 / 3600, -(118 + 17 / 60 + 32.3 / 3600))
parses('36 34.71 N 118 17.538 W', 36 + 34.71 / 60, -(118 + 17.538 / 60))
parses('36 34 42.6 -118 17 32.3', 36 + 34 / 60 + 42.6 / 3600, -(118 + 17 / 60 + 32.3 / 3600))
parses('0, 0', 0, 0)
parses('90, 180', 90, 180)
parses('36.5785,\194\160-118.2923', 36.5785, -118.2923)
refuses('')
refuses(nil)
refuses('36.5785')
refuses('36.5785, -118.2923, 12')
refuses('91, 0')
refuses('0, 181')
refuses('abc, def')
refuses('Mount Whitney')
refuses('36.5 N, 37.5 S')
refuses('-36.5 N, 118 W')
refuses('36 75 N, 118 W')
refuses('36.5 30 N, 118 W')
refuses('1 2 3')

------------------------------------------------------------------------------------------
-- go: through beamngEarth_core.teleport, only with a server and the BeamNG.Earth level; no car needed (core puts the
-- free camera there)

places._reset()
files[F] = { version = 1, places = { { id = 5, name = 'Kern River ravine', lat = 36.506422, lon = -118.402437 } } }
places.load()
local tele = {}
local coreSt = { inOurLevel = false }
beamngEarth_core = { teleport = function(lat, lon, name) tele[#tele + 1] = { lat, lon, name }; return true end,
  _state = function() return coreSt end }
function getPlayerVehicle() return nil end
local okGo, err = places.go(5)
T.check(not okGo and err:match('not connected') and #tele == 0, 'go: refused while the server is not connected')
local ui1 = places.uiState()
T.check(ui1.cannotGo and ui1.cannotGo:match('not connected'), 'page: the not-connected reason')
net.isConnected = function() return true end -- net keeps its state private
okGo, err = places.go(5)
T.check(not okGo and err:match('BeamNG.Earth level') and #tele == 0, 'go: refused outside the BeamNG.Earth level')
T.check(places.uiState().cannotGo == 'Load the BeamNG.Earth level first', 'page: the not-in-the-level reason')
coreSt.inOurLevel = true
okGo = places.go(5)
T.check(okGo and #tele == 1 and tele[1][1] == 36.506422 and tele[1][2] == -118.402437 and tele[1][3] == 'Kern River ravine',
  'go: beamngEarth_core.teleport(lat, lon, name) with the place\'s values, with no car')
function getPlayerVehicle() return { id = 1 } end
T.check(places.cannotGo() == nil, 'go: with a car too')
beamngEarth_core.groundReady = function() return false end
okGo, err = places.go(5)
T.check(not okGo and err == 'Loading the ground: wait a moment' and #tele == 1, 'go: refused while the ground is loading')
beamngEarth_core.groundReady = function() return true end
T.check(places.cannotGo() == nil, 'go: once the ground is in')
beamngEarth_core.groundReady = nil
T.check(places.uiState().cannotGo == nil, 'page: can go')
okGo, err = places.go(6)
T.check(not okGo and err:match('no such') and #tele == 1, 'go: unknown id')
local res = places.uiGoText('36.5785, -118.2923')
T.check(res.ok and #tele == 2 and tele[2][1] == 36.5785 and tele[2][2] == -118.2923 and tele[2][3] == nil, 'go to typed lat, lon')
res = places.uiGoText('nowhere')
T.check(not res.ok and res.error and #tele == 2, 'go to unparsable text: an error, no teleport')
beamngEarth_core.teleport = function() return false end
okGo, err = places.go(5)
T.check(not okGo and err:match('refused'), 'go: a refused teleport is reported')

------------------------------------------------------------------------------------------
-- core records its teleports (and the level start's) in the recent places

places._reset()
files[F] = { version = 1, places = {} }
places.load()
-- recorded by the module function core calls; core itself is tested in test_core_stubbed
places.noteTeleport(36.506422, -118.402437, 'Kern River ravine', 'teleport')
T.check(places.recentList()[1].name == 'Kern River ravine', 'recent: the name passed through teleport')
res = places.uiGoRecent(1)
T.check(not res.ok and res.error:match('refused'), 'go recent: through the same checks and teleport')
beamngEarth_core.teleport = function(lat, lon, name) tele[#tele + 1] = { lat, lon, name }; return true end
res = places.uiGoRecent(1)
T.check(res.ok and tele[#tele][1] == 36.506422 and tele[#tele][3] == 'Kern River ravine', 'go recent: its lat/lon and name')
T.check(not places.uiGoRecent(5).ok, 'go recent: unknown index')

------------------------------------------------------------------------------------------
-- the settings page's bridge (beamngEarth_settings.uiPlace*)

places._reset()
files[F] = { version = 1, places = {} }
-- as core does: a teleport it sends is a recent place
beamngEarth_core.teleport = function(lat, lon, name)
  tele[#tele + 1] = { lat, lon, name }
  places.noteTeleport(lat, lon, name, 'teleport')
  return true
end
local st = settings.uiPlaces()
T.check(st.ok and #st.places == 0 and #st.recent == 0 and st.here == nil, 'uiPlaces: lists, no position yet')
res = settings.uiPlaceAddHere('Here')
T.check(not res.ok and res.error:match('Position unknown') and #res.places == 0, 'save current place: refused without a position')
beamngEarth_core._state = function() return { inOurLevel = true, player = { lat = 46.5286, lon = 10.4532 } } end
st = settings.uiPlaces()
T.check(st.here == '46.52860, 10.45320', 'uiPlaces: the current position')
res = settings.uiPlaceAddHere('Stelvio')
T.check(res.ok and #res.places == 1 and res.places[1].name == 'Stelvio' and res.places[1].lat == 46.5286, 'save current place')
res = settings.uiPlaceAdd('Typed', '28.1414 N, 86.8514 E')
T.check(res.ok and #res.places == 2 and res.places[2].lat == 28.1414 and res.places[2].lon == 86.8514, 'save a typed place')
res = settings.uiPlaceAdd('Typed', '28.1414')
T.check(not res.ok and res.error and #res.places == 2, 'save a typed place: bad text refused')
local id2 = res.places[2].id
res = settings.uiPlaceRename(id2, 'Everest')
T.check(res.ok and res.places[2].name == 'Everest', 'rename from the page')
res = settings.uiPlaceGo(id2)
T.check(res.ok and tele[#tele][1] == 28.1414 and tele[#tele][3] == 'Everest', 'go from the page')
res = settings.uiPlaceDelete(id2)
T.check(res.ok and #res.places == 1, 'delete from the page')
res = settings.uiPlaceGoText('36.5785, -118.2923')
T.check(res.ok and tele[#tele][1] == 36.5785 and res.recent[1].lat == 36.5785, 'go to typed text from the page (now a recent place)')
res = settings.uiPlaceGoRecent(2)
T.check(res.ok and tele[#tele][1] == 28.1414 and res.recent[1].name == 'Everest', 'go to a recent place from the page')
T.check(files[F].places[1].name == 'Stelvio', 'page changes are saved')
local realGoRecent = places.uiGoRecent
places.uiGoRecent = function() error('boom') end
res = settings.uiPlaceGoRecent(1)
T.check(res and res.ok == false and res.error:match('boom'), 'bridge: an error inside answers ok = false (never nothing)')
places.uiGoRecent = realGoRecent

-- review (2026-10-11): the page's row index goes stale once a Go moves its place to the front (a
-- double click, or a level start between the poll and the click): the row's own lat/lon decide
places._reset()
files[F] = { version = 1, places = {} }
places.load()
places.noteTeleport(10, 10, 'C', 'teleport'); places.noteTeleport(20, 20, 'B', 'teleport'); places.noteTeleport(30, 30, 'A', 'teleport')
res = settings.uiPlaceGoRecent(3, 10, 10) -- the row "C" (third)
T.check(res.ok and tele[#tele][1] == 10 and res.recent[1].name == 'C', 'go recent: the row the page showed')
res = settings.uiPlaceGoRecent(3, 10, 10) -- the same click again, before the page re-rendered
T.check(res.ok and tele[#tele][1] == 10 and tele[#tele][3] == 'C', 'go recent: a stale index still goes to the row\'s place (got '
  .. tostring(tele[#tele][1]) .. ')')
res = settings.uiPlaceGoRecent(9, 20, 20)
T.check(res.ok and tele[#tele][1] == 20 and tele[#tele][3] == 'B', 'go recent: an index past the end, found by its lat/lon')
res = settings.uiPlaceGoRecent(1, 'nan', 0)
T.check(res.ok and tele[#tele][1] == 20, 'go recent: no usable lat/lon from the page, the index alone (as before)')

-- review: never overwrite a file that could not be read when no copy of it could be kept (a
-- transient read failure would otherwise lose the whole list at the next level start)
places._reset()
warnings, raw, files[F] = {}, {}, 'NIL' -- exists, jsonReadFile fails, readFile fails too: no .bak
places.load()
T.check(#warnings == 1 and warnings[1]:match('not saved'), 'unreadable, no copy: the warning says changes are not saved')
places.noteTeleport(1, 1, nil, 'start'); places.add('X', 2, 2)
T.check(files[F] == 'NIL' and raw[F .. '.bak'] == nil, 'unreadable, no copy: the file is never overwritten')
T.check(#places.list() == 1 and #places.recentList() == 1, 'unreadable, no copy: the session still works in memory')
T.check(places.uiState().warning:match('not saved'), 'unreadable, no copy: the page says so')
places.load() -- next session: the file reads again
T.check(files[F] == 'NIL', 'unreadable, no copy: a reload does not write either')
places._reset(); files[F] = { version = 1, places = {} }; places.load(); places.add('Y', 3, 3)
T.check(type(files[F]) == 'table' and #files[F].places == 1, 'a readable file is written again after a reset')

-- review: JSON that is not a places file (no places list: an array, another file's content) is
-- kept as a corrupt one, never silently overwritten by an empty list
for _, other in ipairs({ { { id = 1, name = 'A', lat = 1, lon = 2 } }, { version = 1, nextId = 4 }, { places = {}, recent = 'x' } }) do
  places._reset()
  warnings, raw = {}, { [F] = 'the text' }
  files[F] = other
  places.load()
  local isPlaces = other.places ~= nil
  if isPlaces then
    T.check(#warnings == 0, 'a places file with a broken recent list: no warning')
  else
    T.check(#warnings == 1 and raw[F .. '.bak'] == 'the text', 'not a places file: one warning and a .bak copy')
  end
end

-- review: places skipped on load are gone at the next save, so the file as it was is copied first
places._reset()
warnings, raw = {}, { [F] = 'with a typo' }
files[F] = { version = 1, places = { { id = 1, name = 'Good', lat = 1, lon = 1 }, { id = 2, name = 'Typo', lat = 95, lon = 1 } } }
places.load()
T.check(raw[F .. '.bak'] == 'with a typo' and #warnings == 1 and warnings[1]:match('%.bak'), 'skipped places: the file as it was is kept in .bak')
places.add('New', 1, 2)
T.check(#files[F].places == 2, 'skipped places: saved after the copy')
places._reset()
warnings, raw = {}, {} -- the copy fails: the file is left alone
files[F] = { version = 1, places = { { id = 1, name = 'Good', lat = 1, lon = 1 }, { id = 2, name = 'Typo', lat = 95, lon = 1 } } }
places.load()
places.add('New', 1, 2)
T.check(#files[F].places == 2 and files[F].places[2].name == 'Typo', 'skipped places, no copy: the file is not overwritten')

-- review: ids and nextId from a hand-edited file stay distinct integers (a huge nextId made
-- nextId + 1 == nextId, two places shared an id and Delete removed the wrong one)
places._reset()
warnings = {}
files[F] = { version = 1, nextId = 1e300, places = { { id = 1e300, name = 'Big', lat = 1, lon = 1 }, { id = 2.5, name = 'Half', lat = 2, lon = 2 } } }
places.load()
local x1, x2 = places.add('X1', 3, 3), places.add('X2', 4, 4)
local seen, distinct = {}, true
for _, pp in ipairs(places.list()) do
  if seen[pp.id] or pp.id ~= math.floor(pp.id) or pp.id >= 2 ^ 31 then distinct = false end
  seen[pp.id] = true
end
T.check(distinct and #places.list() == 4, 'huge / fractional ids: renumbered, all distinct small integers')
places.remove(x1.id)
T.check(places.find(x2.id).name == 'X2' and #places.list() == 3 and places.list()[1].name == 'Big', 'huge ids: Delete removes the right place')

-- an earlier first-run place still as it was moves to its new spot (on a road); an edited one stays
files[F] = { version = 1, nextId = 4, places = {
  { id = 1, name = 'Mount Whitney summit', lat = 36.5785, lon = -118.2923 },
  { id = 2, name = 'My summit', lat = 36.5785, lon = -118.2923 },
  { id = 3, name = 'Fells Point, Baltimore MD', lat = 39.2834, lon = -76.5934 },
  { id = 4, name = 'Kayenta PV Solar Plant, Arizona', lat = 36.7874, lon = -110.2382 },
  { id = 5, name = 'El Capitan Meadow, Yosemite', lat = 37.7225, lon = -119.6385 } }, recent = {} }
places.load()
local l2 = places.list()
T.check(l2[1].name == 'Mount Whitney, Whitney Portal Road' and l2[1].lat == 36.596718 and l2[1].id == 1,
  'old seed: moved to its road, its id kept')
T.check(l2[2].name == 'My summit' and l2[2].lat == 36.5785, 'renamed by the player: left as it is')
T.check(l2[3].lat == 39.283309 and files[F].places[3].lat == 39.283309, 'Fells Point off its building, and saved')
T.check(l2[4].name == 'Kayenta PV Solar Plant, Arizona' and l2[4].lat == 36.7874,
  'a place no longer in the first-run list: the player keeps it as it was')
T.check(l2[5].name == 'El Capitan Meadow, Yosemite' and l2[5].lat == 37.72098,
  'the places after it: each to its own new spot (by name, not by position)')

-- the level select's spawn points (levelinfo.lua): default, Random, then a place each
local li = require('beamngEarth/levelinfo')
local info = li.build(l2, function(key) return key == li.previewKey(l2[2].lat, l2[2].lon) end)
T.check(info.spawnPoints[1].objectname == li.DEFAULT and info.spawnPoints[2].objectname == li.RANDOM,
  'level info: the default start and Random first')
T.check(#info.spawnPoints == 2 + #l2 and info.spawnPoints[3].objectname == li.PREFIX .. l2[1].id
  and info.spawnPoints[3].translationId == l2[1].name, 'level info: a spawn point per place, by id, named')
T.check(info.spawnPoints[3].preview == li.PREVIEW and info.spawnPoints[4].preview:match('^places/'),
  'level info: a place\'s own picture when there is one, else the Earth')
T.check(info.size[1] * info.size[2] > 5.1e14 and info.size[1] * info.size[2] < 5.102e14,
  'level info: the Earth\'s surface')
T.check(li.placeId(li.PREFIX .. '12') == 12 and li.placeId(li.RANDOM) == nil and li.placeId(nil) == nil, 'placeId')
T.check(not li.previewKey(-33.731908, 150.312007):find('[-.]'), 'preview keys: file-name safe')

-- "Where you left off": the car's place, saved while driving (at most every POS_EVERY_S, once moved POS_MOVE_M), and
-- at once on a teleport, level end and game close (force)
local PF = places.POS_FILE
T.check(places.notePosition(40.0, -75.0, 1000, true) and files[PF].lat == 40.0, 'force: written at once')
T.check(not places.notePosition(40.01, -75.0, 1010), 'within POS_EVERY_S: not written')
T.check(not places.notePosition(40.0003, -75.0, 1100), 'moved under POS_MOVE_M: not written')
T.check(places.notePosition(40.01, -75.0, 1100) and files[PF].lat == 40.01, 'later and farther: written')
T.check(not places.notePosition(nil, -75.0, 2000, true), 'a bad position: not written')
places.noteTeleport(35.5, 139.7, 'x', 'teleport')
T.check(files[PF].lat == 35.5 and places.lastPosition().lon == 139.7, 'a teleport: written at once')

T.done('test_places')
