-- In-game reports for the user and the friends testing the mod (2026-10-13: "a reporting tool that
-- makes it easy for me to take notes on the world or to record the performance of my game";
-- 2026-10-14: reports become GitHub issues: map issues, performance issues, bugs, ideas).
--
--   Ctrl+Shift+E   a report: the moment is captured at once (screenshot, location, frame times, engine
--                  metrics, the mod's state), then a window takes the kind (Map issue / Performance
--                  issue / Bug / Idea), a short title and the description; Save writes it all.
--   Ctrl+Shift+Y   the performance profiler: 30 s of frame times (and position, every second) while the
--                  player drives; then the same window, kind "Performance issue", with its numbers.
--
-- Reports go to <user folder>/beamngEarth/reports/<date>_<time>_<report|profile>_<n>/ (the game
-- lowercases new folders): report.json, screenshot.jpg, hardware.json (what the engine can tell about
-- the machine and the graphics settings, collectHardware()) and issue.md (a ready-to-post GitHub issue
-- the user edits: issuefmt.lua; tools/reports.py issue N opens it and the new-issue page).
-- reports/index.jsonl gets one line per report. Kinds before 2026-10-14: 'note', 'bench'.
--
-- 2026-11-12 (the user: "send reports directly to our mini pc server" and "update the reporter with any info and data
-- that would be useful to us"): a report also holds the exact view (camera, the point and object under the screen's
-- centre), the environment, the mod's settings and the enabled mods, the last 60 s of frames (reportsend.lua), and the
-- world server writes its part into the folder when the report is taken: server.json (build, settings, memory,
-- backlog, the map's ids and features at the spot, data sources) and logs.json (server/reportsend.py), user names and
-- home folders scrubbed out (also from report.json / hardware.json: report_saved). The saved window's "Send to the
-- developers" opens reportsend's window: what is sent, with boxes to leave out the screenshot, the exact location and
-- the logs; nothing is sent before its Send. Reports are private (never published); "Open GitHub issue page" stays.
local M = {}
M.dependencies = { 'ui_imgui' }

M.BENCH_S = 30
-- Only on the BeamNG.Earth level: the hotkeys, the settings page's Feedback buttons and the frame-time ring do nothing
-- elsewhere (the user, 2026-10-20: the mod should be effectively off on other maps)
M.LEVEL_ID = 'beamng_earth'
local ourLevel = false
local function onLevel() ourLevel = rawget(_G, 'getCurrentLevelIdentifier') ~= nil and getCurrentLevelIdentifier() == M.LEVEL_ID end
M.onClientStartMission = onLevel
function M.onClientEndMission() ourLevel = false end
function M.onExtensionLoaded() if scenetree and scenetree.MissionGroup then onLevel() end end
function M.inOurLevel() return ourLevel end
M.RING = 900            -- frames kept for the "recent" statistics in a report (~10 s at 90 fps)
M.DIR = 'beamngEarth/reports/'
M.SAVED_S = 30          -- the "report saved" window closes itself after this long

local fmt = require('beamngEarth/issuefmt')
local sender = require('beamngEarth/reportsend')
M.RING60 = 12000        -- frames kept for the last 60 s (reportsend.frames: 200 fps for 60 s)
local im = nil
local ffi = require('ffi')
local form = nil        -- the open report window: { dir, data, kind, titleBuf, noteBuf, frames }
local prof = nil        -- the running profiler: { t0, dur, dts, track, hitches, dir, start, form }
local saved = nil       -- the "report saved" window: { folder, dir, url, untilT }
local ring, ringN = {}, 0
local ring60, ring60N = {}, 0
local heldRing = {}     -- (beside ring: why the car was held in that frame, or false: stats)
local toast, toastUntil = nil, 0
local count = 0
local indexed = {}      -- report folders already in index.jsonl (a re-save rewrites the files only)
local lastKind = 'map'

-- wall seconds (os.clock is the process CPU time over all threads: stopwatch.lua)
local wallMs = require('beamngEarth/stopwatch').clock()
local function now() return wallMs() / 1000 end

-- dts: frame ms; held (optional, beside dts): why the car was held still while the world loaded in that frame ('edge',
-- 'ground', 'loading': core.heldReason) or false. The held frames are summed apart (res.held: seconds, why, slow ones)
-- and res.playing gives the frames while not held: a car held at the ground's edge is waiting, not a slow game.
local function stats(dts, held)
  local n = #dts
  if n == 0 then return { frames = 0 } end
  local s = {}
  local sum, mx = 0, 0
  for i = 1, n do s[i] = dts[i]; sum = sum + dts[i]; if dts[i] > mx then mx = dts[i] end end
  table.sort(s)
  local function pct(p) return s[math.max(1, math.ceil(p * n))] end
  local over = { 33, 50, 100 }
  local res = { frames = n, avgMs = sum / n, fps = n / (sum / 1000), p50 = pct(0.5), p95 = pct(0.95),
    p99 = pct(0.99), maxMs = mx }
  for _, o in ipairs(over) do
    local c = 0
    for i = 1, n do if dts[i] > o then c = c + 1 end end
    res['over' .. o] = c
  end
  if type(held) == 'table' then
    local h = { s = 0, frames = 0, over50 = 0, why = {} }
    local pn, psum, pover = 0, 0, 0
    for i = 1, n do
      local why = held[i]
      if why then
        h.frames, h.s = h.frames + 1, h.s + dts[i] / 1000
        h.why[why] = (h.why[why] or 0) + dts[i] / 1000
        if dts[i] > 50 then h.over50 = h.over50 + 1 end
      else
        pn, psum = pn + 1, psum + dts[i]
        if dts[i] > 50 then pover = pover + 1 end
      end
    end
    if h.frames > 0 then
      res.held = h
      res.playing = { frames = pn, fps = psum > 0 and pn / (psum / 1000) or nil, over50 = pover }
    end
  end
  return res
end

-- Why the car is held still while the world loads now (core.heldReason), or false.
local function heldNow()
  local core = rawget(_G, 'beamngEarth_core')
  if not (core and core.heldReason) then return false end
  local ok, why = pcall(core.heldReason)
  return ok and why or false
end
M.stats = stats

local function safe(f, ...)
  local ok, v = pcall(f, ...)
  if ok then return v end
  return 'error: ' .. tostring(v)
end

-- A table's plain values (strings, numbers, booleans), one level: what jsonEncode always takes.
local function scalars(t)
  if type(t) ~= 'table' then return nil end
  local c = {}
  for k, v in pairs(t) do
    local tv = type(v)
    if type(k) == 'string' and (tv == 'string' or tv == 'number' or tv == 'boolean') then c[k] = v end
  end
  return c
end

-- An engine call that may be missing in this game version: nil instead of an error.
local function try(f, ...)
  if type(f) ~= 'function' then return nil end
  local ok, v = pcall(f, ...)
  if ok then return v end
  return nil
end

local function platform(name)
  local P = rawget(_G, 'Engine') and Engine.Platform
  return P and try(P[name])
end

-- What the game can tell about the machine, the game and its graphics settings, the mod and the server
-- (hardware.json). The engine calls are those of the game's own hardware page and screenshot metadata
-- (lua/ge/extensions/core/hardwareinfo.lua getInfo(), lua/ge/screenshot.lua getMetadataJson()); every
-- one may be missing: the field is then left out.
function M.collectHardware()
  local hw = {}
  hw.os = scalars(platform('getOSInfo'))
  hw.cpu = scalars(platform('getCPUInfo'))
  hw.gpu = scalars(platform('getGPUInfo'))
  local mem = scalars(platform('getMemoryInfo'))
  if mem then hw.mem = { osPhysAvailable = mem.osPhysAvailable, osPhysUsed = mem.osPhysUsed,
    processPhysUsed = mem.processPhysUsed } end
  if hw.os and hw.os.type == 'linux' then hw.os.windowsystem = platform('getVideoDriver') end
  local R = rawget(_G, 'Engine') and Engine.Render
  hw.graphicsApi = R and try(R.getAdapterType)
  local G = rawget(_G, 'GFXDevice')
  hw.videoMode = scalars(G and try(G.getVideoMode))
  hw.desktopVideoMode = try(rawget(_G, 'getDesktopVideoMode'))
  hw.game = { versiond = rawget(_G, 'beamng_versiond'), versionb = rawget(_G, 'beamng_versionb'),
    buildnumber = rawget(_G, 'beamng_buildnumber'), arch = rawget(_G, 'beamng_arch'),
    buildtype = rawget(_G, 'beamng_buildtype') }
  local S = rawget(_G, 'settings')
  if S and type(S.getValue) == 'function' then
    hw.settings = {}
    for _, e in ipairs(fmt.SETTING_KEYS) do
      local v = try(S.getValue, e[1])
      if v ~= nil and type(v) ~= 'table' then hw.settings[e[1]] = v end
    end
  end
  local mm = rawget(_G, 'core_modmanager')
  if mm and try(mm.isReady) then hw.mods = scalars(try(mm.getStats)) end
  local core = rawget(_G, 'beamngEarth_core')
  hw.modVersion = core and core.MOD_VERSION
  hw.server = core and try(core.serverHello) or nil
  return hw
end

-- Everything the moment can tell: where, what the engine and the mod are doing.
local function capture()
  local d = { time = os.date('%Y-%m-%d %H:%M:%S'), unix = os.time(), level = safe(getCurrentLevelIdentifier) }
  local core = rawget(_G, 'beamngEarth_core')
  if core and core.status then
    local ok, st = pcall(function() return loadstring('return ' .. core.status())() end)
    d.mod = ok and st or tostring(st)
    if ok and type(st) == 'table' and st.player then
      d.lat, d.lon = st.player.lat, st.player.lon
      d.anchorId = st.anchorId
    end
  end
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if veh then
    local p, v, f = veh:getPosition(), veh:getVelocity(), veh:getDirectionVector()
    d.vehicle = { name = safe(function() return veh:getJBeamFilename() end), pos = { p.x, p.y, p.z },
      speedMs = v:length(), speedMph = v:length() * 2.23694, headingDeg = math.deg(math.atan2(f.x, f.y)) % 360 }
  end
  if core_camera then
    local c = core_camera.getPosition()
    local fw = core_camera.getForward and core_camera.getForward()
    d.camera = { pos = { c.x, c.y, c.z }, forward = fw and { fw.x, fw.y, fw.z } or nil }
  end
  local m = {}
  if Engine.Debug and Engine.Debug.getLastPerformanceMetrics then pcall(Engine.Debug.getLastPerformanceMetrics, m) end
  d.engineMetrics = m
  d.fpsLimit = safe(function() return Engine.getCurrentFpsLimitReason and Engine.getCurrentFpsLimitReason() end)
  d.vram = safe(function() return Engine.Render and Engine.Render.getVRAMUsage and Engine.Render.getVRAMUsage() end)
  local recent = {}
  local recentHeld = {}
  for i = 1, math.min(ringN, M.RING) do recent[#recent + 1] = ring[i]; recentHeld[#recentHeld + 1] = heldRing[i] or false end
  d.recentFrames = stats(recent, recentHeld)
  -- (reportsend.lua: the exact view, the environment, the settings and mods, the last 60 s)
  local okV, view = pcall(sender.view)
  d.view = okV and view or { error = tostring(view) }
  local okE, env = pcall(sender.environment)
  d.environment = okE and env or { error = tostring(env) }
  local okC, cfg = pcall(sender.config)
  d.config = okC and cfg or { error = tostring(cfg) }
  local okF, f60 = pcall(sender.frames, ring60, ring60N, M.RING60, stats)
  d.frames60 = okF and f60 or nil
  if d.vehicle and d.lat then d.vehicle.lat, d.vehicle.lon = d.lat, d.lon end
  return d
end

local function newDir(kind)
  count = count + 1
  local rel = M.DIR .. os.date('%Y%m%d_%H%M%S') .. '_' .. kind .. '_' .. count .. '/'
  if not FS:directoryExists(rel) then FS:directoryCreate(rel, true) end
  return rel
end

-- (the file's name only: a full path holds the player's account name)
local function screenshot(rel)
  if type(createScreenshot2) ~= 'function' then return nil end
  local ok = pcall(createScreenshot2, { filename = rel .. 'screenshot', writeJPG = true, superSampling = 1 })
  return ok and 'screenshot.jpg' or nil
end

local function encode(t)
  local ok, s = pcall(jsonEncodePretty or jsonEncode, t)
  if ok and s then return s end
  return jsonEncode(t)
end

local function write(rel, data, quiet)
  -- (the folder inside the user folder: a full path holds the player's account name; the saved window shows it all)
  local full = (FS:getUserPath() .. rel):gsub('\\', '/')
  data.folder = rel
  writeFile(rel .. 'report.json', encode(data))
  local okH, hw = pcall(M.collectHardware)
  if not okH then hw = { error = tostring(hw) } end
  pcall(writeFile, rel .. 'hardware.json', encode(hw))
  local okI, issue = pcall(fmt.buildIssue, data, hw)
  if okI then pcall(writeFile, rel .. 'issue.md', issue) else log('W', 'beamngEarth', 'issue.md: ' .. tostring(issue)) end
  if not indexed[rel] then
    indexed[rel] = true
    local line = jsonEncode({ time = data.time, kind = data.kind, folder = full, lat = data.lat, lon = data.lon,
      title = data.title, note = data.note,
      fps = data.bench and data.bench.fps or (data.recentFrames and data.recentFrames.fps) })
    local idx = M.DIR .. 'index.jsonl'
    local old = readFile(idx) or ''
    writeFile(idx, old .. line .. '\n')
  end
  log('I', 'beamngEarth', 'report saved: ' .. rel)
  pcall(sender.saved, rel)       -- (the server scrubs the files: user names, home folders)
  if quiet then return end
  local url = nil
  if okI then
    local k = fmt.kind(data.kind)
    local title, body = fmt.splitIssue(issue)
    url = fmt.issueUrl(title, body, k.label)
  end
  saved = { folder = full, dir = rel, url = url, untilT = now() + M.SAVED_S, kind = fmt.kind(data.kind).name,
    data = data }
end

local function openForm(rel, data, kind)
  form = { dir = rel, data = data, kind = kind or lastKind, titleBuf = nil, noteBuf = nil, frames = 0 }
end

-- Ctrl+Shift+E: capture now, open the report window.
function M.note()
  if form or not ourLevel then return end
  if prof then M.profilerAttach() return end
  local rel = newDir('report')
  local data = capture()
  data.screenshot = screenshot(rel)       -- the view the player saw (the window opens next frame)
  pcall(sender.askContext, rel, data)     -- (the world server's part of the moment: server.json, logs.json)
  saved = nil
  openForm(rel, data)
end

-- Ctrl+Shift+Y: the performance profiler over the next BENCH_S seconds. With the report window open,
-- the profile goes into that report (kind Performance issue).
function M.profiler(seconds)
  if prof or not ourLevel then return end
  local rel, start, pending
  if form then
    rel, start, pending = form.dir, form.data, form
    pending.kind = 'perf'
    form = nil
  else
    rel = newDir('profile')
    start = capture()
    start.screenshot = screenshot(rel)
    pcall(sender.askContext, rel, start)
  end
  prof = { t0 = now(), dur = tonumber(seconds) or M.BENCH_S, dts = {}, held = {}, track = {}, hitches = {}, dir = rel,
    lastSample = -1, start = start, form = pending }
  toast, toastUntil = string.format('Performance profiler: recording %d s (keep driving)', prof.dur), now() + 3
end
M.benchmark = M.profiler   -- the input action (beamngEarth_reportBench) and older console calls

-- Ctrl+Shift+E while the profiler runs: the report window opens when it ends.
function M.profilerAttach()
  if prof and not prof.form then prof.form = { kind = 'perf' } end
  toast, toastUntil = 'The report window opens when the profiler ends', now() + 3
end

local function profFrame(dtMs)
  local b = prof
  b.dts[#b.dts + 1] = dtMs
  local why = heldNow()
  b.held[#b.held + 1] = why
  local t = now() - b.t0
  local core = rawget(_G, 'beamngEarth_core')
  if dtMs > 50 then
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    local p = veh and veh:getPosition()
    b.hitches[#b.hitches + 1] = { t = t, ms = dtMs, pos = p and { p.x, p.y, p.z } or nil, held = why or nil }
  end
  if math.floor(t) > b.lastSample then
    b.lastSample = math.floor(t)
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    local p = veh and veh:getPosition()
    local s = { t = math.floor(t), pos = p and { p.x, p.y, p.z } or nil,
      speedMph = veh and veh:getVelocity():length() * 2.23694 or nil }
    if core and core.status then
      local ok, st = pcall(function() return loadstring('return ' .. core.status())() end)
      if ok and type(st) == 'table' then
        s.lat, s.lon = st.player and st.player.lat, st.player and st.player.lon
        s.far = st.far and { live = st.far.live, queued = st.far.queued } or nil
        s.trees = st.trees and st.trees.items or nil
        s.sea = st.sea and st.sea.tiles or nil
      end
    end
    local m = {}
    if Engine.Debug and Engine.Debug.getLastPerformanceMetrics then pcall(Engine.Debug.getLastPerformanceMetrics, m) end
    s.metrics = m
    b.track[#b.track + 1] = s
  end
  if t >= b.dur then
    local data = b.start
    data.kind = 'perf'
    data.bench = stats(b.dts, b.held)
    data.bench.seconds = t
    data.bench.hitches = b.hitches
    data.track = b.track
    data.endState = capture()
    prof = nil
    local f = b.form
    if f and f.data then
      -- started from the report window ("Save + record"): the user's text is already in data
      write(b.dir, data)
    else
      -- the hotkey: saved at once (nothing is lost), then the window for the title / description
      write(b.dir, data, true)
      openForm(b.dir, data, 'perf')
      toast, toastUntil = 'Performance profiler done: add a title and Save', now() + 4
    end
  end
end

local function formText(f)
  if f.titleBuf then f.data.title = ffi.string(f.titleBuf) end
  if f.noteBuf then f.data.note = ffi.string(f.noteBuf) end
  f.data.kind = f.kind
  lastKind = f.kind
end

-- Save the open report with this text (tests, the console). kind: map / perf / bug / idea.
function M.saveNote(text, kind, title)
  if not form then M.note() end
  if not form then return end
  form.data.note = tostring(text or '')
  form.data.title = title and tostring(title) or form.data.title
  form.data.kind = kind or form.kind
  write(form.dir, form.data)
  form = nil
end

function M.status()
  local name = saved and saved.dir and saved.dir:match('([^/]+)/?$')
  return { formOpen = form ~= nil, noteOpen = form ~= nil, profilerRunning = prof ~= nil,
    benchRunning = prof ~= nil, saved = count, lastFolder = saved and saved.folder, lastDir = saved and saved.dir,
    sendOpen = sender.isOpen(), send = name and sender.status(name) or nil }
end

function M.onPreRender(dtReal)
  if not ourLevel or not dtReal or dtReal <= 0 then return end
  local ms = dtReal * 1000
  ringN = ringN + 1
  ring[(ringN - 1) % M.RING + 1] = ms
  ring60N = ring60N + 1
  ring60[(ring60N - 1) % M.RING60 + 1] = ms
  heldRing[(ringN - 1) % M.RING + 1] = heldNow()
  if prof then profFrame(ms) end
end

local function wrapped(s)
  im.PushTextWrapPos(0)
  im.TextUnformatted(tostring(s))
  im.PopTextWrapPos()
end

local function drawSaved()
  if not saved then return end
  if sender.isOpen() then saved.untilT = now() + M.SAVED_S end
  if now() > saved.untilT then saved = nil return end
  im.SetNextWindowSize(im.ImVec2(560, 0), im.Cond_FirstUseEver)
  if im.Begin('BeamNG.Earth report saved##bngeSaved', nil, im.WindowFlags_AlwaysAutoResize) then
    im.TextUnformatted(saved.kind .. ' report saved in:')
    im.TextUnformatted(saved.folder)
    -- (no "python3 tools/reports.py" here: the packaged app has no Python or tools folder for players to run, and
    -- one tried to and asked how to fix Python, GitHub #744)
    wrapped('"Send to the developers" sends it privately to the BeamNG.Earth developers (not published; you see '
      .. 'what is sent first). Or post it yourself: issue.md there is a ready-to-post GitHub issue: "Open GitHub '
      .. 'issue page" fills in the text; drag screenshot.jpg from the folder into it. If the page opens empty, paste '
      .. 'issue.md\'s text into it.')
    local st = sender.status(saved.dir:match('([^/]+)/?$'))
    if st and st.state == 'sent' then
      im.TextUnformatted('Sent to the developers. Report id: ' .. tostring(st.id or '?'))
    elseif st and st.state and st.state ~= '' then
      im.TextUnformatted('Send: ' .. st.state .. (st.text and (' (' .. st.text .. ')') or ''))
    end
    if im.Button('Send to the developers') then
      sender.open(saved.dir, saved.data)
      saved.untilT = now() + M.SAVED_S
    end
    im.SameLine()
    if im.Button('Open folder') then
      pcall(Engine.Platform.exploreFolder, '/' .. saved.dir)
      saved.untilT = now() + M.SAVED_S
    end
    if saved.url then
      im.SameLine()
      if im.Button('Open GitHub issue page') then
        local core = rawget(_G, 'beamngEarth_core')
        if core and core.openPage then pcall(core.openPage, saved.url)
        else pcall(openWebBrowser, saved.url) end
        saved.untilT = now() + M.SAVED_S
      end
    end
    im.SameLine()
    if im.Button('Close') then saved = nil end
  end
  im.End()
end

local function drawForm()
  local f = form
  f.frames = f.frames + 1
  if not f.titleBuf then
    f.titleBuf = im.ArrayChar(128, f.data.title or '')
    f.noteBuf = im.ArrayChar(4096, f.data.note or '')
  end
  im.SetNextWindowSize(im.ImVec2(560, 360), im.Cond_FirstUseEver)
  if im.Begin('BeamNG.Earth report##bngeNote', nil, 0) then
    im.TextUnformatted(string.format('%s  |  %s', f.data.time or '',
      f.data.lat and string.format('%.5f, %.5f', f.data.lat, f.data.lon) or 'no location'))
    for i, k in ipairs(fmt.KINDS) do
      if i > 1 then im.SameLine() end
      if im.RadioButton1(k.name .. '##bngeKind' .. i, f.kind == k.id) then f.kind = k.id end
    end
    wrapped(fmt.kind(f.kind).hint)
    im.TextUnformatted('Title:')
    if f.frames == 2 then im.SetKeyboardFocusHere() end
    im.PushItemWidth(-1)
    im.InputText('##bngeTitle', f.titleBuf, im.GetLengthArrayCharPtr(f.titleBuf))
    im.PopItemWidth()
    im.TextUnformatted('Description:')
    im.InputTextMultiline('##bngeNoteText', f.noteBuf, im.GetLengthArrayCharPtr(f.noteBuf), im.ImVec2(-1, 130))
    local b = f.data.bench
    if b and b.fps then
      im.TextUnformatted('Performance profiler: ' .. (fmt.framesLine(b) or ''))
    end
    if im.Button('Save report') then
      formText(f)
      write(f.dir, f.data)
      form = nil
    end
    if form and f.kind == 'perf' and not b then
      im.SameLine()
      if im.Button(string.format('Save + record %d s profile', M.BENCH_S)) then
        formText(f)
        M.profiler()
      end
    end
    im.SameLine()
    if form and im.Button(b and 'Close (keep the profile)' or 'Cancel') then form = nil end
  end
  im.End()
end

function M.onUpdate()
  im = im or rawget(_G, 'ui_imgui')
  if not im then return end
  if toast and now() < toastUntil then
    im.SetNextWindowPos(im.ImVec2(20, 120), im.Cond_Always)
    if im.Begin('##bngeReportToast', nil, im.WindowFlags_NoDecoration + im.WindowFlags_AlwaysAutoResize
        + im.WindowFlags_NoInputs) then
      im.TextUnformatted(toast)
    end
    im.End()
  end
  if prof then
    im.SetNextWindowPos(im.ImVec2(20, 160), im.Cond_Always)
    if im.Begin('##bngeBench', nil, im.WindowFlags_NoDecoration + im.WindowFlags_AlwaysAutoResize
        + im.WindowFlags_NoInputs) then
      im.TextUnformatted(string.format('Performance profiler: %.0f / %d s', now() - prof.t0, prof.dur))
    end
    im.End()
  end
  drawSaved()
  if form then drawForm() end
  sender.draw(im)
end

-- The world server's answers about reports (core.lua passes them): report_status, report_context_done.
function M.onServerMessage(msg) sender.onMessage(msg) end

-- The send window for a saved report folder (tests, the console): rel 'beamngEarth/reports/<name>/'.
function M.openSend(rel)
  local t = readFile(rel .. 'report.json')
  local ok, data = pcall(jsonDecode, t or '')
  sender.open(rel, ok and type(data) == 'table' and data or {})
end

return M
