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
T.check(light and light.id == id and light.fields.range == tostring(nl.HIGH_MAST.range) and light.fields.color == nl.HIGH_MAST.color,
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
-- a map is shipped in the mod, or made at install from mod/texture_sources (textures.json names every DDS it makes)
local mf = io.open(dir .. '/../texture_sources/textures.json', 'rb'); local made = mf and mf:read('*a') or ''
if mf then mf:close() end
for name, maps in pairs(nl.MAPS) do
  T.check(nl.MATERIALS[name] ~= nil and #maps == 3, 'a lit material, three maps: ' .. name)
  for _, p in ipairs(maps) do
    local fh = io.open(dir .. '/../beamng_earth' .. p, 'rb')
    T.check(fh ~= nil or made:find('"' .. p:sub(2) .. '"', 1, true) ~= nil, 'the map is there: ' .. p)
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

-- The airfield lights' glow (tools/build_airfield_lights.py; server airfieldlights.py's afl_<colour> groups): five shapes
-- of eight balls each, every ball's material defined and lit in its colour, no brighter than the lamps' glow, and the
-- groups switched with it (one call: trees.lua refreshes once) at dusk and dawn.
local nAfl = 0
for colour, rgb in pairs(nl.AFL_COLOURS) do
  local fa = io.open(shapes .. 'bne_afl_' .. colour .. '.dae'); local ad = fa:read('*a'); fa:close()
  local seen = 0
  for name in ad:gmatch('target="#([%w_]+)"') do
    seen = seen + 1
    local m = nl.MATERIALS[name]
    T.check(m ~= nil and m[1] == rgb,
            'afl material lit: ' .. name)
  end
  T.check(seen == #nl.GLOW_NITS + 1, 'a material per ball and the speck: ' .. colour .. ' ' .. seen)
  for k = 1, #nl.GLOW_NITS do
    local name = 'bne_afl_' .. colour .. (k - 1)
    local m = nl.MATERIALS[name]
    T.check(m and m[1] == rgb and m[2] == nl.GLOW_NITS[k] * nl.AFL_NITS_FACTOR * nl.AFL_COLOUR_GAIN[colour],
            'afl ball ' .. name .. ' in its colour, at its colour brightness')
    T.check(colour == 'blue' or (m and m[2] > nl.MATERIALS['bne_afl_blue' .. (k - 1)][2]),
            'afl ball ' .. name .. ': a runway light brighter than a blue taxiway light')
    nAfl = nAfl + 1
  end
end
T.check(nAfl == 40, 'five colours of eight balls: ' .. nAfl)
local fm = io.open(shapes .. 'airfield_lights.materials.json'); local aflJson = fm:read('*a'); fm:close()
for colour in pairs(nl.AFL_COLOURS) do
  for k = 0, #nl.GLOW_NITS - 1 do
    T.check(aflJson:find('"bne_afl_' .. colour .. k .. '": {', 1, true) ~= nil, 'defined in the json: ' .. colour .. k)
  end
end
T.check(#nl.AFL_GROUPS == 5, 'five groups')
local set, enabled = {}, {}
package.loaded['beamngEarth/trees'] = { setGroupEnabled = function(gs, on)
  set[#set + 1] = { gs = gs, on = on }
  for _, g in ipairs(gs) do enabled[g] = on end
  return true
end }
core_environment = { getLightState = function() return { isNight = true } end }
nl.reset()
clock = clock + 2; nl.update()
T.check(#set == 1 and set[1].on == true, 'dusk: one switch call')
T.check(enabled.lampglow == true, 'dusk: the lamp glow on')
T.check(enabled.apron_glow == true, 'dusk: the apron pools of light on')
for colour in pairs(nl.AFL_COLOURS) do
  T.check(enabled['afl_' .. colour] == true, 'dusk: afl_' .. colour .. ' on')
end
core_environment.getLightState = function() return { isNight = false } end
clock = clock + 2; nl.update()
T.check(#set == 2 and set[2].on == false, 'dawn: one switch call')
T.check(enabled.lampglow == false, 'dawn: the lamp glow off')
T.check(enabled.apron_glow == false, 'dawn: the apron pools off')
for colour in pairs(nl.AFL_COLOURS) do
  T.check(enabled['afl_' .. colour] == false, 'dawn: afl_' .. colour .. ' off')
end
package.loaded['beamngEarth/trees'] = nil
nl.reset()

-- The apron masts (server airsideprops.py G_LENS, G_POOL; tools/build_apron_glow.py): their own light profile, and a share of
-- the pool taken before the street lamps (the user, 2026-11-17: a whole apron lit, not the one or two masts nearest the
-- camera), the ground pool's shape, material and map made.
T.check(#nl.NIGHT_GROUPS == 2 and nl.NIGHT_GROUPS[1] == 'lampglow' and nl.NIGHT_GROUPS[2] == 'apron_glow', 'the night-only groups')
local af = io.open(shapes .. 'bne_apron_pool.dae'); local poolDae = af:read('*a'); af:close()
af = io.open(shapes .. 'lamps.materials.json'); local lampsJson = af:read('*a'); af:close()
T.check(poolDae:find('target="#bne_apron_pool"', 1, true) ~= nil and nl.MATERIALS.bne_apron_pool ~= nil
        and lampsJson:find('"bne_apron_pool"', 1, true) ~= nil, 'the pool: its material lit at night and defined')
T.check(lampsJson:find('"translucent": true', 1, true) ~= nil and lampsJson:find('bne_apron_pool_o.data.dds', 1, true) ~= nil,
        'the pool: translucent, its opacity map a DDS')
T.check(made:find('"levels/beamng_earth/art/shapes/beamngEarth/bne_apron_pool_o.data.dds"', 1, true) ~= nil,
        "the pool's opacity map made at install (textures.json)")
T.check(nl.APRON_MAST.drop < 0 and nl.APRON_MAST.drop == -nl.APRON_LIFT_M, 'the apron light raised over the mast head')
T.check(nl.APRON_MAST.range >= (18 + nl.APRON_LIFT_M) / math.cos(math.rad(nl.APRON_MAST.outer / 2)),
        "its range reaches the cone's slant edge: the head 18 m up, the light 30 m over it")
T.check(nl.APRON_MAST.brightness <= 6 and nl.APRON_MAST.outer > nl.APRON_MAST.inner and nl.APRON_MAST.inner >= 90,
        "a wide, flat cone, no brighter than the road masts' ballpark (overlapping pools do not burn out)")

nl.reset()
clock = clock + 10
core_camera = { getPosition = function() return vec3(0, 0, 0) end, getForward = function() return vec3(1, 0, 0) end }
core_environment = { getLightState = function() return { isNight = true } end }
nl.LIGHTS_MAX = 20
local ap = {}
for i = 1, 3 do ap[#ap + 1] = { 0, 150 + 90 * i, (i - 2) * 30, 17.9, 0 } end
nl.onRows('apron', ap, { 'apron_mast_light' })
local al = nl.nearest(0, 0, 900, 10)
T.check(#al == 3 and al[1][7] == true and al[1][6] == false and al[1][5] == false,
        "an apron lens: its own profile, neither the road masts' nor a lantern's")
T.check(#nl.nearest(0, 0, 900, 10, nil, nil, nil, 'apron') == 3 and #nl.nearest(0, 0, 900, 10, nil, nil, nil, 'other') == 0,
        'the kinds: apron lenses apart from the others')
local street = {}
for i = 1, 40 do street[#street + 1] = { 0, 5 + i * 2, (i % 2) * 6, 8, 0 } end
nl.onRows('street', street, { 'lamplens' })
local nearestOnly = nl.nearest(0, 0, 900, 20)
local nApronNear = 0
for _, l in ipairs(nearestOnly) do if l[7] then nApronNear = nApronNear + 1 end end
T.check(nApronNear == 0, 'by distance alone the 40 street lamps nearer would take all 20 lights (the masts 240 m and more off)')
local function settle()                  -- (the pool is made a few lights a step: MAKE_PER_STEP)
  for _ = 1, 8 do clock = clock + 1; nl.update() end
end
local function lit()
  local apronLit, others, apronZ = 0, 0, nil
  for _, o in pairs(S.objects) do
    if o.class == 'SpotLight' and o.fields.isEnabled == 'true' then
      if o.fields.range == tostring(nl.APRON_MAST.range) then
        apronLit = apronLit + 1
        apronZ = o.pos.z
      else
        others = others + 1
      end
    end
  end
  return apronLit, others, apronZ
end
settle()
local a, o, z = lit()
T.check(a == 3 and o == 17, 'all three masts lit, the others of the 20 the nearest lamps: ' .. a .. ' + ' .. o)
T.check(z and math.abs(z - (17.9 + nl.APRON_LIFT_M)) < 1e-6, 'the apron light raised 30 m over the lens: ' .. tostring(z))
nl.dropKey('apron')
local many = {}
for i = 1, 14 do many[#many + 1] = { 0, 100 + 20 * i, 0, 17.9, 0 } end
nl.onRows('apron', many, { 'apron_mast_light' })
settle()
a, o = lit()
T.check(a == math.floor(20 * nl.APRON_SHARE) and a + o == 20, 'many masts: their share of the pool, no more: ' .. a .. ' + ' .. o)
nl.dropKey('apron')
settle()
a, o = lit()
T.check(a == 0 and o == 20, 'no masts in reach: the share goes back to the street lamps')
nl.onRows('far', { { 0, nl.APRON_NEAR_M + 50, 0, 17.9, 0 } }, { 'apron_mast_light' })
settle()
a = lit()
T.check(a == 0, 'a mast past APRON_NEAR_M: no real light (its pool of glow shows)')
nl.dropKey('far')
nl.onRows('behind', { { 0, -(nl.APRON_AROUND_M + 20), 0, 17.9, 0 }, { 0, -(nl.APRON_AROUND_M - 20), 0, 17.9, 0 } }, { 'apron_mast_light' })
settle()
a = lit()
T.check(a == 1, 'behind the camera: only the mast within APRON_AROUND_M of it: ' .. a)
nl.dropKey('behind')
nl.setLightCount('few')
T.check(nl.LIGHTS_MAX == 48 and math.floor(nl.LIGHTS_MAX * nl.APRON_SHARE) == 19, 'the few-lights setting: 19 for the masts')
nl.setLightCount('normal')
core_environment.getLightState = function() return { isNight = false } end
settle()
a, o = lit()
T.check(a == 0 and o == 0 and nl.status().errors == 0, 'day: every light off, no errors')
nl.reset()
T.done('test_nightlights')
