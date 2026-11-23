-- luajit mod/tests/test_seasondate.lua  (seasondate.lua: the seasons' date from the PC's clock or the game's
-- environment, the settings page's Season date; the server told the game's week)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local S = require('beamngEarth/seasondate')

T.check(S.dayOfYear(2026, 1, 1) == 1 and S.dayOfYear(2026, 12, 31) == 365, 'a common year')
T.check(S.dayOfYear(2024, 3, 1) == 61 and S.dayOfYear(2024, 12, 31) == 366, 'a leap year')
T.check(S.dayOfYear(2026, 13, 1) == nil and S.dayOfYear(nil, 1, 1) == nil, 'a bad date')

-- the game's date (core_environment.getTimeOfDay: year, month, day)
local saved = rawget(_G, 'core_environment')
core_environment = { getTimeOfDay = function() return { year = 2026, month = 8, day = 15, time = 0.3 } end }
S.setSource('clock')
T.check(S.doy() == tonumber(os.date('*t').yday), 'clock: the PC date')
T.check(S.serverDay() == nil, 'clock: the server keeps its own date')
S.setSource('game')
T.check(S.doy() == 227, 'game: 15 August')
T.check(S.serverDay() == 225, 'game: the week of it for the server (days 225-231)')
-- no environment yet: the PC's date
core_environment = nil
T.check(S.doy() == tonumber(os.date('*t').yday), 'game without an environment: the PC date')
-- sync: sent once, again only on a change or after a hello
core_environment = { getTimeOfDay = function() return { year = 2026, month = 8, day = 15 } end }
local sent = {}
local net = { isConnected = function() return true end, send = function(m) sent[#sent + 1] = m end }
S.resetSent()
T.check(S.sync(net) and sent[1].type == 'season_date' and sent[1].doy == 225, 'sent the week')
T.check(not S.sync(net), 'not again unchanged')
S.setSource('clock')
T.check(S.sync(net) and sent[2].doy == nil, 'back to the clock: sent once (no day)')
T.check(not S.sync(net), 'not again')
S.resetSent()
T.check(S.sync(net), 'after a hello: sent again')
core_environment = saved
S.setSource('clock')

T.done('test_seasondate')
