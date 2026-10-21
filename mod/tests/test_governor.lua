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
  T.check(g.target == governor.DEFAULT_TARGET_MS, 'default target 2.5 ms')
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
  -- Why 1.5 ms/frame instead of 2.0 ms/frame for 3 ms 'cdae' units (P10-B L-H-L spacing):
  -- At b = 2.0 ms and cdae cost = 3.0 ms, frameCap = b + maxUnit = 5.0 ms fits at most 1 unit (3 ms)
  -- per active frame; because cdae is in SCENE_STRUCT_KINDS and 3.0 ms >= STRUCT_COOLDOWN_MS (1.8 ms),
  -- each active frame is followed by 1 L-H-L calm frame (structCooldown = 1), so 50 of 100 frames run
  -- 1 unit -> 50 * 3 ms / 100 = 1.5 ms/frame. Non-structural kinds (e.g. 'retex') still spend ~ b (2.0 ms).
  local total = 0
  for _ = 1, 100 do local s = frame(g, { cost = 3 }); total = total + s end
  T.near(total / 100, 1.5, 0.1, 'sustained structural streaming spend with L-H-L 1-frame cooldown ~ 1.5 ms per frame')
  local totalNonStruct = 0
  for _ = 1, 100 do local s = frame(g, { kind = 'retex', cost = 3 }); totalNonStruct = totalNonStruct + s end
  T.near(totalNonStruct / 100, 2.0, 0.1, 'sustained non-structural streaming spend ~ b per frame')
  for _ = 1, 10 do frame(g, { hasWork = false }) end
  T.near(g.b, 1.5, 1e-9, 'idle: b ramps down 0.05 ms per frame')
  for _ = 1, 40 do frame(g, { hasWork = false }) end
  T.check(g.b == 0 and g.tokens <= 1e-12, 'idle: b and tokens reach 0 (no banking while idle)')
end

-- an expensive unit (DAE, 8 ms) waits until 8 ms of tokens accumulated: density-ramped, never two
-- in a frame, never more than b + maxUnit
do
  local g = governor.new({ clock = clock })
  g:setTarget(2)
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
-- is ever within 2 frames of a frame that ran a unit; immediate 2-frame quiet gate
do
  math.randomseed(12)
  local g = governor.new({ clock = clock })
  local log = {} -- per frame: { units, heavy, b }
  local wantHeavy = false
  local waits, waitStart = {}, nil
  for i = 1, 3000 do
    if not wantHeavy and math.random() < 0.01 then wantHeavy, waitStart = true, i end
    local q = {}
    for k = 1, 10 do q[k] = 0.5 + math.random() * 4 end
    local _, units, heavy = frame(g, { heavy = wantHeavy, queue = q, kind = 'cdae' })
    if heavy then wantHeavy = false; waits[#waits + 1] = i - waitStart end
    log[i] = { units = units, heavy = heavy }
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
  local worst = 0
  for _, w in ipairs(waits) do worst = math.max(worst, w) end
  T.check(worst <= 6, '2-frame heavy quiet gate grants without 60-frame starvation (' .. worst .. ' frames)')
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

-- D3D12 presentation pacing awareness: w1 > 11.0 ms holds b = 0 for 1 cooldown frame
do
  local g = governor.new({ clock = clock })
  for _ = 1, 40 do g:beginFrame(true, 6.7) end
  local bBefore = g.b
  T.check(bBefore > 1.5, 'steady 120 FPS budget active before stall')
  g:beginFrame(true, 12.5)
  T.check(g.b == 0 and g.blocked, 'w1 > 11.0 ms holds b = 0 for 1 cooldown frame')
  g:beginFrame(true, 6.7)
  T.check(g.b >= bBefore and not g.blocked, 'budget resumes cleanly after 1 cooldown frame')
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
  T.near(g.b, governor.DEFAULT_TARGET_MS, 1e-9, 'back to default target after loading mode')
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
  g.prevFrameUnits = 1
  T.check(math.abs(g:chargeEngine(103.5) - 100) < 1e-9 and g.tokens < 0, 'a 103.5 ms engine tick: 100 ms of debt')
  T.check(not g:try('cdae'), 'in debt: no import')
  local frames = 0
  repeat t = t + 16; g:beginFrame(true); frames = frames + 1 until g:try('cdae') or frames > 500
  T.check(frames > 3 and frames < 500, 'imports again once the budget paid it (' .. frames .. ' frames)')
  g.prevFrameUnits = 1
  g:chargeEngine(1e6)
  T.check(g.tokens >= -governor.ENGINE_MAX_DEBT_MS, "the debt is capped")
  T.check(g:chargeEngine(1.5) == 0, 'a normal tick is free')
end

------------------------------------------------------------------------------------------
-- P10-B: closed-loop RenderShadows + FileWatchers debt, per-kind deferredEst EMA,
-- scene-structure mutual exclusion + L-H-L calm frame spacing, burst-mode hysteresis, and maxFrameSpent cap
do
  local t = 0
  local g = governor.new({ clock = function() return t end })
  for _ = 1, 120 do t = t + 10; g:beginFrame(true, 10.0) end
  -- 1. Learn shadowBase on idle frames
  g.prevFrameUnits = 0
  g:chargeEngine(0.3, 0.4, 0.0)
  T.near(g.shadowBase, 0.4, 1e-6, 'shadowBase initialized on calm non-streaming frame')

  -- 2. Run a cdae unit, then report a 9.4 ms RenderShadows spike on the next frame
  T.check(g:try('cdae'), 'cdae allowed before spike')
  g:charge('cdae', 2.2)
  -- Co-scheduling different scene-structure kind in the same frame is blocked:
  T.check(not g:try('treeBatch') and not g:try('reveal'), 'never co-schedule two scene-structure kinds in one frame')
  -- Next frame: L-H-L cooldown after >= 1.8 ms structural spend + RenderShadows debt
  t = t + 18
  g:beginFrame(true, 18.0)
  T.check(g.structCooldown == 1, 'L-H-L calm frame scheduled after heavy structural frame')
  T.check(not g:try('cdae'), 'structural unit blocked on L-H-L calm frame')
  local debt = g:chargeEngine(0.5, 9.4, 2.5)
  T.check(debt > 7.0 and g.tokens < 0, 'RenderShadows 9.4 ms + FileWatchers 2.5 ms charged as engine debt (' .. debt .. ')')
  T.check((g.deferredEst.cdae or 0) > 0.5, 'deferredEst.cdae learned from RenderShadows spike (' .. tostring(g.deferredEst.cdae) .. ')')
  T.check(g:effectiveEst('cdae') > g.est.cdae, 'effectiveEst includes learned deferred cost')

  -- 3. No starvation: budget recovers once calm frames pay off the debt and work completes within N frames
  local completed = 0
  local framesToComplete = 0
  for f = 1, 120 do
    t = t + 10
    g:beginFrame(true, 10.0)
    g:chargeEngine(0.3, 0.4, 0.0)
    if g:try('cdae') then
      g:charge('cdae', 2.0)
      completed = completed + 1
      if completed >= 10 then framesToComplete = f; break end
    end
  end
  T.check(completed == 10 and framesToComplete <= 100, 'no starvation: 10 units complete after spike within ' .. framesToComplete .. ' frames')
  T.check(g.stats.maxFrameSpent <= governor.LOADING_TARGET_MS + 1e-9,
    'maxFrameSpent capped <= 8.0 ms (' .. g.stats.maxFrameSpent .. ')')

  -- 4. Burst detector hysteresis
  local gb = governor.new({ clock = function() return t end })
  gb:noteQueue(2)
  T.check(not gb.burst, 'calm queue: not in burst mode')
  gb:noteQueue(12) -- growth rate +10 >= BURST_ENTER_RATE (8)
  T.check(gb.burst, 'queue jump +10 enters burst mode')
  T.check(gb:targetNow() <= governor.BURST_TARGET_MS, 'burst mode lowers target to BURST_TARGET_MS')
  for i = 1, governor.BURST_HYST_FRAMES - 1 do
    gb:noteQueue(4)
    T.check(gb.burst, 'hysteresis holds burst mode on calm frame ' .. i)
  end
  gb:noteQueue(4)
  T.check(not gb.burst, 'exits burst mode after BURST_HYST_FRAMES calm frames')

  -- 5. R1: urgent beats burst (loading > urgent > burst > normal), and structural kinds can run twice in one urgent frame
  local gu = governor.new({ clock = function() return t end })
  gu:setUrgent(true)
  gu:noteQueue(20) -- sudden jump -> burst = true while urgent = true
  T.check(gu.burst and gu.urgent, 'both burst and urgent active')
  T.near(gu:targetNow(), governor.URGENT_TARGET_MS, 1e-9, 'R1: urgent + burst -> targetNow() == urgent target')
  gu:setLoading(true)
  T.near(gu:targetNow(), governor.LOADING_TARGET_MS, 1e-9, 'R1: loading beats urgent and burst')
  gu:setLoading(false)
  for _ = 1, 150 do t = t + 8; gu:beginFrame(true, 8.0, 40.0) end
  T.check(gu:try('cdae'), 'urgent + burst: first structural unit allowed')
  gu:charge('cdae', 2.5)
  T.check(gu:try('roadWalk'), 'urgent + burst: second structural unit (distinct kind) allowed in same urgent frame')
  gu:charge('roadWalk', 2.5)
  T.check(gu.sceneStructCount == 2, 'sceneStructCount tracks both structural units charged in urgent frame')
  t = t + 8
  gu:beginFrame(true, 8.0, 40.0)
  T.check(gu.structCooldown == 0, 'R1: beginFrame does not arm structCooldown while urgent')
  T.check(gu:try('cdae'), 'urgent: consecutive frame also allows structural unit without L-H-L cooldown')

  -- 6. Burst transient cap: cannot last longer than BURST_MAX_FRAMES; refractory blocks immediate re-entry
  local gm = governor.new({ clock = function() return t end })
  gm:noteQueue(20) -- rate +20 -> enters burst (frame 1 of burst)
  T.check(gm.burst, 'burst entered on initial surge')
  local activeBurstFrames = 1
  for i = 1, governor.BURST_MAX_FRAMES + 10 do
    gm:noteQueue(20 + i) -- continuously growing queue
    if gm.burst then activeBurstFrames = activeBurstFrames + 1 end
  end
  T.check(activeBurstFrames == governor.BURST_MAX_FRAMES,
    'burst cannot last longer than BURST_MAX_FRAMES (' .. activeBurstFrames .. ' == ' .. governor.BURST_MAX_FRAMES .. ')')
  T.check(not gm.burst, 'burst is off after BURST_MAX_FRAMES')
  -- Refractory period blocks immediate re-entry even on large rate spikes
  local gm2 = governor.new({ clock = function() return t end })
  gm2:noteQueue(20)
  for _ = 1, governor.BURST_MAX_FRAMES do gm2:noteQueue(30) end
  T.check(not gm2.burst and gm2.burstRefractory == governor.BURST_REFRACTORY_FRAMES,
    'refractory armed to BURST_REFRACTORY_FRAMES upon hitting BURST_MAX_FRAMES')
  for i = 1, governor.BURST_REFRACTORY_FRAMES do
    gm2:noteQueue(30 + i * 10) -- +10 per frame >= BURST_ENTER_RATE, still blocked by refractory
    T.check(not gm2.burst, 'refractory blocks burst re-entry on frame ' .. i)
  end
  gm2:noteQueue(1000) -- first frame after refractory expires
  T.check(gm2.burst, 'burst can re-enter once refractory period finishes')

  -- 7. A constantly high but NOT growing queue (q = 40 every frame) does not keep burst on
  local gc = governor.new({ clock = function() return t end })
  gc.prevQueued, gc.prevQueued2, gc.prevQueued3 = 40, 40, 40
  gc:noteQueue(40)
  T.check(not gc.burst, 'steady non-growing q = 40 does not enter burst')
  -- Even if an initial arrival spike triggered burst at q = 40, flat q = 40 exits at BURST_MAX_FRAMES and stays off
  local gc2 = governor.new({ clock = function() return t end })
  for _ = 1, governor.BURST_MAX_FRAMES + governor.BURST_REFRACTORY_FRAMES + 30 do
    gc2:noteQueue(40)
  end
  T.check(not gc2.burst, 'constantly high non-growing queue (q = 40 every frame) does not keep burst on')
end
T.done('test_governor')
