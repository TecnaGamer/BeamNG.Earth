-- luajit mod/tests/test_groundcover.lua  (grass by climate and season, groundcover.lua): the Koppen classes' profiles,
-- the months (the south's half a year later, the server's fixed season), every profile within the generic one's
-- element budget in every month (the block move's regeneration: the user, 2026-11-19, "performance should not
-- suffer, maybe even improve compared to dev"), every type a profile points at holds a plant, the objects only for
-- the layers a block lists, a new climate or month makes them again.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
jsonNull = jsonNull or {}

-- The stock definitions the profiles use (read from the game files 2026-11-19): maxElements, the plant types with data
local STOCK = {
  att = {
    Bush = { maxElements = 400000, types = { 0, 1, 2, 3, 4 } },
    grass_dry_2 = { maxElements = 76800, types = { 0, 1, 2, 3, 4, 5, 6, 7 } },
    weed1 = { maxElements = 240000, types = { 0, 1, 2, 3, 4, 5, 6 } },
  },
  cliff = {
    Buttercup1 = { maxElements = 100000, types = { 0, 1, 2, 3, 4 } },
  },
  eca = {
    Daisies = { maxElements = 40000, types = { 0, 1, 2, 3, 4 } },
    Purple_flowers = { maxElements = 100000, types = { 0, 1, 2, 3, 4 } },
    long_grass_clump_close = { maxElements = 1920, types = { 0, 1, 2, 3, 4, 5 } },
    long_grass_clump_close_03 = { maxElements = 48000, types = { 0, 1, 2, 3, 4, 5 } },
  },
  hirochi = {
    long_grass_distant = { maxElements = 192000, types = { 0, 1, 2, 3, 4, 5 } },
    small_grass_close = { maxElements = 306000, types = { 0, 1, 2, 3, 4, 5, 6 } },
  },
  industrial = {
    Nettles_01 = { maxElements = 100000, types = { 0, 1, 2, 3, 4 } },
    small_grass_dry_close = { maxElements = 30000, types = { 0, 1, 2, 3, 4, 5, 6 } },
    weed_03 = { maxElements = 100000, types = { 0, 1, 2, 3, 4 } },
    wet_weed_01 = { maxElements = 350000, types = { 0, 1, 2, 3, 4, 5, 6 } },
  },
  island = {
    spanish_bayonet = { maxElements = 40000, types = { 0, 1, 2, 3, 4 } },
  },
  italy = {
    flowers = { maxElements = 40000, types = { 0, 1, 2, 3, 4, 5, 6, 7 } },
    shrub_grass_02 = { maxElements = 28160, types = { 0, 1, 2, 3, 4, 5 } },
    shrubs = { maxElements = 492940, types = { 0, 1, 2 } },
  },
  jri = {
    Beach_grass = { maxElements = 400000, types = { 0, 1, 2, 3 } },
    Tall_grasses2 = { maxElements = 400000, types = { 0, 1, 2, 3 } },
    forest_plants = { maxElements = 640000, types = { 0, 1, 2, 3, 4 } },
  },
  jv = {
    dirt_grass_dry = { maxElements = 400000, types = { 0, 1, 2, 3, 4, 5, 6, 7 } },
    dirt_plant_dry = { maxElements = 80000, types = { 0, 1, 2, 3, 4, 5 } },
  },
  utah = {
    Dry_grass_clump = { maxElements = 300000, types = { 0, 1, 2, 3, 4, 5, 6, 7 } },
    plants = { maxElements = 100000, types = { 0, 1, 2, 3, 4, 5, 6 } },
  },
  crops = {     -- (the mod's own: art/beamngEarth/groundcover/crops.items.json)
    bngEarth_ripe_grain = { maxElements = 48000, types = { 0 } },
    bngEarth_stubble = { maxElements = 60000, types = { 0, 1, 2, 3 } },
  },
  wcusa = {
    grass_dry_1 = { maxElements = 256000, types = { 0, 1, 2, 3, 4, 5, 6, 7 } },
    mown_grass_close = { maxElements = 40000, types = { 0, 1, 2, 3, 4, 5, 6, 7 } },
    mown_grass_dry = { maxElements = 500000, types = { 0, 1, 2, 3, 4, 5, 6, 7 } },
    shrubs_near = { maxElements = 160000, types = { 0, 1, 2, 3, 4 } },
  },
}

-- the stock items files as one GroundCover line per definition (maxElements and 8 types, those with data sized)
local function defLine(name, d)
  local has, types = {}, {}
  for _, i in ipairs(d.types) do has[i] = true end
  for i = 0, 7 do
    types[#types + 1] = has[i] and '{"layer":"Grass","probability":1,"sizeMin":0.5}' or '{}'
  end
  return string.format('{"name":"%s","class":"GroundCover","material":"m","maxElements":%d,"radius":50,"Types":[%s]}',
    name, d.maxElements, table.concat(types, ','))
end
local gc = require('beamngEarth/groundcover')
local files = {}
for source, defs in pairs(STOCK) do
  local items = gc.SOURCES[source].items
  local first = type(items) == 'table' and items[1] or items
  local lines = {}
  for name, d in pairs(defs) do lines[#lines + 1] = defLine(name, d) end
  files[first] = table.concat(lines, '\n')
end
function readFile(f) return files[f] or '' end
function jsonDecode(s)
  local d = { name = s:match('"name":"([^"]+)"'), class = 'GroundCover', material = 'm',
              maxElements = tonumber(s:match('"maxElements":(%d+)')), radius = 50, Types = {} }
  for t in s:match('"Types":%[(.*)%]'):gmatch('{[^}]*}') do
    d.Types[#d.Types + 1] = t == '{}' and {} or { layer = 'Grass', probability = 1, sizeMin = 0.5 }
  end
  return d
end
local flushes = 0
function flushGroundCoverGrids() flushes = flushes + 1 end

-- Koppen -> profile
T.check(gc.profileOf(15) == 'temperate' and gc.profileOf(8) == 'mediterranean' and gc.profileOf(4) == 'desert_hot'
  and gc.profileOf(5) == 'desert_cold' and gc.profileOf(7) == 'steppe' and gc.profileOf(1) == 'tropical'
  and gc.profileOf(3) == 'savanna' and gc.profileOf(14) == 'subtropical' and gc.profileOf(26) == 'continental'
  and gc.profileOf(27) == 'boreal' and gc.profileOf(29) == 'tundra', 'Koppen classes to profiles')
T.check(gc.profileOf(nil, { 1, 1 }) == 'mediterranean' and gc.profileOf(nil, { 2, 1 }) == 'desert_hot'
  and gc.profileOf(0) == 'generic' and gc.profileOf(nil) == 'generic', 'old tiles by their ambience set; none generic')
for k = 1, 30 do T.check(gc.PROFILES[gc.profileOf(k)] ~= nil, 'class ' .. k .. ' has a profile') end

-- days: the south half a year on, the server's fixed season, weekly steps
local function near(a, b, tol) return math.abs(a - b) <= (tol or 4) end
T.check(near(gc.dayFor(196, 50), 196) and near(gc.dayFor(15, -35), 197) and near(gc.dayFor(350, -35), 167),
  'the south half a year on')
T.check(gc.dayFor(196, 50, 'fall') == gc.dayFor(288, 50) and gc.dayFor(100, -35, 'spring') == gc.dayFor(105, 50),
  "the server's fixed season")
T.check(gc.dayFor(190, 50) == gc.dayFor(191, 50), 'one profile day a week')
-- the fade: 1 inside, 0 outside, 0.5 on the edges, two windows that meet sum to 1
local jun_aug = { from = 152, to = 243 }
T.check(gc.weight(jun_aug, 196) == 1 and gc.weight(jun_aug, 20) == 0 and gc.weight(jun_aug, 152) == 0.5
  and gc.weight(jun_aug, 244) == 0.5, 'weights inside, outside, on the edges')
local oct_may, jun_sep = { from = 274, to = 151 }, { from = 152, to = 273 }
local sums = true
for d = 1, 365 do
  if math.abs(gc.weight(oct_may, d) + gc.weight(jun_sep, d) - 1) > 1e-9 then sums = false end
end
T.check(sums, 'a cross-fade sums to 1 every day')
T.check(gc.weight(oct_may, 10) == 1 and gc.weight(jun_sep, 10) == 0, 'a window over the new year')

-- every profile within the generic budget, every month; every entry's definition and pointed types exist
local generic = gc.budget(gc.plan('generic', 1), STOCK)
T.check(generic > 290000 and generic < 320000, 'generic budget as dev: ' .. generic)
local report = {}
for name, p in pairs(gc.PROFILES) do
  local ids = {}
  for _, e in ipairs(p) do
    local d = STOCK[e.source] and STOCK[e.source][e.src]
    T.check(d ~= nil, name .. ': ' .. tostring(e.source) .. '/' .. tostring(e.src) .. ' exists')
    local id = e.name or e.id or e.src
    T.check(not ids[id], name .. ': ' .. id .. ' once')
    ids[id] = true
    if d then
      local has = {}
      for _, i in ipairs(d.types) do has[i] = true end
      for i, t in pairs(e.types or {}) do
        T.check(has[i] or (t.p or 1) == 0, name .. '/' .. id .. ': type ' .. i .. ' holds a plant')
      end
    end
  end
  local worst, most = 0, 0
  for d = 1, 365, 2 do
    local pl = gc.plan(name, d)
    local b = gc.budget(pl, STOCK)
    worst, most = math.max(worst, b), math.max(most, #pl)
    if b > generic then T.check(false, string.format('%s day %d: %d elements over the generic %d', name, d, b, generic)) end
    if #pl > 16 then T.check(false, name .. ' day ' .. d .. ': ' .. #pl .. ' objects (at most 16; the wetland and beach ones only where a block has those layers)') end
  end
  report[#report + 1] = string.format('  %-13s at most %6d elements (%3.0f%% of generic), %2d objects', name, worst,
    100 * worst / generic, most)
end
table.sort(report)
print('budgets:\n' .. table.concat(report, '\n'))
-- the meadows flower in their months only
local function has(name, m, id)
  for _, e in ipairs(gc.plan(name, gc.dayOfMonth(m))) do if (e.id or e.src) == id then return true end end
  return false
end
T.check(has('temperate', 5, 'buttercups') and not has('temperate', 9, 'buttercups') and has('temperate', 7, 'flowers')
  and not has('temperate', 1, 'flowers'), 'temperate flowers in season')
T.check(has('temperate', 4, 'cropYoung') and has('temperate', 7, 'cropRipe') and has('temperate', 9, 'stubble')
  and not has('temperate', 1, 'cropYoung') and not has('temperate', 1, 'stubble'), 'fields: green, ripe, stubble, bare')
T.check(has('mediterranean', 5, 'cropRipe') and has('mediterranean', 7, 'stubble') and not has('tropical', 7, 'stubble'),
  "the Mediterranean's earlier harvest; no fields in the tropics' profile")
T.check(has('mediterranean', 4, 'flowers') and not has('mediterranean', 7, 'flowers') and has('mediterranean', 7, 'dry')
  and has('mediterranean', 1, 'small') and not has('mediterranean', 7, 'small'), 'Mediterranean: green spring, dry summer')

-- objects: only for the layers the block lists, more once a block lists more
local st
local function named(id)
  for _, n in ipairs(st.names) do if n == id then return true end end
  return false
end
gc.setMonth(7)
T.check(gc.setClimate(15), 'a climate')
gc.ensure({ { 'bngEarthGroundGrass', 'GRASS' }, { 'beamngEarthDirt', 'DIRT' } })
st = gc.status()
T.check(st.profile == 'temperate' and near(st.day, gc.dayOfMonth(7)), 'temperate in July')
T.check(named('small') and named('flowers') and not named('cattails') and not named('lawn') and not named('shrubs'),
  'grassland only: no wetland, lawn or scrub objects')
local n0 = st.objects
gc.ensure({ { 'bngEarthGroundWet', 'MUD' }, { 'beamngEarthDirt', 'DIRT' } })
st = gc.status()
T.check(st.objects > n0 and named('cattails') and named('small'), 'a block with wetland adds the cattails, keeps the rest')
T.check(scenetree.findObject(gc.PREFIX .. 'small') ~= nil, 'short grass made')
-- a type pointed at another layer: its layer and probability
local calls = gc.fieldsFor(jsonDecode(defLine('x', STOCK.hirochi.small_grass_close)), 'L', 1, 1,
  { [2] = { layer = 'F', p = 0.27 } })
local f2, n2 = {}, 0
for _, c in ipairs(calls) do
  if c[2] == 2 then f2[c[1]] = c[3]; if c[1] == 'layer' then n2 = n2 + 1 end end
end
T.check(f2.layer == 'F' and tonumber(f2.probability) == 0.27 and n2 == 1, 'type override: its layer and probability')
-- another climate: made again at once, for the latest block's layers
local fl = flushes
T.check(gc.setClimate(8) and gc.status().profile == 'mediterranean', 'Mediterranean')
st = gc.status()
T.check(flushes == fl + 1 and named('cattails') and named('dry') and not named('flowers') and not named('small'),
  "rebuilt at once: July's dry grass, the wetland block's cattails")
T.check(not gc.setClimate(9), 'Csb: the same profile, nothing made again')
gc.setMonth(4)
st = gc.status()
T.check(near(st.day, gc.dayOfMonth(4)) and named('small') and not named('dry2'), 'April: green')
gc.setMonth(nil)
gc.reset()
T.check(gc.status().profile == 'generic', 'a new level: generic until the tiles vote')

T.done('test_groundcover')
