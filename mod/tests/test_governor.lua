-- luajit mod/tests/test_governor.lua  (main-thread frame governor, V1_2_DESIGN 5.4, 5.7)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local governor = require('beamngEarth/governor')

-- A fake wall clock: frames advance it by their base time, units by their cost.
local now = 0
local function clock() return now end
local BASE = 11.5 -- ms, the design's modelled baseline frame

-- Run one frame: units of `kind` with cost `cost` while the governor allows them; a heavy request
-- (if wanted) is made first, as core.lua does. Returns spent, units, heavyRan.
local function frame(gov, opts)
  opts = opts or {}
  local start = now
  gov:beginFrame(opts.hasWork ~= false, opts.wall)
  local heavyRan = false
  if opts.heavy and gov:requestHeavy(opts.urgent) then
    heavyRan = true
    now = now + (opts.heavyMs or 45)
  end
  local spent, units = 0, 0
  if opts.motion then gov:chargeMotion(opts.motion); spent = spent + opts.motion end
  while (opts.queue == nil or opts.queue[1] ~= nil) do
    local kind = opts.kind or 'cdae'
    local cost = opts.queue and opts.queue[1] or opts.cost or 3
    if not gov:try(kind) then break end
    now = now + cost + (opts.defer or 0)     -- (defer: the engine's own work per unit, outside its Lua cost)
    gov:charge(kind, cost, opts.lane)
    if opts.queue then table.remove(opts.queue, 1) end
    spent, units = spent + cost, units + 1
  end
  now = start + BASE + (now - start) -- the frame's own work plus the base frame time
  return spent, units, heavyRan
end

------------------------------------------------------------------------------------------
-- ramp: +0.05 ms/frame with work, -0.05 idle; capped at the target; no unit before tokens suffice
do
  local g = governor.new({ clock = clock })
  T.check(g.target == 2.0, 'default target 2 ms')
  T.check(g:setTarget(9) == 4 and g:setTarget(0.2) == 1 and g:setTarget(2) == 2, 'target clamped to 1..4 ms')
  local firstUnit
  local prevB = 0
  local maxStep, maxOver = 0, -1
  for i = 1, 200 do
    local spent, units = frame(g, { cost = 3 })
    maxStep = math.max(maxStep, math.abs(g.b - prevB))
    prevB = g.b
    maxOver = math.max(maxOver, spent - (g.b + g:maxUnit()))
    if units > 0 and not firstUnit then firstUnit = i end
  end
  T.check(maxStep <= 0.05 + 1e-12, 'b ramps by at most 0.05 ms per frame (got ' .. maxStep .. ')')
  T.near(g.b, 2.0, 1e-9, 'b reaches the 2 ms target and stays')
  T.check(firstUnit and firstUnit > 5, 'no "at least one per frame": the first 3 ms unit waits for tokens (frame ' .. tostring(firstUnit) .. ')')
  T.check(maxOver <= 1e-9, 'per-frame spend <= b + maxUnit')
  -- sustained rate ~ b: 100 frames at b = 2 -> ~67 units of 3 ms
  local total = 0
  for _ = 1, 100 do local s = frame(g, { cost = 3 }); total = total + s end
  T.near(total / 100, 2.0, 0.1, 'sustained streaming spend ~ b per frame')
  for _ = 1, 10 do frame(g, { hasWork = false }) end
  T.near(g.b, 1.5, 1e-9, 'idle: b ramps down 0.05 ms per frame')
  for _ = 1, 40 do frame(g, { hasWork = false }) end
  T.check(g.b == 0 and g.tokens <= 1e-12, 'idle: b and tokens reach 0 (no banking while idle)')
end

-- an expensive unit (DAE, 8 ms) waits until 8 ms of tokens accumulated: density-ramped, never two
-- in a frame, never more than b + maxUnit
do
  local g = governor.new({ clock = clock })
  local frames, units, maxOver = 0, 0, -1
  for _ = 1, 400 do
    local s, u = frame(g, { kind = 'dae', cost = 8 })
    frames, units = frames + 1, units + u
    T.check(u <= 1, 'never two 8 ms units in one frame')
    maxOver = math.max(maxOver, s - (g.b + g:maxUnit()))
  end
  T.check(units > 50 and units < 110, 'DAE units every ~4 frames at b = 2 ms (' .. units .. ' in 400)')
  T.check(maxOver <= 1e-9, 'DAE: per-frame spend <= b + maxUnit')
end

-- cost estimates follow the measured costs (EMA), so cheap .cdae units speed up
do
  local g = governor.new({ clock = clock })
  for _ = 1, 300 do frame(g, { cost = 1.2 }) end
  T.near(g.est.cdae, 1.2, 0.05, 'cdae estimate converges to the measured 1.2 ms')
  T.near(g:maxUnit(), 1.2, 0.05, 'maxUnit = largest estimate of the kinds in use')
end

-- the far cap (PR #1128 by SwimmingRemy): one unit far over its estimate (a cold shape load) inflates its kind's EMA,
-- and maxUnit with it, so the plan line b + maxUnit and the token cap 2 * maxUnit open for every OTHER kind - the
-- banked tokens then ran 100+ ms of cheap units in single frames (repeated 104-356 ms hitches with far tiles queued,
-- 2026-11-10). Past its first unit a frame now plans at most FAR_CAP_MS; the queue drains over more frames.
do
  local function seed(g)
    for _ = 1, 100 do frame(g, { cost = 4 }) end                     -- imports (cdae), est ~ 4
    for _ = 1, 100 do frame(g, { kind = 'reveal', cost = 0.3 }) end  -- reveals, est ~ 0.3
    local q0, n0 = { 700 }, 0                                        -- one cdae runs 700 ms (est 4: charged after)
    repeat frame(g, { queue = q0 }); n0 = n0 + 1 until q0[1] == nil or n0 > 50
    T.check(g.est.cdae > 100, 'the slow unit drags its estimate up (' .. string.format('%.0f', g.est.cdae) .. ' ms)')
    for _ = 1, 800 do frame(g, { queue = {} }) end                   -- work waits; the tokens bank towards 2 * maxUnit
    T.check(g.tokens > 100, 'the bank grew with maxUnit (' .. string.format('%.0f', g.tokens) .. ' ms of tokens)')
  end
  local g = governor.new({ clock = clock })
  T.check(g.farCap == governor.FAR_CAP_MS and governor.FAR_CAP_MS > 0, 'the far cap is on by default')
  seed(g)
  local q = {}
  for k = 1, 600 do q[k] = 0.3 end
  local sIn, uIn = frame(g, { kind = 'reveal', queue = q })
  T.check(sIn <= governor.FAR_CAP_MS + 1e-9,
    'after the spike a frame plans at most the far cap (' .. string.format('%.1f', sIn) .. ' ms)')
  local n, total, worst = 0, uIn, 0
  while q[1] ~= nil and n < 100 do
    local s, u = frame(g, { kind = 'reveal', queue = q })
    worst = math.max(worst, s)
    n, total = n + 1, total + u
  end
  T.check(worst <= governor.FAR_CAP_MS + 1e-9, 'every frame stays under the far cap (worst ' .. worst .. ' ms)')
  T.check(total == 600 and n > 3, 'the same units run, over ' .. (n + 1) .. ' frames')
  -- the first unit of a frame is exempt: a big one still runs (alone)
  local g3 = governor.new({ clock = clock })
  for _ = 1, 400 do frame(g3, { cost = 30 }) end
  T.check(g3.stats.units > 0, 'a unit bigger than the cap still runs (the frame\'s first)')
  -- off (FAR_CAP_MS 0 / setFarCap(0)): the pre-cap behaviour, kept for A/B
  local g2 = governor.new({ clock = clock })
  g2:setFarCap(0)
  T.check(g2.farCap == nil, 'setFarCap(0): off')
  seed(g2)
  local q2 = {}
  for k = 1, 600 do q2[k] = 0.3 end
  local sOff = frame(g2, { kind = 'reveal', queue = q2 })
  -- (the frame cap, FRAME_CAP_MS, still bounds it: base + spent under 33 ms)
  T.check(sOff > governor.FAR_CAP_MS and sOff <= governor.FRAME_CAP_MS - BASE + 1e-9,
    'far cap off: the banked tokens run up to the frame cap (' .. string.format('%.0f', sOff) .. ' ms)')
end

-- with every estimate near its prior the cap binds nothing: the same frames admit the same units as without it
do
  local g = governor.new({ clock = clock })
  local g2 = governor.new({ clock = clock })
  g2:setFarCap(0)
  local pat, pat2 = {}, {}
  for _, run in ipairs({ { g, pat }, { g2, pat2 } }) do     -- (one after the other: each its own frames' wall times)
    for i = 1, 500 do
      local kind = (i % 3 == 0) and 'reveal' or 'cdae'
      local cost = (kind == 'reveal') and 0.2 or (2 + (i * 37 % 50) / 25)       -- 2 .. 4 ms
      local _, u = frame(run[1], { kind = kind, cost = cost })
      run[2][i] = u
    end
  end
  T.check(table.concat(pat, ',') == table.concat(pat2, ','), 'sane estimates: the cap changes no decision')
end

------------------------------------------------------------------------------------------
-- isolation: a scripted queue of streaming units with heavy requests at random times; no heavy op
-- is ever within 2 frames of a frame that ran a unit; b changes by at most 0.1 ms per frame
do
  math.randomseed(12)
  local g = governor.new({ clock = clock })
  local log = {} -- per frame: { units, heavy, b }
  local wantHeavy = false
  local prevB, maxStep = 0, 0
  local waits, waitStart = {}, nil
  for i = 1, 3000 do
    if not wantHeavy and math.random() < 0.01 then wantHeavy, waitStart = true, i end
    local q = {}
    for k = 1, 10 do q[k] = 0.5 + math.random() * 4 end
    local _, units, heavy = frame(g, { heavy = wantHeavy, queue = q, kind = 'cdae' })
    if heavy then wantHeavy = false; waits[#waits + 1] = i - waitStart end
    log[i] = { units = units, heavy = heavy }
    maxStep = math.max(maxStep, math.abs(g.b - prevB))
    prevB = g.b
  end
  local bad, nHeavy = 0, 0
  for i, f in ipairs(log) do
    if f.heavy then
      nHeavy = nHeavy + 1
      for k = i - 2, i + 2 do
        if log[k] and log[k].units > 0 then bad = bad + 1 end
      end
    end
  end
  T.check(nHeavy >= 10, 'heavy ops granted (' .. nHeavy .. ')')
  T.check(bad == 0, 'no streaming unit within +-2 frames of a heavy op (violations: ' .. bad .. ')')
  T.check(maxStep <= 0.1 + 1e-12, 'b changes by at most 0.1 ms per frame (got ' .. maxStep .. ')')
  local worst = 0
  for _, w in ipairs(waits) do worst = math.max(worst, w) end
  T.check(worst <= 25, 'worst heavy wait ~ b/0.1 + 2 frames (' .. worst .. ' frames)')
end

-- the heavy slot waits for two calm frames (<= base + 1.5 ms) once b == 0; a spike delays it
do
  local g = governor.new({ clock = clock })
  for _ = 1, 80 do frame(g, { hasWork = false }) end -- base = 11.5, b = 0
  local _, _, h = frame(g, { heavy = true, wall = 20 })
  T.check(not h, 'not granted right after a slow (20 ms) frame')
  _, _, h = frame(g, { heavy = true, wall = 11.5 })
  T.check(not h, 'not granted with one slow frame among the last two')
  _, _, h = frame(g, { heavy = true, wall = 11.6 })
  T.check(h, 'granted once the last two frames were calm')
  T.check(g.quiet == 2, 'two quiet frames follow')
  local _, u1 = frame(g, { cost = 0.01 })
  local _, u2 = frame(g, { cost = 0.01 })
  T.check(u1 == 0 and u2 == 0, 'no streaming in the 2 frames after the heavy op')
  T.check(g.base < 12, 'the heavy frame is not part of base (' .. g.base .. ')')
  -- a request that is not repeated lapses: streaming ramps up again
  frame(g, { heavy = true, wall = 30 })
  for _ = 1, 30 do frame(g, { cost = 0.5 }) end
  T.check(not g.heavyPending and g.b > 1, 'unrepeated heavy request lapses, b ramps up again')
  -- never calm: granted anyway after CALM_TIMEOUT frames at b == 0
  local granted
  for i = 1, 200 do
    local _, _, hh = frame(g, { heavy = true, wall = 40, cost = 0.5 })
    if hh then granted = i; break end
  end
  T.check(granted and granted <= 21 + governor.CALM_TIMEOUT, 'restless frames: heavy op still granted (frame ' .. tostring(granted) .. ')')
end

-- urgent heavy (teleport) is granted at once; loading mode ramps to 8 ms quickly
do
  local g = governor.new({ clock = clock })
  for _ = 1, 50 do frame(g, { cost = 1 }) end
  local _, _, h = frame(g, { heavy = true, urgent = true })
  T.check(h and g.b == 0, 'urgent heavy op granted at once, streaming stopped')
  T.check(g:heavyNow(), 'a second heavy op in the same frame shares the slot')
  g:setLoading(true)
  for _ = 1, 40 do frame(g, { cost = 2 }) end
  T.check(g.b == 8 and g:targetNow() == 8, 'loading mode: target 8 ms')
  g:setLoading(false)
  for _ = 1, 200 do frame(g, { cost = 2 }) end
  T.near(g.b, 2, 1e-9, 'back to 2 ms after loading mode')
end

-- motion is charged but always runs; it delays units instead of adding up
do
  local g = governor.new({ clock = clock })
  for _ = 1, 100 do frame(g, { cost = 1 }) end
  local s, u = frame(g, { cost = 1, motion = 2.5 })
  T.check(u == 0 or s <= g.b + g:maxUnit() + 1e-9, 'motion counts against the frame cap')
end

-- M1 (docs/V1_4_SPEC.md 3.1): textured 64-cell tiles carry their texture size (far_tile.tex), and the
-- unit kind follows it; priors scale today's cdaeTex by texture area until probe P8 measures them
do
  T.check(governor.texKind(128) == 'cdaeTex128' and governor.texKind(256) == 'cdaeTex256'
    and governor.texKind(512) == 'cdaeTex512', 'kind from tex: cdaeTex128/256/512')
  T.check(governor.texKind(nil) == 'cdaeTex', 'no tex (FORMAT 3): cdaeTex')
  T.check(governor.texKind(1024) == 'cdaeTex' and governor.texKind(256.5) == 'cdaeTex' and governor.texKind('x') == 'cdaeTex',
    'a size without a prior: cdaeTex')
  T.check(governor.texKind('512') == 'cdaeTex512' and governor.texKind(256.0) == 'cdaeTex256', 'numeric forms')
  local P = governor.PRIOR
  -- measured in-game (P8, 64-cell tiles): the 348 KB shape dominates, the texture adds a little
  T.check(P.cdaeTex128 < P.cdaeTex256 and P.cdaeTex256 < P.cdaeTex512, 'priors grow with the texture')
  T.near(P.cdaeTex256, 4.0, 1e-12, '256 px prior = the P8 measurement')
  T.near(P.cdaeTex512, 4.2, 1e-12, '512 px prior = the P8 measurement')
  local g = governor.new({ clock = clock })
  local s = g:snapshot()
  T.check(s.estCdaeTex128 == P.cdaeTex128 and s.estCdaeTex256 == P.cdaeTex256 and s.estCdaeTex512 == P.cdaeTex512
    and s.estCdaeTex == P.cdaeTex, 'snapshot reports every textured kind')
  -- each kind keeps its own estimate, and a 512 px unit in use sets maxUnit
  for _ = 1, 100 do frame(g, { kind = 'cdaeTex256', cost = 5 }) end
  T.near(g.est.cdaeTex256, 5, 0.01, 'cdaeTex256 estimate learns its cost')
  T.check(g.est.cdaeTex512 == P.cdaeTex512 and g.est.cdaeTex == P.cdaeTex, 'other kinds untouched')
  g:try('cdaeTex512')
  T.near(g:maxUnit(), math.max(P.cdaeTex512, g.est.cdaeTex256), 1e-12, 'maxUnit covers every used kind')
end

-- the engine's tick beyond ENGINE_FREE_MS is a debt: no import until the budget paid it (100 mph drives, 2026-10-15)
do
  local t = 0
  local g = governor.new({ clock = function() return t end })
  g:try('cdae')                          -- the kind in use (it sets the bucket's size)
  for i = 1, 200 do t = t + 16; g:beginFrame(true) end
  T.check(g:try('cdae'), 'a calm stream: an import may run')
  T.check(math.abs(g:chargeEngine(102) - 100) < 1e-9 and g.tokens < 0, 'a 102 ms engine tick: 100 ms of debt')
  T.check(not g:try('cdae'), 'in debt: no import')
  local frames = 0
  repeat t = t + 16; g:beginFrame(true); frames = frames + 1 until g:try('cdae') or frames > 500
  T.check(frames > 10 and frames < 500, 'imports again once the budget paid it (' .. frames .. ' frames)')
  g:chargeEngine(1e6)
  T.check(g.tokens >= -governor.ENGINE_MAX_DEBT_MS, "the debt is capped")
  T.check(g:chargeEngine(1.5) == 0, 'a normal tick is free')
end
-- roomFor / noteFrame: the mod's occasional ops wait for a light frame, other work counted in it but not charged
do
  local t = 0
  local g = governor.new({ clock = function() return t end })
  g:try('cdae')
  for _ = 1, 50 do t = t + 16; g:beginFrame(true) end
  T.check(g:roomFor(4), 'a frame with nothing spent: room')
  local tok = g.tokens
  g:noteFrame(6)
  T.check(not g:roomFor(4) and g:roomFor(8), 'noted work counts in the frame')
  T.check(g.tokens == tok, 'but is not charged to the bucket')
  t = t + 16; g:beginFrame(true)
  T.check(g:roomFor(4), 'the next frame starts empty')
  g:requestHeavy(true)
  T.check(not g:roomFor(100), 'never in a heavy frame')
end
-- engine share (2026-11-13): what a frame took beyond its base and its units' Lua is learned per kind, planned with
-- the unit, and base stays the frame without streaming
do
  local g = governor.new({ clock = clock })
  for _ = 1, 400 do frame(g, { kind = 'cdaeTex256', cost = 4, defer = 6 }) end
  T.near(g.def.cdaeTex256, 6, 0.5, 'the engine share converges to the deferred cost (' .. string.format('%.2f', g.def.cdaeTex256) .. ')')
  T.near(g.base, BASE, 0.5, 'base excludes the units and their engine share (' .. string.format('%.2f', g.base) .. ')')
  T.near(g:cost('cdaeTex256'), 10, 0.6, 'planned cost = Lua estimate + engine share')
  -- the rate: ~b of planned cost per frame (Lua + engine), not b of Lua alone
  local total = 0
  for _ = 1, 200 do local s, u = frame(g, { kind = 'cdaeTex256', cost = 4, defer = 6 }); total = total + u * 10 end
  T.near(total / 200, g.b, 0.3, 'sustained planned spend ~ b per frame')
  -- a kind without deferred work learns ~0
  local g2 = governor.new({ clock = clock })
  for _ = 1, 300 do frame(g2, { cost = 3 }) end
  T.check((g2.def.cdae or 0) < 0.05, 'no deferred work: share ~0')
end

-- the frame cap: outside the cover no frame plans past FRAME_CAP_MS (its first unit excepted)
do
  local g = governor.new({ clock = clock })
  g:setLoading(true)
  for _ = 1, 100 do frame(g, { cost = 3, defer = 3 }) end
  local worst = 0
  for _ = 1, 300 do
    local q = {}
    for k = 1, 30 do q[k] = 3 end
    local t0 = now
    frame(g, { queue = q, defer = 3 })
    worst = math.max(worst, now - t0)
  end
  T.check(worst <= governor.FRAME_CAP_MS + 3.5, 'loading, not covered: frames stay near the 33 ms cap (worst ' .. worst .. ')')
end

-- the loading cover: the whole cover budget at once, frames up to COVER_FRAME_MS, heavy ops at once, then back down
do
  local g = governor.new({ clock = clock })
  for _ = 1, 80 do frame(g, { cost = 3 }) end
  g:setLoading(true); g:setCover(true)
  local q = {}
  for k = 1, 4000 do q[k] = 3 end
  local s1, u1 = frame(g, { queue = q })
  T.check(u1 >= 5, 'the cover\'s first frame already streams (' .. u1 .. ' units)')
  T.near(g.b, math.min(governor.COVER_TARGET_MS, governor.COVER_FRAME_MS - BASE), 0.01, 'b = what the cover frame leaves')
  local worst, units = 0, u1
  for i = 1, 100 do
    local t0 = now
    local _, u = frame(g, { queue = q, defer = 1 })
    if i > 15 then worst = math.max(worst, now - t0) end      -- (once the engine share of the kind is learned)
    units = units + u
  end
  T.check(worst <= governor.COVER_FRAME_MS + 4.5, 'cover frames stay near COVER_FRAME_MS (worst ' .. worst .. ')')
  T.check(units > 500, 'and stream ~8x the loading budget (' .. units .. ' units in 101 frames)')
  local _, _, h = frame(g, { heavy = true, queue = q })
  T.check(h, 'in the cover a heavy op is granted at once')
  g:chargeEngine(200)
  T.check(g.tokens >= 0, 'no engine debt in the cover')
  g:setCover(false); g:setLoading(false)
  for _ = 1, 40 do frame(g, { queue = q }) end
  T.check(g.b <= governor.DEFAULT_TARGET_MS + 1e-9, 'after the cover b is back at the normal target within 40 frames (' .. g.b .. ')')
  -- a unit bigger than the whole cover budget still runs, alone
  local g2 = governor.new({ clock = clock })
  g2:setLoading(true); g2:setCover(true)
  local big = { 45, 45, 45 }
  local _, ub = frame(g2, { queue = big })
  T.check(ub == 1, 'a 45 ms unit runs alone in a cover frame')
end

-- lanes: while far tiles wait, mid units (meshes away from the car) take at most ~(1 - FAR_SHARE) of the two lanes
do
  local g = governor.new({ clock = clock })
  g:setLoading(true)
  local mid, far = 0, 0
  for _ = 1, 600 do
    g:beginFrame(true)
    g.farWait = true
    while g:try('roadWalk', 'mid') do now = now + 6; g:charge('roadWalk', 6, 'mid'); mid = mid + 6 end
    while g:try('cdaeTex256', 'far') do now = now + 4; g:charge('cdaeTex256', 4, 'far'); far = far + 4 end
    now = now + BASE
  end
  local share = far / (mid + far)
  T.check(math.abs(share - governor.FAR_SHARE) < 0.1, 'far tiles get ~FAR_SHARE of the streaming (' .. string.format('%.2f', share) .. ')')
  -- near units never give way; mid units run freely when no far tile waits
  g.farWait = true
  g.laneUse.mid, g.laneUse.far = 1000, 0
  g.tokens = 100
  T.check(not g:try('roadWalk', 'mid') and g:try('roadWalk', 'near'), 'mid gives way, near does not')
  g.farWait = false
  T.check(g:try('roadWalk', 'mid'), 'no far tile waiting: mid runs')
end

-- due work (2026-11-14, the Steam Deck's road queue): a caller's setDue raises the target to DUE_TARGET_MS while it says
-- so (ramped at RAMP_UP_DUE, lapsing a frame after it stops), and a due unit waiting for tokens reserves them: the rest
-- of the frame no other kind takes the bucket below its cost (cheap units excepted)
do
  now = 0
  local g = governor.new({ clock = clock })
  for _ = 1, 100 do frame(g, { cost = 1e9 }) end         -- (work waits, nothing affordable: b at the target)
  T.check(math.abs(g.b - 2) < 1e-9 and not g:isDue(), 'not due: the default target')
  local steps = 0
  for _ = 1, 100 do
    g:setDue(true)
    local b0 = g.b
    frame(g, { cost = 1e9 })
    T.check(g.b - b0 <= governor.RAMP_UP_DUE + 1e-9, 'due: b ramps by at most RAMP_UP_DUE')
    steps = steps + 1
    if g.b >= governor.DUE_TARGET_MS then break end
  end
  T.check(math.abs(g.b - governor.DUE_TARGET_MS) < 1e-9 and steps < 30,
    'due: b reaches DUE_TARGET_MS within ' .. steps .. ' frames')
  T.check(g:snapshot().due == true and g:targetNow() == governor.DUE_TARGET_MS, 'the snapshot and target say due')
  frame(g, { cost = 1e9 })     -- (setDue is said during a frame and sets the next one's target; none in the last)
  T.check(not g:isDue() and g:targetNow() == 2, 'not said in the frame before: lapsed, the default target again')
  T.check(g.b < governor.DUE_TARGET_MS, '... b ramps back down')
  -- reserve: a due road mesh of 8 ms waits; a far tile of 4 ms cannot take the bucket below it, a reveal can
  local g2 = governor.new({ clock = clock })
  for _ = 1, 50 do frame(g2, { cost = 1e9 }) end
  g2.est.roadSurf, g2.est.cdaeTex256 = 8, 4
  g2:beginFrame(true); g2.tokens = 6
  T.check(not g2:try('roadSurf', 'near'), '6 ms of tokens: the 8 ms mesh waits')
  g2:reserve('roadSurf')
  T.check(not g2:try('cdaeTex256', 'far'), 'reserved: the far tile may not take the bucket below it')
  T.check(g2:try('reveal'), 'a cheap unit still runs')
  T.check(g2:snapshot().reserveYields == 1, 'the yield counted')
  g2:beginFrame(true); g2.tokens = 6
  T.check(g2:try('cdaeTex256', 'far'), 'the reservation lasts one frame')
  g2:beginFrame(true); g2.tokens = 14
  g2:reserve('roadSurf')
  T.check(g2:try('cdaeTex256', 'far'), 'enough tokens for both: the far tile runs')
  -- the far field no longer starves a due mesh: an 8 ms mesh and 4 ms far tiles every frame, the mesh first
  local g3 = governor.new({ clock = clock })
  g3.est.roadSurf, g3.est.cdaeTex256 = 8, 4
  local meshes = { plain = 0, due = 0 }
  for _, mode in ipairs({ 'plain', 'due' }) do
    g3 = governor.new({ clock = clock })
    g3.est.roadSurf, g3.est.cdaeTex256 = 8, 4
    for _ = 1, 600 do
      g3:beginFrame(true)
      if mode == 'due' then g3:setDue(true) end
      if g3:try('roadSurf', 'near') then now = now + 8; g3:charge('roadSurf', 8, 'near'); meshes[mode] = meshes[mode] + 1
      elseif mode == 'due' then g3:reserve('roadSurf') end
      while g3:try('cdaeTex256', 'far') do now = now + 4; g3:charge('cdaeTex256', 4, 'far') end
      now = now + BASE
    end
  end
  T.check(meshes.due > 4 * math.max(1, meshes.plain), 'due + reserve: the meshes keep coming beside the far tiles ('
    .. meshes.plain .. ' -> ' .. meshes.due .. ' in 600 frames)')
end

-- tryUrgent (roads.lua's drawn surfaces under the car): may run short of tokens, but only as the frame's first unit,
-- with little else done in the frame, and not deep in debt
do
  now = 0
  local g = governor.new({ clock = clock })
  for _ = 1, 50 do frame(g, { cost = 1e9 }) end
  g:beginFrame(true); g.tokens = -3
  T.check(not g:try('roadSurf', 'near') and g:tryUrgent('roadSurf'), 'starved (tokens -3): try no, tryUrgent yes')
  g:charge('roadSurf', 8, 'near')
  T.check(not g:tryUrgent('roadSurf'), 'never a second unit in the frame')
  g:beginFrame(true); g.tokens = -governor.URGENT_DEBT_MS - 1
  T.check(not g:tryUrgent('roadSurf'), 'deep in debt (an engine tick, earlier urgent units): waits')
  g:beginFrame(true); g.tokens = 0
  g:charge('roadDelete', governor.URGENT_ROOM_MS + 1)
  T.check(not g:tryUrgent('roadSurf'), 'the frame already busy with other units: waits')
  g:beginFrame(true); g.tokens = 0
  g:noteOther(governor.URGENT_ROOM_MS + 1)
  T.check(not g:tryUrgent('roadSurf'), '... or with the mod\'s other noted work (messages, trees)')
  g:beginFrame(true); g.tokens = 0
  g.blocked = true
  T.check(not g:tryUrgent('roadSurf'), 'not in quiet frames')
  g.blocked = false
  g:requestHeavy()
  T.check(not g:tryUrgent('roadSurf'), 'not while a heavy op waits for its slot')
  -- a run of urgent units paces itself: each charged, the bucket's debt stops them until the budget repays it
  local g2 = governor.new({ clock = clock })
  for _ = 1, 50 do frame(g2, { queue = {} }) end     -- (work waiting, none run: b at its target)
  local ran = 0
  for _ = 1, 300 do
    g2:beginFrame(true)
    if g2:tryUrgent('roadSurf') then g2:charge('roadSurf', 9, 'near'); ran = ran + 1 end
    now = now + BASE
  end
  T.check(ran > 30 and ran < 300 * 2.0 / 9 + 10, 'urgent units paced by the bucket (' .. ran .. ' in 300 frames at b 2)')
end
T.done('test_governor')
