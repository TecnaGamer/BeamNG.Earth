-- Dynamic weather (the user, 2026-10-26: "dynamic weather should run in the background and the weather app tells you
-- the weekly forecast, it varies through the week or as time goes on"). A week of weather, day by day and through each
-- day, made from a seed and the real climate where you are (the server's real weather: its temperature is the day's
-- base, and below freezing precipitation falls as snow). Each game day has four periods (night, morning, afternoon,
-- evening) with a condition and a temperature; it drives the weather the way the real feed does: a 'weather' message
-- of the same shape handed to beamngEarth_weather (which eases every value in), so the sky, rain, snow, fog, wind and
-- the roads (roadweather.lua) follow. Days pass with the game clock; after the 7th a new week is drawn.
-- Mode: 'dynamic' (the default) or 'real' (the server's real weather as before); the phone's settings switch it.
-- forecast() and conditionAt() are pure given the seed (mod/tests/test_dynweather.lua).
local M = {}

M.FILE = '/settings/beamngEarth_dynweather.json'
M.DAYS = 7
M.APPLY_S = 20              -- the drive's message this often (beamngEarth_weather eases in between)
M.CONDITIONS = {            -- name -> message fields (cover %, rain mm/h, snow cm/h, visibility m, wind m/s)
  clear = { cover = 5, rain = 0, vis = 30000, wind = 2 },
  partly = { cover = 35, rain = 0, vis = 25000, wind = 3 },
  cloudy = { cover = 85, rain = 0, vis = 20000, wind = 4 },
  fog = { cover = 70, rain = 0, vis = 400, wind = 1, fog = true },
  rain = { cover = 95, rain = 2.5, vis = 8000, wind = 5 },
  storm = { cover = 100, rain = 9, vis = 3000, wind = 11 },
}
M.LABELS = { clear = 'Clear', partly = 'Partly cloudy', cloudy = 'Cloudy', fog = 'Fog', rain = 'Rain', storm = 'Storm',
             snow = 'Snow' }
-- weather fronts: a day's base pick, weighted (persistence makes the next day likely the same kind)
M.WEIGHTS = { { 'clear', 30 }, { 'partly', 28 }, { 'cloudy', 18 }, { 'rain', 14 }, { 'fog', 4 }, { 'storm', 6 } }
M.PERIODS = { 'Night', 'Morning', 'Afternoon', 'Evening' }
M.SWING = { -4, -1, 4, 1 }
-- (the user, 2026-10-26: "allow the weather to switch up a little, don't follow irl so close")
M.PERSIST = 0.25            -- the chance a day keeps the day before's weather
M.TEMP_SPREAD = 14          -- °C: a day's temperature strays up to half this from the real one  -- °C against the day's base, per period

local state = nil           -- { mode, seed, day (0..), lastMins }
local baseTempC = 12        -- the real temperature where you are (the server's message), °C
local lastReal = nil
local applyT = M.APPLY_S

-- Pure: a deterministic number in [0, 1) for a seed and a few integers.
local function rnd(seed, a, b)
  local x = (seed * 374761393 + (a or 0) * 668265263 + (b or 0) * 2147483647) % 4294967296
  x = math.sin(x) * 43758.5453
  return x - math.floor(x)
end

local function pick(u)
  local total = 0
  for _, w in ipairs(M.WEIGHTS) do total = total + w[2] end
  local acc = 0
  for _, w in ipairs(M.WEIGHTS) do
    acc = acc + w[2] / total
    if u < acc then return w[1] end
  end
  return M.WEIGHTS[#M.WEIGHTS][1]
end

-- Pure: the week for seed around base °C: { { label, hi, lo, cond, periods = { {name, cond, tempC}... } }... }.
function M.forecast(seed, base, firstDay)
  firstDay = firstDay or 0
  local out, prev = {}, nil
  for d = 0, M.DAYS - 1 do
    local day = firstDay + d
    local cond = pick(rnd(seed, day, 1))
    if prev and rnd(seed, day, 2) < M.PERSIST then cond = prev end     -- fronts last a day or two
    prev = cond
    -- the real temperature only as the season's middle: each day strays up to TEMP_SPREAD / 2 from it
    local dayBase = (base or 12) + (rnd(seed, day, 3) - 0.5) * M.TEMP_SPREAD
    local periods, hi, lo = {}, -99, 99
    for p = 1, 4 do
      local c = cond
      local u = rnd(seed, day, 10 + p)
      if c == 'rain' or c == 'storm' then
        if u < 0.35 then c = 'cloudy' elseif u < 0.45 then c = 'partly' end      -- showers come and go
      elseif c == 'fog' then
        if p >= 3 then c = 'partly' end                    -- fog lifts by the afternoon
      elseif c == 'partly' then
        if u < 0.2 then c = 'rain' elseif u < 0.3 then c = 'cloudy' elseif u > 0.85 then c = 'clear' end
      elseif c == 'clear' then
        if u < 0.25 then c = 'partly' elseif u > 0.95 then c = 'storm' end      -- a pop-up storm now and then
      elseif c == 'cloudy' then
        if u < 0.25 then c = 'rain' elseif u > 0.75 then c = 'partly' end
      end
      local t = dayBase + M.SWING[p] + (u - 0.5) * 2
      if (c == 'rain' or c == 'storm') and t <= 1 then c = 'snow' end
      t = math.floor(t * 10 + 0.5) / 10
      hi, lo = math.max(hi, t), math.min(lo, t)
      periods[p] = { name = M.PERIODS[p], cond = c, label = M.LABELS[c], tempC = t }
    end
    local main = periods[3].cond
    for _, pp in ipairs(periods) do if pp.cond == 'snow' or pp.cond == 'storm' then main = pp.cond end end
    out[#out + 1] = { day = day, cond = main, label = M.LABELS[main], hi = hi, lo = lo, periods = periods }
  end
  return out
end

-- Pure: the period (1..4) of minutes after midnight.
function M.periodOf(mins)
  mins = (mins or 0) % 1440
  if mins < 360 then return 1 elseif mins < 720 then return 2 elseif mins < 1080 then return 3 end
  return 4
end

-- Pure: a 'weather' message (server/weather.py's shape) for a period { cond, tempC }.
function M.message(period, lat, lon)
  local c = period.cond == 'snow' and M.CONDITIONS.rain or (M.CONDITIONS[period.cond] or M.CONDITIONS.partly)
  local snow = period.cond == 'snow'
  return {
    type = 'weather', dynamic = true, lat = lat, lon = lon,
    cover = { total = c.cover, low = c.cover, mid = c.cover * 0.7, high = math.min(100, c.cover * 0.5) },
    wind = { ms = c.wind, fromDeg = 250, gustMs = c.wind * 1.6 }, aloft = { ms = c.wind * 2.2, fromDeg = 260 },
    visM = c.vis, fog = c.fog or false, baseAglM = c.cover > 80 and 700 or 1600,
    rainMmH = snow and 0 or c.rain, snowCmH = snow and 1.5 or 0, slTempC = period.tempC,
  }
end

local function load()
  if state then return state end
  local read = rawget(_G, 'jsonReadFile')
  local ok, got = false, nil
  if read then ok, got = pcall(read, M.FILE) end
  if ok and type(got) == 'table' and tonumber(got.seed) then
    state = got
  else
    state = { mode = 'dynamic', seed = (os.time and os.time() or 1) % 100000, day = 0 }
  end
  return state
end

local function save()
  local write = rawget(_G, 'jsonWriteFile')
  if write and state then pcall(write, M.FILE, state, true) end
end

function M.mode() return load().mode end
function M.setMode(mode)
  local s = load()
  s.mode = mode == 'real' and 'real' or 'dynamic'
  save()
  applyT = M.APPLY_S
  if s.mode == 'real' and lastReal then
    local w = rawget(_G, 'beamngEarth_weather')
    if w and w.onWeather then pcall(w.onWeather, lastReal) end
  end
end

-- core: the server's real weather. Returns true when it should drive the weather (real mode), false when dynamic
-- weather keeps it only as the climate.
function M.onRealWeather(msg)
  lastReal = msg
  if type(msg) == 'table' and tonumber(msg.slTempC) then baseTempC = tonumber(msg.slTempC) end
  return load().mode == 'real'
end

-- For the phone: { mode, today, period, days = forecast }.
function M.uiState(mins)
  local s = load()
  local week = M.forecast(s.seed, baseTempC, s.day)
  return { mode = s.mode, days = week, period = M.periodOf(mins), now = week[1] and week[1].periods[M.periodOf(mins)] }
end

-- Every frame (core), with the game clock (minutes after midnight) and the player's lat / lon.
function M.update(dt, mins, lat, lon)
  local s = load()
  if mins then
    if s.lastMins and mins + 720 < s.lastMins then           -- past midnight: the next day
      s.day = (s.day or 0) + 1
      if s.day % M.DAYS == 0 then s.seed = (s.seed * 1103515245 + 12345) % 100000 end
      save()
    end
    s.lastMins = mins
  end
  if s.mode ~= 'dynamic' then return end
  applyT = applyT + (dt or 0)
  if applyT < M.APPLY_S then return end
  applyT = 0
  local week = M.forecast(s.seed, baseTempC, s.day)
  local p = week[1].periods[M.periodOf(mins)]
  local w = rawget(_G, 'beamngEarth_weather')
  if w and w.onWeather then pcall(w.onWeather, M.message(p, lat or (lastReal and lastReal.lat), lon or (lastReal and lastReal.lon))) end
end

function M._reset() state, baseTempC, lastReal, applyT = nil, 12, nil, M.APPLY_S end   -- tests
function M._state() return load() end

return M
