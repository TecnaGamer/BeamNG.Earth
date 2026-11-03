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
local M = {}
M.dependencies = { 'ui_imgui' }

M.BENCH_S = 30
M.STAGE_MAX_S = 60
M.RAY_UP = 1000
M.RAY_LEN = 3000
M.MAX_VZ = 30
M.ZERO_INPUTS = "input.event('throttle', 0, 2); input.event('brake', 0, 2); input.event('steering', 0, 2); "
  .. "input.event('clutch', 0, 2); input.event('parkingbrake', 0, 2)"

-- Speed presets for Route A -> B Auto-Benchmark (distance adapted to speed so every run covers ~45 s of driving):
--   - slow:  80 km/h (50 mph)   -> 1.00 km
--   - fast:  200 km/h (124 mph) -> 2.50 km
--   - ultra: 300 km/h (186 mph) -> 3.75 km
M.BENCH_SPEEDS = {
  {
    id = 'slow',
    label = 'Slow: 80 km/h (50 mph)',
    shortLabel = '80 km/h (50 mph)',
    kmh = 80,
    mph = 50,
    mps = 22.222,
    targetKm = 1.0,
    duration = 45,
  },
  {
    id = 'fast',
    label = 'Fast: 200 km/h (124 mph)',
    shortLabel = '200 km/h (124 mph)',
    kmh = 200,
    mph = 124,
    mps = 55.556,
    targetKm = 2.5,
    duration = 45,
  },
  {
    id = 'ultra',
    label = 'Ultra: 300 km/h (186 mph)',
    shortLabel = '300 km/h (186 mph)',
    kmh = 300,
    mph = 186,
    mps = 83.333,
    targetKm = 3.75,
    duration = 45,
  },
}

-- Extensible list of point-to-point Auto-Benchmark routes (Mode 2: Route Auto-Drive).
-- Each route spans >= 6 km along OSRM roads so Slow (1.0 km), Fast (2.5 km), and Ultra (3.75 km) runs all fit.
-- Maintainers and regional addons can register additional routes via M.registerBenchRoute(route).
M.BENCH_ROUTES = {
  {
    id = 'ventoux_d974',
    name = 'Mont Ventoux D974 Climb (Malaucene -> Summit)',
    shortName = 'Mont Ventoux D974 (Climb)',
    region = 'Provence',
    startLat = 44.171279, startLon = 5.139169,
    endLat = 44.173433, endLon = 5.277728,
    speedMps = 22.222,
    duration = 45,
  },
  {
    id = 'tavel_vineyards',
    name = 'Tavel Vineyards -> Lirac Village (D4 / Route de la Commanderie)',
    shortName = 'Tavel Vineyards -> Lirac',
    region = 'Provence',
    startLat = 43.994226, startLon = 4.740771,
    endLat = 44.036874, endLon = 4.690922,
    speedMps = 22.222,
    duration = 45,
  },
  {
    id = 'lirac_tavel',
    name = 'Lirac Narrow Streets -> Tavel Vineyards',
    shortName = 'Lirac Streets -> Vineyards',
    region = 'Provence',
    startLat = 44.036874, startLon = 4.690922,
    endLat = 43.994226, endLon = 4.740771,
    speedMps = 22.222,
    duration = 45,
  },
  {
    id = 'sault_lavender',
    name = 'Sault Plateau Lavender Fields -> Aurel (D942)',
    shortName = 'Sault Lavender Fields (D942)',
    region = 'Provence',
    startLat = 44.081847, startLon = 5.365593,
    endLat = 44.129500, endLon = 5.428000,
    speedMps = 22.222,
    duration = 45,
  },
}

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
local gps = require('beamngEarth/gps')
local im = nil
local ffi = require('ffi')
local form = nil        -- the open report window: { dir, data, kind, titleBuf, noteBuf, frames }
local prof = nil        -- the running profiler: { t0, dur, stage, route, speedPreset, loadMode, fromCef, dts, track, hitches, dir, start, form }
local saved = nil       -- the ImGui "report saved" window: { folder, dir, url, untilT }
local lastSaved = nil   -- persistent last saved report for CEF UI (never cleared by ImGui timer)
local ring, ringN = {}, 0
local toast, toastUntil = nil, 0
local count = 0
local indexed = {}      -- report folders already in index.jsonl (a re-save rewrites the files only)
local lastKind = 'map'

-- wall seconds (os.clock is the process CPU time over all threads: stopwatch.lua)
local wallMs = require('beamngEarth/stopwatch').clock()
local function now() return wallMs() / 1000 end

function M.getIssueRepo()
  return fmt.ISSUE_REPO or fmt.REPO
end

function M.getBenchRoutes()
  return M.BENCH_ROUTES
end

function M.getBenchSpeeds()
  return M.BENCH_SPEEDS
end

function M.registerBenchRoute(r)
  if type(r) ~= 'table' or type(r.id) ~= 'string' then return false end
  for i, existing in ipairs(M.BENCH_ROUTES) do
    if existing.id == r.id then
      M.BENCH_ROUTES[i] = r
      return true
    end
  end
  M.BENCH_ROUTES[#M.BENCH_ROUTES + 1] = r
  return true
end

local function findRoute(routeId)
  for _, r in ipairs(M.BENCH_ROUTES) do
    if r.id == routeId then return r end
  end
  return M.BENCH_ROUTES[1]
end

local function findSpeed(speedId)
  for _, s in ipairs(M.BENCH_SPEEDS) do
    if s.id == speedId then return s end
  end
  return M.BENCH_SPEEDS[1]
end

-- Kinematic OSRM road-spline helpers (same math as tools/drive_driver.lua)
local function headingOf(dx, dy)
  return math.deg(math.atan2(dx, dy)) % 360
end

local function angleDiff(a, b)
  local d = (a - b) % 360
  if d > 180 then d = d - 360 end
  return d
end

local function yawQuat(dh)
  local h = math.rad(dh) / 2
  return 0, 0, math.sin(h), math.cos(h)
end

local function groundAt(x, y, zTop)
  if type(castRayStatic) ~= 'function' or type(vec3) ~= 'function' then return nil end
  local d = castRayStatic(vec3(x, y, zTop), vec3(0, 0, -1), M.RAY_LEN)
  if d and d < M.RAY_LEN then return zTop - d end
  return nil
end

local function makePath(pts)
  if type(pts) ~= 'table' or #pts < 2 then return nil end
  local la0 = tonumber(pts[1][1])
  if not la0 then return nil end
  local kx = 111320 * math.cos(math.rad(la0))
  local path = { la = {}, lo = {}, cum = { 0 }, total = 0, k = 1 }
  for i, q in ipairs(pts) do
    path.la[i], path.lo[i] = tonumber(q[1]), tonumber(q[2])
    if i > 1 then
      local dx = (path.lo[i] - path.lo[i - 1]) * kx
      local dy = (path.la[i] - path.la[i - 1]) * 110540
      path.cum[i] = path.cum[i - 1] + math.sqrt(dx * dx + dy * dy)
    end
  end
  path.total = path.cum[#path.cum] or 0
  return path
end

local function pathAt(path, s)
  local n = #path.cum
  local k = path.k or 1
  if s <= 0 then k = 1 end
  while k < n - 1 and path.cum[k + 1] < s do k = k + 1 end
  while k > 1 and path.cum[k] > s do k = k - 1 end
  path.k = k
  local seg = path.cum[k + 1] - path.cum[k]
  local f = seg > 0 and math.max(0, math.min(1, (s - path.cum[k]) / seg)) or 0
  return path.la[k] + f * (path.la[k + 1] - path.la[k]), path.lo[k] + f * (path.lo[k + 1] - path.lo[k])
end

local function pathXY(path, s)
  local core = rawget(_G, 'beamngEarth_core')
  local st = core and core._state and core._state()
  if not (st and st.anchor) then return nil end
  local la, lo = pathAt(path, s)
  local x, y = st.anchor:geodeticToEnu(la, lo, 0)
  return x, y
end

local function stats(dts)
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
  return res
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
-- (hardware.json).
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
  local bngSettings = rawget(_G, 'beamngEarth_settings')
  if bngSettings and type(bngSettings.getAll) == 'function' then
    local allS = try(bngSettings.getAll)
    if type(allS) == 'table' then
      hw.effortProfile = allS.effortProfile or 'balanced'
      hw.addonsConfig = {
        addonProvence = allS.addonProvence ~= false,
        sourceIgnRpg = allS.sourceIgnRpg ~= false,
        sourceIgnOrtho = allS.sourceIgnOrtho ~= false,
        sourceIgnBuildings = allS.sourceIgnRpg ~= false,
        sourceProvenceBiomes = allS.sourceProvenceBiomes ~= false,
      }
      local parts = {}
      if allS.addonProvence ~= false then
        parts[#parts + 1] = 'provence=ON'
        parts[#parts + 1] = 'ign_rpg=' .. (allS.sourceIgnRpg ~= false and 'ON' or 'OFF')
        parts[#parts + 1] = 'ign_ortho=' .. (allS.sourceIgnOrtho ~= false and 'ON' or 'OFF')
        parts[#parts + 1] = 'biomes=' .. (allS.sourceProvenceBiomes ~= false and 'ON' or 'OFF')
      else
        parts[#parts + 1] = 'provence=OFF (base global)'
      end
      hw.activeAddons = table.concat(parts, ', ')
    end
  end
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
  for i = 1, math.min(ringN, M.RING) do recent[#recent + 1] = ring[i] end
  d.recentFrames = stats(recent)
  return d
end

local function newDir(kind)
  count = count + 1
  local rel = M.DIR .. os.date('%Y%m%d_%H%M%S') .. '_' .. kind .. '_' .. count .. '/'
  if not FS:directoryExists(rel) then FS:directoryCreate(rel, true) end
  return rel
end

local function screenshot(rel)
  if type(createScreenshot2) ~= 'function' then return nil end
  local ok = pcall(createScreenshot2, { filename = rel .. 'screenshot', writeJPG = true, superSampling = 1 })
  return ok and (FS:getUserPath() .. rel .. 'screenshot.jpg'):gsub('\\', '/') or nil
end

local function encode(t)
  local ok, s = pcall(jsonEncodePretty or jsonEncode, t)
  if ok and s then return s end
  return jsonEncode(t)
end

-- Open a GitHub issue URL via the companion server (`open_page`), bypassing BeamNG 0.40's C++ domain filter.
-- Never overwrites the user's clipboard automatically; use M.copyLastReport() for explicit clipboard copy.
function M.openIssueUrl(url)
  if type(url) ~= 'string' or url == '' then return false end
  local core = rawget(_G, 'beamngEarth_core')
  if core and type(core.openPage) == 'function' then
    return core.openPage(url)
  end
  if openWebBrowser then
    pcall(openWebBrowser, url)
    return true
  end
  return false
end

-- Recover `lastSaved` from the latest entry in `beamngEarth/reports/index.jsonl` if `lastSaved` is nil
-- (for example after an extension reload or game restart).
local function ensureLastSaved()
  if lastSaved and lastSaved.dir then return lastSaved end
  local idx = readFile(M.DIR .. 'index.jsonl')
  if not idx or idx == '' then return nil end
  local lastLine = nil
  for line in idx:gmatch('[^\r\n]+') do
    if line:match('%S') then lastLine = line end
  end
  if not lastLine then return nil end
  local ok, entry = pcall(jsonDecode, lastLine)
  if not ok or type(entry) ~= 'table' or type(entry.folder) ~= 'string' then return nil end
  local folder = entry.folder:gsub('\\', '/')
  local rel = folder:match('([Bb]eam[Nn][Gg][Ee]arth/reports/[^/]+/?)$')
  if not rel then return nil end
  if not rel:match('/$') then rel = rel .. '/' end
  local md = readFile(rel .. 'issue.md')
  local repRaw = readFile(rel .. 'report.json')
  local okR, repData = pcall(function() return repRaw and jsonDecode(repRaw) or nil end)
  local summary = nil
  if okR and type(repData) == 'table' then
    summary = repData.bench and fmt.framesLine(repData.bench) or (repData.recentFrames and fmt.framesLine(repData.recentFrames))
  end
  if not summary and type(entry.fps) == 'number' then
    summary = string.format('%.1f fps avg', entry.fps)
  end
  local url = nil
  if md and md ~= '' then
    local k = fmt.kind(entry.kind or 'perf')
    local title, body = fmt.splitIssue(md)
    url = fmt.issueUrl(title, body, k.label, fmt.URL_MAX, M.getIssueRepo())
  end
  lastSaved = {
    folder = folder,
    dir = rel,
    url = url,
    issueMd = md,
    summary = summary,
    kind = fmt.kind(entry.kind or 'perf').name,
    repo = M.getIssueRepo(),
  }
  return lastSaved
end

local function write(rel, data, quiet)
  data.folder = (FS:getUserPath() .. rel):gsub('\\', '/')
  writeFile(rel .. 'report.json', encode(data))
  local okH, hw = pcall(M.collectHardware)
  if not okH then hw = { error = tostring(hw) } end
  pcall(writeFile, rel .. 'hardware.json', encode(hw))
  local okI, issue = pcall(fmt.buildIssue, data, hw)
  if okI then pcall(writeFile, rel .. 'issue.md', issue) else log('W', 'beamngEarth', 'issue.md: ' .. tostring(issue)) end
  if not indexed[rel] then
    indexed[rel] = true
    local line = jsonEncode({ time = data.time, kind = data.kind, folder = data.folder, lat = data.lat, lon = data.lon,
      title = data.title, note = data.note,
      fps = data.bench and data.bench.fps or (data.recentFrames and data.recentFrames.fps) })
    local idx = M.DIR .. 'index.jsonl'
    local old = readFile(idx) or ''
    writeFile(idx, old .. line .. '\n')
  end
  log('I', 'beamngEarth', 'report saved: ' .. data.folder)
  local url = nil
  if okI then
    local k = fmt.kind(data.kind)
    local title, body = fmt.splitIssue(issue)
    url = fmt.issueUrl(title, body, k.label, fmt.URL_MAX, M.getIssueRepo())
  end
  local summary = data.bench and fmt.framesLine(data.bench) or (data.recentFrames and fmt.framesLine(data.recentFrames)) or nil
  lastSaved = {
    folder = data.folder,
    dir = rel,
    url = url,
    issueMd = okI and issue or nil,
    summary = summary,
    mode = data.benchMode or 'free',
    routeName = data.route and (data.route.shortName or data.route.name) or nil,
    repo = M.getIssueRepo(),
    kind = fmt.kind(data.kind).name,
  }
  if quiet then return end
  saved = { folder = data.folder, dir = rel, url = url, issueMd = okI and issue or nil, summary = summary,
    untilT = now() + M.SAVED_S, kind = fmt.kind(data.kind).name }
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
  end
  prof = { t0 = now(), dur = tonumber(seconds) or M.BENCH_S, stage = 'recording', dts = {}, track = {}, hitches = {},
    dir = rel, lastSample = -1, start = start, form = pending }
  toast, toastUntil = string.format('Performance profiler: recording %d s (keep driving)', prof.dur), now() + 3
end
M.benchmark = M.profiler   -- the input action (beamngEarth_reportBench) and older console calls

-- Ctrl+Shift+E while the profiler runs: the report window opens when it ends.
function M.profilerAttach()
  if prof and not prof.form then prof.form = { kind = 'perf' } end
  toast, toastUntil = 'The report window opens when the profiler ends', now() + 3
end

local function stopRouteAutodrive()
  pcall(function()
    gps.speedOverride = nil
    if gps.autodrive and gps.autodrive() then gps.setAutodrive(false) end
    gps.clear()
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    if veh then
      veh:applyClusterVelocityScaleAdd(-1, 0, 0, 0, 0)
      veh:queueLuaCommand(M.ZERO_INPUTS .. '; ai.setMode("disabled")')
    end
  end)
end

local function computeTrackDistance(track)
  local dist = 0
  local prev = nil
  for _, s in ipairs(track or {}) do
    local p = s.pos
    if type(p) == 'table' and p[1] and p[2] then
      if prev then
        local dx, dy = p[1] - prev[1], p[2] - prev[2]
        local step = math.sqrt(dx * dx + dy * dy)
        if step < 350 then dist = dist + step end
      end
      prev = p
    end
  end
  return dist
end

local function finishProf(b, elapsedS)
  if b.route then stopRouteAutodrive() end
  local data = b.start or capture()
  data.kind = 'perf'
  data.benchMode = b.mode or (b.route and 'route' or 'free')
  if b.route then
    local sp = b.speedPreset or findSpeed('slow')
    data.route = {
      id = b.route.id,
      name = b.route.name,
      shortName = b.route.shortName,
      startLat = b.route.startLat,
      startLon = b.route.startLon,
      endLat = b.route.endLat,
      endLon = b.route.endLon,
      speedId = sp.id,
      speedLabel = sp.shortLabel or sp.label,
      speedKmh = sp.kmh,
      speedMph = sp.mph,
      speedMps = b.speedMps or sp.mps,
      targetKm = sp.targetKm,
      loadMode = b.loadMode or 'full',
      distanceDrivenM = (b.s and b.s > 0) and b.s or computeTrackDistance(b.track),
    }
  end
  data.bench = stats(b.dts)
  data.bench.seconds = elapsedS
  data.bench.hitches = b.hitches
  data.track = b.track
  data.endState = capture()
  prof = nil
  local f = b.form
  if b.fromCef then
    -- Started from the CEF Generation Addons window: write files & update lastSaved quietly without popping ImGui
    write(b.dir, data, true)
  elseif f and f.data then
    write(b.dir, data, false)
  else
    write(b.dir, data, true)
    openForm(b.dir, data, 'perf')
    toast, toastUntil = 'Performance profiler done: add a title and Save', now() + 4
  end
  if M.pushUiStatus then M.pushUiStatus() end
end

-- Check world load state during `stage == 'staging'` before starting the route drive.
local function checkStagingReady(b, elapsed)
  if b.forceStart then return true end

  local core = rawget(_G, 'beamngEarth_core')
  local st = nil
  if core and core.status then
    local ok, s = pcall(function() return loadstring('return ' .. core.status())() end)
    if ok and type(s) == 'table' then st = s end
  end
  local teleporting = st and st.teleporting == true
  local atStartLoc = false
  if st and st.player and st.player.lat and st.player.lon and b.route then
    local dlat = (st.player.lat - b.route.startLat) * 110540
    local dlon = (st.player.lon - b.route.startLon) * 111320 * math.cos(math.rad(b.route.startLat))
    atStartLoc = (dlat * dlat + dlon * dlon) < (250 * 250)
  end

  -- Only request the OSRM route AFTER the teleport has placed the player at the route's start coordinates!
  -- Requesting before teleport completes causes gps.lua to plan a route from the previous location (kilometres away).
  if atStartLoc and not teleporting and not b.destSet and b.route then
    b.destSet = true
    b.destSetAt = elapsed
    b.nextRouteRetry = elapsed + 3.5
    pcall(function()
      gps.clear()
      gps.setDestination(b.route.endLat, b.route.endLon, b.route.shortName or b.route.name)
    end)
  end

  local r = gps.route and gps.route()
  local routeReady = false
  if type(r) == 'table' and type(r.points) == 'table' and #r.points >= 2 and b.route then
    local p1 = r.points[1]
    if type(p1) == 'table' and tonumber(p1[1]) and tonumber(p1[2]) then
      local rlat = (tonumber(p1[1]) - b.route.startLat) * 110540
      local rlon = (tonumber(p1[2]) - b.route.startLon) * 111320 * math.cos(math.rad(b.route.startLat))
      routeReady = (rlat * rlat + rlon * rlon) < (250 * 250)
    end
  end

  -- Re-request route every 3.5 seconds if OSRM route from startLat/startLon hasn't arrived yet
  if atStartLoc and not teleporting and not routeReady and b.route and elapsed >= (b.nextRouteRetry or 3.0) then
    b.nextRouteRetry = elapsed + 3.5
    pcall(function()
      gps.clear()
      gps.setDestination(b.route.endLat, b.route.endLon, b.route.shortName or b.route.name)
    end)
  end
  -- If OSRM takes > 12s after arriving at start, allow fallback straight-line path so benchmark never hangs
  local sinceDest = b.destSetAt and (elapsed - b.destSetAt) or 0
  local routeOrFallback = routeReady or (atStartLoc and sinceDest >= 12.0)

  local veh = getPlayerVehicle and getPlayerVehicle(0)
  local p = veh and veh:getPosition()
  local g = p and groundAt(p.x, p.y, p.z + M.RAY_UP)
  -- Note: st.commits in core.status() is a table of commit records (or a count)
  local commitsCount = st and (type(st.commits) == 'table' and #st.commits or tonumber(st.commits)) or 0
  local hasGround = (g ~= nil) or (commitsCount >= 1)
  local baseReady = (elapsed >= 1.5) and (not teleporting) and atStartLoc and routeOrFallback and hasGround

  local queued = st and st.far and tonumber(st.far.queued) or 0
  local backlog = st and tonumber(st.serverBacklog) or 0
  local farDone = st and ((st.far and st.far.done == true) or (tostring(st.status or ''):find('Far field ready', 1, true) ~= nil))
  local fullReady = baseReady and farDone and (queued == 0) and (backlog == 0)

  if not baseReady then
    b.readySince = nil
    if teleporting or not atStartLoc or not hasGround then
      b.stageText = string.format('Waiting for terrain spawn (%ds)...', math.floor(elapsed))
    elseif not routeReady then
      b.stageText = string.format('Waiting for OSRM road route (%ds)...', math.floor(elapsed))
    else
      b.stageText = string.format('Preparing start (%ds)...', math.floor(elapsed))
    end
    return false
  end

  if b.loadMode == 'cold' then
    -- Immediate / Cold streaming mode: start as soon as start terrain + OSRM route are ready
    b.readySince = b.readySince or now()
    local settle = now() - b.readySince
    b.stageText = string.format('Ready (starting drive in %.1fs)...', math.max(0, 0.8 - settle))
    return settle >= 0.8
  end

  -- Default ('full'): wait until all initial tiles, far-field meshes, and server backlog finish loading
  if fullReady then
    b.readySince = b.readySince or now()
    local settle = now() - b.readySince
    b.stageText = string.format('World 100%% loaded - starting (%.1fs)...', math.max(0, 1.5 - settle))
    if settle >= 1.5 then return true end
  else
    b.readySince = nil
    b.stageText = string.format('Loading world (tiles:%d srv:%d, %ds)...', queued, backlog, math.floor(elapsed))
    if elapsed >= M.STAGE_MAX_S then
      return true
    end
  end
  return false
end

local function beginRouteRecording(b)
  b.stage = 'recording'
  b.t0 = now()
  b.lastSample = -1
  b.s = 0
  b.frame = 0
  local r = gps.route and gps.route()
  local validRoute = false
  if r and type(r.points) == 'table' and #r.points >= 2 and b.route then
    local p1 = r.points[1]
    if type(p1) == 'table' and tonumber(p1[1]) and tonumber(p1[2]) then
      local rlat = (tonumber(p1[1]) - b.route.startLat) * 110540
      local rlon = (tonumber(p1[2]) - b.route.startLon) * 111320 * math.cos(math.rad(b.route.startLat))
      validRoute = (rlat * rlat + rlon * rlon) < (250 * 250)
    end
  end
  if validRoute then
    b.path = makePath(r.points)
  elseif b.route then
    -- Fallback 2-point path if OSRM was unreachable or stale
    b.path = makePath({
      { b.route.startLat, b.route.startLon },
      { b.route.endLat, b.route.endLon },
    })
  end
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if veh then
    local p = veh:getPosition()
    local d = veh:getDirectionVector()
    local g = groundAt(p.x, p.y, p.z + M.RAY_UP) or p.z
    b.ground = g
    b.clearance = math.max(0.05, math.min(3.5, p.z - g))
    b.z = g + b.clearance
    b.cmd = (d and (d.x ~= 0 or d.y ~= 0)) and headingOf(d.x, d.y) or 0
    veh:queueLuaCommand(M.ZERO_INPUTS .. '; ai.setMode("disabled")')
  end
  b.start = capture()
  b.start.screenshot = screenshot(b.dir)
  b.start.kind = 'perf'
  b.start.title = b.title
  b.start.note = b.note
end

local function stepRouteKinematic(b, dtReal)
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if not veh or not b.path then return false end
  local dtSim = math.max(0.001, math.min(tonumber(dtReal) or 0.016, 0.1))
  b.frame = (b.frame or 0) + 1
  local speed = b.speedMps or 22.222
  local ds = speed * dtSim
  local x1, y1 = pathXY(b.path, b.s + ds)
  local x2, y2 = pathXY(b.path, b.s + ds + 4)
  if not x1 or not y1 then return false end

  local hx, hy = 0, 1
  local h = b.cmd or 0
  if x2 and y2 then
    local dx, dy = x2 - x1, y2 - y1
    local len = math.sqrt(dx * dx + dy * dy)
    if len > 1e-6 then
      hx, hy = dx / len, dy / len
      h = headingOf(hx, hy)
    end
  end

  local p = veh:getPosition()
  local zTop = (p and p.z or b.ground or 0) + M.RAY_UP
  local g = groundAt(x1, y1, zTop)
  if g then b.ground = g end
  local z = (b.ground or (p and p.z) or 0) + (b.clearance or 0.6)
  local vz = math.max(-M.MAX_VZ, math.min(M.MAX_VZ, (z - (b.z or z)) / dtSim))
  b.z = z

  if b.frame == 1 or b.frame % 20 == 0 then
    local d = veh:getDirectionVector()
    if d and (d.x ~= 0 or d.y ~= 0) then
      local mh = headingOf(d.x, d.y)
      if b.frame == 1 or math.abs(angleDiff(mh, b.cmd or mh)) > 5 then
        b.cmd = mh
      end
    end
  end

  local qx, qy, qz, qw = yawQuat(angleDiff(h, b.cmd or h))
  b.cmd = h
  veh:setClusterPosRelRot(-1, x1, y1, z, qx, qy, qz, qw)
  veh:applyClusterVelocityScaleAdd(-1, 0, hx * speed, hy * speed, vz)
  if b.frame % 15 == 1 then
    veh:queueLuaCommand(string.format(
      "for _, wd in pairs(wheels.wheels or {}) do wd.tireSoundVolumeCoef = 0 if wd.radius and wd.radius > 0 then wd.angularVelocity = %f / wd.radius * (wd.wheelDir or 1) end end",
      speed))
  end

  b.s = b.s + ds
  local targetKm = (b.targetMeters or 1000) / 1000
  local kmh = b.speedPreset and b.speedPreset.kmh or math.floor(speed * 3.6 + 0.5)
  b.stageText = string.format('Driving %.2f / %.2f km (%d km/h)', b.s / 1000, targetKm, kmh)

  if b.s >= (b.targetMeters or 1000) or (b.path.total > 20 and b.s >= b.path.total - 5) then
    return true -- finished target distance
  end
  return false
end

local function profFrame(dtMs, dtReal)
  local b = prof
  local t = now() - b.t0
  if b.stage == 'staging' then
    local ready = checkStagingReady(b, t)
    if math.floor(t * 2) ~= b.lastStagePush then
      b.lastStagePush = math.floor(t * 2)
      if M.pushUiStatus then M.pushUiStatus() end
    end
    if ready then
      beginRouteRecording(b)
      if M.pushUiStatus then M.pushUiStatus() end
    end
    return
  end

  b.dts[#b.dts + 1] = dtMs
  if b.route and b.path then
    local doneDist = stepRouteKinematic(b, dtReal)
    if doneDist then
      finishProf(b, t)
      return
    end
  end

  local core = rawget(_G, 'beamngEarth_core')
  if dtMs > 50 then
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    local p = veh and veh:getPosition()
    b.hitches[#b.hitches + 1] = { t = t, ms = dtMs, pos = p and { p.x, p.y, p.z } or nil }
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
    if M.pushUiStatus then M.pushUiStatus() end
  end
  if t >= b.dur then
    finishProf(b, t)
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
  local ls = ensureLastSaved()
  local stStage = prof and (prof.stage or 'recording') or nil
  local stDur = prof and prof.dur or M.BENCH_S
  return {
    formOpen = form ~= nil,
    noteOpen = form ~= nil,
    profilerRunning = prof ~= nil,
    benchRunning = prof ~= nil,
    stage = stStage,
    stageText = prof and prof.stageText or nil,
    loadMode = prof and prof.loadMode or nil,
    mode = prof and prof.mode or (ls and ls.mode) or 'free',
    routeName = prof and prof.route and (prof.route.shortName or prof.route.name) or (ls and ls.routeName) or nil,
    distanceM = prof and prof.s or 0,
    targetMeters = prof and prof.targetMeters or 0,
    saved = count,
    lastFolder = ls and ls.folder or nil,
    elapsed = prof and math.max(0, math.floor(now() - prof.t0)) or 0,
    duration = stDur,
    lastSummary = ls and ls.summary or nil,
    lastUrl = ls and ls.url or nil,
    issueRepo = M.getIssueRepo(),
    routes = M.BENCH_ROUTES,
    speeds = M.BENCH_SPEEDS,
  }
end

-- Push live status directly to the CEF Generation Addons window (`earthAddonsWindow.js`) so the UI never freezes
-- even if CEF timers are cleared by a route change or reload.
function M.pushUiStatus()
  if not be or not be.queueJS then return end
  local ok, js = pcall(function()
    return 'if(window.bngEarthAddonsWindow && window.bngEarthAddonsWindow.applyStatus) window.bngEarthAddonsWindow.applyStatus(' .. jsonEncode(M.status()) .. ');'
  end)
  if ok and js then pcall(be.queueJS, be, js) end
end

-- Allow the CEF UI to change loadMode live while staging (e.g. clicking "Immediate (Stream on Move)" starts driving right away)
function M.uiSetLoadMode(loadMode)
  local lMode = (loadMode == 'cold') and 'cold' or 'full'
  if prof and prof.stage == 'staging' then
    prof.loadMode = lMode
    if lMode == 'cold' then
      prof.forceStart = true
    end
    return { ok = true, loadMode = lMode, forced = (lMode == 'cold') }
  end
  return { ok = true, loadMode = lMode }
end

-- Allow the user to manually start the drive immediately if currently staging
function M.uiForceStartNow()
  if prof and prof.stage == 'staging' then
    prof.forceStart = true
    return { ok = true, started = true }
  end
  return { ok = false }
end

-- Start a benchmark from the CEF Generation Addons UI:
--   mode: 'free' (30s free drive) or 'route' (point-to-point route autodrive)
--   arg: duration seconds (for 'free') or routeId string (for 'route')
--   speedId: 'slow' (80 km/h, 1.0 km) | 'fast' (200 km/h, 2.5 km) | 'ultra' (300 km/h, 3.75 km)
--   loadMode: 'full' (wait for full world load before driving, default) | 'cold' (start as soon as terrain + road ready)
function M.uiStartBench(mode, arg, speedId, loadMode)
  onLevel()
  if not ourLevel then return { ok = false, error = 'Not in BeamNG.Earth level' } end
  if prof then return { ok = true, alreadyRunning = true } end
  if type(mode) == 'number' then
    arg = mode
    mode = 'free'
  end
  mode = (mode == 'route') and 'route' or 'free'

  local bngSettings = rawget(_G, 'beamngEarth_settings')
  local allS = bngSettings and bngSettings.getAll and bngSettings.getAll() or {}
  local addonTag = (allS.addonProvence ~= false) and 'provence=ON' or 'provence=OFF'
  local effortTag = tostring(allS.effortProfile or 'balanced')
  local rel = newDir('profile')
  saved = nil -- ensure old ImGui saved box is closed

  if mode == 'route' then
    local route = findRoute(tostring(arg or 'ventoux_d974'))
    local sp = findSpeed(tostring(speedId or 'slow'))
    local lMode = (loadMode == 'cold') and 'cold' or 'full'
    local core = rawget(_G, 'beamngEarth_core')
    -- Only teleport if player is more than 120 m away from the route start
    local alreadyAtStart = false
    if core and core.status then
      local ok, st = pcall(function() return loadstring('return ' .. core.status())() end)
      if ok and type(st) == 'table' and st.player and st.player.lat and st.player.lon then
        local dlat = (st.player.lat - route.startLat) * 110540
        local dlon = (st.player.lon - route.startLon) * 111320 * math.cos(math.rad(route.startLat))
        if (dlat * dlat + dlon * dlon) < (120 * 120) then
          alreadyAtStart = true
        end
      end
    end
    pcall(gps.clear)
    if not alreadyAtStart and core and core.teleport then
      pcall(core.teleport, route.startLat, route.startLon, route.shortName or route.name, { record = false })
    else
      pcall(gps.setDestination, route.endLat, route.endLon, route.shortName or route.name)
    end
    local loadLabel = (lMode == 'cold') and 'immediate-cold' or 'full-load'
    local title = string.format('Route Benchmark: %s @ %d km/h (%s, %s, effort=%s)',
      route.shortName or route.id, sp.kmh, loadLabel, addonTag, effortTag)
    local note = string.format(
      'Automated point-to-point route benchmark on %s (%.5f, %.5f -> %.5f, %.5f, speed %d km/h / %d mph, target %.2f km, start=%s, %s, effortProfile=%s).',
      route.name, route.startLat, route.startLon, route.endLat, route.endLon,
      sp.kmh, sp.mph, sp.targetKm, loadLabel, addonTag, effortTag)
    prof = {
      t0 = now(),
      dur = tonumber(sp.duration) or 45,
      stage = 'staging',
      stageText = alreadyAtStart and 'Requesting OSRM route...' or 'Teleporting to route start...',
      destSet = alreadyAtStart,
      destSetAt = alreadyAtStart and 0 or nil,
      mode = 'route',
      route = route,
      speedPreset = sp,
      speedMps = sp.mps,
      targetMeters = (sp.targetKm or 1.0) * 1000,
      loadMode = lMode,
      fromCef = true,
      title = title,
      note = note,
      dts = {},
      track = {},
      hitches = {},
      dir = rel,
      lastSample = -1,
    }
    return {
      ok = true,
      mode = 'route',
      route = route.shortName or route.name,
      speed = sp.shortLabel,
      targetKm = sp.targetKm,
      loadMode = lMode,
      duration = prof.dur,
    }
  end

  local dur = tonumber(arg) or M.BENCH_S
  local start = capture()
  start.screenshot = screenshot(rel)
  start.kind = 'perf'
  start.benchMode = 'free'
  start.title = string.format('Benchmark (%s, effort=%s)', addonTag, effortTag)
  start.note = string.format('Automated %ds in-game free-drive performance benchmark ticket (%s, effortProfile=%s).', dur, addonTag, effortTag)
  prof = {
    t0 = now(),
    dur = dur,
    stage = 'recording',
    mode = 'free',
    fromCef = true,
    dts = {},
    track = {},
    hitches = {},
    dir = rel,
    lastSample = -1,
    start = start,
    form = { data = start, kind = 'perf' },
  }
  return { ok = true, mode = 'free', duration = prof.dur }
end

function M.uiStopBench()
  if not prof then return { ok = false } end
  local b = prof
  local elapsed = math.max(1, now() - b.t0)
  if b.stage == 'staging' or #b.dts < 10 then
    if b.route then stopRouteAutodrive() end
    prof = nil
    if M.pushUiStatus then M.pushUiStatus() end
    return { ok = true, cancelled = true }
  end
  finishProf(b, elapsed)
  return { ok = true, stoppedEarly = true }
end

function M.openLastFolder()
  local ls = ensureLastSaved()
  if ls and ls.dir and Engine and Engine.Platform and Engine.Platform.exploreFolder then
    pcall(Engine.Platform.exploreFolder, '/' .. ls.dir)
    return { ok = true, folder = ls.folder }
  end
  return { ok = false, error = 'No saved report folder found' }
end

function M.openLastIssueUrl()
  local ls = ensureLastSaved()
  if ls and ls.url then
    local ok = M.openIssueUrl(ls.url)
    return { ok = ok, repo = ls.repo or M.getIssueRepo(), url = ls.url }
  end
  return { ok = false, error = 'No saved report ticket found' }
end

function M.copyLastReport()
  local ls = ensureLastSaved()
  if not ls then return { ok = false, error = 'No saved report found' } end
  local md = ls.issueMd or (ls.dir and readFile(ls.dir .. 'issue.md')) or nil
  if type(md) == 'string' and md ~= '' and setClipboard then
    pcall(setClipboard, md)
    return { ok = true, bytes = #md }
  end
  return { ok = false, error = 'Clipboard unavailable or issue.md empty' }
end

function M.onPreRender(dtReal)
  if not ourLevel or not dtReal or dtReal <= 0 then return end
  local ms = dtReal * 1000
  ringN = ringN + 1
  ring[(ringN - 1) % M.RING + 1] = ms
  if prof then profFrame(ms, dtReal) end
end

local function wrapped(s)
  im.PushTextWrapPos(0)
  im.TextUnformatted(tostring(s))
  im.PopTextWrapPos()
end

local function drawSaved()
  if not saved then return end
  if now() > saved.untilT then saved = nil return end
  im.SetNextWindowSize(im.ImVec2(560, 0), im.Cond_FirstUseEver)
  if im.Begin('BeamNG.Earth report saved##bngeSaved', nil, im.WindowFlags_AlwaysAutoResize) then
    im.TextUnformatted(saved.kind .. ' report saved in:')
    im.TextUnformatted(saved.folder)
    wrapped('issue.md there is a ready-to-post GitHub issue: edit it, then post it with the screenshot '
      .. '(or run: python3 tools/reports.py issue).')
    if im.Button('Open folder') then
      pcall(Engine.Platform.exploreFolder, '/' .. saved.dir)
      saved.untilT = now() + M.SAVED_S
    end
    if saved.url then
      im.SameLine()
      if im.Button('Open GitHub issue page') then
        M.openIssueUrl(saved.url)
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
  if prof and not prof.fromCef then
    im.SetNextWindowPos(im.ImVec2(20, 160), im.Cond_Always)
    if im.Begin('##bngeBench', nil, im.WindowFlags_NoDecoration + im.WindowFlags_AlwaysAutoResize
        + im.WindowFlags_NoInputs) then
      im.TextUnformatted(string.format('Performance profiler: %.0f / %d s', now() - prof.t0, prof.dur))
    end
    im.End()
  end
  drawSaved()
  if form then drawForm() end
end

return M
