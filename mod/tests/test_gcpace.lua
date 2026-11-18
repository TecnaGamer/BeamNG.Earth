-- luajit mod/tests/test_gcpace.lua  (Lua garbage collection pacing, gcpace.lua)
-- The engine's full collections seen from the heap's fall (the base and trigger estimate, exact from then on), the
-- trigger raised when the heap passes the estimate, pacing only when the garbage took PACE_START of the room over the
-- live data (within the time budget, stopping at a finished cycle), nothing with little garbage, the provoked
-- collection in a hidden frame (ballast size, held until the fall, a miss raising the estimate, one per gap), the
-- switches.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local gp = require('beamngEarth/gcpace')

-- a fake collector: the heap in KB, steps counted (each freeing `perStep` KB), a cycle ending after `cycleAfter` steps
local heap, steps, perStep, cycleAfter = 0, 0, 0, math.huge
local function fake(opt, n)
  if opt == 'count' then return heap end
  if opt == 'step' then
    steps = steps + 1
    heap = heap - perStep
    return steps >= cycleAfter
  end
end
local now = 0
local fakeClock = 0
gp.setCollector(fake)
gp.setClock(function() fakeClock = fakeClock + 0.0001; return fakeClock end)   -- 0.1 ms a reading
local ballasts = {}
gp.setBallast(function(n) ballasts[#ballasts + 1] = n; heap = heap + n / 1024; return { n = n } end)

local function fresh()
  gp.reset(); gp.resetStats()
  heap, steps, perStep, cycleAfter, now = 0, 0, 0, math.huge, 0
  ballasts = {}
  gp.PACE, gp.PROVOKE = true, true
end
local function frame(kb, hidden)
  if kb then heap = kb end
  now = now + 1 / 60
  return gp.update(now, hidden)
end

------------------------------------------------------------------------------------------
-- the estimate: a full collection's fall, the smallest heap before one, raised past the trigger

fresh()
frame(120000); frame(110000); frame(115000)
T.check(gp.status().baseMB == math.floor(110000 / 1024 + 0.5) and not gp.status().exact, 'before a full collection: the smallest heap is the base')
frame(150000)                                       -- above 1.33 x 110000 = 146300: the trigger must be higher
T.near(gp.trigger(), 150000, 1, 'a heap above the estimate raises the trigger to it')
frame(100000)                                       -- a fall of a third: the engine's full collection
local s = gp.status()
T.check(s.fulls == 1 and s.exact and s.baseMB == math.floor(100000 / 1024 + 0.5), 'a fall of more than FULL_DROP: a full collection, its heap the exact base')
frame(90000)
T.check(gp.status().baseMB == math.floor(100000 / 1024 + 0.5), 'exact: a smaller heap later (live data freed) leaves the base')
frame(97000); frame(92000)
T.check(gp.status().fulls == 1, 'small falls (incremental sweeps) are no full collections')

------------------------------------------------------------------------------------------
-- pacing

fresh()
frame(100000); frame(60000)                          -- a full collection at 60 MB: trigger 79.8 MB
for i = 1, 70 do frame(60000) end                    -- the live level settles (smallest heap of each second)
steps = 0
T.check(frame(66000) == 0 and steps == 0, 'garbage 6 MB of 19.8 MB room: no pacing')
local ms = frame(72000)                              -- 12 / 19.8 = 0.61 > 0.5
T.check(steps > 0 and ms > 0, 'garbage past PACE_START of the room: steps')
local budget = gp.BUDGET_MS + (gp.MAX_MS - gp.BUDGET_MS) * ((12000 / 19800 - 0.5) / 0.5)
T.check(ms <= budget + 0.25, string.format('within its budget (%.2f <= %.2f ms)', ms, budget))
steps = 0
local msHigh = frame(79000)
T.check(msHigh > ms, 'nearer the trigger: a larger budget')
T.check(msHigh <= gp.MAX_MS + 0.25, 'never over MAX_MS')
steps, cycleAfter = 0, 3
frame(75000)
T.check(steps == 3 and gp.status().cycles >= 1, 'a finished cycle ends the frame\'s stepping')
cycleAfter = math.huge
gp.PACE = false
steps = 0
frame(79000)
T.check(steps == 0, 'PACE = false: no steps')
gp.PACE = true
-- little garbage: nothing even when the live data is near the trigger
fresh()
frame(100000); frame(60000)
for i = 1, 1300 do frame(78500) end                    -- live data grew to 78.5 MB (trigger 79.8)
steps = 0
frame(79500)
T.check(steps == 0, 'garbage under PACE_MIN_KB: no pacing (the live data is what fills the room)')

------------------------------------------------------------------------------------------
-- the provoked collection

fresh()
frame(100000); frame(60000)                          -- base 60 MB, trigger 79.8 MB
for i = 1, 1300 do frame(72000) end                    -- live data grew 12 of 19.8 MB room (> PROVOKE_LIVE)
gp.resetStats()
T.check(#ballasts == 0, 'not hidden: no provoked collection')
frame(72000, true)
T.check(#ballasts == 1, 'hidden with the live data grown: provoked')
local want = math.max(60000 * 1.33 * 1.02 - 72000, 72000 * gp.BALLAST)
T.near(ballasts[1] / 1024, want, 1, 'ballast: past the trigger, at least BALLAST of the heap')
T.check(gp.status().provoked == 1, 'counted')
frame(nil, true)                                     -- the engine has not collected yet (held)
frame(72000 + want - 20000, true)                    -- the garbage freed, the ballast kept: a smaller fall
s = gp.status()
T.check(s.fulls == 1 and s.fullsHidden == 1 and s.exact, 'the fall after a provoked frame: the full collection, hidden')
T.near(gp.trigger(), (72000 + want - 20000) * gp.RATIO, 1, 'the trigger from the heap with the ballast in it')
frame(72000, true)
T.check(#ballasts == 1, 'no second one within PROVOKE_GAP_S, nor with the room free again')
-- a miss: no collection within PROVOKE_FRAMES (before any full collection was seen: the smallest heap the guess)
fresh()
for i = 1, 30 do frame(60000) end
for i = 1, 1500 do frame(60000 + math.min(i, 120) * 100) end      -- (the live data grows slowly to 72 MB)
gp.resetStats()
frame(72000, true)
local held = heap
for i = 1, gp.PROVOKE_FRAMES + 1 do frame(held, true) end
s = gp.status()
T.check(s.provokeMissed == 1 and s.fulls == 0, 'no fall within PROVOKE_FRAMES: a miss')
T.check(gp.trigger() >= held - 1, 'a miss: the trigger is at least what was held')
for i = 1, 10 do frame(held - 3000 * i) end          -- (falling slowly: no full collection)
T.check(gp.trigger() >= held - 1, 'a miss stays a floor: a smaller heap later does not lower the estimate under it')
-- PROVOKE = false
fresh()
gp.PROVOKE = false
frame(100000); frame(60000)
for i = 1, 1300 do frame(72000) end
frame(72000, true)
T.check(#ballasts == 0, 'PROVOKE = false: no provoked collection')

T.done('test_gcpace')
