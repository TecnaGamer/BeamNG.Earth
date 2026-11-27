-- luajit mod/tests/test_wetstate.lua
-- beamngEarth/wetstate.lua (the puddle plan R1): the integer wet state. Fill by how much it rained (1 h at 1 / 5 / 25
-- mm/h), drying (1 mm in under 2 h overcast, 15 mm about a day), the deep ones still there 2 h after an hour of heavy
-- rain, drizzle a film and no puddles; the same timeline gives the same integers whatever the checks' timing and when
-- joined late from a snapshot (BeamMP); snow and frozen air add nothing; no water steps under packed snow or ice; the
-- seeds (history, presets steady, the host's snapshot, a far move) and the shown step's one-a-check pace.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local W = require('beamngEarth/wetstate')
local check = T.check

local H = W.H_SCALE
local function hours(s, n, r, e) for _ = 1, n * 3600 / W.TICK_S do W.step(s, r, e) end return s end
local function um(s) return math.floor(s.h / H) end
local function isInt(v) return type(v) == 'number' and v == math.floor(v) end

-- fill by how much it rained (overcast ET0, from dry)
local E = W.ET0_OVERCAST
local s1 = hours({ t = 0, w = 0, i = 0, h = 0 }, 1, W.umh(1), W.ET0_WET)
local s5 = hours({ t = 0, w = 0, i = 0, h = 0 }, 1, W.umh(5), W.ET0_WET)
local s25 = hours({ t = 0, w = 0, i = 0, h = 0 }, 1, W.umh(25), W.ET0_WET)
check(W.fillStep(s1.h) >= 1 and W.fillStep(s1.h) <= 2, '1 h of 1 mm/h: the first few puddles (' .. um(s1) .. ' um, step '
  .. W.fillStep(s1.h) .. ')')
check(W.fillStep(s5.h) >= 6 and W.fillStep(s5.h) < #W.H_STEPS_UM, '1 h of 5 mm/h: well filled, not full (step '
  .. W.fillStep(s5.h) .. ')')
check(W.fillStep(s25.h) == #W.H_STEPS_UM and um(s25) == W.H_CAP_UM, '1 h of 25 mm/h: full, at the cap')
local s10 = hours({ t = 0, w = 0, i = 0, h = 0 }, 1, W.umh(10), W.ET0_WET)
check(W.fillStep(s10.h) == #W.H_STEPS_UM, 'an hour of heavy rain (10 mm/h) fills them (' .. um(s10) .. ' um)')
check(s1.w == W.wTarget(1000) and s1.w < W.W_MAX and s5.w == W.W_MAX and s5.i == 5000 and s1.t == 120,
  'the film at its target (soaked from 2 mm/h), the flow at the rate')
hours(s10, 2, 0, E)
check(W.fillStep(s10.h) >= #W.H_STEPS_UM - 2, 'the deep ones remain 2 h after (step ' .. W.fillStep(s10.h) .. ')')
-- drizzle: ten minutes wet the film, no puddles
local d = { t = 0, w = 0, i = 0, h = 0 }
for _ = 1, 20 do W.step(d, W.umh(0.3), W.ET0_WET) end
check(d.w > 300 and d.h == 0 and W.fillStep(d.h) == 0, 'ten minutes of drizzle: a film, no puddles (w ' .. d.w .. ')')
-- drying, overcast
local function hoursToDry(h0, e)
  local s = { t = 0, w = 0, i = 0, h = h0 * H }
  local n = 0
  while s.h > 0 and n < 100000 do W.step(s, 0, e); n = n + 1 end
  return n * W.TICK_S / 3600
end
local h1, h15 = hoursToDry(1000, E), hoursToDry(15000, E)
check(h1 < 2, '1 mm gone in under 2 h overcast (' .. string.format('%.2f', h1) .. ' h)')
check(h15 > 18 and h15 < 30, '15 mm about a day (' .. string.format('%.1f', h15) .. ' h)')
check(hoursToDry(15000, 600) < h15, 'faster in sun')
-- the film dries over ~45 min after the rain
local f = { t = 0, w = W.W_MAX, i = 0, h = 0 }
local n = 0
while f.w > 0 do W.step(f, 0, E); n = n + 1 end
check(n * W.TICK_S > 30 * 60 and n * W.TICK_S < 50 * 60, 'the film dries in ~45 min overcast (' .. n * W.TICK_S / 60 .. ' min)')
-- every value an integer, whatever the inputs
local z = { t = 0, w = 0, i = 0, h = 0 }
for k = 1, 500 do W.step(z, (k * 7919) % 30000, (k * 104729) % 700) end
check(isInt(z.w) and isInt(z.i) and isInt(z.h) and isInt(z.t), 'integers only')
check(W.umh(0.0005) == 1 and W.umh(1.2345) == 1235 and W.umh(-3) == 0 and W.umh(0 / 0) == 0 and W.umh(1e9) == 500000,
  'rates to um/h')

-- snow and frozen air add nothing; snow and ice hide the water
check(W.rainOf({ rainMmH = 0, snowCmH = 3 }) == 0, 'snow adds nothing')
check(W.rainOf({ rainMmH = 2, tempC = -2 }) == 0 and W.rainOf({ rainMmH = 2, tempC = 3 }) == 2000
  and W.rainOf({ rainMmH = 2, slTempC = -1 }) == 0, 'freezing rain adds nothing; liquid above freezing')
check(W.frozen('packed') and W.frozen('ice') and W.frozen('black_ice') and not W.frozen('wet') and not W.frozen('slush')
  and not W.frozen('dry') and not W.frozen(nil), 'frozen road conditions')
check(W.frozen(nil, -3) and not W.frozen(nil, 2), 'frozen air')

-- runtime: seeds and ticks on the UTC clock
local now = 1764200000
W.clock = function() return now end
W.reset()
local storm = { rainMmH = 15, code = 95, tempC = 17 }
W.sync(storm, 'preset', 'storm@1')
check(W.state().w == W.wTarget(15000) and W.state().h == W.H_CAP_UM * H and W.state().i == 15000,
  'a preset starts in its steady state: soaked (' .. W.debugLine() .. ')')
local st0 = W.snapshot()
now = now + 3600
W.sync(storm, 'preset', 'storm@1')
check(W.state().h == st0.h and W.state().w == st0.w and W.state().t == st0.t + 120, 'a steady preset stays steady')
-- a late joiner of the same server preset gets the same integers
local a = W.snapshot()
W.reset()
W.sync(storm, 'preset', 'storm@1')
local b = W.snapshot()
check(a.t == b.t and a.w == b.w and a.i == b.i and a.h == b.h, 'a server preset: the same state whenever one joined')
W.reset()
W.sync({ rainMmH = 0.3, code = 53 }, 'preset', 'drizzle@1')
check(W.state().h == 0 and W.state().w > 0, 'drizzle preset: a film, no puddles')
W.sync({ rainMmH = 0, code = 0 }, 'preset', 'clear@1')
check(W.state().h == 0 and W.state().w == 0, 'another preset: its own steady state')

-- the shown step: at most one a check toward the state's; none under snow and ice
W.reset()
W.sync(storm, 'preset', 'storm@1')
local s, ch = W.puddleStep(nil, 17)
check(s == 1 and ch, 'one step a check (1)')
for _ = 1, 4 do W.puddleStep(nil, 17) end
check(W.shownStep() == 5, 'one step a check (5)')
for _ = 1, 20 do W.puddleStep(nil, 17) end
check(W.shownStep() == #W.H_STEPS_UM, 'full after enough checks')
W.puddleStep('packed', 17)
check(W.shownStep() == #W.H_STEPS_UM - 1, 'packed snow: going away a step a check')
for _ = 1, 20 do W.puddleStep('ice', -5) end
check(W.shownStep() == 0, 'no water steps under ice')

-- real weather: the server's history (24 h of hourly um), then the hours since at the rain now
local function hist(t0, rains)
  local et0 = {}
  for k = 1, #rains do et0[k] = 50 end
  return { t0 = t0, rain = rains, et0 = et0 }
end
local h0 = math.floor(now / 3600) * 3600
local wetMorning = {}
for k = 1, 24 do wetMorning[k] = (k >= 14 and k <= 18) and 6000 or 0 end      -- 5 wet hours, ending 6 h before now
W.reset()
local dry = { rainMmH = 0, code = 3, tempC = 12, et0Umh = 60 }
W.sync(dry, 'real', nil, hist(h0 - 24 * 3600, wetMorning), 39.28, -76.59)
check(W.lastSeed == 'history' and W.fillStep(W.state().h) >= 6, 'arriving after a wet morning: the puddles are there ('
  .. W.debugLine() .. ')')
local after = W.snapshot()
-- the same timeline checked at other times (2 s, 7 s, 61 s apart, frames of any length): the same integers
local function runChecks(gaps)
  W.reset()
  now = h0 + 10
  W.sync(dry, 'real', nil, hist(h0 - 24 * 3600, wetMorning), 39.28, -76.59)
  local k = 1
  while now < h0 + 2 * 3600 do
    now = math.min(h0 + 2 * 3600, now + gaps[k])
    W.sync(dry, 'real', nil, nil, 39.28, -76.59)
    k = k % #gaps + 1
  end
  return W.snapshot()
end
local r1, r2, r3 = runChecks({ 2 }), runChecks({ 7, 0.016, 61 }), runChecks({ 3599 })
check(r1.t == r2.t and r1.w == r2.w and r1.i == r2.i and r1.h == r2.h and r1.h == r3.h and r1.w == r3.w,
  'the same timeline, any check timing: identical integers')
-- BeamMP 'real-host': the host's snapshot, joined late, caught up with the relayed message: the host's own integers
W.reset()
now = h0 + 100
local rainy = { rainMmH = 3.2, code = 63, tempC = 9, et0Umh = 15 }
W.sync(rainy, 'real', nil, hist(h0 - 24 * 3600, wetMorning), 39.28, -76.59)
local snap = W.snapshot()                                   -- (sent with the relayed weather)
now = now + 1777
W.sync(rainy, 'real', nil, nil, 39.28, -76.59)
local host = W.snapshot()
W.reset()                                                   -- a client joining now
W.sync(rainy, 'host', nil, nil, nil, nil, snap)
local client = W.snapshot()
check(client.t == host.t and client.w == host.w and client.i == host.i and client.h == host.h,
  'joined late from the host\'s snapshot: the host\'s integers')
W.sync(rainy, 'host', nil, nil, nil, nil, snap)
check(W.seeds > 0 and W.lastSeed == 'host snapshot', 'the same snapshot is taken once')
check(W.fromSnapshot({ t = 'x' }) == nil and W.fromSnapshot({ t = 5, w = 9e9, h = -4, i = 0 / 0 }).w == W.W_MAX
  and W.fromSnapshot({ t = 5, w = 9e9, h = -4, i = 0 / 0 }).h == 0, 'snapshots clamped')
-- a far move seeds from the new place's history; driving on does not
W.reset()
W.sync(dry, 'real', nil, hist(h0 - 24 * 3600, wetMorning), 39.28, -76.59)
local seeds = W.seeds
W.sync(dry, 'real', nil, hist(h0 - 24 * 3600, {}), 39.30, -76.60)
check(W.seeds == seeds, 'driving 2 km: the state goes on')
local drySpell = {}
for k = 1, 24 do drySpell[k] = 0 end
W.sync(dry, 'real', nil, hist(h0 - 24 * 3600, drySpell), 40.75, -73.99)
check(W.seeds == seeds + 1 and W.state().h == 0, 'a teleport far away: that place\'s own history')
-- no history (an old server, BNE_PUDDLES off): the rain now's steady state
W.reset()
W.sync(rainy, 'real', nil, nil, 39.28, -76.59)
check(W.lastSeed == 'steady (no history)' and W.state().h == W.H_CAP_UM * H, 'no history: the rain now, steady')
-- ET0 by code without the server's
check(W.et0Of({ code = 0 }) == W.ET0_CLEAR and W.et0Of({ code = 63 }) == W.ET0_WET and W.et0Of({ code = 3, et0Umh = 333 }) == 333,
  'ET0')
check(W.toward(3, 5) == 4 and W.toward(3, 1) == 2 and W.toward(2, 2) == 2, 'toward')

T.done('test_wetstate')
