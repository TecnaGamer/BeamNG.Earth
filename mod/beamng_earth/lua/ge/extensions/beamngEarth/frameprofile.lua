-- Frame-level CPU phases, GPU passes and Earth scopes. Engine values are milliseconds.
-- API verified against Engine.Debug.getLastPerformanceMetrics / getGpuTimings.
-- GPU queries are asynchronous: each row preserves the GPU reading observed at capture,
-- rather than claiming that a deferred GPU query belongs to the same CPU frame.
local M = {}
M.mode = 'full'
M.KEEP = 600
M.BUDGET_MS = 1000 / 60
local cpuNames = { 'UImsgLoop', 'UIpixelsToGPU', 'UIframeJSmain', 'UIframeJSvehicles', 'UImenuCompositor',
  'UIimgui', 'Audio', 'LuaPre', 'Lua', 'LuaGC', 'PreRender', 'Render', 'RenderShadows', 'RenderReflections',
  'RenderMisc', 'FramePresent', 'FrameSecondary', 'PostRender', 'OtherRender', 'TextureCooker', 'Crowd',
  'PhysicsOther', 'PhysicsState', 'PhysicsAndVLUA', 'PhysicsNodes', 'PhysicsBeams', 'PhysicsLights',
  'BookKeeping', 'FileWatchers', 'Pool', 'Ticking', 'Replay', 'Input', 'ThirdParties', 'Middle',
  'WaitNextFrame', 'AssetSystem', 'SaveData', 'Multiplayer', 'Other', 'FPSLimiter' }
local frames, at, count, serial = {}, 0, 0, 0
local engineScratch = {}

local function number(v)
  v = tonumber(v)
  if v and v == v and v >= 0 and v < math.huge then return v end
end
M.number = number

local function copyTimings(input, names)
  local out, total = {}, 0
  if type(input) ~= 'table' then return out, total end
  if names then
    for _, name in ipairs(names) do
      local value = number(input[name])
      if value and value > 0 then out[name], total = value, total + value end
    end
  else
    local n = 0
    for name, value in pairs(input) do
      value = number(value)
      if type(name) == 'string' and #name <= 128 and value and value > 0 and n < 128 then
        out[name], total, n = value, total + value, n + 1
      end
    end
  end
  return out, total
end

local function registry(key)
  local r = rawget(_G, 'VariableRegistry')
  if not (r and r.get) then return nil end
  local ok, value = pcall(r.get, key, -1)
  return ok and number(value) or nil
end

function M.capture()
  local result = { cpu = {}, gpu = {}, gpuTiming = 'unavailable' }
  local engine = rawget(_G, 'Engine')
  local debug = engine and engine.Debug
  if M.mode == 'full' and debug and debug.getLastPerformanceMetrics then
    for k in pairs(engineScratch) do engineScratch[k] = nil end
    local ok, returned = pcall(debug.getLastPerformanceMetrics, engineScratch)
    if ok then result.cpu = copyTimings(type(returned) == 'table' and returned or engineScratch, cpuNames) end
  end
  if M.mode == 'full' and debug and debug.getGpuTimings then
    local ok, data = pcall(debug.getGpuTimings)
    if ok and type(data) == 'table' then
      result.gpu = copyTimings(data)
      if next(result.gpu) then result.gpuTiming = 'latest completed GPU query; frame association unavailable' end
    end
  end
  result.cpuMs, result.gpuMs, result.waitMs = registry('$fps::cpuTime'), registry('$fps::gpuTime'), registry('$fps::waitForGPU')
  if M.mode == 'essential' then
    if result.cpuMs then result.cpu['CPU work'] = result.cpuMs end
    if result.gpuMs then result.gpu['GPU total'] = result.gpuMs end
    result.gpuTiming = 'engine total; detailed queries disabled'
  end
  if engine and engine.getCurrentFpsLimitReason then
    local ok, limits = pcall(engine.getCurrentFpsLimitReason)
    if ok and type(limits) == 'table' then result.focused = limits.windowFocused end
  end
  return result
end

function M.reset()
  frames, at, count, serial = {}, 0, 0, 0
end

function M.setMode(mode)
  if mode ~= 'essential' and mode ~= 'full' then return false end
  M.mode, M.KEEP = mode, mode == 'essential' and 180 or 600
  M.reset()
  return true
end

function M.add(ms, marks, metrics, observedAtMs)
  ms = number(ms)
  if not ms or ms <= 0 then return end
  metrics = metrics or M.capture()
  serial, at, count = serial + 1, at % M.KEEP + 1, math.min(count + 1, M.KEEP)
  local cpu, cpuSum = copyTimings(metrics.cpu)
  local gpu, gpuSum = copyTimings(metrics.gpu)
  local mod, modSum = copyTimings(M.mode == 'full' and marks or {})
  -- Never scale reported timings to fit the wall interval. An overrun stays visible.
  local cpuResidual = math.max(0, ms - cpuSum)
  if M.mode == 'full' and next(cpu) and cpuResidual > 0.01 then cpu['Unattributed'] = cpuResidual end
  local gpuMs = number(metrics.gpuMs)
  if next(gpu) and gpuMs and gpuMs - gpuSum > 0.01 then gpu['Unattributed GPU'] = gpuMs - gpuSum end
  frames[at] = { id = serial, ms = ms, observedAtMs = number(observedAtMs), cpu = cpu, gpu = gpu, mod = mod, cpuSumMs = cpuSum, gpuSumMs = gpuSum,
    modMs = modSum, cpuMs = number(metrics.cpuMs), gpuMs = gpuMs, waitMs = number(metrics.waitMs),
    focused = metrics.focused, gpuTiming = metrics.gpuTiming,
    phaseOverrun = cpuSum > ms * 1.1 + 0.5, gpuOverrun = gpuMs and gpuSum > gpuMs * 1.1 + 0.5 or false }
end

function M.history()
  local out = {}
  for i = 1, count do out[i] = frames[(at - count + i - 1) % M.KEEP + 1] end
  return out
end

local function pct(values, p)
  table.sort(values)
  return values[math.max(1, math.ceil(#values * p))] or 0
end

local function ranked(map)
  local out = {}
  for _, row in pairs(map) do out[#out + 1] = row end
  table.sort(out, function(a, b) return a.totalMs == b.totalMs and a.name < b.name or a.totalMs > b.totalMs end)
  return out
end

function M.analyze(history, budget)
  history, budget = history or M.history(), budget or M.BUDGET_MS
  local times, sum, slow, worst, series = {}, 0, 0, nil, { cpu = {}, gpu = {}, mod = {} }
  local gpuHigh, both, pacing, unfocused, gpuSamples, phaseOverruns = 0, 0, 0, 0, 0, 0
  for _, f in ipairs(history) do
    times[#times + 1], sum = f.ms, sum + f.ms
    if f.ms > budget then slow = slow + 1 end
    if not worst or f.ms > worst.ms then worst = f end
    if next(f.gpu) then gpuSamples = gpuSamples + 1 end
    if f.phaseOverrun or f.gpuOverrun then phaseOverruns = phaseOverruns + 1 end
    if f.focused == false then unfocused = unfocused + 1 end
    pacing = pacing + (f.cpu.FPSLimiter or 0) + (f.cpu.WaitNextFrame or 0)
    if f.cpuMs and f.gpuMs and f.ms > budget then
      both = both + 1
      if f.gpuMs > budget * 0.8 and f.gpuMs > f.cpuMs * 1.15 then gpuHigh = gpuHigh + 1 end
    end
    for _, kind in ipairs({ 'cpu', 'gpu', 'mod' }) do
      for name, ms in pairs(f[kind]) do
        local row = series[kind][name] or { name = name, totalMs = 0, maxMs = 0, active = 0, slowFrames = 0, slowMs = 0 }
        series[kind][name] = row
        row.totalMs, row.active = row.totalMs + ms, row.active + 1
        if ms > row.maxMs then row.maxMs, row.peakFrame = ms, f.id end
        if f.ms > budget then row.slowFrames, row.slowMs = row.slowFrames + 1, row.slowMs + ms end
      end
    end
  end
  local observations = {}
  local n = #history
  if n > 0 then
    observations[#observations + 1] = { kind = 'frame', frame = worst.id,
      text = string.format('%d / %d frames exceeded %.2f ms; worst %.2f ms at frame #%d.', slow, n, budget, worst.ms, worst.id) }
    if pacing / n > budget * 0.25 then observations[#observations + 1] = { kind = 'pacing',
      text = string.format('Frame pacing/limiting averaged %.2f ms. This is deliberate waiting, separate from CPU/GPU workload.', pacing / n) } end
    if both >= 5 and gpuHigh / both > 0.7 then observations[#observations + 1] = { kind = 'gpu',
      text = string.format('GPU time exceeded CPU work time in %d / %d slow observations with both readings. GPU pressure is a candidate, not proven causation.', gpuHigh, both) } end
    if unfocused > 0 then observations[#observations + 1] = { kind = 'focus',
      text = string.format('%d observations were captured while unfocused; background limits can affect the history.', unfocused) } end
    if phaseOverruns > 0 then observations[#observations + 1] = { kind = 'overlap',
      text = string.format('%d samples have phase sums above their reported total. Raw durations are retained; overlap/query lag is not hidden.', phaseOverruns) } end
  end
  local impacts = {}
  for _, kind in ipairs({ 'cpu', 'gpu', 'mod' }) do
    for _, row in pairs(series[kind]) do
      row.avgMs = row.totalMs / math.max(1, n)
      row.activeAvgMs = row.totalMs / row.active
      row.kind = kind
      if row.name ~= 'Unattributed' and row.name ~= 'Unattributed GPU' and row.name ~= 'FPSLimiter'
          and row.name ~= 'WaitNextFrame' and row.slowFrames > 0
          and (row.activeAvgMs >= 0.25 or row.maxMs >= 1) then impacts[#impacts + 1] = row end
    end
    series[kind] = ranked(series[kind])
  end
  table.sort(impacts, function(a, b) return a.slowMs > b.slowMs end)
  for i = 1, math.min(4, #impacts) do
    local r = impacts[i]
    observations[#observations + 1] = { kind = r.kind, frame = r.peakFrame,
      text = string.format('%s / %s: %.2f ms average on active frames, %.2f ms peak at #%d; present in %d slow frames.',
        r.kind:upper(), r.name, r.activeAvgMs, r.maxMs, r.peakFrame, r.slowFrames) }
  end
  return { mode = M.mode, count = n, avgMs = n > 0 and sum / n or 0, p95 = pct(times, 0.95), p99 = pct(times, 0.99),
    worst = worst, overBudget = slow, budgetMs = budget, series = series, observations = observations,
    history = history, gpuSamples = gpuSamples }
end

return M
