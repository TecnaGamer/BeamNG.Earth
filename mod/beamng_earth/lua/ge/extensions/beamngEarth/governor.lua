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
--    requested, b ramps down at -0.1 ms/frame; the slot is granted only when b == 0, no unit ran in
--    the 2 frames before, and those 2 frames were calm (<= base + 1.5 ms); then 2 quiet frames
--    follow (no streaming) and b ramps up from 0 again. So no tile import ever runs within 2 frames
--    of a heavy frame, and the heavy frame stays a single isolated spike the dt filter rejects.
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

-- Slow frames (the BeamMP session's test 'perf' and the dev tools): a frame of at least SLOW_MS wall time is kept in
-- M.slow (the latest SLOW_KEEP) with the units charged in it ("kind:ms ...") and whether a heavy op ran in it; none
-- of ours (no units, not heavy) points at the engine itself.
M.SLOW_MS, M.SLOW_KEEP = 150, 40
M.slow = {}

local max, min, huge = math.max, math.min, math.huge

M.DEFAULT_TARGET_MS = 2.0
M.MIN_TARGET_MS, M.MAX_TARGET_MS = 1.0, 4.0
M.LOADING_TARGET_MS = 8.0
M.RAMP_UP = 0.05           -- ms per frame while there is work
M.RAMP_UP_LOADING = 0.25   -- ms per frame in loading mode
M.RAMP_DOWN_IDLE = 0.05    -- ms per frame without work
M.RAMP_DOWN_HEAVY = 0.1    -- ms per frame while a heavy op waits
M.BASE_FRAMES = 64         -- base = median of this many non-heavy frames
M.CALM_MARGIN_MS = 1.5     -- a calm frame is <= base + this
M.QUIET_BEFORE = 2         -- frames without streaming before a heavy op
M.QUIET_AFTER = 2          -- frames without streaming after it
M.CALM_TIMEOUT = 60        -- frames a granted-but-not-calm heavy op waits at most (b == 0 already)
M.EST_ALPHA = 0.2          -- cost EMA gain
-- The engine's own deferred work for what streaming made (2026-10-15, 100 mph drives: new tiles' shapes, materials and
-- textures cost the NEXT frame's engine "Ticking" 50-250 ms, which no unit's measured Lua cost included, so the bucket
-- kept importing at full rate and every frame ran 100+ ms): each frame's engine tick beyond ENGINE_FREE_MS is a debt on
-- the bucket (chargeEngine), at most ENGINE_MAX_DEBT_MS, so imports wait until the budget has paid for it.
M.ENGINE_FREE_MS = 2.0
M.ENGINE_MAX_DEBT_MS = 50.0
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
-- 'fuel' (gasstations.lua: one station, ~12-30 TSStatics of cached West Coast shapes + its areas) ~3 ms, a guess until
-- probes/fuel_pump.lua is timed in game.
M.PRIOR = { cdae = 3.0, dae = 8.0, reveal = 0.05, delete = 0.3, cdaeTex = 3.0, material = 0.3, matDelete = 0.2, cdaeTex128 = 3.9, cdaeTex256 = 4.0, cdaeTex512 = 4.2, retex = 1.0, roadChunk = 0.5, roadDelete = 0.05, roadWalk = 3.0, fuel = 3.0 }

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
  for k, v in pairs(M.PRIOR) do est[k] = v end
  local g = setmetatable({
    clock = opts.clock or stopwatch.clock(),
    target = M.DEFAULT_TARGET_MS,
    loading = false,
    b = 0,              -- current streaming budget, ms per frame
    bPrev = 0,          -- b of the previous frame
    tokens = 0,         -- token bucket, ms
    est = est,          -- per kind cost estimate, ms
    used = {},          -- kinds asked for at least once (they define maxUnit)
    hist = {}, histN = 0, -- ring of recent non-heavy frame times
    base = nil,
    frame = 0,
    lastStart = nil,
    w1 = nil, w2 = nil, -- wall durations of the previous two frames
    prevHeavy = false,  -- the frame that just ended was heavy (its time is not part of base)
    frameSpent = 0,     -- ms charged this frame (units + motion)
    frameUnits = 0,     -- units run this frame
    lastStreamFrame = -huge, -- last frame a unit ran in
    heavyPending = false, heavySince = nil, heavyReqFrame = nil,
    heavyFrame = -huge, -- last frame that ran a heavy op
    quiet = 0,          -- quiet frames still to come after a heavy op
    blocked = false,    -- no units this frame (quiet or heavy)
    stats = { heavy = 0, units = 0, maxFrameSpent = 0 },
    frameLog = {},      -- this frame's charges "kind:ms" (the slow-frame record)
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

function Gov:targetNow() return self.loading and M.LOADING_TARGET_MS or self.target end

-- Largest estimated unit cost among the kinds in use.
function Gov:maxUnit()
  local m = 0
  for k in pairs(self.used) do m = max(m, self.est[k] or 0) end
  return m
end

local function median(t, n)
  local s = {}
  for i = 1, n do s[i] = t[i] end
  table.sort(s)
  if n % 2 == 1 then return s[(n + 1) / 2] end
  return 0.5 * (s[n / 2] + s[n / 2 + 1])
end

-- Call once per frame, first thing in onUpdate. wallMs: duration of the previous frame (nil =
-- measure with the clock). hasWork: streaming work is waiting.
function Gov:beginFrame(hasWork, wallMs)
  local t = self.clock()
  if wallMs == nil then wallMs = self.lastStart and (t - self.lastStart) or nil end
  if wallMs and wallMs >= M.SLOW_MS then
    M.slow[#M.slow + 1] = { ms = math.floor(wallMs + 0.5), units = table.concat(self.frameLog, ' '),
      heavy = self.heavyFrame == self.frame, engineMs = self.lastEngineMs }
    if #M.slow > M.SLOW_KEEP then table.remove(M.slow, 1) end
  end
  self.frameLog = {}
  self.frame = self.frame + 1
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
  self.frameSpent, self.frameUnits = 0, 0
  -- a heavy request lives as long as the caller repeats it every frame
  if self.heavyPending and self.heavyReqFrame < self.frame - 1 then self.heavyPending = false end

  local target = self:targetNow()
  self.bPrev = self.b
  if self.quiet > 0 then
    self.quiet = self.quiet - 1
    self.b, self.tokens, self.blocked = 0, 0, true
    return
  end
  self.blocked = false
  if self.heavyPending then
    self.b = max(0, self.b - M.RAMP_DOWN_HEAVY)
  elseif hasWork then
    if self.b < target then
      self.b = min(target, self.b + (self.loading and M.RAMP_UP_LOADING or M.RAMP_UP))
    else
      self.b = max(target, self.b - M.RAMP_DOWN_IDLE)
    end
  else
    self.b = max(0, self.b - M.RAMP_DOWN_IDLE)
  end
  self.tokens = min(self.tokens + self.b, 2 * self:maxUnit())     -- (a debt stays: chargeEngine)
  -- no banking while idle or while a heavy op waits: the budget must really go down to zero
  if self.heavyPending or not hasWork then self.tokens = min(self.tokens, self.b) end
end

-- May a unit of this kind run now? (The caller then runs it and calls charge with its cost.)
function Gov:try(kind)
  self.used[kind] = true
  if self.blocked or self.heavyFrame == self.frame then return false end
  local e = self.est[kind] or 1
  if self.tokens < e then return false end
  if self.frameSpent + e > self.b + self:maxUnit() then return false end
  return true
end

-- Charge a unit that ran (ms = its measured cost).
function Gov:charge(kind, ms)
  ms = max(0, ms or 0)
  if ms >= 1 then self.frameLog[#self.frameLog + 1] = kind .. ':' .. math.floor(ms + 0.5) end
  self.tokens = self.tokens - ms
  self.frameSpent = self.frameSpent + ms
  self.frameUnits = self.frameUnits + 1
  self.lastStreamFrame = self.frame
  local e = self.est[kind] or ms
  self.est[kind] = max(0.005, e + M.EST_ALPHA * (ms - e))
  self.stats.units = self.stats.units + 1
  if self.frameSpent > self.stats.maxFrameSpent then self.stats.maxFrameSpent = self.frameSpent end
end

-- The previous frame's engine tick (ms, Engine.Debug metrics "Ticking"): its excess is a debt on the bucket.
function Gov:chargeEngine(tickMs)
  self.lastEngineMs = tickMs and math.floor(tickMs + 0.5) or nil
  local debt = max(0, (tickMs or 0) - M.ENGINE_FREE_MS)
  if debt <= 0 then return 0 end
  self.tokens = max(-M.ENGINE_MAX_DEBT_MS, self.tokens - debt)
  self.stats.engineDebt = (self.stats.engineDebt or 0) + debt
  return debt
end

-- Charge per-frame motion (crossover / cover transforms). It always runs (it is time based and
-- cheap) but its cost comes out of the same bucket, so it delays imports instead of adding up.
function Gov:chargeMotion(ms)
  ms = max(0, ms or 0)
  self.tokens = self.tokens - ms
  self.frameSpent = self.frameSpent + ms
end

-- A frame's other work (navgraph, parking: not streaming units): counted in this frame's spent time, so the occasional
-- big ones wait for a light frame instead of landing on the same one (roomFor), but not charged to the bucket.
function Gov:noteFrame(ms)
  self.frameSpent = self.frameSpent + max(0, ms or 0)
end

-- Is this frame light enough for an occasional op of the mod's own (navgraph's map rebuild, parking's spots, a
-- collision rebuild): not a heavy frame and at most ms spent in it so far? (2026-10-23, a Steam Deck drive in Baltimore:
-- a 66 ms parking rebuild, a 50 ms navgraph apply and 33 ms of world work in one 187 ms frame.)
function Gov:roomFor(ms)
  return self.heavyFrame ~= self.frame and (self.frameSpent or 0) <= ms
end

local function grant(self)
  self.heavyPending, self.heavySince = false, nil
  self.heavyFrame = self.frame
  self.quiet = M.QUIET_AFTER
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
    -- this frame already ramped for streaming: ramp down from the previous frame's b instead
    if self.frameUnits == 0 and not self.blocked then self.b = max(0, self.bPrev - M.RAMP_DOWN_HEAVY) end
  end
  self.heavyReqFrame = self.frame
  self.tokens = min(self.tokens, self.b)
  if urgent then return grant(self) end
  if self.b > 0 or self.frame - self.lastStreamFrame <= M.QUIET_BEFORE then return false end
  local calm = self.base ~= nil and self.w1 ~= nil and self.w2 ~= nil
    and self.w1 <= self.base + M.CALM_MARGIN_MS and self.w2 <= self.base + M.CALM_MARGIN_MS
  if calm or self.frame - self.heavySince >= M.CALM_TIMEOUT then return grant(self) end
  return false
end

-- A heavy op that must run now regardless (rebase phase 2, an immediate teleport commit).
function Gov:heavyNow() return self:requestHeavy(true) end

function Gov:isHeavyFrame() return self.heavyFrame == self.frame end

function Gov:snapshot()
  return { b = self.b, tokens = self.tokens, target = self:targetNow(), loading = self.loading, base = self.base,
    heavyPending = self.heavyPending, estCdae = self.est.cdae, estDae = self.est.dae, estCdaeTex = self.est.cdaeTex,
    estCdaeTex128 = self.est.cdaeTex128, estCdaeTex256 = self.est.cdaeTex256, estCdaeTex512 = self.est.cdaeTex512,
    estMaterial = self.est.material, estMatDelete = self.est.matDelete, heavy = self.stats.heavy,
    units = self.stats.units, maxFrameSpent = self.stats.maxFrameSpent, engineDebt = self.stats.engineDebt }
end

M.Gov = Gov
return M
