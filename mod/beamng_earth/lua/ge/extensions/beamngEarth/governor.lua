-- Main-thread frame governor (docs/V1_2_DESIGN.md 5.4). Pure Lua, runs under plain luajit.
--
-- Why: the engine drives physics from a smoothed dt (clamp, median of 3, slow EMA; design 5.1).
-- One isolated slow frame is rejected by the median (a hitch, its time is never simulated); runs of
-- slow frames, or H,L,H,L patterns, pass the median and drag the EMA, so the sim runs slow and then
-- fast ("time warp"). The v1.1 pattern - a 60 ms terrain commit followed by 23-74 tile imports about
-- 25 ms apart - did exactly that. So:
--  * streaming (tile imports, reveals, deletes) gets a SMALL, SMOOTH per-frame budget b that ramps
--    towards bT (default 2 ms) at +0.05 ms/frame while there is work and back down at -0.05 ms/frame
--    when idle. A token bucket (tokens += b per frame) decides which units run: a unit runs only
--    when tokens >= its estimated cost, and a frame never plans more than b + maxUnit. There is no
--    "at least one unit per frame" rule: an expensive unit waits until enough tokens accumulated.
--  * heavy operations (terrain commit, rebase phase 2, teleport) are isolated: once one is
--    requested, b is immediately clamped to 0 for a 2-frame quiet gate (QUIET_BEFORE = 2); the slot
--    is granted once b == 0 for QUIET_BEFORE frames, no unit ran in the 2 frames before, and those
--    2 frames were calm (<= base + 1.5 ms); then QUIET_AFTER = 2 quiet frames follow (no streaming)
--    and b ramps up from 0 again. So no tile import ever runs within 2 frames of a heavy frame, and
--    the heavy frame stays a single isolated spike the dt filter rejects without 60-frame starvation.
--  * D3D12 presentation pacing awareness: when frame delta w1 > 11.0 ms (PRESENT_STALL_MS, indicating
--    a D3D12 present stall), b is held at 0 for 1 cooldown frame so the D3D12 flip queue drains cleanly.
--  * loading mode (after a teleport, or below 1 m/s for over 3 s): bT = 8 ms, ramped faster; a warp
--    is invisible there and it speeds up the initial fill.
-- Clock: the wall time between beginFrame() calls (hptimer, not the smoothed dtReal). base = median
-- of the last 64 non-heavy frames.
-- Unit cost estimates are an EMA per unit kind of the measured cost. (Design 5.4 also scales them by
-- triangle count; every far tile of a session has the same grid, so one estimate per kind suffices.
-- 64-cell tiles (docs/V1_4_SPEC.md 3.1 M1, FORMAT 4) carry their texture size, and the kind is
-- cdaeTex128/256/512 by that size: texKind.)

local stopwatch = require('beamngEarth/stopwatch')

local M = {}

local max, min, huge = math.max, math.min, math.huge

M.DEFAULT_TARGET_MS = 2.5
M.MIN_TARGET_MS, M.MAX_TARGET_MS = 1.0, 4.0
M.URGENT_TARGET_MS = 6.0
M.LOADING_TARGET_MS = 8.0
M.RAMP_UP = 0.05           -- ms per frame while there is work
M.RAMP_UP_LOADING = 0.25   -- ms per frame in loading mode
M.RAMP_DOWN_IDLE = 0.05    -- ms per frame without work
M.RAMP_DOWN_HEAVY = 0.1    -- legacy constant retained for protocol/test table readers
M.BASE_FRAMES = 64         -- base = median of this many non-heavy frames
M.CALM_MARGIN_MS = 1.5     -- a calm frame is <= base + this
M.QUIET_BEFORE = 2         -- frames without streaming before a heavy op
M.QUIET_AFTER = 2          -- frames without streaming after it
M.PRESENT_STALL_MS = 11.0  -- frame delta w1 (ms) threshold indicating a D3D12 present stall
M.PRESENT_COOLDOWN = 1     -- cooldown frames with b = 0 after a D3D12 present stall
M.CALM_TIMEOUT = 60        -- frames a granted-but-not-calm heavy op waits at most (b == 0 already)
M.EST_ALPHA = 0.2          -- cost EMA gain
-- The engine's own deferred work for what streaming made (2026-10-15, 100 mph drives: new tiles' shapes, materials and
-- textures cost the NEXT frame's engine "Ticking" 50-250 ms, which no unit's measured Lua cost included, so the bucket
-- kept importing at full rate and every frame ran 100+ ms): each frame's engine tick beyond ENGINE_FREE_MS is a debt on
-- the bucket (chargeEngine), at most ENGINE_MAX_DEBT_MS, so imports wait until the budget has paid for it.
M.ENGINE_FREE_MS = 2.0
M.ENGINE_MAX_DEBT_MS = 8.0
M.SHADOW_FREE_MS = 1.2     -- baseline RenderShadows (ms) before excess counts as engine debt
M.FW_FREE_MS = 0.5         -- baseline FileWatchers (ms) before excess counts as engine debt
M.DEFERRED_ALPHA = 0.15    -- EMA gain for per-kind learned deferred engine cost
M.MAX_SCENE_STRUCT_PER_FRAME = 1 -- max distinct scene-structure kinds per non-urgent governed frame
M.STRUCT_COOLDOWN_MS = 1.8 -- Lua or deferred spend threshold that forces a 1-frame L-H-L calm gap after a structural mutation
M.BURST_ENTER_QUEUED = 24  -- enter burst mode when queued streaming backlog >= this and queue grew recently
M.BURST_ENTER_RATE = 8     -- ... or when queue grows by >= this many items in one frame
M.BURST_EXIT_QUEUED = 8    -- exit burst mode once backlog drops <= this for BURST_HYST_FRAMES
M.BURST_HYST_FRAMES = 8    -- consecutive low-backlog frames required to exit burst mode early
M.BURST_MAX_FRAMES = 90    -- maximum burst duration (~1.5 s at 60 Hz) so burst stays a transient
M.BURST_REFRACTORY_FRAMES = 60 -- cooldown frames (~1.0 s) after max-duration burst before burst can re-enter
M.BURST_TARGET_MS = 2.0    -- tighter Lua streaming target in burst mode to leave room for RenderShadows/FileWatchers
-- Prior cost estimates (ms) per unit kind: in-game P5/P6 (2026-10-10): a 2k-tri .cdae first load
-- 3.4 ms (8k-tri 1.5 ms; v1.2 drive EMA 1.93), a 2k-tri .dae 7.5-25 ms; unhide is free; setPosition
-- ~0.006 ms (p6). v1.3 satellite imagery (lead probe p_sat.lua): a Lua-created per-tile Material
-- 0.13-0.46 ms ('material'), a .cdae tile spawn including its own texture 2.5-3.5 ms ('cdaeTex');
-- deleting such a Material (frees its texture) is not measured yet ('matDelete', a guess).
-- 64-cell textured tiles (M1, far_tile.tex): cdaeTex scaled by texture area until probe P8 measures
-- them (128 px 0.25x, 256 px 1x, 512 px 4x); 'cdaeTex' stays the kind of FORMAT 3 tiles.
-- (One line: server/tests/modsim.py lua_consts reads flat tables line by line.)
-- cdaeTex256 / cdaeTex512: the in-game P8 EMA for 64-cell tiles (2026-10-10, ~300 objects, Baltimore:
-- 4.03 / 4.21 ms; the texture size barely matters next to the 348 KB shape); cdaeTex128 not measured
-- (scaled from 256 by the shape's share, ~3.9).
-- 'retex' (far_retex: a material's colorMap switched, flush + reload, the new texture loaded) is not
-- measured yet: a guess between a material (0.3) and a textured import (4).
-- 'roadChunk' (roads.lua: one DecalRoad of <= 100 nodes, insertNodeNoRegen + regenerate) ~0.45 ms in
-- probe P3; 'roadDelete' 0.02-0.04 ms (P3); 'roadWalk' (a road tile's sidewalk TSStatic, a .cdae like a far tile) ~3 ms, a guess.
M.PRIOR = { cdae = 3.0, dae = 8.0, reveal = 0.05, delete = 0.3, cdaeTex = 3.0, material = 0.3, matDelete = 0.2, cdaeTex128 = 3.9, cdaeTex256 = 4.0, cdaeTex512 = 4.2, retex = 1.0, roadChunk = 0.5, roadDelete = 0.05, roadWalk = 3.0, treeBatch = 0.5 }
M.DELETE_BUDGET_MS = 0.5   -- dedicated per-frame delete budget (ms) independent of import tokens

local DELETE_KINDS = { delete = true, matDelete = true, roadDelete = true }
local SCENE_STRUCT_KINDS = {
  cdae = true, dae = true, cdaeTex = true, cdaeTex128 = true, cdaeTex256 = true, cdaeTex512 = true,
  reveal = true, roadWalk = true, treeBatch = true,
}

-- Unit kind of a textured .cdae import from its far_tile.tex (px): cdaeTex<px> for a size with a
-- prior, else (FORMAT 3 tiles have no tex) 'cdaeTex'.
function M.texKind(px)
  px = tonumber(px)
  if px and px == math.floor(px) then
    local k = 'cdaeTex' .. string.format('%d', px)
    if M.PRIOR[k] then return k end
  end
  return 'cdaeTex'
end

local Gov = {}
Gov.__index = Gov

-- opts: { clock = function() -> ms (monotonic), targetMs = number }
function M.new(opts)
  opts = opts or {}
  local est = {}
  local deferredEst = {}
  for k, v in pairs(M.PRIOR) do est[k] = v; deferredEst[k] = 0 end
  local g = setmetatable({
    clock = opts.clock or stopwatch.clock(),
    target = M.DEFAULT_TARGET_MS,
    loading = false,
    urgent = false,
    burst = false,      -- burst mode when many new tiles arrive at once
    burstCalmFrames = 0,
    burstActiveFrames = 0,
    burstRefractory = 0,
    prevQueued = 0,
    prevQueued2 = 0,
    prevQueued3 = 0,
    maxUnitCached = 0,
    b = 0,              -- current streaming budget, ms per frame
    bPrev = 0,          -- b of the previous frame
    tokens = 0,         -- token bucket, ms
    emaEngineMs = 8.0,  -- EMA of engine base frame cost (excluding our Lua streaming work), ms
    lastSpentMs = 0.0,  -- Lua streaming ms spent in the previous frame
    spent = 0.0,        -- Lua streaming ms spent in the current frame
    heavyAllowed = true, -- whether predictive D3D12 pacing allows a heavy op this frame
    est = est,          -- per kind cost estimate, ms
    deferredEst = deferredEst, -- per kind learned deferred engine cost (RenderShadows/Ticking), ms
    used = {},          -- kinds asked for at least once (they define maxUnit)
    frameKinds = {},    -- kind -> count run in the current frame
    prevFrameKinds = {}, -- kind -> count run in the previous frame (for deferred cost attribution)
    hist = {}, histN = 0, -- ring of recent non-heavy frame times
    base = nil,
    tickBase = nil,     -- EMA of idle C++ engine Ticking (ms) when prevFrameUnits == 0
    shadowBase = nil,   -- EMA of idle C++ engine RenderShadows (ms) when prevFrameUnits == 0
    frame = 0,
    lastStart = nil,
    w1 = nil, w2 = nil, -- wall durations of the previous two frames
    prevHeavy = false,  -- the frame that just ended was heavy (its time is not part of base)
    frameSpent = 0,     -- ms charged this frame (import/reveal units + motion)
    delSpent = 0,       -- ms charged this frame for delete units
    engineDebt = 0,     -- unpaid engine tick/shadow debt (ms) from chargeEngine
    frameUnits = 0,     -- units run this frame
    prevFrameUnits = 0, -- units run in the previous frame
    units = 0,          -- alias for units run this frame
    sceneStructCount = 0, -- scene-structure mutation kinds run this frame
    prevStructCount = 0,
    structCooldown = 0, -- L-H-L calm frames remaining after a heavy structural mutation frame
    lastStreamFrame = -huge, -- last frame a unit ran in
    heavyPending = false, heavySince = nil, heavyReqFrame = nil,
    heavyFrame = -huge, -- last frame that ran a heavy op
    quiet = 0,          -- quiet frames still to come after a heavy op
    zeroFrames = 0,     -- consecutive completed frames with b == 0 and no streaming units
    presentCooldown = 0, -- cooldown frames remaining after a D3D12 present stall (w1 > 11.0 ms)
    bSaved = nil,       -- pre-cooldown streaming budget restored after 1 present cooldown frame
    blocked = false,    -- no units this frame (quiet or heavy)
    stats = { heavy = 0, units = 0, maxFrameSpent = 0, presentCooldowns = 0, burstFrames = 0, shadowDebt = 0 },
  }, Gov)
  if opts.targetMs then g:setTarget(opts.targetMs) end
  return g
end

function Gov:setTarget(ms)
  ms = tonumber(ms)
  if ms then self.target = max(M.MIN_TARGET_MS, min(M.MAX_TARGET_MS, ms)) end
  return self.target
end

function Gov:setLoading(on) self.loading = on and true or false end

function Gov:setUrgent(urgent) self.urgent = urgent and true or false end

-- Update burst detector from total pending streaming items (far tiles + roads + trees + IPC queue).
-- Enters burst mode transiently on a sudden arrival (rate >= BURST_ENTER_RATE) or high growing backlog
-- (q >= BURST_ENTER_QUEUED with positive recent growth); exits after BURST_HYST_FRAMES calm frames
-- (<= BURST_EXIT_QUEUED) or after at most BURST_MAX_FRAMES frames (followed by BURST_REFRACTORY_FRAMES).
function Gov:noteQueue(totalQueued)
  local q = max(0, tonumber(totalQueued) or 0)
  local p1 = self.prevQueued or 0
  local p2 = self.prevQueued2 or p1
  local p3 = self.prevQueued3 or p2
  local rate = q - p1
  local grew = rate > 0 or (rate >= 0 and (q > p2 or q > p3))
  self.prevQueued3, self.prevQueued2, self.prevQueued = p2, p1, q
  if not self.burst then
    if (self.burstRefractory or 0) > 0 then
      self.burstRefractory = self.burstRefractory - 1
    elseif rate >= M.BURST_ENTER_RATE or (q >= M.BURST_ENTER_QUEUED and grew) then
      self.burst = true
      self.burstCalmFrames = 0
      self.burstActiveFrames = 0
    end
  else
    self.burstActiveFrames = (self.burstActiveFrames or 0) + 1
    if q <= M.BURST_EXIT_QUEUED then
      self.burstCalmFrames = (self.burstCalmFrames or 0) + 1
      if self.burstCalmFrames >= M.BURST_HYST_FRAMES then
        self.burst = false
        self.burstCalmFrames = 0
        self.burstActiveFrames = 0
      end
    else
      self.burstCalmFrames = 0
    end
    if self.burst and self.burstActiveFrames >= M.BURST_MAX_FRAMES then
      self.burst = false
      self.burstCalmFrames = 0
      self.burstActiveFrames = 0
      self.burstRefractory = M.BURST_REFRACTORY_FRAMES
    end
  end
  return self.burst
end

function Gov:targetNow()
  if self.loading then return M.LOADING_TARGET_MS end
  if self.urgent then return max(self.target, M.URGENT_TARGET_MS) end
  if self.burst then return min(self.target, M.BURST_TARGET_MS) end
  return self.target
end

-- Effective estimated cost of a unit kind (Lua cost + damped share of learned deferred engine cost).
function Gov:effectiveEst(kind)
  local e = self.est[kind] or 1
  local def = self.deferredEst and self.deferredEst[kind] or 0
  if def > 0 then
    return e + min(e * 0.5, def * 0.35)
  end
  return e
end

local function recomputeMaxUnit(self)
  local m = 0
  for k in pairs(self.used) do
    local ek = self.est[k] or 0
    if ek > m then m = ek end
  end
  self.maxUnitCached = m
  return m
end

-- Largest estimated unit cost among the kinds in use (cached per frame and updated on try/charge).
function Gov:maxUnit()
  return self.maxUnitCached or 0
end

local medianScratch = {}

local function median(t, n)
  for i = 1, n do medianScratch[i] = t[i] end
  for i = #medianScratch, n + 1, -1 do medianScratch[i] = nil end
  table.sort(medianScratch)
  if n % 2 == 1 then return medianScratch[(n + 1) / 2] end
  return 0.5 * (medianScratch[n / 2] + medianScratch[n / 2 + 1])
end

-- Call once per frame, first thing in onUpdate. Supports both:
--   Gov:beginFrame(dtReal, speedMps)
-- and
--   Gov:beginFrame(hasWork, wallMs, speedMps)
function Gov:beginFrame(hasWork, wallMs, speedMps)
  if self.frame > 0 then
    if self.b == 0 and (self.frameUnits or 0) == 0 and not self.prevHeavy then
      self.zeroFrames = (self.zeroFrames or 0) + 1
    else
      self.zeroFrames = 0
    end
  end
  self.frame = self.frame + 1
  local t = self.clock()
  local dtReal
  if type(hasWork) == 'number' then
    dtReal = hasWork
    speedMps = wallMs
    if dtReal > 1.0 then
      wallMs = dtReal
      dtReal = dtReal / 1000.0
    else
      wallMs = dtReal * 1000.0
    end
    hasWork = true
  else
    if wallMs == nil then wallMs = self.lastStart and (t - self.lastStart) or nil end
    if wallMs ~= nil and wallMs > 0 and wallMs < 1.0 then
      dtReal = wallMs
      wallMs = wallMs * 1000.0
    elseif wallMs ~= nil then
      dtReal = wallMs / 1000.0
    else
      dtReal = 0.016
    end
  end
  self.lastStart = t
  if wallMs then
    if not self.prevHeavy then
      self.histN = self.histN + 1
      self.hist[(self.histN - 1) % M.BASE_FRAMES + 1] = wallMs
      self.base = median(self.hist, min(self.histN, M.BASE_FRAMES))
    end
    self.w1, self.w2 = wallMs, self.w1
  end
  self.prevHeavy = false
  self.prevFrameUnits = self.frameUnits or 0
  self.prevStructCount = self.sceneStructCount or 0
  self.lastSpentMs = self.spent or self.frameSpent or 0.0
  -- Swap frameKinds -> prevFrameKinds zero-allocation
  local pfk, fk = self.prevFrameKinds, self.frameKinds
  for k in pairs(pfk) do pfk[k] = nil end
  for k, v in pairs(fk) do pfk[k] = v; fk[k] = nil end
  self.prevFrameKinds, self.frameKinds = pfk, fk
  self.frameSpent, self.delSpent, self.frameUnits, self.units, self.sceneStructCount = 0, 0, 0, 0, 0
  recomputeMaxUnit(self)
  if (self.structCooldown or 0) > 0 then
    self.structCooldown = self.structCooldown - 1
  elseif not self.loading and not self.urgent and self.prevStructCount > 0
      and (self.burst or self.lastSpentMs >= M.STRUCT_COOLDOWN_MS) then
    -- L-H-L median-of-3 friendly spacing: leave 1 calm frame after a heavy structural frame
    self.structCooldown = 1
  end
  if self.burst then
    self.stats.burstFrames = (self.stats.burstFrames or 0) + 1
  end
  -- a heavy request lives as long as the caller repeats it every frame
  if self.heavyPending and self.heavyReqFrame < self.frame - 1 then self.heavyPending = false end

  -- Predictive D3D12 swapchain pacing: Exponential Moving Average (EMA) Engine Base-Cost Estimator
  local dtMs = (dtReal or 0.016) * 1000.0
  local engineOnlyMs = math.max(1.0, dtMs - (self.lastSpentMs or 0))
  self.emaEngineMs = (self.emaEngineMs or 8.0) * 0.85 + engineOnlyMs * 0.15

  -- Target 14.2 ms total frame time to keep 2.47 ms safety margin below 60 Hz (16.67 ms)
  local headroomMs = 14.2 - self.emaEngineMs
  local speedBoost = math.min(1.5, math.max(0.0, ((speedMps or 0) - 35.0) * 0.025))

  local target = self:targetNow()
  if not self.loading then
    target = math.max(M.MIN_TARGET_MS, math.min(target, headroomMs + speedBoost))
  end
  self.spent = 0.0

  self.bPrev = self.b
  if self.quiet > 0 then
    self.quiet = self.quiet - 1
    self.presentCooldown = 0
    self.bSaved = nil
    self.b, self.tokens, self.engineDebt, self.blocked, self.heavyAllowed = 0, 0, 0, true, false
    return
  end
  self.blocked = false
  if self.heavyPending then
    self.b = max(0, self.b - M.RAMP_DOWN_HEAVY)
    self.tokens = min(self.tokens, self.b)
    self.heavyAllowed = false
  elseif hasWork then
    if self.b < target then
      self.b = min(target, self.b + (self.loading and M.RAMP_UP_LOADING or M.RAMP_UP))
    else
      self.b = max(target, self.b - M.RAMP_DOWN_IDLE)
    end
    local cap = math.max(8.4, 2 * (self.maxUnitCached or 0))
    self.tokens = min(self.tokens + self.b, cap)
    self.heavyAllowed = (dtMs < 12.5) and (self.emaEngineMs < 10.5)
  else
    self.b = max(0, self.b - M.RAMP_DOWN_IDLE)
    self.tokens = min(self.tokens, self.b)
  end
  self.engineDebt = max(0, (self.engineDebt or 0) - self.b)
end

-- May a unit of this kind run now? (The caller then runs it and calls charge with its cost.)
function Gov:try(kind)
  if not self.used[kind] then
    self.used[kind] = true
    local ek = self.est[kind] or 0
    if ek > (self.maxUnitCached or 0) then self.maxUnitCached = ek end
  end
  if self.blocked or self.heavyFrame == self.frame then return false end
  local burstThrottle = self.burst and not self.urgent
  local isStruct = SCENE_STRUCT_KINDS[kind]
  if isStruct and not self.loading and not self.urgent then
    if (self.structCooldown or 0) > 0 then return false end
    -- Never co-schedule two distinct scene-structure kinds in one frame (or >1 structural unit in burst mode)
    if (self.sceneStructCount or 0) >= M.MAX_SCENE_STRUCT_PER_FRAME then
      if burstThrottle or not (self.frameKinds and self.frameKinds[kind]) then
        return false
      end
    end
  end
  local e = self:effectiveEst(kind)
  local maxU = self.maxUnitCached or 0
  -- Hard ceiling when not loading: prevent maxFrameSpent from reaching 14+ ms (cap at LOADING_TARGET_MS)
  local frameCap = self.b + maxU
  if not self.loading then
    frameCap = min(frameCap, burstThrottle and (self.b + min(maxU, 4.5)) or M.LOADING_TARGET_MS)
  end
  if self.frameSpent + (self.est[kind] or 1) > frameCap then return false end
  -- Only allow the first-unit partial-token discount when not in burst mode (unless urgent) and not in engine debt
  local req = (self.frameUnits == 0 and self.tokens >= 2.0 and not burstThrottle and (self.engineDebt or 0) <= 0)
    and min(e, 4.0) or e
  if self.tokens < req then
    if DELETE_KINDS[kind] and (self.engineDebt or 0) > 0 and not self.heavyPending and self.b > 0 then
      return self.delSpent + min(e, M.DELETE_BUDGET_MS) <= M.DELETE_BUDGET_MS
    end
    return false
  end
  return true
end

-- Charge a unit that ran (ms = its measured cost).
function Gov:charge(kind, ms)
  ms = max(0, ms or 0)
  if DELETE_KINDS[kind] and (self.engineDebt or 0) > 0 then
    local freeDel = max(0, M.DELETE_BUDGET_MS - self.delSpent)
    self.delSpent = self.delSpent + ms
    self.tokens = self.tokens - max(0, ms - freeDel)
  else
    self.tokens = self.tokens - ms
  end
  self.frameSpent = self.frameSpent + ms
  self.spent = (self.spent or 0.0) + ms
  if self.frameSpent > self.stats.maxFrameSpent then self.stats.maxFrameSpent = self.frameSpent end
  self.frameUnits = self.frameUnits + 1
  self.units = (self.units or 0) + 1
  if self.frameKinds then
    self.frameKinds[kind] = (self.frameKinds[kind] or 0) + 1
  end
  if SCENE_STRUCT_KINDS[kind] then
    self.sceneStructCount = (self.sceneStructCount or 0) + 1
  end
  self.lastStreamFrame = self.frame
  local e = self.est[kind] or ms
  local ne = max(0.005, e + M.EST_ALPHA * (ms - e))
  self.est[kind] = ne
  if self.used[kind] then
    local curMax = self.maxUnitCached or 0
    if ne > curMax then
      self.maxUnitCached = ne
    elseif e >= curMax and ne < e then
      recomputeMaxUnit(self)
    end
  end
  self.stats.units = self.stats.units + 1
end

-- Closed-loop engine debt: reads previous frame's Ticking, RenderShadows, and FileWatchers (ms) from
-- Engine.Debug.getLastPerformanceMetrics. Excess over running baseline is charged to the token bucket
-- AND attributed to the unit kinds that ran in the previous frame (per-kind EMA of deferred engine cost).
-- Supports both Gov:chargeEngine(tickMs, shadowMs, fwMs) and Gov:chargeEngine({Ticking=..., RenderShadows=..., FileWatchers=...}).
function Gov:chargeEngine(tickMs, shadowMs, fwMs)
  local tick, shad, fw
  if type(tickMs) == 'table' then
    tick = tonumber(tickMs.Ticking) or 0
    shad = tonumber(tickMs.RenderShadows) or 0
    fw = tonumber(tickMs.FileWatchers) or 0
  else
    tick = tonumber(tickMs) or 0
    shad = tonumber(shadowMs) or 0
    fw = tonumber(fwMs) or 0
  end
  local prevU = self.prevFrameUnits or 0
  if prevU == 0 then
    if tick > 0 and tick < 15 then
      self.tickBase = self.tickBase and (self.tickBase * 0.95 + tick * 0.05) or tick
    end
    if shad > 0 and shad < 3.0 then
      self.shadowBase = self.shadowBase and (self.shadowBase * 0.95 + shad * 0.05) or shad
    end
  end
  local freeTick = max(M.ENGINE_FREE_MS, (self.tickBase or 2.0) + 1.5)
  local tickDebt = (prevU > 0) and max(0, tick - freeTick) or 0
  local freeShad = max(M.SHADOW_FREE_MS, (self.shadowBase or 0.45) + 0.8)
  local shadDebt = max(0, shad - freeShad)
  local fwDebt = max(0, fw - M.FW_FREE_MS)
  -- RenderShadows and FileWatchers spikes during streaming bursts are deferred costs even if prevU == 0
  -- (e.g. FileWatchers firing 1-2 frames after file write, or multi-frame shadow cache rebuilds).
  local debt = tickDebt + shadDebt + fwDebt * 0.5
  if prevU == 0 and shadDebt <= 0 and fwDebt <= 0 then return 0 end
  if debt <= 0 then
    -- Decay learned deferredEst gently when streaming units ran without causing a shadow/tick spike
    if prevU > 0 and self.prevFrameKinds and self.deferredEst then
      for k in pairs(self.prevFrameKinds) do
        local cur = self.deferredEst[k] or 0
        if cur > 0 then
          self.deferredEst[k] = max(0, cur * (1 - M.DEFERRED_ALPHA * 0.5))
        end
      end
    end
    return 0
  end
  -- Attribute excess engine cost (shadDebt + tickDebt) across the structural kinds run in the previous frame
  local attrDebt = shadDebt + tickDebt
  if attrDebt > 0 and prevU > 0 and self.prevFrameKinds and self.deferredEst then
    local totalWeight = 0
    for k, cnt in pairs(self.prevFrameKinds) do
      if SCENE_STRUCT_KINDS[k] then
        totalWeight = totalWeight + cnt * (self.est[k] or 1)
      end
    end
    if totalWeight > 0 then
      for k, cnt in pairs(self.prevFrameKinds) do
        if SCENE_STRUCT_KINDS[k] then
          local share = attrDebt * ((self.est[k] or 1) / totalWeight)
          local cur = self.deferredEst[k] or 0
          self.deferredEst[k] = min(12.0, cur + M.DEFERRED_ALPHA * (share - cur))
        end
      end
    end
  end
  self.tokens = max(-M.ENGINE_MAX_DEBT_MS, self.tokens - debt)
  self.engineDebt = min(M.ENGINE_MAX_DEBT_MS, (self.engineDebt or 0) + debt)
  self.stats.engineDebt = (self.stats.engineDebt or 0) + debt
  if shadDebt > 0 then
    self.stats.shadowDebt = (self.stats.shadowDebt or 0) + shadDebt
    if not self.loading and not self.urgent then
      self.structCooldown = max(self.structCooldown or 0, 1)
    end
  end
  return debt
end

-- Charge per-frame motion (crossover / cover transforms). It always runs (it is time based and
-- cheap) but its cost comes out of the same bucket, so it delays imports instead of adding up.
function Gov:chargeMotion(ms)
  ms = max(0, ms or 0)
  self.tokens = self.tokens - ms
  self.frameSpent = self.frameSpent + ms
  self.spent = (self.spent or 0.0) + ms
end

local function grant(self)
  self.heavyPending, self.heavySince = false, nil
  self.heavyFrame = self.frame
  self.quiet = M.QUIET_AFTER
  self.zeroFrames = 0
  self.bSaved = nil
  self.b, self.tokens, self.blocked = 0, 0, true
  self.prevHeavy = true -- this frame's duration is not part of base
  self.stats.heavy = self.stats.heavy + 1
  return true
end

-- Ask for a heavy slot this frame. Returns true when the caller may run its heavy op NOW (in this
-- frame, before any streaming); false = ask again next frame (the request lapses if not repeated).
-- urgent = grant at once (teleport: the user asked for a jump, a hitch is expected).
function Gov:requestHeavy(urgent)
  if self.heavyFrame == self.frame then return true end -- one slot per frame, shared
  if not self.heavyPending then
    self.heavyPending, self.heavySince = true, self.frame
    -- Immediate 2-frame quiet gate: clamp b = 0 for QUIET_BEFORE = 2 frames before commit
    if self.frameUnits == 0 and not self.blocked then self.b = 0 end
  end
  self.heavyReqFrame = self.frame
  self.b = 0
  self.bSaved = nil
  self.tokens = min(self.tokens, 0)
  if urgent then return grant(self) end
  if self.b > 0 or (self.zeroFrames or 0) < M.QUIET_BEFORE or self.frame - self.lastStreamFrame <= M.QUIET_BEFORE then
    return false
  end
  local calm = (self.heavyAllowed ~= false) or (self.base ~= nil and self.w1 ~= nil and self.w2 ~= nil
    and self.w1 <= self.base + M.CALM_MARGIN_MS and self.w2 <= self.base + M.CALM_MARGIN_MS)
  if calm or self.frame - self.heavySince >= M.CALM_TIMEOUT then return grant(self) end
  return false
end

-- A heavy op that must run now regardless (rebase phase 2, an immediate teleport commit).
function Gov:heavyNow() return self:requestHeavy(true) end

function Gov:isHeavyFrame() return self.heavyFrame == self.frame end

function Gov:snapshot()
  return { b = self.b, tokens = self.tokens, target = self:targetNow(), loading = self.loading, urgent = self.urgent,
    burst = self.burst, shadowBase = self.shadowBase,
    base = self.base, tickBase = self.tickBase, emaEngineMs = self.emaEngineMs, heavyAllowed = self.heavyAllowed,
    lastSpentMs = self.lastSpentMs,
    heavyPending = self.heavyPending, estCdae = self.est.cdae, estDae = self.est.dae, estCdaeTex = self.est.cdaeTex,
    estCdaeTex128 = self.est.cdaeTex128, estCdaeTex256 = self.est.cdaeTex256, estCdaeTex512 = self.est.cdaeTex512,
    estMaterial = self.est.material, estMatDelete = self.est.matDelete, estTreeBatch = self.est.treeBatch,
    defCdae = self.deferredEst and self.deferredEst.cdae, defTreeBatch = self.deferredEst and self.deferredEst.treeBatch,
    heavy = self.stats.heavy, units = self.stats.units, maxFrameSpent = self.stats.maxFrameSpent,
    engineDebt = self.stats.engineDebt, shadowDebt = self.stats.shadowDebt, burstFrames = self.stats.burstFrames,
    presentCooldowns = self.stats.presentCooldowns }
end

M.Gov = Gov
return M
