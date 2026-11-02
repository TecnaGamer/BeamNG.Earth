-- luajit mod/tests/test_framepacer.lua  (even frame pacing)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local P = require('beamngEarth/framepacer')

-- uneven frames (a light, light, heavy rhythm and runs of slower ones) come out even once paced, but for the
-- heaviest few, which are left alone
P.reset()
local before, after = {}, {}
for i = 1, 2000 do
  local ms = ((i % 3 == 0) and 9.0 or 6.0) + ((i % 400 < 60) and 2.0 or 0.0)
  local w = P.step(ms)
  T.check(w >= 0, 'never a negative wait')
  before[#before + 1], after[#after + 1] = ms, ms + w
end
local function spread(t, from)
  local lo, hi = math.huge, 0
  for i = from, #t do lo, hi = math.min(lo, t[i]), math.max(hi, t[i]) end
  return hi - lo
end
local function share(t, from, v)
  local k = 0
  for i = from, #t do if math.abs(t[i] - v) < 0.6 then k = k + 1 end end
  return k / (#t - from + 1)
end
T.check(spread(before, 500) >= 4.9, 'the frames as they came: uneven')
local tg = P.target()
T.check(tg and tg > 6.0 and tg <= 11.0, 'the target: a high share of the frames\' own time: ' .. tostring(tg))
T.check(share(after, 500, tg) > 0.6, string.format('paced: most frames at the target (%.0f %%)', share(after, 500, tg) * 100))
-- a frame over the target waits nothing; nothing before enough frames are seen
T.check(P.step(50) == 0, 'a slow frame: no wait')
P.reset()
T.check(P.step(5) == 0, 'no target yet: no wait')
-- the target stays within its range
P.reset()
for _ = 1, 200 do P.step(1.0) end
T.check(P.target() >= P.MIN_MS, 'at least MIN_MS')
P.reset()
for _ = 1, 200 do P.step(40.0) end
T.check(P.target() == nil and P.step(20.0) == 0, 'slower than 30 fps (a Steam Deck): no pacing')
T.done('test_framepacer')
