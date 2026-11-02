-- Opt-in live profiler. Core supplies the previous completed frame's wall time and marks.
-- All history is bounded; no frame samples, UI allocations or network requests while off.
local M = { enabled = false, mode = 'essential', page = 'frames', compact = false }
local frameProfile = require('beamngEarth/frameprofile')
local frameUI = require('beamngEarth/frameprofile_ui')
local ioUI = require('beamngEarth/ioprofile_ui')
local transport = require('beamngEarth/net')
local clock = require('beamngEarth/stopwatch').clock()
local frames, frameAt, frameN = {}, 0, 0
local server, supported, receivedAt, sentAt = nil, nil, nil, -math.huge
local frozen, wasInLevel, lastSend, openPtr, message = false, false, nil, nil, nil
local view, refreshedAt = nil, -math.huge
local FRAME_KEEP = 600
local startedAt, captured = 0, 0
local supportedVersion, probe, pending, rtt = nil, 0, {}, { count = 0, sum = 0, timeouts = 0, samples = {} }

function M.reset()
  frameProfile.reset()
  frameUI.reset()
  frames, frameAt, frameN = {}, 0, 0
  server, receivedAt, view, message = nil, nil, nil, nil
  refreshedAt = -math.huge
  startedAt, captured, frozen = clock(), 0, false
  pending, rtt = {}, { count = 0, sum = 0, timeouts = 0, samples = {} }
end

function M.setMode(mode)
  if mode ~= 'essential' and mode ~= 'full' then return false end
  if M.mode == mode and frameProfile.mode == mode then return true end
  M.mode = mode
  frozen = false
  frameProfile.setMode(mode)
  FRAME_KEEP = frameProfile.KEEP
  M.reset()
  sentAt = -math.huge
  if transport.setProfiling then transport.setProfiling(false);transport.setProfiling(M.enabled) end
  return true
end

function M.setPage(page)
  if page == 'frames' or page == 'workers' or page == 'io' then M.page = page; return true end
  return false
end

function M.setFrozen(on)
  on = on == true
  if on == frozen then return end
  if on then view = view or M.snapshot() else view = nil end
  frozen = on
end

function M.setCompact(on)
  M.compact = on == true
end

function M.pong(msg)
  local started = pending[msg.probe]
  if not started or not M.enabled then return end
  pending[msg.probe] = nil
  local elapsed = math.max(0, clock()-started)
  rtt.count, rtt.sum, rtt.lastMs = rtt.count+1, rtt.sum+elapsed, elapsed
  if M.mode == 'full' then
    rtt.samples[#rtt.samples+1] = elapsed
    if #rtt.samples > 60 then table.remove(rtt.samples, 1) end
  end
end

function M.setEnabled(on)
  on = on == true
  if on ~= M.enabled then M.reset(); M.compact = false; sentAt = -math.huge end
  M.enabled = on
  if transport.setProfiling then transport.setProfiling(on) end
  if on and frameProfile.mode ~= M.mode then frameProfile.setMode(M.mode) end
  FRAME_KEEP = frameProfile.KEEP
  if not on and lastSend then lastSend({ type = 'runtime_profiler', enabled = false }) end
end

function M.serverHello(version)
  supported, supportedVersion = version == 1 or version == 2, version
  M.reset()
  sentAt = -math.huge
end

function M.receive(data)
  if not M.enabled or type(data) ~= 'table' or not data.enabled then return end
  receivedAt = clock()
  server = data
end

function M.update(ms, marks, inLevel, connected, send)
  if not M.enabled then return end
  local now = clock()
  lastSend = send
  if inLevel ~= wasInLevel then M.reset(); wasInLevel = inLevel end
  if connected and supported and now - sentAt >= (M.mode == 'essential' and 1000 or 500) then
    local enabled = inLevel and (supportedVersion == 2 or M.mode == 'full')
    probe = (probe + 1) % 2147483647
    if enabled and supportedVersion == 2 then pending[probe] = now end
    send({ type = 'runtime_profiler', enabled = enabled, mode = M.mode, probe = probe })
    sentAt = now
  end
  for id, started in pairs(pending) do if now-started>5000 then pending[id]=nil;rtt.timeouts=rtt.timeouts+1 end end
  if not connected then server, receivedAt, supported = nil, nil, nil end
  if not inLevel or type(ms) ~= 'number' or ms <= 0 or ms ~= ms or ms == math.huge then return end
  frameProfile.add(ms, marks)
  captured = captured + 1
  frameAt = frameAt % FRAME_KEEP + 1
  local sample = { ms = ms, marks = {} }
  if M.mode == 'full' then for k, v in pairs(marks or {}) do sample.marks[k] = v end end
  frames[frameAt] = sample
  frameN = math.min(frameN + 1, FRAME_KEEP)
end

local function ordered(map)
  local rows = {}
  for _, row in pairs(map) do rows[#rows + 1] = row end
  table.sort(rows, function(a, b) return (a.totalMs or 0) > (b.totalMs or 0) end)
  return rows
end

local function percentile(values, fraction)
  table.sort(values)
  return values[math.max(1, math.ceil(#values * fraction))] or 0
end

function M.snapshot()
  local sum, frameTimes, parts, pools, stages, plot = 0, {}, {}, {}, {}, {}
  for i = 1, frameN do plot[i - 1] = frames[(frameAt - frameN + i - 1) % FRAME_KEEP + 1].ms end
  for _, frame in ipairs(frames) do
    sum = sum + frame.ms
    frameTimes[#frameTimes + 1] = frame.ms
    for name, ms in pairs(frame.marks) do
      parts[name] = parts[name] or { name = name, totalMs = 0, maxMs = 0 }
      local p = parts[name]
      p.totalMs, p.maxMs = p.totalMs + ms, math.max(p.maxMs, ms)
    end
  end
  for _, job in ipairs(server and server.recent or {}) do
    local completed = job.endOffsetMs or job.doneAtOffsetMs
    if not completed or not server.elapsedMs or server.elapsedMs - completed <= 5000 then
    local p = pools[job.pool] or { name = job.pool, totalMs = 0, queueMs = 0, cpuMs = 0,
      count = 0, failed = 0, incomplete = 0, times = {} }
    pools[job.pool] = p
    if job.incomplete then p.incomplete = p.incomplete + 1
    elseif job.runMs then
      p.count = p.count + 1
      p.totalMs = p.totalMs + job.runMs
      p.queueMs = p.queueMs + (job.queueMs or 0)
      p.cpuMs = p.cpuMs + (job.cpuMs or 0)
      p.times[#p.times + 1] = job.runMs
    end
    if job.outcome == 'error' or job.outcome == 'cancelled' then p.failed = p.failed + 1 end
    for _, s in ipairs(job.steps or {}) do
      local key = job.pool .. ' / ' .. s.name
      local row = stages[key] or { name = key, totalMs = 0, cpuMs = 0, count = 0, maxMs = 0 }
      stages[key] = row
      row.totalMs, row.cpuMs, row.count = row.totalMs + s.totalMs, row.cpuMs + s.cpuMs, row.count + s.count
      row.maxMs = math.max(row.maxMs, s.maxMs)
    end
    end
  end
  for _, p in pairs(pools) do p.p95 = percentile(p.times, 0.95) end
  local feedback, queued, longest = {}, 0, nil
  for _, job in ipairs(server and server.active or {}) do
    if not job.worker then
      queued = queued + 1
      if not longest or (job.queueMs or 0) > (longest.queueMs or 0) then longest = job end
    elseif (job.runMs or 0) >= 3000 and #feedback < 2 then
      feedback[#feedback + 1] = string.format('%s / %s has run for %.0f ms; current sampled step: %s. Worker wall time is separate from game-frame delay.',
        job.pool, job.name, job.runMs, job.stage or 'job body')
    end
  end
  if longest and (longest.queueMs or 0) >= 500 then feedback[#feedback + 1] = string.format('%d jobs await a worker; longest queue wait %.0f ms in %s.', queued, longest.queueMs, longest.pool) end
  for _, p in ipairs(ordered(pools)) do
    if p.count > 0 and p.queueMs / p.count > 100 then
      feedback[#feedback + 1] = string.format('%s: queue wait averaged %.0f ms versus %.0f ms execution across %d recent jobs.',p.name,p.queueMs/p.count,p.totalMs/p.count,p.count)
      break
    end
  end
  local link = transport.profileState and transport.profileState() or {}
  local latency = {count=rtt.count, avgMs=rtt.count>0 and rtt.sum/rtt.count or nil, lastMs=rtt.lastMs, timeouts=rtt.timeouts}
  if M.mode=='full' and #rtt.samples>0 then
    local times, total = {}, 0
    for i,v in ipairs(rtt.samples) do times[i]=v;total=total+v end
    latency.p95 = percentile(times,0.95)
    local variance, mean = 0, total/#times
    for _,v in ipairs(times) do variance=variance+(v-mean)^2 end
    latency.jitterMs=math.sqrt(variance/#times)
  end
  return { mode = M.mode, capturedFrames = captured, elapsedMs = math.max(0, clock() - startedAt),
    frames = frameN, avgMs = frameN > 0 and sum / frameN or 0, fps = sum > 0 and frameN * 1000 / sum or 0,
    p95 = percentile(frameTimes, 0.95), maxMs = frameTimes[#frameTimes] or 0,
    parts = ordered(parts), pools = ordered(pools), stages = ordered(stages), server = server, plot = plot,
    frameDetail = frameProfile.analyze(), feedback = feedback, localLink = link, latency = latency }
end

local function text(im, s) im.TextUnformatted(tostring(s)) end
local function duration(ms) return ms and string.format('%.1f ms', ms) or 'unknown' end
local function wrapped(im, s) im.PushTextWrapPos(0); text(im, s); im.PopTextWrapPos() end
local function color(im, r, g, b) return im.GetColorU322(im.ImVec4(r, g, b, 1)) end
local function rect(im, dl, x, y, w, h, c)
  im.ImDrawList_AddRectFilled(dl, im.ImVec2(x, y), im.ImVec2(x + w, y + h), c)
end
local function bar(im, width, id, fraction, label)
  local pos = im.GetCursorScreenPos()
  im.InvisibleButton('##profile-' .. id, im.ImVec2(width, 19))
  local dl = im.GetWindowDrawList()
  rect(im, dl, pos.x, pos.y, width, 19, color(im, 0.12, 0.14, 0.17))
  rect(im, dl, pos.x, pos.y, math.min(1, math.max(0, fraction)) * width, 19, color(im, 0.12, 0.42, 0.57))
  im.ImDrawList_AddText1(dl, im.ImVec2(pos.x + 4, pos.y + 1), color(im, 0.95, 0.96, 0.98), label, nil)
end

local function timeline(im, data, width)
  local lanes, order = {}, {}
  local function add(job)
    if not job.worker then return end
    local key = job.pool .. ' / ' .. job.worker
    if not lanes[key] then lanes[key] = {}; order[#order + 1] = key end
    lanes[key][#lanes[key] + 1] = job
  end
  for _, job in ipairs(data.recent or {}) do add(job) end
  for _, job in ipairs(data.active or {}) do add(job) end
  table.sort(order)
  local now = data.elapsedMs or 0
  for _, key in ipairs(order) do
    local visible = false
    for _, job in ipairs(lanes[key]) do if (job.endOffsetMs or now) > now - 10000 then visible = true end end
    if visible then
      text(im, key)
      local pos = im.GetCursorScreenPos()
      im.InvisibleButton('##lane-' .. key, im.ImVec2(width, 13))
      local dl = im.GetWindowDrawList()
      rect(im, dl, pos.x, pos.y, width, 13, color(im, 0.12, 0.14, 0.17))
      local function span(a, b, c, y, h)
        a, b = math.max(now - 10000, a), math.min(now, b)
        if b > a then rect(im, dl, pos.x + (a - now + 10000) * width / 10000, pos.y + y,
          math.max(1, (b - a) * width / 10000), h, c) end
      end
      for _, job in ipairs(lanes[key]) do
        local start = job.startOffsetMs
        if start then
          span(job.submittedOffsetMs, start, color(im, 0.80, 0.51, 0.16), 10, 3)
          span(start, job.endOffsetMs or now, color(im, 0.18, 0.64, 0.79), 0, 9)
        end
      end
    end
  end
  if #order == 0 then text(im, 'Waiting for measured jobs. Already running jobs are captured on their next submission.') end
end

local function stop()
  M.setEnabled(false)
  local settings = require('beamngEarth/settings')
  if settings.SCHEMA and settings.SCHEMA.runtimeProfiler then settings.set('runtimeProfiler', false) end
end

local function refresh(now)
  if not view or (not frozen and now - refreshedAt >= (M.mode=='essential' and 250 or 100)) then
    view, refreshedAt = M.snapshot(), now
  end
end

local function summary(im)
  wrapped(im, string.format('%.1f FPS | frame avg %.2f ms | p95 %.2f ms | worst %.2f ms (%d recent frames)',
    view.fps, view.avgMs, view.p95, view.maxMs, view.frames))
end

function M.draw(inLevel)
  if not M.enabled or not inLevel then return end
  local im = rawget(_G, 'ui_imgui')
  if not im then return end
  local now = clock()
  if M.compact then
    im.SetNextWindowSize(im.ImVec2(420, 145), im.Cond_FirstUseEver)
    if im.Begin('Earth profiler / collecting') then
      refresh(now)
      summary(im)
      wrapped(im, frozen and 'Display frozen; collection continues.' or 'Live / continuous collection')
      if im.Button('Open profiler') then M.setCompact(false) end
      im.SameLine()
      if im.Button('Stop profiling') then stop() end
    end
    im.End()
    return
  end
  openPtr = openPtr or im.BoolPtr(true)
  openPtr[0] = true
  im.SetNextWindowSize(im.ImVec2(740, 680), im.Cond_FirstUseEver)
  if im.SetNextWindowSizeConstraints then im.SetNextWindowSizeConstraints(im.ImVec2(560, 360), im.ImVec2(10000, 10000)) end
  if im.Begin('BeamNG.Earth performance profiler', openPtr) then
    local available = im.GetContentRegionAvail()
    local width = math.max(1, available.x)
    local graphHeight = math.max(90, math.min(240, (im.GetWindowSize().y - 340) / 3))
    for _, mode in ipairs({'essential','full'}) do
      if im.Button((mode=='essential' and 'Essential' or 'Full')..(M.mode==mode and ' [selected]' or '')) then
        local settings = require('beamngEarth/settings')
        if settings.SCHEMA and settings.SCHEMA.profilerMode then settings.set('profilerMode',mode) else M.setMode(mode) end
      end
      im.SameLine()
    end
    text(im,'Collection mode')
    for _, page in ipairs({{'frames','Frames'},{'workers','Workers'},{'io','Network & Disk'}}) do
      if im.Button(page[2]..(M.page==page[1] and ' [open]' or '')) then M.setPage(page[1]) end
      im.SameLine()
    end
    text(im,'')
    if im.Button(frozen and 'Resume display' or 'Freeze display') then M.setFrozen(not frozen) end
    im.SameLine()
    refresh(now)
    if im.Button('Save snapshot') then
      local ok, result = pcall(function()
        -- Export exactly what is being inspected, including a frozen display.
        local capture = {}
        for k, v in pairs(view) do if k ~= 'plot' then capture[k] = v end end
        capture.frameDetail = {}
        for k, v in pairs(view.frameDetail) do if k ~= '_layouts' then capture.frameDetail[k] = v end end
        return jsonWriteFile('/settings/beamngEarth-profiler.json', capture, true)
      end)
      message = ok and result ~= false and 'Saved /settings/beamngEarth-profiler.json' or 'Snapshot could not be saved'
    end
    im.SameLine()
    if im.Button('Compact view') then M.setCompact(true) end
    im.SameLine()
    if im.Button('Stop profiling') then stop(); im.End(); return end
    summary(im)
    wrapped(im, string.format('%s | collecting %.1f s | %d frames sampled | history limit %d',
      frozen and 'DISPLAY FROZEN' or 'LIVE', math.max(0, now-startedAt)/1000, captured, FRAME_KEEP))
    if message then text(im, message) end
    if frozen then wrapped(im, 'Display frozen; frame, worker and network collection continues. Resume display to catch up.') end
    if supported == false then wrapped(im, 'This server does not support worker profiling. Update the server to see worker timings.')
    elseif not receivedAt or now - receivedAt > 3000 then text(im, 'Worker feed unavailable or stale; waiting for server telemetry.') end
    wrapped(im, 'Blue: execution (includes I/O and waits). Amber: queue wait. CPU is the executing thread only. Parallel jobs and nested stages overlap; do not add their times together.')
    if M.page=='frames' and im.CollapsingHeader1('Per-frame CPU / GPU history', im.TreeNodeFlags_DefaultOpen) then
      if view.frames > 0 then im.PlotLines1('##earth-profiler-frames', view.plot, view.frames, 0, 'Frame wall time (ms)', 0, nil, im.ImVec2(width, 55)) end
      frameUI.draw(im, view.frameDetail, width, graphHeight)
    end
    local data = view.server
    if M.page=='io' then ioUI.draw(im,data and data.io,view.localLink,view.latency,M.mode,width) end
    if data and data.collectionMode and data.collectionMode~=M.mode then wrapped(im,'Shared server collection is Full because another client requested it.') end
    if M.page=='workers' and M.mode=='essential' then wrapped(im,'Detailed worker timers, stage checks and timelines are disabled in Essential mode. Select Full to collect them.') end
    if M.page=='workers' and M.mode=='full' and data then
      if #view.feedback > 0 and im.CollapsingHeader1('Significant worker workloads', im.TreeNodeFlags_DefaultOpen) then
        for _, line in ipairs(view.feedback) do wrapped(im, line) end
      end
      if (data.skipped or 0) > 0 then text(im, string.format('%d jobs omitted at the telemetry capacity limit', data.skipped)) end
      if im.CollapsingHeader1('Worker timeline / last 10 seconds', im.TreeNodeFlags_DefaultOpen) then timeline(im, data, width) end
      if im.CollapsingHeader1('Running and queued jobs', im.TreeNodeFlags_DefaultOpen) then
        local active = data.active or {}
        for _, job in ipairs(active) do
          text(im, string.format('%s | %s | %s | %s | queue %s | run %s', job.pool, job.worker or 'waiting for worker',
            job.name, job.stage or 'job body', duration(job.queueMs), duration(job.runMs)))
        end
        if #active == 0 then text(im, 'No measured jobs in flight.') end
      end
      if #view.pools > 0 and im.CollapsingHeader1('Worker statistics / last 5 s, up to 120 jobs', im.TreeNodeFlags_DefaultOpen) then
        local max = view.pools[1] and view.pools[1].totalMs or 1
        for _, p in ipairs(view.pools) do
          local n = math.max(1, p.count)
          bar(im, width, 'pool-' .. p.name, p.totalMs / math.max(1, max), string.format('%s | %d jobs | avg %.1f | p95 %.1f | queue %.1f | CPU %.1f ms',
            p.name, p.count, p.totalMs / n, p.p95, p.queueMs / n, p.cpuMs / n))
          if p.failed + p.incomplete > 0 then text(im, string.format('  %d failed/cancelled; %d incomplete timing records', p.failed, p.incomplete)) end
        end
      end
      if #view.stages > 0 and im.CollapsingHeader1('Active generation steps / last 5 s, inclusive wall time') then
        local max = view.stages[1] and view.stages[1].totalMs or 1
        for _, s in ipairs(view.stages) do
          bar(im, width, 'stage-' .. s.name, s.totalMs / math.max(1, max), string.format('%s | %d calls | avg %.1f | max %.1f ms',
            s.name, s.count, s.totalMs / math.max(1, s.count), s.maxMs))
        end
      end
      if im.CollapsingHeader1('Recent jobs / expand for steps') then
        local recent = data.recent or {}
        for i = #recent, math.max(1, #recent - 19), -1 do
          local j = recent[i]
          if im.CollapsingHeader1(j.pool .. ' / ' .. j.name .. ' / ' .. (j.outcome or '?') .. '##' .. j.id) then
            text(im, string.format('Queue %s | execution %s | CPU %s | %s', duration(j.queueMs), duration(j.runMs), duration(j.cpuMs), j.worker or 'worker not observed'))
            if j.incomplete or (j.dropped or 0) > 0 then text(im, 'Telemetry incomplete; missing values are unknown.') end
            for _, s in ipairs(j.steps or {}) do bar(im, width, j.id .. s.name, s.totalMs / math.max(1, j.runMs or 1),
              string.format('%s | %.1f ms wall | %.1f ms CPU | %d calls', s.name, s.totalMs, s.cpuMs, s.count)) end
          end
        end
      end
    end
  end
  im.End()
  if not openPtr[0] then M.setCompact(true) end
end

return M
