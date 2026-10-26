-- luajit mod/tests/test_dynweather.lua  (the dynamic week: forecast, periods, messages, days passing, modes)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
jsonWriteFile = function() end
jsonReadFile = function() return nil end
local D = require('beamngEarth/dynweather')

local w = D.forecast(42, 12, 0)
T.check(#w == 7, 'seven days')
local same = D.forecast(42, 12, 0)
T.check(w[3].cond == same[3].cond and w[3].hi == same[3].hi, 'deterministic')
local kinds, differs = {}, false
for s = 1, 40 do
  local f = D.forecast(s, 12, 0)
  for _, d in ipairs(f) do kinds[d.cond] = true; if d.cond ~= f[1].cond then differs = true end end
end
T.check(kinds.clear and kinds.rain and kinds.cloudy, 'a mix of weather over many weeks')
T.check(differs, 'it varies through the week')
for _, d in ipairs(w) do
  T.check(d.hi >= d.lo and #d.periods == 4, 'day ' .. d.day .. ': four periods, hi >= lo')
end
local cold = D.forecast(7, -6, 0)
local snow = false
for _, d in ipairs(cold) do for _, p in ipairs(d.periods) do
  if p.cond == 'snow' then snow = true end
  T.check(not (p.cond == 'rain' and p.tempC <= 1), 'no rain below freezing')
end end
local warm = D.forecast(7, 25, 0)
for _, d in ipairs(warm) do for _, p in ipairs(d.periods) do T.check(p.cond ~= 'snow', 'no snow when warm') end end
T.check(D.periodOf(0) == 1 and D.periodOf(8 * 60) == 2 and D.periodOf(14 * 60) == 3 and D.periodOf(20 * 60) == 4, 'periods')
local m = D.message({ cond = 'storm', tempC = 10 }, 40, -80)
T.check(m.rainMmH > 5 and m.cover.low == 100 and m.slTempC == 10 and m.snowCmH == 0, 'a storm message')
m = D.message({ cond = 'snow', tempC = -3 }, 40, -80)
T.check(m.snowCmH > 0 and m.rainMmH == 0, 'a snow message')

-- the drive: real weather kept as the climate in dynamic mode, applied in real mode; days pass at midnight
local applied = {}
beamngEarth_weather = { onWeather = function(msg) applied[#applied + 1] = msg end }
D._reset()
T.check(D.mode() == 'dynamic', 'dynamic by default')
T.check(D.onRealWeather({ slTempC = -8, lat = 40, lon = -80 }) == false, 'dynamic: the real feed does not drive')
D.update(D.APPLY_S, 10 * 60, 40, -80)
T.check(#applied == 1 and applied[1].dynamic, 'a dynamic message applied')
D.update(1, 23 * 60 + 50)
D.update(1, 5)
T.check(D._state().day == 1, 'past midnight: the next day')
D.setMode('real')
T.check(applied[#applied].slTempC == -8 and not applied[#applied].dynamic, 'real mode: the real weather back at once')
T.check(D.onRealWeather({ slTempC = 3 }) == true, 'real: the feed drives')
local n = #applied
D.update(D.APPLY_S, 600)
T.check(#applied == n, 'real mode: no dynamic messages')
T.done('test_dynweather')
