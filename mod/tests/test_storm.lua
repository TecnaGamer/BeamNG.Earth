-- luajit mod/tests/test_storm.lua
-- beamngEarth/storm.lua (weather phase B, lightning and thunder): the hash (pinned values, checked against exact integer
-- maths in Python: the same strikes on every CPU and OS), the thunder class, the strike lattice (rate, a stronger class
-- keeps the weaker one's strikes, the same answer for every caller), the flash and thunder curves, then the runtime
-- against stubs: a strike's flash, bolt and thunder after its distance, nothing without a storm, the reset.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local storm = require('beamngEarth/storm')
local check, near = T.check, T.near

-- the hash: MurmurHash3 mixing in exact integer maths (python3 with Python ints gives the same four numbers)
check(storm.hash(0) == 3411927179, 'hash(0)')
check(storm.hash(1, 2, 3) == 344003798, 'hash(1, 2, 3)')
check(storm.hash(-5, 17, 179000000, 4) == 591654083, 'hash of a negative cell and a big slot')
check(storm.hash(4294967295) == 3196524898, 'hash(2^32 - 1)')

-- the thunder class
check(storm.thunderClass(nil) == 0 and storm.thunderClass({ code = 63, rainMmH = 4 }) == 0, 'rain: no thunder')
check(storm.thunderClass({ code = 95 }) == 2, 'code 95: a thunderstorm')
check(storm.thunderClass({ code = 96 }) == 3 and storm.thunderClass({ code = 99 }) == 3, 'with hail: strong')
check(storm.thunderClass({ code = 3, lpi = 1 }) == 1, 'ICON-D2 lightning potential: possible')
check(storm.thunderClass({ code = 95, lpi = 6 }) == 3, 'a high lightning potential: strong')
check(storm.thunderClass({ code = 95, lpi = 0 }) == 2, 'a zero potential does not take the code\'s storm away')
check(storm.thunderClass({ code = 80, capeJkg = 1500, rainMmH = 2 }) == 1, 'showers with CAPE: possible')
check(storm.thunderClass({ code = 3, capeJkg = 1500, rainMmH = 0 }) == 0, 'CAPE without rain: none')

-- the lattice: the rate per class, a stronger class keeps the weaker one's strikes
check(storm.share(0) == 0 and storm.strikeAt(1, 1, 1, 0) == nil, 'class 0: no strikes')
local n1, n2, n3, kept = 0, 0, 0, true
for slot = 1, 30000 do
  local a, b, c = storm.strikeAt(401, -740, slot, 1), storm.strikeAt(401, -740, slot, 2), storm.strikeAt(401, -740, slot, 3)
  n1, n2, n3 = n1 + (a and 1 or 0), n2 + (b and 1 or 0), n3 + (c and 1 or 0)
  if (a and not b) or (b and not c) then kept = false end
  if a and b and (a.t ~= b.t or a.lat ~= b.lat) then kept = false end
end
check(kept, 'a stronger class keeps every strike of a weaker one, at the same place and time')
near(n1 / 30000, storm.share(1), 0.01, 'class 1 rate')
near(n2 / 30000, storm.share(2), 0.012, 'class 2 rate')
near(n3 / 30000, storm.share(3), 0.015, 'class 3 rate')
-- a strike lies in its own cell and slot
local s
for slot = 1, 1000 do s = storm.strikeAt(401, -740, slot, 3); if s then break end end
check(s and s.lat >= 401 * storm.CELL_DEG and s.lat < 402 * storm.CELL_DEG and s.lon >= -740 * storm.CELL_DEG
  and s.lon < -739 * storm.CELL_DEG, 'the strike in its cell')
check(s and s.t >= math.floor(s.t / storm.SLOT_S) * storm.SLOT_S and s.pulses >= 1 and s.pulses <= 3
  and s.variant >= 1 and s.variant <= storm.VARIANTS, 'its slot, pulses and bolt shape')
-- strikesNear: sorted, inside the window, and two players in the same cell get the same list
local A = storm.strikesNear(40.12, -73.95, 1.8e9, 1.8e9 + 600, 2)
local B = storm.strikesNear(40.13, -73.99, 1.8e9, 1.8e9 + 600, 2)
check(#A > 20, 'ten minutes of a class 2 storm: some strikes (' .. #A .. ')')
local same, sorted = #A == #B, true
for i = 1, #A do
  if not B[i] or A[i].t ~= B[i].t or A[i].lat ~= B[i].lat then same = false end
  if i > 1 and A[i].t < A[i - 1].t then sorted = false end
  if A[i].t < 1.8e9 or A[i].t >= 1.8e9 + 600 then sorted = false end
end
check(same, 'two players in one cell: the same strikes')
check(sorted, 'sorted and inside the window')

-- the flash and the thunder
local p = storm.pulses({ seed = 12345, pulses = 3 })
check(#p == 3 and p[1][1] == 0 and p[2][1] > p[1][1] + p[1][2] and p[3][3] < p[1][3], 'three pulses with gaps, fading')
check(storm.flashAt(p, p[1][2] * 0.2) > 0.9 and storm.flashAt(p, p[1][2] + 0.001) == 0, 'bright in a pulse, dark between')
check(storm.flashGain(0) > storm.flashGain(5000) and storm.flashGain(storm.SEE_M) == 0, 'the flash fades with distance')
local th = storm.thunderFor(3430)
near(th.delay, 10, 1e-9, '3.43 km: thunder after 10 s')
check(th.kind == 'mid' and storm.thunderFor(500).kind == 'close' and storm.thunderFor(10000).kind == 'far', 'kinds by distance')
check(storm.thunderFor(storm.HEAR_M + 1) == nil, 'too far to hear')
local sk = storm.thunderFor(200)
check(sk.kind == 'strike' and sk.delay == 0 and sk.gain == 1 and #storm.THUNDER.strike == 2, 'a strike within STRIKE_M: its own sound, at once, full')
check(storm.thunderFor(storm.STRIKE_M + 1).kind == 'close', 'past STRIKE_M: a close crack')
check(storm.thunderFor(500).gain > storm.thunderFor(10000).gain, 'nearer is louder')
-- the stand-in distance inside the fog
check(storm.proxyDistance(5000, nil) == 5000, 'no visibility known: its own distance')
check(storm.proxyDistance(5000, 2000) == math.max(storm.PROXY_MIN_M, storm.PROXY_VIS * 2000), 'in a storm\'s fog: nearer')
check(storm.proxyDistance(200, 2000) == 200, 'never farther than it is')

-- the clock: never ahead of os.time() by more than a second
local now = storm.now()
check(now >= os.time() - 1 and now <= os.time() + 1, 'the epoch clock')

------------------------------------------------------------------------------------------
-- runtime

local played = {}
Engine = { Audio = { playOnce = function(desc, file, opts) played[#played + 1] = { desc, file, opts.volume } end } }
local camP = vec3(0, 0, 10)
core_camera = { getPosition = function() return camP end }
-- the strike lattice laid over a flat frame: 1 degree = 100 km, the camera at lat 40.12, lon -73.95
local LAT0, LON0 = 40.12, -73.95
beamngEarth_api = {
  -- (as the real api.lua: plain numbers, not tables; the first broker run failed on exactly this)
  toGeo = function(x, y) return LAT0 + y / 1e5, LON0 + x / 1e5, 0 end,
  toGame = function(lat, lon) return (lon - LON0) * 1e5, (lat - LAT0) * 1e5, 0 end,
  game = function(lat, lon) local x, y = (lon - LON0) * 1e5, (lat - LAT0) * 1e5 return vec3(x, y, 0) end,
}
-- nothing without a storm: no objects, no state
check(storm.update(0.016, 0, {}) == false and #S.liveOf('TSStatic') == 0 and #S.liveOf('PointLight') == 0,
  'class 0: nothing made')
-- a clock we drive, starting just before a known strike near the camera
local strikes = storm.strikesNear(LAT0, LON0, 1.8e9, 1.8e9 + 3600, 3)
local first
for _, st in ipairs(strikes) do
  local g = beamngEarth_api.game(st.lat, st.lon)
  if st.cg and math.sqrt(g.x * g.x + g.y * g.y) < storm.BOLT_M then first = st break end
end
check(first ~= nil, 'a ground strike within bolt range in the hour')
local clock = first.t - 2
storm.now = function() return clock end
local function run(seconds, step)
  step = step or 0.02
  for _ = 1, math.floor(seconds / step + 0.5) do
    clock = clock + step
    storm.update(step, 3, { groundZ = 0, cloudAboveM = 1500, sheltered = false })
  end
end
run(1.5)
check(storm.strikes == 0 and #S.liveOf('PointLight') == 0, 'before the strike: nothing drawn yet')
-- run until just past the first strike: the flash on, its light, the glow, a bolt
while clock < first.t + 0.02 do run(0.02) end
local g = beamngEarth_api.game(first.lat, first.lon)
local d = math.sqrt(g.x * g.x + g.y * g.y)
check(storm.strikes >= 1 and storm.last and math.abs(storm.last.d - d) < 1e-6, 'the strike at its distance ' .. math.floor(d))
local light = S.liveOf('PointLight')[1]
check(light and light.fields.isEnabled == '1' and tonumber(light.fields.intensity) > 0, 'the flash light on')
-- the light near the camera towards the strike (far lights are culled), as bright at the camera as the strike
local kk = math.min(1, storm.LIGHT_NEAR_M / d)
check(light and math.abs(light.pos.x - (camP.x + g.x * kk)) < 1e-6 and math.abs(light.pos.y - (camP.y + g.y * kk)) < 1e-6
  and math.abs(light.pos.z - (camP.z + storm.LIGHT_UP_M)) < 1e-6, 'the light near the camera, towards the strike')
local rl = math.sqrt((g.x * kk) ^ 2 + (g.y * kk) ^ 2 + storm.LIGHT_UP_M ^ 2)
check(tonumber(light.fields.intensity) <= storm.LIGHT_LM * (rl / d) ^ 2 * 1.0001, 'its lumens: the strike\'s illuminance at the camera')
local shownBolts = 0
for _, o in ipairs(S.liveOf('TSStatic')) do
  if not o.hidden and o.name and o.name:find('^bngEarthStormBolt') then shownBolts = shownBolts + 1 end
end
check(shownBolts == 1, 'one bolt shown')
local veil = scenetree.findObject('bngEarthStormVeil')
check(veil and not veil.hidden, 'the lit air shown in the flash')
-- no object takes a material's name (an object named like a material shadows it: the glow was 'bngEarthFlashGlow',
-- its own material's name, and nothing showed in the first broker shots)
local mj = io.open(dir .. '/../beamng_earth/levels/beamng_earth/art/weather/weather.materials.json'):read('*a')
local clash = {}
for _, o in pairs(S.objects) do
  if o.name and o.name ~= '' and mj:find('"' .. o.name .. '"', 1, true) then clash[#clash + 1] = o.name end
end
check(#clash == 0, 'no object named like a material: ' .. table.concat(clash, ', '))
check(storm.debugLine():find('glow#') ~= nil, 'the debug line names the objects')
-- the flash ends: everything hidden again
run(storm.FLASH_S + 0.1)
local anyShown = false
for _, o in ipairs(S.liveOf('TSStatic')) do if not o.hidden then anyShown = true end end
check(not anyShown and light.fields.isEnabled == '0', 'after the flash: hidden, the light off')
-- the thunder after distance / 343 m/s (and none before)
local th1 = storm.thunderFor(d)
local nBefore = #played
clock = first.t + th1.delay - 0.1
storm.update(0.016, 0, { groundZ = 0 })
local heardEarly = false
for i = nBefore + 1, #played do if played[i][2]:find(th1.kind) then heardEarly = true end end
check(not heardEarly, 'no thunder before the sound arrives')
clock = first.t + th1.delay + 0.05
storm.update(0.016, 0, { groundZ = 0 })
local heard = false
for i = nBefore + 1, #played do
  if played[i][2]:find('bne_thunder_' .. th1.kind) and played[i][1] == 'AudioGui' then heard = true end
end
check(heard, 'its thunder (' .. th1.kind .. ') after ' .. string.format('%.1f', th1.delay) .. ' s')
-- the storm gone, the queue empty: the module says it is done
clock = clock + 120
check(storm.update(0.016, 0, {}) == false, 'class 0 and nothing queued: done')
storm.reset()
check(#S.liveOf('TSStatic') == 0 and #S.liveOf('PointLight') == 0, 'reset: the objects deleted')

T.done('test_storm')
