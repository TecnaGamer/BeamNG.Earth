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
core_environment.getLightState = function() return { isNight = false } end
clock = clock + 2; nl.update()
T.check(light and light.fields.isEnabled == 'false' and nl.status().errors == 0, 'day disables without errors')
nl.reset()

-- Late at night fewer windows lit, fewest at 5 am: the variants dark at their own hours, the first all night.
T.check(nl.level('bne_bld_facade', 5) == 1 and nl.level('bne_lamp_lens', 3) == 1, 'the first variant, the lamps: all night')
T.check(nl.level('bne_bld_facade_c', 20) == 1 and nl.level('bne_bld_facade_b', 23) == 1, 'all lit in the evening')
T.check(nl.level('bne_bld_facade_c', 1.5) == 0 and nl.level('bne_bld_office_b', 3) < 1, 'the third dark by 1, the second going')
T.check(nl.level('bne_bld_facade_b', 5) == 0 and nl.level('bne_bld_shop_c', 5) == 0, 'at 5 am only the first lit')
T.check(nl.level('bne_bld_facade_b', 7) == 1 and nl.level('bne_bld_facade_c', 12) == 1, 'all lit again by morning')
local prev, fewer = 2, true
for h = 21.5, 29, 0.25 do                             -- (21:30 to 5 am)
  local k = nl.level('bne_bld_facade_c', h % 24) + nl.level('bne_bld_facade_b', h % 24)
  fewer = fewer and k <= prev
  prev = k
end
T.check(fewer and prev == 0, 'fewer and fewer to 5 am')
T.check(nl.level('bne_bld_sky', 5) > 0 and nl.level('bne_bld_sky', 5) < 1, 'the skyline dimmer, not dark')
local tod = 0.5                                       -- (midnight: the stock time 0.5)
core_environment = { getLightState = function() return { isNight = true } end,
                     getTimeOfDay = function() return { time = tod } end }
T.check(math.abs(nl.hour()) < 1e-9, 'the clock hour from the stock time')
local mats = {}
for _, name in ipairs({ 'bne_bld_facade', 'bne_bld_facade_c' }) do
  local o = S.newObject('Material'); o:registerObject(name); mats[name] = o
end
clock = clock + 2; nl.update()
T.check(mats.bne_bld_facade.fields.emissiveFactor == '1 1 1' and mats.bne_bld_facade_c.fields.emissiveFactor ~= '1 1 1',
  'midnight: the third dimmed: ' .. tostring(mats.bne_bld_facade_c.fields.emissiveFactor))
local before = S.calls.reload_Material or 0
clock = clock + 2; nl.update()
T.check((S.calls.reload_Material or 0) == before, 'nothing set again while its step is the same')
tod = (3 - 12) / 24 % 1                               -- (3 am)
clock = clock + 2; nl.update()
T.check(mats.bne_bld_facade_c.fields.emissiveFactor == '0 0 0' and mats.bne_bld_facade.fields.emissiveFactor == '1 1 1',
  '3 am: the third dark')
nl.reset()
T.done('test_nightlights')
