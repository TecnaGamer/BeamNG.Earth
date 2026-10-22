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
    now = now + cost
    gov:charge(kind, cost)
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
-- KyzoFox 1.5.5: driving fast with work queued, the budget's target grows to FAST_TARGET_MS (not past loading mode)
do
  local g = governor.new({ clock = clock })
  T.check(g:targetNow() == governor.DEFAULT_TARGET_MS, 'fast: off by default')
  g:setFast(true)
  T.check(g:targetNow() == governor.FAST_TARGET_MS, 'fast: the larger target')
  g:setLoading(true)
  T.check(g:targetNow() == governor.LOADING_TARGET_MS, 'fast: loading mode still wins')
  g:setLoading(false); g:setFast(false)
  T.check(g:targetNow() == governor.DEFAULT_TARGET_MS, 'fast: back to the target')
end
T.done('test_governor')
