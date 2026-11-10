-- luajit mod/tests/test_nightlights.lua  (night lighting, nightlights.lua): the road tiles' lamp lenses kept per tile,
-- the nearest within reach, moved by a rebase, gone with their tile; day / night from the stock environment.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
os.clockhp = function() return 0 end

local nl = require('beamngEarth/nightlights')
local groups = { 'streetlight', 'lamplens', 'pole' }
nl.onRows('1:0_0', { { 0, 0, 0, 0 }, { 1, 10, 0, 8 }, { 1, 50, 0, 8 }, { 2, 5, 5, 0 }, { 1, 400, 0, 8 } }, groups)
nl.onRows('1:1_0', { { 1, 20, 3, 8 } }, groups)
T.check(nl.status().lenses == 4, 'the lens rows only: ' .. nl.status().lenses)
local near = nl.nearest(0, 0, 150, 2)
T.check(#near == 2 and near[1][1] == 10 and near[2][1] == 20, 'the two nearest within reach, nearest first')
T.check(#nl.nearest(0, 0, 150, 10) == 3, 'the one 400 m away left out')
local ahead = nl.nearest(0, 0, 500, 10, -1, 0, 30)
T.check(#ahead == 2 and ahead[1][1] == 10, 'looking away: only those within reach all round: ' .. #ahead)
T.check(#nl.nearest(0, 0, 500, 10, 1, 0, 30) == 4, 'looking at them: out to the far reach')
nl.transform({ apply = function(_, x, y, z) return x + 100, y, z end })
T.check(nl.nearest(110, 0, 1, 1)[1] ~= nil and nl.nearest(10, 0, 1, 1)[1] == nil, 'a rebase moves them')
nl.dropKey('1:0_0')
T.check(nl.status().lenses == 1, 'its tile goes: its lenses')
nl.onRows('1:1_0', { { 0, 1, 1, 1 } }, { 'streetlight' })
T.check(nl.status().lenses == 0, 'a tile without lenses: none kept')

core_environment = { getLightState = function() return { isNight = true } end }
T.check(nl.isNight() == true, 'night from the stock environment')
core_environment = nil
T.check(nl.isNight() == nil, 'no environment (the menu): unknown')
-- Dense mapped areas retain the exact nearest lights without sorting every lens.
nl.reset()
local stress = {}
for i = 1000, 1, -1 do stress[#stress + 1] = { 0, i / 2, 0, 8 } end
nl.onRows('stress', stress, { 'lamplens' })
local sort, sorted = table.sort, 0
table.sort = function(rows, compare) sorted = #rows; return sort(rows, compare) end
local selected = nl.nearest(0, 0, 900, 7)
table.sort = sort
T.check(#selected == 7 and sorted == 7, 'candidate storage and sorting stay within the requested light count')
for i, lens in ipairs(selected) do T.near(lens[1], i / 2, 1e-8, 'dense-area selection remains nearest first') end
local providerCalls = 0
nl.providers.stress = function() providerCalls = providerCalls + 1; return {} end
T.check(#nl.nearest(0, 0, 900, 0) == 0 and providerCalls == 0, 'zero lights skips candidate and provider work')
nl.providers.stress = nil
nl.reset()

-- Mixed profiles survive a shared pool slot being reused.
nl.reset()
local clock = 100
os.clockhp = function() return clock end
core_camera = { getPosition = function() return vec3(0,0,0) end, getForward = function() return vec3(1,0,0) end }
core_environment = { getLightState = function() return { isNight = true } end }
nl.LIGHTS_MAX = 1
nl.onRows('warm', { {0, 4, 0, 4.67, 0} }, {'fr_lantern_light'})
T.check(nl.nearest(0,0,30,1)[1][5] == true, 'warm lantern profile retained')
nl.update()
local light
for _, o in pairs(S.objects) do if o.class == 'SpotLight' then light = o end end
T.check(light and light.fields.color == nl.LANTERN.color and light.fields.range == '20', 'lantern yellow and short range')
local id = light and light.id
nl.dropKey('warm')
nl.onRows('stock', { {0, 5, 0, 8, 0} }, {'lamplens'})
clock = clock + 1; nl.update()
T.check(light and light.id == id and light.fields.color == nl.LIGHT.color and light.fields.range == '36', 'same pool slot restores stock profile')
nl.onRows('stock', { {0, 5, 0, 8, 0} }, {'lamplens'})
clock = clock + 1; nl.update()
local light
for _,o in pairs(S.objects) do if o.class=='SpotLight' then light=o end end
local id=light and light.id
nl.dropKey('stock')
nl.onRows('high-mast', { {0, 5, 0, 17.90, 0} }, {'high_mast_light'})
clock = clock + 1; nl.update()
T.check(nl.nearest(0,0,30,1)[1][6] == true, 'high-mast lighting profile retained')
T.check(light and light.id == id and light.fields.range == '70' and light.fields.color == nl.HIGH_MAST.color,
        'same bounded light pool serves high-mast floodlighting')
nl.dropKey('high-mast')
nl.onRows('double-arm', { {0, 4.9, 0, 12.62, 0}, {0, -4.9, 0, 12.62, 180} }, {'lamplens'})
T.check(nl.status().lenses == 2, 'double-arm pole supplies two separately aimed lamps')
clock = clock + 1; nl.update()
T.check(light and light.fields.range == '36' and light.fields.color == nl.LIGHT.color,
        'high-mast pool slot restores the ordinary lamp profile')
core_environment.getLightState = function() return { isNight = false } end
clock = clock + 2; nl.update()
T.check(light and light.fields.isEnabled == 'false' and nl.status().errors == 0, 'day disables without errors')
-- The far glow's balls: each detail level's material lit at night, dimmer as the balls grow (tools/build_lamp_glow.py).
local shapes = dir .. '/../beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/'
local f = io.open(shapes .. 'bne_lamp_glow.dae'); local dae = f:read('*a'); f:close()
f = io.open(shapes .. 'lamps.materials.json'); local mats = f:read('*a'); f:close()
local n = 0
for name in dae:gmatch('target="#([%w_]+)"') do
  n = n + 1
  T.check(nl.MATERIALS[name] ~= nil and mats:find('"' .. name .. '"', 1, true) ~= nil,
          'glow material lit and defined: ' .. name)
end
T.check(n == #nl.GLOW_NITS + 1, 'a material per ball (and the speck, the lens): ' .. n)
for k = 2, #nl.GLOW_NITS do
  T.check(nl.GLOW_NITS[k] <= nl.GLOW_NITS[k - 1] and nl.GLOW_NITS[k] >= 25,
          'the far balls dimmer, as bright as floodlit stone at least')
end
nl.reset()

-- Late at night fewer windows lit, fewest by 5 am (after S-moreo's PR #916): each window material's map swapped
-- through the night's steps, one material per sheet, set only when its step changes.
T.check(nl.step(20) == 1 and nl.step(23) == 1 and nl.step(12) == 1 and nl.step(nil) == 1, 'the evening: all lit')
T.check(nl.step(23.5) == 2 and nl.step(0) == 2 and nl.step(2.9) == 2, 'from 23:15 fewer')
T.check(nl.step(3) == 3 and nl.step(5) == 3 and nl.step(6.4) == 3, 'from 3:00 fewest (5 am among them)')
T.check(nl.step(6.5) == 1 and nl.step(8) == 1, 'all lit again by morning')
local prev, fewer = 1, true
for h = 18, 29, 0.25 do                               -- (18:00 to 5 am: never back up)
  local k = nl.step(h % 24)
  fewer = fewer and k >= prev
  prev = k
end
T.check(fewer and prev == 3, 'fewer and fewer to 5 am')
for name, maps in pairs(nl.MAPS) do
  T.check(nl.MATERIALS[name] ~= nil and #maps == 3, 'a lit material, three maps: ' .. name)
  for _, p in ipairs(maps) do
    local fh = io.open(dir .. '/../beamng_earth' .. p, 'rb')
    T.check(fh ~= nil, 'the map is there: ' .. p)
    if fh then fh:close() end
  end
end
f = io.open(shapes .. 'buildings.materials.json'); local bm = f:read('*a'); f:close()
T.check(bm:find(nl.MAPS.bne_bld_facade[1], 1, true) and not bm:find('bne_bld_facade_b', 1, true),
  'the facade sheet one material, its evening map in its materials.json')
local tod = 0.5                                       -- (midnight: the stock time 0.5)
core_environment = { getLightState = function() return { isNight = true } end,
                     getTimeOfDay = function() return { time = tod } end }
T.check(math.abs(nl.hour()) < 1e-9, 'the clock hour from the stock time')
local mats = {}
for _, name in ipairs({ 'bne_bld_facade', 'bne_bld_shop', 'bne_lamp_lens' }) do
  local o = S.newObject('Material'); o:registerObject(name); mats[name] = o
end
clock = clock + 2; nl.update()
T.check(mats.bne_bld_facade.fields.emissiveMap == nl.MAPS.bne_bld_facade[2]
  and mats.bne_bld_shop.fields.emissiveMap == nl.MAPS.bne_bld_shop[2] and mats.bne_bld_facade.fields.emissiveFactor == '1 1 1',
  'midnight: the late maps, the glow on')
T.check(mats.bne_lamp_lens.fields.emissiveMap == nil, 'the lamps: no maps')
local before = S.calls.reload_Material or 0
clock = clock + 2; nl.update()
T.check((S.calls.reload_Material or 0) == before, 'nothing set again while the step is the same')
tod = (4 - 12) / 24 % 1                               -- (4 am)
clock = clock + 2; nl.update()
T.check(mats.bne_bld_facade.fields.emissiveMap == nl.MAPS.bne_bld_facade[3] and nl.status().step == 3, '4 am: the fewest')
T.check((S.calls.reload_Material or 0) == before + 2, 'only the swapped materials reloaded')
tod = (14 - 12) / 24                                  -- (day: the glow off, the map left as it was)
core_environment.getLightState = function() return { isNight = false } end
before = S.calls.reload_Material or 0
clock = clock + 2; nl.update()
T.check(mats.bne_bld_facade.fields.emissiveFactor == '0 0 0' and mats.bne_bld_facade.fields.emissiveMap == nl.MAPS.bne_bld_facade[3],
  'by day the glow off, the map not swapped')
core_environment.getLightState = function() return { isNight = true } end
tod = (20 - 12) / 24                                  -- (the next evening: the evening's map back with the glow)
clock = clock + 2; nl.update()
T.check(mats.bne_bld_facade.fields.emissiveMap == nl.MAPS.bne_bld_facade[1] and nl.status().errors == 0,
  'the evening: its map back')
nl.reset()
T.done('test_nightlights')
