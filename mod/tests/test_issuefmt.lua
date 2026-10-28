-- luajit mod/tests/test_issuefmt.lua  (issuefmt.lua: report kinds, hardware summary, issue.md, URL)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local fmt = require('beamngEarth/issuefmt')

-- kinds, legacy kinds
T.check(fmt.kind('map').label == 'map' and fmt.kind('idea').label == 'enhancement', 'kind labels')
T.check(fmt.kind('bench').id == 'perf', 'old benchmark reports read as performance')
T.check(fmt.kind('note').name == 'Note', 'old notes keep their name')
T.check(fmt.kindIndex('bug') == 3, 'kind index')

-- hardware: a full table
local hw = {
  os = { fullname = 'Windows 11 Pro', bits = 64 },
  cpu = { name = 'AMD Ryzen 7 5800X', coresPhysical = 8, coresLogical = 16 },
  mem = { osPhysAvailable = 32 * 1073741824 },
  gpu = { name = 'NVIDIA GeForce RTX 3070', memoryMB = 8192, driverVersion = '560.94' },
  graphicsApi = 'Vulkan',
  videoMode = { width = 2560, height = 1440, refreshRate = 144, displayMode = 'Fullscreen' },
  game = { versiond = '0.38.3.0', buildnumber = 12345, arch = 'x64' },
  settings = { GraphicOverallQuality = 'High', GraphicMeshQuality = 'Ultra', GraphicAnisotropic = 16,
    vsync = false, PostFXDOFGeneralEnabled = true },
  modVersion = '0.4.0',
  server = { type = 'hello', serverVersion = '0.9.1', horizon = true, season = 'auto', sessionId = 'x',
    attribution = 'long text' },
}
local L = table.concat(fmt.formatHardware(hw), '\n')
T.check(L:find('Windows 11 Pro (64-bit)', 1, true), 'OS line: ' .. L)
T.check(L:find('AMD Ryzen 7 5800X (8 cores / 16 threads)', 1, true), 'CPU line')
T.check(L:find('**RAM:** 32 GB', 1, true), 'RAM line')
T.check(L:find('8192 MB VRAM, driverVersion 560.94', 1, true), 'GPU line with VRAM and driver')
local unknownGpu = table.concat(fmt.formatHardware({ gpu = { name = 'NVIDIA GPU', memoryMB = 0 } }), '\n')
T.check(unknownGpu:find('VRAM capacity unavailable', 1, true), 'zero total VRAM is identified as unavailable')
T.check(not unknownGpu:find('0 MB VRAM', 1, true), 'unknown capacity is not reported as zero MB')
local missingGpuCapacity = table.concat(fmt.formatHardware({ gpu = { name = 'NVIDIA GPU' } }), '\n')
T.check(missingGpuCapacity:find('VRAM capacity unavailable', 1, true), 'missing total VRAM is identified as unavailable')
T.check(L:find('2560x1440 @ 144 Hz, Fullscreen', 1, true), 'display line')
T.check(L:find('Vulkan', 1, true), 'graphics API')
T.check(L:find('0.38.3.0 (build 12345), x64', 1, true), 'game version')
T.check(L:find('Preset High, Mesh Ultra, Anisotropic 16, Vsync off, DOF on', 1, true), 'settings line')
T.check(L:find('0.9.1 (horizon=true, season=auto)', 1, true), 'server line without session / attribution')
-- hardware: nothing known (every engine call missing), or an error string
local E = fmt.formatHardware({ cpu = { name = 'error: x' } })
T.check(#E == 1 and E[1]:find('not connected'), 'empty hardware: only the server line (' .. #E .. ')')
T.check(#fmt.formatHardware(nil) == 1, 'nil hardware')

-- issue.md
local data = { kind = 'map', title = 'Bridge missing', note = 'The bridge over the river is gone.',
  lat = 39.283, lon = -76.593, time = '2026-10-14 12:00:00', folder = '/home/u/beamngearth/reports/x/',
  vehicle = { name = 'pickup', speedMph = 31.2, headingDeg = 90 },
  recentFrames = { frames = 600, fps = 60, p99 = 25, maxMs = 80, over50 = 2 } }
local md = fmt.buildIssue(data, hw)
local title, body = fmt.splitIssue(md)
T.check(title == '[Map issue] Bridge missing', 'title line: ' .. title)
T.check(body:find('**Kind:** Map issue', 1, true), 'kind in body')
T.check(body:find('The bridge over the river is gone.', 1, true), 'note in body')
T.check(body:find('https://www.openstreetmap.org/?mlat=39.283000&mlon=-76.593000#map=17/39.283000/-76.593000', 1, true), 'OSM link')
T.check(body:find('60.0 fps avg, 1% low 40.0 fps, worst frame 80 ms', 1, true), 'perf summary')
T.check(body:find('<details><summary>Hardware / settings</summary>', 1, true) and body:find('</details>', 1, true), 'details')
T.check(body:find('screenshot.jpg', 1, true), 'screenshot hint')
T.check(not body:find('Performance profiler', 1, true), 'no profiler line without a profile')
-- a performance report with the profiler, no title: title from the note's first line
local pd = { kind = 'bench', note = 'stutters downtown\nmore text', time = 't',
  bench = { frames = 1800, fps = 45, p50 = 20, p95 = 30, p99 = 50, maxMs = 210, over50 = 5, seconds = 30,
    hitches = { {}, {}, {} } }, mod = { status = 'World server connected' } }
local pmd = fmt.buildIssue(pd, {})
T.check(pmd:match('^# %[Performance issue%] stutters downtown\n'), 'legacy bench -> performance title')
T.check(pmd:find('**Performance profiler (30 s):** 45.0 fps avg, 1% low 20.0 fps, worst frame 210 ms', 1, true), 'profiler line')
T.check(pmd:find('3 hitches over 50 ms', 1, true), 'hitch count')
T.check(pmd:find('**Mod status:** World server connected', 1, true), 'mod status for perf')
T.check(pmd:find('Location:** unknown', 1, true), 'no location')
-- 0.40's FPS-limit reason is a table, not a string (GitHub issue #38 showed "table: 0x...")
local lim = { kind = 'bug', fpsLimit = { backgroundCap = 30, backgroundActive = false, fpsCap = 0, windowFocused = true,
  vr = false, foregroundCap = 60, foregroundActive = true, vsync = true } }
local lmd = fmt.buildIssue(lim, {})
T.check(lmd:find('**FPS limited by:** vsync, foreground cap 60', 1, true), 'FPS limit table spelled out')
T.check(not lmd:find('table: ', 1, true), 'no table address')
local none = fmt.buildIssue({ kind = 'bug', fpsLimit = { fpsCap = 0, vsync = false } }, {})
T.check(none:find('**FPS limited by:** nothing', 1, true), 'no cap active')
T.check(fmt.buildIssue({ kind = 'bug', fpsLimit = 'vsync' }, {}):find('**FPS limited by:** vsync', 1, true), 'old string reason')
-- empty everything
local emd = fmt.buildIssue({}, nil)
T.check(emd:match('^# %[[^%]]+%] %(short title%)'), 'empty report still makes an issue')
-- long title is cut
T.check(#fmt.title({ kind = 'idea', title = string.rep('a', 200) }) < 110, 'long title cut')

-- URL encoding and truncation
T.check(fmt.urlEncode('a b&c=d/é') == 'a%20b%26c%3Dd%2F%C3%A9', 'urlEncode: ' .. fmt.urlEncode('a b&c=d/é'))
local url, cut = fmt.issueUrl('t', 'short', 'map')
T.check(not cut and url == 'https://github.com/TecnaGamer/BeamNG.Earth/issues/new?title=t&labels=map&body=short', 'short url')
local long = string.rep('é line\n', 3000)
url, cut = fmt.issueUrl('A title', long, 'bug', 7000)
T.check(cut and #url <= 7000 and #url > 6500, 'long body truncated under the limit (' .. #url .. ')')
T.check(url:find(fmt.urlEncode('Truncated'), 1, true), 'truncation note')
T.check(not url:find('%%C3%%%x%x?$') and not url:find('%%C3' .. fmt.urlEncode(fmt.TRUNCATED), 1, true), 'cut at a character boundary')

do
  local F = require('beamngEarth/issuefmt')
  local fn = F.buildIssue
  if fn then
    local ok, md = pcall(fn, { kind = 'map', title = 't', folder = '/home/bob/.local/share/BeamNG/BeamNG.drive/current/beamngEarth/reports/x_1/' })
    T.check(ok and type(md) == 'string' and not md:find('bob', 1, true) and md:find('<BeamNG user folder>/beamngEarth/reports/x_1/', 1, true) ~= nil,
      'the folder line has no account name')
  end
end
T.done('test_issuefmt')
