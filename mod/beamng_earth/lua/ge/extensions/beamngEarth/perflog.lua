-- The performance log (the user, 2026-10-23: "update the log to include as much useful info as possible. Including
-- detecting any performance drops and where they occurred. So players who experience issues can send me their log").
-- Fed every frame by core.lua; returns lines for the world server's log (server.log, the file players send, beside
-- the server's own block timings) and the game's beamng.log:
--   perf hitch    a frame of HITCH_MS or more: where, how fast, the mod's parts and the engine's subsystems in it, and
--                 what the mod was doing (core's context: block swap, far tiles, roads, trees, holds, server backlog)
--   perf drop     a WINDOW_S window under DROP_SHARE of the usual frame rate (the median of the last BASELINE_N
--                 windows): the window's fps, its slowest frames, the mod's parts summed over it, the engine's mean
--   perf back     the frame rate back over BACK_SHARE of the usual: how long the drop lasted, its lowest fps
--   perf minute   every SUMMARY_S: average fps, 1 % low, hitches, the worst frame and its cause, where
-- Pure but for the clock and the context the caller passes.
local M = {}

M.HITCH_MS = 100
M.WINDOW_S = 5
M.DROP_SHARE = 0.65
M.BACK_SHARE = 0.85
M.BASELINE_N = 24           -- (2 minutes of windows)
M.BASELINE_MIN = 3          -- windows before a drop can be called
M.MIN_FPS = 12              -- a usual rate below this (a weak machine, a menu): no drops called
M.SUMMARY_S = 60
M.HITCH_GAP_S = 2           -- hitch lines at most this often (the ones between are counted into the next)
M.ENGINE_EVERY = 5          -- the engine's breakdown sampled every this many frames for the window means

local w, base, frames, sumMin, hitchesMin, worstMin, lastHitchAt, skipped, drop, summaryAt
-- the drops since takeDrops (the anonymous performance summary, perfsummary.lua): how many, their seconds, the lowest
local drops = { n = 0, s = 0, low = nil }
function M.takeDrops()
  local d = drops
  drops = { n = 0, s = 0, low = nil }
  return d
end

function M.reset()
  w = { t0 = nil, n = 0, ms = 0, worst = {}, mod = {}, eng = {}, engN = 0, k = 0 }
  base = {}
  frames, sumMin, hitchesMin, worstMin = {}, 0, 0, nil
  lastHitchAt, skipped, drop, summaryAt = -math.huge, 0, nil, nil
end
M.reset()

local function r1(v) return math.floor(v * 10 + 0.5) / 10 end
local function r0(v) return math.floor(v + 0.5) end

-- "k1 v1, k2 v2" of a {name = ms} table, largest first, at most n, those of min ms or more
local function top(t, n, min)
  local arr = {}
  for k, v in pairs(t or {}) do if type(v) == 'number' and v >= (min or 0) then arr[#arr + 1] = { k, v } end end
  table.sort(arr, function(a, b) return a[2] > b[2] end)
  local out = {}
  for i = 1, math.min(n, #arr) do out[i] = arr[i][1] .. ' ' .. r1(arr[i][2]) end
  return table.concat(out, ', ')
end
M.top = top

-- The cause named for a slow frame: its largest measured part (the mod's, else the engine's) when that part is at
-- least CAUSE_SHARE of the frame; else UNMEASURED (a first-time load, the GPU driver, the OS: outside both timers).
M.CAUSE_SHARE = 0.25
M.UNMEASURED = 'mostly outside the mod and engine timers (a load, the GPU driver or the system)'
-- The engine's Lua total (all of the game's Lua: ours, traffic, UI apps, the garbage collector) is named with whom it
-- went to: 'Lua:<our part>' when one of the mod's parts is at least CAUSE_SHARE of it, 'Lua:other' when all of the
-- mod's parts together are under CAUSE_SHARE of it (the game's own Lua), else 'Lua' (2026-11-09 telemetry: 'Lua' was
-- the top cause of slow frames, which said nothing about whose).
function M.cause(ms, hitch)
  local best, bk = 0, nil
  for _, part in ipairs({ hitch and hitch.mod or {}, hitch and hitch.engine or {} }) do
    for k, v in pairs(part) do if type(v) == 'number' and v > best then best, bk = v, k end end
  end
  if not bk or best < M.CAUSE_SHARE * ms then return M.UNMEASURED end
  if bk == 'Lua' then
    local sum, top, tk = 0, 0, nil
    for k, v in pairs(hitch.mod or {}) do
      if type(v) == 'number' then
        sum = sum + v
        if v > top then top, tk = v, k end
      end
    end
    if tk and top >= M.CAUSE_SHARE * best then bk = 'Lua:' .. tk
    elseif sum < M.CAUSE_SHARE * best then bk = 'Lua:other' end
  end
  return bk .. ' ' .. r1(best)
end

local function median(t)
  local s = {}
  for i, v in ipairs(t) do s[i] = v end
  table.sort(s)
  local n = #s
  if n == 0 then return nil end
  if n % 2 == 1 then return s[(n + 1) / 2] end
  return 0.5 * (s[n / 2] + s[n / 2 + 1])
end

-- "at 39.28345,-76.59341, 52 mph, block 1024,512 [extra]" from the caller's context
local function where(c)
  if not c then return '' end
  local s = {}
  if c.lat and c.lon then s[#s + 1] = string.format('at %.5f,%.5f', c.lat, c.lon) end
  if c.mph then s[#s + 1] = string.format('%d mph', r0(c.mph)) end
  if c.block then s[#s + 1] = 'block ' .. c.block end
  if c.state and c.state ~= '' then s[#s + 1] = c.state end
  return table.concat(s, ', ')
end

-- One frame. ms: its wall time; now: a clock (s); ctx(): the caller's context {lat, lon, mph, block, state, doing}
-- (called only when a line is made); hitch: core's hitch record for this frame ({mod = {}, engine = {}}) or nil;
-- mod: the mod's parts this frame {name = ms} (summed over the window); engine(): the engine's breakdown {name = ms}
-- (sampled every ENGINE_EVERY frames). Returns a list of lines (often empty).
function M.frame(ms, now, ctx, hitch, mod, engine)
  local out = {}
  if type(ms) ~= 'number' or ms ~= ms or ms <= 0 then return out end
  summaryAt = summaryAt or now
  -- hitches
  if ms >= M.HITCH_MS then
    hitchesMin = hitchesMin + 1
    if not worstMin or ms > worstMin.ms then worstMin = { ms = ms, cause = M.cause(ms, hitch) } end
    if now - lastHitchAt >= M.HITCH_GAP_S then
      local c = ctx and ctx() or {}
      local line = string.format('perf hitch %d ms %s', r0(ms), where(c))
      if hitch and next(hitch.mod or {}) then line = line .. ' | mod: ' .. top(hitch.mod, 5, 1) end
      if hitch and next(hitch.engine or {}) then line = line .. ' | engine: ' .. top(hitch.engine, 5, 1) end
      if M.cause(ms, hitch) == M.UNMEASURED then line = line .. ' | ' .. M.UNMEASURED end
      if c.doing and c.doing ~= '' then line = line .. ' | doing: ' .. c.doing end
      if skipped > 0 then line = line .. string.format(' | (%d more hitches since the last line)', skipped) end
      out[#out + 1] = line
      lastHitchAt, skipped = now, 0
    else
      skipped = skipped + 1
    end
  end
  -- the minute's frames
  frames[#frames + 1] = ms
  sumMin = sumMin + ms
  -- the window
  w.t0 = w.t0 or now
  w.n, w.ms, w.k = w.n + 1, w.ms + ms, w.k + 1
  if #w.worst < 3 or ms > w.worst[#w.worst] then
    w.worst[#w.worst + 1] = ms
    table.sort(w.worst, function(a, b) return a > b end)
    if #w.worst > 3 then w.worst[4] = nil end
  end
  for k, v in pairs(mod or {}) do if type(v) == 'number' then w.mod[k] = (w.mod[k] or 0) + v end end
  if engine and w.k % M.ENGINE_EVERY == 0 then
    local e = engine()
    if e then
      w.engN = w.engN + 1
      for k, v in pairs(e) do if type(v) == 'number' then w.eng[k] = (w.eng[k] or 0) + v end end
    end
  end
  if now - w.t0 >= M.WINDOW_S then
    local fps = w.n / (w.ms / 1000)
    local usual = #base >= M.BASELINE_MIN and median(base) or nil
    if usual and usual >= M.MIN_FPS then
      if not drop and fps < M.DROP_SHARE * usual then
        local c = ctx and ctx() or {}
        local engMean = {}
        for k, v in pairs(w.eng) do engMean[k] = v / math.max(1, w.engN) end
        local worst = {}
        for i, v in ipairs(w.worst) do worst[i] = tostring(r0(v)) end
        local line = string.format('perf drop %.0f fps for %ds (usual %.0f) %s | slowest frames %s ms',
          fps, r0(now - w.t0), usual, where(c), table.concat(worst, ' '))
        local m = top(w.mod, 5, 10)
        if m ~= '' then line = line .. ' | mod over the window (ms): ' .. m end
        local e = top(engMean, 5, 1)
        if e ~= '' then line = line .. ' | engine per frame: ' .. e end
        if c.doing and c.doing ~= '' then line = line .. ' | doing: ' .. c.doing end
        out[#out + 1] = line
        drop = { since = w.t0, low = fps }
        drops.n = drops.n + 1
      elseif drop then
        drop.low = math.min(drop.low, fps)
        if fps >= M.BACK_SHARE * usual then
          out[#out + 1] = string.format('perf back to %.0f fps after %ds (lowest %.0f) %s', fps, r0(now - drop.since),
            drop.low, where(ctx and ctx() or {}))
          drops.s = drops.s + (now - drop.since)
          drops.low = math.min(drops.low or math.huge, drop.low)
          drop = nil
        end
      end
    end
    if not drop then                -- the usual rate: windows outside drops
      base[#base + 1] = fps
      if #base > M.BASELINE_N then table.remove(base, 1) end
    end
    w = { t0 = now, n = 0, ms = 0, worst = {}, mod = {}, eng = {}, engN = 0, k = 0 }
  end
  -- the minute
  if now - summaryAt >= M.SUMMARY_S and #frames > 0 then
    local s = {}
    for i, v in ipairs(frames) do s[i] = v end
    table.sort(s, function(a, b) return a > b end)
    local low = s[math.max(1, math.ceil(#s * 0.01))]
    local c = ctx and ctx() or {}
    local line = string.format('perf minute %.0f fps, 1%% low %.0f, worst %d ms, %d hitches', #frames / (sumMin / 1000),
      1000 / low, r0(s[1]), hitchesMin)
    if worstMin and worstMin.cause ~= '' then line = line .. ' (worst: ' .. worstMin.cause .. ')' end
    line = line .. ' ' .. where(c)
    if c.extra and c.extra ~= '' then line = line .. ' | ' .. c.extra end
    out[#out + 1] = line
    frames, sumMin, hitchesMin, worstMin, summaryAt = {}, 0, 0, nil, now
  end
  return out
end

return M
