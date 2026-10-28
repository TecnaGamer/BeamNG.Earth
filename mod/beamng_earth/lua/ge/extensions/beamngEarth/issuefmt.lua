-- Pure formatting for the in-game reports (report.lua): the report kinds, the hardware summary and the
-- ready-to-post GitHub issue (issue.md) the user edits before sending. No engine calls here, so the
-- offline tests (mod/tests/test_issuefmt.lua) run it under plain luajit; tools/reports.py mirrors the
-- kinds, labels and the URL truncation.
local M = {}

M.REPO = 'TecnaGamer/BeamNG.Earth'
M.URL_MAX = 7000        -- characters of the whole new-issue URL (GitHub refuses ~8 KB)

-- The kinds the user picks from (order = the window's radio buttons). label = the GitHub label.
M.KINDS = {
  { id = 'map', name = 'Map issue', label = 'map',
    hint = 'What is wrong, missing or broken in the world here? (roads, buildings, terrain, water...)' },
  { id = 'perf', name = 'Performance issue', label = 'performance',
    hint = 'What felt slow or stuttered? Run the performance profiler (30 s) while it happens.' },
  { id = 'bug', name = 'Bug', label = 'bug',
    hint = 'What broke? What did you do just before, and what did you expect?' },
  { id = 'idea', name = 'Idea / suggestion', label = 'enhancement',
    hint = 'What would you like to see?' },
}
-- Reports saved before the kinds (2026-10-13): a "note" and a "bench" (the benchmark, now the
-- performance profiler).
local LEGACY = { note = { id = 'note', name = 'Note', label = nil, hint = '' }, bench = 'perf', profile = 'perf' }

function M.kind(id)
  for _, k in ipairs(M.KINDS) do if k.id == id then return k end end
  local l = LEGACY[id]
  if type(l) == 'string' then return M.kind(l) end
  return l or { id = tostring(id), name = tostring(id), label = nil, hint = '' }
end

function M.kindIndex(id)
  for i, k in ipairs(M.KINDS) do if k.id == id then return i end end
  return 1
end

local function num(v) return type(v) == 'number' and v == v and v or nil end

function M.osmLink(lat, lon, zoom)
  lat, lon = num(lat), num(lon)
  if not lat or not lon then return nil end
  zoom = zoom or 17
  return string.format('https://www.openstreetmap.org/?mlat=%.6f&mlon=%.6f#map=%d/%.6f/%.6f', lat, lon, zoom, lat, lon)
end

-- A frame statistics table (report.lua stats(): fps, p99, maxMs...) as one line.
function M.framesLine(s)
  if type(s) ~= 'table' or not num(s.fps) or (s.frames or 0) == 0 then return nil end
  local parts = { string.format('%.1f fps avg', s.fps) }
  if num(s.p99) and s.p99 > 0 then parts[#parts + 1] = string.format('1%% low %.1f fps', 1000 / s.p99) end
  if num(s.maxMs) then parts[#parts + 1] = string.format('worst frame %.0f ms', s.maxMs) end
  if num(s.over50) then parts[#parts + 1] = string.format('%d frames over 50 ms', s.over50) end
  return table.concat(parts, ', ')
end

local function gb(bytes)
  bytes = num(bytes)
  return bytes and string.format('%.0f GB', bytes / 1073741824) or nil
end

local function str(v)
  if v == nil then return nil end
  if type(v) == 'string' then return v ~= '' and not v:match('^error:') and v or nil end
  return tostring(v)
end

-- Engine.getCurrentFpsLimitReason(): a string, or (0.40) a table of caps and flags
-- ({ vsync, fpsCap, foregroundCap, foregroundActive, backgroundCap, backgroundActive, ... }); the issue showed
-- "table: 0x..." (GitHub issue #38)
local function fpsLimit(v)
  if type(v) ~= 'table' then return str(v) end
  local parts = {}
  if v.vsync == true then parts[#parts + 1] = 'vsync' end
  if (num(v.fpsCap) or 0) > 0 then parts[#parts + 1] = string.format('fps cap %d', num(v.fpsCap)) end
  if v.foregroundActive == true then parts[#parts + 1] = string.format('foreground cap %d', num(v.foregroundCap) or 0) end
  if v.backgroundActive == true then parts[#parts + 1] = string.format('unfocused cap %d', num(v.backgroundCap) or 0) end
  if v.vr == true then parts[#parts + 1] = 'VR' end
  return #parts > 0 and table.concat(parts, ', ') or 'nothing'
end

local function first(t, keys)
  if type(t) ~= 'table' then return nil end
  for _, k in ipairs(keys) do
    local v = str(t[k])
    if v then return v end
  end
  return nil
end

local function onoff(v)
  if v == true or v == 1 or v == '1' or v == 'true' then return 'on' end
  if v == false or v == 0 or v == '0' or v == 'false' then return 'off' end
  return str(v)
end

-- The graphics settings shown in the issue (hardware.json keeps them all): { key, label, formatter }.
M.SETTING_KEYS = {
  { 'GraphicOverallQuality', 'Preset' }, { 'GraphicMeshQuality', 'Mesh' }, { 'GraphicTextureQuality', 'Textures' },
  { 'GraphicTerrainQuality', 'Terrain' }, { 'GraphicLightingQuality', 'Lighting' },
  { 'GraphicShadowsQuality', 'Shadows' }, { 'GraphicAnisotropic', 'Anisotropic' },
  { 'GraphicAntialiasType', 'AA' }, { 'GraphicAntialias', 'AA level' }, { 'vsync', 'Vsync', onoff },
  { 'PostFXDOFGeneralEnabled', 'DOF', onoff }, { 'PostFXSSAOGeneralEnabled', 'SSAO', onoff },
  { 'PostFXMotionBlurEnabled', 'Motion blur', onoff }, { 'GraphicDynReflectionEnabled', 'Reflections', onoff },
  { 'GraphicGrassDensity', 'Grass' }, { 'GraphicDisplayModes', 'Display mode' },
  { 'GraphicDisplayResolutions', 'Resolution setting' }, { 'GraphicDisplayRefreshRates', 'Refresh setting' },
}

-- The hardware / settings table (report.lua collectHardware(), saved as hardware.json) as Markdown
-- list lines. Every field is optional: whatever the engine could not tell is left out.
function M.formatHardware(hw)
  hw = type(hw) == 'table' and hw or {}
  local L = {}
  local function add(label, v) if v and v ~= '' then L[#L + 1] = '- **' .. label .. ':** ' .. v end end
  local os_ = hw.os or {}
  local osName = first(os_, { 'fullname', 'shortname', 'type' })
  if osName then
    local extra = {}
    if str(os_.bits) then extra[#extra + 1] = str(os_.bits) .. '-bit' end
    if str(os_.windowsystem) then extra[#extra + 1] = str(os_.windowsystem) end
    osName = osName .. (#extra > 0 and ' (' .. table.concat(extra, ', ') .. ')' or '')
  end
  add('OS', osName)
  local cpu = hw.cpu or {}
  local cpuName = first(cpu, { 'name', 'brand', 'model' })
  local phys = num(cpu.coresPhysical)
  local logical = num(cpu.coresLogical) or num(cpu.threads) or num(cpu.coresTotal)
  if cpuName or phys then
    local c = cpuName or 'CPU'
    if phys and logical and logical ~= phys then c = c .. string.format(' (%d cores / %d threads)', phys, logical)
    elseif phys or logical then c = c .. string.format(' (%d cores)', phys or logical) end
    add('CPU', c)
  end
  local mem = hw.mem or {}
  add('RAM', gb(mem.osPhysAvailable or mem.totalPhys))
  local gpu = hw.gpu or {}
  local gpuName = first(gpu, { 'name', 'description' })
  if gpuName then
    local extra = {}
    -- memoryMB is the native total-capacity field in MB; zero does not identify usable capacity.
    local memoryMB = num(gpu.memoryMB)
    if memoryMB and memoryMB > 0 then
      extra[#extra + 1] = string.format('%.0f MB VRAM', memoryMB)
    else
      extra[#extra + 1] = 'VRAM capacity unavailable'
    end
    local keys = {}
    for k in pairs(gpu) do keys[#keys + 1] = k end
    table.sort(keys)
    for _, k in ipairs(keys) do
      local lk = k:lower()
      if (lk:find('driver') or lk:find('version') or lk:find('vendor')) and str(gpu[k]) then
        extra[#extra + 1] = k .. ' ' .. str(gpu[k])
      end
    end
    add('GPU', gpuName .. (#extra > 0 and ' (' .. table.concat(extra, ', ') .. ')' or ''))
  end
  add('Graphics API', str(hw.graphicsApi))
  local vm = hw.videoMode or {}
  if num(vm.width) and num(vm.height) and vm.width > 0 then
    local d = string.format('%dx%d', vm.width, vm.height)
    if num(vm.refreshRate) and vm.refreshRate > 0 then d = d .. string.format(' @ %d Hz', vm.refreshRate) end
    if str(vm.displayMode) then d = d .. ', ' .. str(vm.displayMode) end
    add('Display', d)
  end
  local game = hw.game or {}
  local ver = first(game, { 'versiond', 'versionb', 'version' })
  if ver then
    add('BeamNG.drive', ver .. (str(game.buildnumber) and ' (build ' .. str(game.buildnumber) .. ')' or '')
      .. (str(game.arch) and ', ' .. str(game.arch) or ''))
  end
  local s = hw.settings or {}
  local parts = {}
  for _, e in ipairs(M.SETTING_KEYS) do
    local v = (e[3] or str)(s[e[1]])
    if v then parts[#parts + 1] = e[2] .. ' ' .. v end
  end
  if #parts > 0 then add('Graphics settings', table.concat(parts, ', ')) end
  local mods = hw.mods or {}
  if num(mods.zip) or num(mods.unpacked) then
    add('Other mods', string.format('%d zipped, %d unpacked', num(mods.zip) or 0, num(mods.unpacked) or 0))
  end
  -- (the release, from the server's VERSION; the mod's own MOD_VERSION never followed the releases: "0.4.0" in a 1.5.4 report)
  local app = (hw.server or {}).appVersion
  if app and app ~= '' then add('BeamNG.Earth', tostring(app))
  elseif hw.modVersion then add('BeamNG.Earth', 'mod ' .. tostring(hw.modVersion)) end
  local srv = hw.server or {}
  if next(srv) then
    local sp = {}
    local keys = {}
    for k in pairs(srv) do
      if k ~= 'type' and k ~= 'attribution' and k ~= 'serverVersion' and k ~= 'sessionId' and k ~= 'appVersion' then
        keys[#keys + 1] = k
      end
    end
    table.sort(keys)
    for _, k in ipairs(keys) do
      local v = srv[k]
      if type(v) ~= 'table' then sp[#sp + 1] = k .. '=' .. tostring(v) end
    end
    add('Server', (str(srv.serverVersion) or '?') .. (#sp > 0 and ' (' .. table.concat(sp, ', ') .. ')' or ''))
  else
    add('Server', 'not connected')
  end
  return L
end

local function trim(s) return (tostring(s or ''):gsub('^%s+', ''):gsub('%s+$', '')) end

-- The issue's title (no newlines, ~80 characters) from the user's title, else the first line of the note.
function M.title(data)
  local k = M.kind(data.kind)
  local t = trim(data.title)
  if t == '' then t = trim((tostring(data.note or '')):match('^[^\n]*')) end
  if t == '' then t = '(short title)' end
  t = t:gsub('[\r\n]+', ' ')
  if #t > 80 then t = t:sub(1, 77):gsub('[\128-\191]+$', ''):gsub('[\192-\255]$', '') .. '...' end
  return '[' .. k.name .. '] ' .. t
end

-- issue.md: line 1 '# <title>', then the body. data = report.json's table, hw = hardware.json's.
function M.buildIssue(data, hw)
  data = data or {}
  local k = M.kind(data.kind)
  local B = {}
  local function p(s) B[#B + 1] = s end
  p('# ' .. M.title(data))
  p('')
  p('**Kind:** ' .. k.name)
  p('')
  local note = trim(data.note)
  p('### What happened')
  p(note ~= '' and note or '_' .. (k.hint ~= '' and k.hint or 'Describe it here.') .. '_')
  p('')
  local link = M.osmLink(data.lat, data.lon)
  if link then
    p(string.format('**Location:** %.6f, %.6f ([OpenStreetMap](%s))', data.lat, data.lon, link))
  else
    p('**Location:** unknown (not in a BeamNG.Earth world?)')
  end
  local v = data.vehicle
  if type(v) == 'table' and num(v.speedMph) then
    p(string.format('**Vehicle:** %s, %.0f mph, heading %.0f°', str(v.name) or '?', v.speedMph, num(v.headingDeg) or 0))
  end
  p('**Time:** ' .. (str(data.time) or '?'))
  local fr = M.framesLine(data.recentFrames)
  if fr then p('**Performance (last ~10 s):** ' .. fr) end
  local b = data.bench
  if type(b) == 'table' then
    local line = M.framesLine(b)
    if line then
      p(string.format('**Performance profiler (%.0f s):** %s', num(b.seconds) or 0, line))
      local hitches = type(b.hitches) == 'table' and #b.hitches or 0
      p(string.format('- median frame %.1f ms, 95th %.1f ms, 99th %.1f ms; %d hitches over 50 ms',
        num(b.p50) or 0, num(b.p95) or 0, num(b.p99) or 0, hitches))
    end
  end
  local mod = type(data.mod) == 'table' and data.mod or {}
  if k.id == 'bug' or k.id == 'perf' then
    if str(mod.status) then p('**Mod status:** ' .. str(mod.status)) end
    if str(mod.lastError) then p('**Last connection error:** ' .. str(mod.lastError)) end
    if fpsLimit(data.fpsLimit) then p('**FPS limited by:** ' .. fpsLimit(data.fpsLimit)) end
  end
  p('')
  p('### Screenshot')
  p('_Attach the screenshot from this report folder: drag `screenshot.jpg` into this box._')
  -- the folder inside the BeamNG user folder only: a full path would put the player's account name in a public issue
  if str(data.folder) then
    local f = str(data.folder):gsub('\\', '/')
    local rel = f:match('([Bb]eam[Nn][Gg][Ee]arth/reports/.*)$') or f:match('([^/]+/?)$') or f
    p('Folder: `<BeamNG user folder>/' .. rel .. '`')
  end
  p('')
  p('<details><summary>Hardware / settings</summary>')
  p('')
  for _, l in ipairs(M.formatHardware(hw)) do p(l) end
  p('')
  p('</details>')
  p('')
  return table.concat(B, '\n')
end

-- Split issue.md back into its title (line 1 without '# ') and body.
function M.splitIssue(text)
  text = tostring(text or '')
  local first_, rest = text:match('^([^\n]*)\n?(.*)$')
  return (first_:gsub('^#+%s*', '')), (rest:gsub('^\n+', ''))
end

function M.urlEncode(s)
  return (tostring(s):gsub('[^%w%-_%.~]', function(c) return string.format('%%%02X', c:byte()) end))
end

M.TRUNCATED = '\n\n_(Truncated: paste the rest from issue.md in the report folder.)_'

-- The GitHub new-issue URL with title, body and label, the body cut (at a UTF-8 character boundary)
-- so the whole URL stays under maxLen.
function M.issueUrl(title, body, label, maxLen, repo)
  maxLen = maxLen or M.URL_MAX
  local base = 'https://github.com/' .. (repo or M.REPO) .. '/issues/new?title=' .. M.urlEncode(title)
    .. (label and ('&labels=' .. M.urlEncode(label)) or '') .. '&body='
  local enc = M.urlEncode(body)
  if #base + #enc <= maxLen then return base .. enc, false end
  local room = maxLen - #base - #M.urlEncode(M.TRUNCATED)
  local lo, hi = 0, #body
  while lo < hi do         -- the longest prefix whose encoding fits
    local mid = math.floor((lo + hi + 1) / 2)
    if #M.urlEncode(body:sub(1, mid)) <= room then lo = mid else hi = mid - 1 end
  end
  local cut = lo
  while cut > 0 and cut < #body and body:byte(cut + 1) >= 128 and body:byte(cut + 1) < 192 do cut = cut - 1 end
  return base .. M.urlEncode(body:sub(1, cut) .. M.TRUNCATED), true
end

return M
