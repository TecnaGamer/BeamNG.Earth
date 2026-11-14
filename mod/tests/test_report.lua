-- luajit mod/tests/test_report.lua  (report.lua: hardware collection and a saved report's files)
-- Engine globals are stubbed here; which ones the real game has is checked in-game.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')

local files = {}
function log() end
function getCurrentLevelIdentifier() return 'beamng_earth' end
function writeFile(p, s) files[p] = s; return true end
function readFile(p) return files[p] end
local function enc(v)
  local t = type(v)
  if t == 'table' then
    local parts = {}
    for k, x in pairs(v) do parts[#parts + 1] = string.format('%q:%s', tostring(k), enc(x)) end
    table.sort(parts)
    return '{' .. table.concat(parts, ',') .. '}'
  elseif t == 'string' then return string.format('%q', v) end
  return tostring(v)
end
jsonEncode = enc
FS = { directoryExists = function() return false end, directoryCreate = function() end,
  getUserPath = function() return '/u/' end }

-- 1) no engine at all: every hardware field missing, no error
Engine = {}
local report = require('beamngEarth/report')
-- other levels: the hotkeys do nothing (the user, 2026-10-20: the mod effectively off there)
getCurrentLevelIdentifier = function() return 'gridmap_v2' end
report.onClientStartMission()
report.note()
T.check(not report.inOurLevel() and not report.status().formOpen, 'Ctrl+Shift+E does nothing on another level')
getCurrentLevelIdentifier = function() return 'beamng_earth' end
report.onClientStartMission()
T.check(report.inOurLevel(), 'our level')
local ok, hw = pcall(report.collectHardware)
T.check(ok, 'collectHardware without the engine APIs: ' .. tostring(hw))
T.check(ok and hw.os == nil and hw.gpu == nil and hw.graphicsApi == nil, 'missing APIs leave fields out')

-- 2) the engine APIs (field names as core/hardwareinfo.lua uses them), one of them throwing
Engine = {
  Platform = {
    getOSInfo = function() return { fullname = 'Linux', type = 'linux', bits = 64 } end,
    getCPUInfo = function() return { name = 'Test CPU', coresPhysical = 6, nested = { x = 1 } } end,
    getGPUInfo = function() error('no gpu api') end,
    getMemoryInfo = function() return { osPhysAvailable = 16 * 1073741824, osPhysUsed = 1 } end,
    getVideoDriver = function() return 'x11' end,
  },
  Render = { getAdapterType = function() return 'Vulkan' end },
}
GFXDevice = { getVideoMode = function() return { width = 1920, height = 1080, refreshRate = 60 } end }
beamng_versiond = '0.38.0.0'
settings = { getValue = function(k) if k == 'vsync' then return true end; if k == 'GraphicMeshQuality' then return 'High' end end }
beamngEarth_core = { MOD_VERSION = '0.4.0', serverHello = function() return { serverVersion = '1.0', horizon = true } end }
hw = report.collectHardware()
T.check(hw.os.fullname == 'Linux' and hw.os.windowsystem == 'x11', 'OS + window system')
T.check(hw.cpu.coresPhysical == 6 and hw.cpu.nested == nil, 'CPU scalars only')
T.check(hw.gpu == nil, 'a throwing API is left out')
T.check(hw.graphicsApi == 'Vulkan' and hw.videoMode.width == 1920, 'API and video mode')
T.check(hw.settings.vsync == true and hw.settings.GraphicMeshQuality == 'High', 'settings')
T.check(hw.game.versiond == '0.38.0.0' and hw.modVersion == '0.4.0' and hw.server.serverVersion == '1.0', 'versions')

-- 3) a saved report: report.json, hardware.json, issue.md, one index line; a re-save adds no line
beamngEarth_core.status = function() return '{ player = { lat = 39.28, lon = -76.59 }, anchorId = 3 }' end
report.note()
T.check(report.status().formOpen, 'Ctrl+Shift+E opens the report window')
report.saveNote('Trees in the river', 'map', 'Trees in the water')
local folder
for p in pairs(files) do if p:match('issue%.md$') then folder = p:match('^(.*/)issue%.md$') end end
T.check(folder and files[folder .. 'report.json'] and files[folder .. 'hardware.json'], 'the three files')
local md = folder and files[folder .. 'issue.md'] or ''
T.check(md:match('^# %[Map issue%] Trees in the water\n'), 'issue title: ' .. md:sub(1, 60))
T.check(md:find('mlat=39.280000', 1, true) and md:find('Test CPU', 1, true), 'location and hardware in issue.md')
local idx = files['beamngEarth/reports/index.jsonl'] or ''
local _, lines = idx:gsub('\n', '')
T.check(lines == 1, 'one index line (' .. lines .. ')')
T.check(report.status().lastFolder == '/u/' .. folder, 'the saved window names the folder')
T.check(report.benchmark == report.profiler, 'the old benchmark entry point stays')


-- the car held while the world loads, apart from slow frames (2026-11-11)
local hs = report.stats({ 16, 16, 120, 300, 16 }, { false, false, false, 'edge', 'edge' })
T.check(hs.held and hs.held.frames == 2 and math.abs(hs.held.s - 0.316) < 1e-9 and hs.held.over50 == 1
  and math.abs(hs.held.why.edge - 0.316) < 1e-9, 'held frames summed apart with their reason')
T.check(hs.playing.frames == 3 and hs.playing.over50 == 1 and hs.over50 == 2, 'frames while playing apart; all as before')
T.check(report.stats({ 16, 16 }, { false, false }).held == nil, 'never held: no held part')

-- The saved-report button uses the same companion/browser route as the settings links (GitHub #1109).
local pages, browserPages = {}, {}
beamngEarth_core.openPage = function(url) pages[#pages + 1] = url end
openWebBrowser = function(url) browserPages[#browserPages + 1] = url end
ui_imgui = {
  ImVec2 = function() return {} end,
  SetNextWindowPos = function() end, SetNextWindowSize = function() end,
  Begin = function(name) return name:find('report saved', 1, true) ~= nil end,
  End = function() end, TextUnformatted = function() end,
  PushTextWrapPos = function() end, PopTextWrapPos = function() end, SameLine = function() end,
  Button = function(name) return name == 'Open GitHub issue page' end,
  Cond_Always = 0, Cond_FirstUseEver = 0, WindowFlags_NoDecoration = 0,
  WindowFlags_AlwaysAutoResize = 0, WindowFlags_NoInputs = 0,
}
report.onUpdate()
T.check(#pages == 1 and pages[1]:find('https://github.com/TecnaGamer/BeamNG.Earth/issues/new?', 1, true) == 1,
  'the saved issue URL goes through core.openPage')
T.check(#browserPages == 0, 'the report does not bypass the companion with the game browser')
beamngEarth_core = nil
report.onUpdate()
T.check(#browserPages == 1 and browserPages[1] == pages[1], 'without core, the game browser is the fallback')

-- 2026-11-12: no full path in report.json (the account name), the extra parts there, and the send window: nothing goes
-- before its Send; the choices go to send.json, the server's report_status shows the id
local rj = folder and files[folder .. 'report.json'] or ''
T.check(not rj:find('/u/', 1, true), 'report.json holds no full path')
T.check(rj:find('"frames60"', 1, true) and rj:find('"environment"', 1, true) and rj:find('"config"', 1, true),
  'the extra parts in report.json')
local netSent = {}
package.loaded['beamngEarth/net'] = { send = function(m) netSent[#netSent + 1] = m; return true end }
FS.fileExists = function(_, p) return files[p] ~= nil end
FS.fileSize = function(_, p) return files[p] and #files[p] or -1 end
jsonDecode = function() return { kind = 'map', title = 'Trees in the water', lat = 39.28, lon = -76.59 } end
files[folder .. 'screenshot.jpg'] = '\255\216\255'
local clicks = {}
local im2 = ui_imgui

im2.Begin = function() return true end
im2.Button = function(name) return clicks[name] == true end
im2.BoolPtr = function(v) return { [0] = v } end
im2.Checkbox = function(label, p) if clicks[label] ~= nil then p[0] = clicks[label] end return false end
im2.Separator = function() end
im2.CollapsingHeader1 = function() return false end

report.openSend(folder)
report.onUpdate()
T.check(report.status().sendOpen and #netSent == 0 and not files[folder .. 'send.json'], 'nothing is sent before Send')
clicks['Exact location: 39.28000, -76.59000##bngeSendLoc'] = false
clicks['Send##bngeSendGo'] = true
report.onUpdate()
clicks = {}
local name = folder:match('([^/]+)/$')
T.check(#netSent == 1 and netSent[1].type == 'report_send' and netSent[1].folder == name, 'Send: report_send')
local sj = files[folder .. 'send.json'] or ''
T.check(sj:find('"exactLocation":false', 1, true) and sj:find('"screenshot":true', 1, true), 'the choices in send.json: ' .. sj)
report.onServerMessage({ type = 'report_status', folder = name, state = 'sent', id = 'ABCD-EFGH' })
local st = require('beamngEarth/reportsend').status(name)
T.check(st and st.state == 'sent' and st.id == 'ABCD-EFGH', 'the id from the server')

T.done('test_report')
