-- luajit mod/tests/test_phone.lua  (the phone's one poll, its settings, the key, the wallpapers, the music request)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local written, triggered, sent = {}, {}, {}
jsonWriteFile = function(p, t) written[p] = t end
jsonReadFile = function() return nil end
guihooks = { trigger = function(n, d) triggered[#triggered + 1] = { n, d } end }
FS = { findFiles = function(_, dir, pat)
  if dir == '/screenshots/' and pat == '*.png' then return { '/screenshots/screenshot_2026-10-25_1.png', '/screenshots/screenshot_2026-10-26_1.png' } end
  return {} end }
core_environment = { getTimeOfDay = function() return { time = 0.1 } end }       -- 14:24
beamngEarth_core = { _state = function() return { player = { lat = 40, lon = -80 } } end,
  requestMedia = function(a) sent[#sent + 1] = a or 'state' return true end,
  mediaState = function() return { ok = true, playing = true, artist = 'M83', title = 'Midnight City' } end }
local ok, S = pcall(require, 'beamngEarth/settings')
T.check(ok, 'settings loads: ' .. tostring(not ok and S or ''))
if not ok then T.done('test_phone') return end
local p = S.uiPhone()
T.check(p.time == '2:24 PM', 'game time on the phone: ' .. tostring(p.time))
T.check(p.up == false, 'down at first')
T.check(p.bank and p.bank.text == '$0.00', 'bank')
T.check(p.weather and #p.weather.days == 7 and p.weather.now, 'the week')
T.check(p.jobs and p.jobs.offers, 'jobs')
T.check(p.media.title == 'Midnight City' and sent[1] == 'state', 'music asked for and shown')
T.check(#p.wallpapers == 2 and p.wallpapers[1]:find('10%-26'), 'wallpapers, newest first')
S.togglePhone()
T.check(triggered[#triggered][1] == 'BneEarthPhone' and triggered[#triggered][2].up == true, 'the key brings it up')
T.check(S.uiPhone().up == true, 'and the poll says so')
S.uiPhoneSet('size', 5)
S.uiPhoneSet('theme', 'light')
local saved = written['/settings/beamngEarth_phone.json']
T.check(saved and saved.size == 1.3 and saved.theme == 'light', 'settings saved, size clamped')
S.uiPhoneMedia('next')
T.check(sent[#sent] == 'next', 'music: next')
S.uiPhoneWeatherMode('real')
T.check(S.uiPhone().weather.mode == 'real', 'weather mode switched')
T.done('test_phone')
