-- BeamNG.Earth: the one wet state of the ground (the puddle plan R1, ~/bnge-tests/puddles/PLAN_FINAL.md 1.5 and 1.8,
-- decisions D7, D13, D15, D17). Behind the setting "New puddle look" (weatherPuddleLook, default off); wetlook.lua reads
-- it for the wet look, the grip and the puddles while the setting is on, its own float model otherwise.
--
-- Three numbers, all integers, stepped on a 30 s tick of the UTC clock (os.time), never per frame:
--   w  the surface film, 0..W_MAX (per mille): rain wets it within minutes, it dries over ~45 min after (faster in sun);
--      the wet look's level and the grip's damp / wet come from it;
--   i  the rain rate lagged ~4 min (um/h): the gutter strips' flow (plan 1.8; drawn from R2 on);
--   h  the water standing in the road's dips (1/H_SCALE um): rain in, drains and evaporation (ET0) out, at most
--      H_CAP_UM; the puddles' fill steps come from it (deep pools first, the shallow ruts last).
-- Integers because the same rain must give the same puddles and the same grip on every machine (plan 1.5: Lua's
-- math.exp is the platform's libm, and frame dt differs): only adds, subtracts, integer multiplies and floor divisions
-- of small integers, exact in a double everywhere. A tick's inputs are the weather SHOWN (its target message, never the
-- lerped values): the liquid rain rate (um/h, 0 when the shown air is below freezing: snow and sleet's ice add nothing,
-- D17) and ET0 (um/h: the server's Open-Meteo et0_fao_evapotranspiration, else a table by the weather code).
--
-- Where a state starts (M.sync's seed, one per source; then the ticks carry it on):
--   real weather: the server's 24 h history (hist: hourly rain and ET0, sent with BNE_PUDDLES=v2), run tick by tick from
--     zero 24 h ago, so arriving after a wet morning finds the puddles (D7); again only after a move of RESEED_M (a
--     teleport), not while driving through cells. Without a history: the steady state of the rain now.
--   a preset (the player's or a BeamMP server's): its steady state (presets start soaked; D7): the film at the rain's
--     target, the gutters flowing, the dips full while the rain beats the drains, else empty. A steady state stays
--     steady, so every client of a server preset agrees whenever it joined.
--   'real-host' (BeamMP): the host's snapshot (tick, w, i, h) riding on the relayed weather message, caught up tick by
--     tick with that message's rain: the same integers as the host's own.
-- The shown puddle step moves at most one step a check toward the state's (a shower's edge does not pop every puddle
-- in view; plan 1.10), and is 0 under packed snow and ice (W1's road state) or below freezing (D17).
local M = {}

M.TICK_S = 30
M.MAX_CATCHUP = 2880        -- ticks run at most in one go (24 h); older gaps start from there
M.W_MAX = 1000
M.W_TAU_TICKS = 8           -- wetting: 1/8 of the gap to the target a tick (63 % in ~4 min: wetlook's WET_TAU_S 240)
M.W_DRY_PER_TICK = 11       -- drying: 1000 -> 0 in ~45 min (wetlook's DRY_S 2700) ...
M.W_DRY_ET0_DIV = 60        -- ... plus ET0 (um/h) / 60 a tick (a sunny 600 um/h: twice as fast)
M.RAIN_MIN_UMH = 20         -- 0.02 mm/h: it rains (wetlook's 0.02)
M.RAIN_SOAK_UMH = 2000      -- this soaks the surface fully (wetlook's RAIN_SOAK_MMH 2)
M.W_RAIN_BASE = 350         -- the film's target in rain: BASE + SPAN x rate / SOAK (wetlook's 0.35 + 0.65 x)
M.W_RAIN_SPAN = 650
M.I_LAG_TICKS = 8           -- the flow's lag (~4 min)
M.H_SCALE = 120             -- h's unit: 1/120 um, so a rate in um/h adds exactly that many units in a 30 s tick
M.H_SEEP_UMH = 500          -- the dips' loss to drains, cracks and the edges (overcast: 1 mm in under 2 h, 15 mm ~a day;
                            -- plan R1's acceptance), plus ...
M.H_EVAP_PCT = 100          -- ... open-water evaporation, this % of ET0
M.H_CAP_UM = 15000          -- the dips hold at most this (more runs off)
-- The fill steps: step k shows from H_STEPS_UM[k] um standing (exponentially spaced, 0.3 to 9 mm, x1.362 a step: an
-- hour of heavy rain, 10 mm/h, fills them; an hour of 1 mm/h shows the first; drizzle never). Literal integers, not a
-- pow(): the same everywhere.
M.H_STEPS_UM = { 300, 409, 557, 758, 1033, 1408, 1918, 2613, 3560, 4850, 6607, 9000 }
M.RESEED_M = 25000          -- a real history seeds again after a move this far (the server's REFETCH_M)
-- ET0 (um/h) when the weather has none (presets, an old server): by the weather code
M.ET0_CLEAR, M.ET0_CLOUDY, M.ET0_OVERCAST, M.ET0_WET = 250, 150, 80, 20
-- W1's road conditions under which no water is drawn (packed snow, ice: D17)
M.FROZEN = { packed = true, packed_icy = true, ice = true, black_ice = true, wet_ice = true, frost = true, dusting = true,
  loose = true }
M.FREEZE_C = 0              -- liquid rain only above this (the air shown)

local floor, max, min = math.floor, math.max, math.min

------------------------------------------------------------------------------------------
-- pure

-- (a + b - 1) // b for a >= 0, b > 0, small integers
local function ceilDiv(a, b) return floor((a + b - 1) / b) end

-- Pure: the tick of a UTC time (s).
function M.tickOf(unixS) return floor((tonumber(unixS) or 0) / M.TICK_S) end

-- Pure: a rate in mm/h as integer um/h (0..500000).
function M.umh(mmh)
  mmh = tonumber(mmh) or 0
  if mmh ~= mmh or mmh <= 0 then return 0 end
  return min(500000, floor(mmh * 1000 + 0.5))
end

-- Pure: the film's target in rain of r um/h (0 below RAIN_MIN_UMH).
function M.wTarget(r)
  if r < M.RAIN_MIN_UMH then return 0 end
  return min(M.W_MAX, M.W_RAIN_BASE + floor(M.W_RAIN_SPAN * min(r, M.RAIN_SOAK_UMH) / M.RAIN_SOAK_UMH))
end

-- Pure: the dips' loss per tick (h units) at ET0 e um/h.
function M.hLoss(e) return M.H_SEEP_UMH + floor(e * M.H_EVAP_PCT / 100) end

-- Pure: one tick of state s ({ t, w, i, h }, changed in place) under rain r and ET0 e (um/h, integers).
function M.step(s, r, e)
  if r >= M.RAIN_MIN_UMH then
    local tgt = M.wTarget(r)
    if s.w < tgt then s.w = s.w + ceilDiv(tgt - s.w, M.W_TAU_TICKS) end      -- (more rain than the film: it stays)
  else
    s.w = max(0, s.w - M.W_DRY_PER_TICK - floor(e / M.W_DRY_ET0_DIV))
  end
  local d = r - s.i
  if d > 0 then s.i = s.i + ceilDiv(d, M.I_LAG_TICKS) elseif d < 0 then s.i = s.i - ceilDiv(-d, M.I_LAG_TICKS) end
  s.h = min(M.H_CAP_UM * M.H_SCALE, max(0, s.h + r - M.hLoss(e)))
  s.t = s.t + 1
  return s
end

-- Pure: s run to tick t with rain r and ET0 e (at most MAX_CATCHUP ticks; an older state starts from there).
function M.advance(s, t, r, e)
  if t - s.t > M.MAX_CATCHUP then s.t = t - M.MAX_CATCHUP end
  while s.t < t do M.step(s, r, e) end
  return s
end

-- Pure: the steady state of rain r, ET0 e at tick t (a preset's start; real weather without a history).
function M.steady(t, r, e)
  return { t = t, w = M.wTarget(r), i = r, h = (r > M.hLoss(e)) and M.H_CAP_UM * M.H_SCALE or 0 }
end

-- Pure: the state at tick t from a history (hist = { t0 = UTC s of its first hour, rain = { um per hour .. },
-- et0 = { um per hour .. } }, integers) run from zero, then the hours since at rain r, ET0 e. nil for a bad history.
function M.fromHistory(hist, t, r, e)
  if type(hist) ~= 'table' or type(hist.rain) ~= 'table' or not tonumber(hist.t0) then return nil end
  local t0 = M.tickOf(hist.t0)
  local s = { t = t0, w = 0, i = 0, h = 0 }
  local per = 3600 / M.TICK_S
  for k = 1, #hist.rain do
    local hr = M.clampInt(hist.rain[k], 0, 500000)
    local he = M.clampInt(type(hist.et0) == 'table' and hist.et0[k] or 0, 0, 5000)
    local stop = min(t, t0 + k * per)
    while s.t < stop do M.step(s, hr, he) end
    if s.t >= t then return s end
  end
  return M.advance(s, t, r, e)
end

-- Pure: an integer in lo..hi (nil, NaN or a non-number: lo).
function M.clampInt(v, lo, hi)
  v = tonumber(v)
  if not v or v ~= v then return lo end
  return max(lo, min(hi, floor(v)))
end

-- Pure: a relayed snapshot ({ t, w, i, h }) as a state, every field an integer in range; nil when unusable.
function M.fromSnapshot(sn)
  if type(sn) ~= 'table' or not tonumber(sn.t) then return nil end
  return { t = M.clampInt(sn.t, 0, 2 ^ 40), w = M.clampInt(sn.w, 0, M.W_MAX), i = M.clampInt(sn.i, 0, 500000),
    h = M.clampInt(sn.h, 0, M.H_CAP_UM * M.H_SCALE) }
end

-- Pure: the fill step (0..#H_STEPS_UM) of h.
function M.fillStep(h)
  local n = 0
  for k = 1, #M.H_STEPS_UM do
    if h >= M.H_STEPS_UM[k] * M.H_SCALE then n = k else break end
  end
  return n
end

-- Pure: a step toward want from have, at most one.
function M.toward(have, want)
  if want > have then return have + 1 elseif want < have then return have - 1 end
  return have
end

-- Pure: ET0 (um/h) of a shown weather message: its own (et0Umh), else by its code.
function M.et0Of(msg)
  if type(msg) ~= 'table' then return M.ET0_OVERCAST end
  local e = tonumber(msg.et0Umh)
  if e and e == e then return M.clampInt(e, 0, 5000) end
  local code = tonumber(msg.code) or 3
  if code >= 45 then return M.ET0_WET end
  if code <= 1 then return M.ET0_CLEAR end
  if code == 2 then return M.ET0_CLOUDY end
  return M.ET0_OVERCAST
end

-- Pure: the liquid rain (um/h) of a shown message: its rain, none when the shown air is at or below FREEZE_C.
function M.rainOf(msg)
  if type(msg) ~= 'table' then return 0 end
  local tc = tonumber(msg.tempC) or tonumber(msg.slTempC)
  if tc and tc <= M.FREEZE_C then return 0 end
  return M.umh(msg.rainMmH)
end

-- Pure: is the water hidden (W1's road condition cond, the shown air tC)?
function M.frozen(cond, tC)
  if cond and M.FROZEN[cond] then return true end
  tC = tonumber(tC)
  return tC ~= nil and tC <= M.FREEZE_C - 1
end

-- Pure: the distance (m, a sphere) between two lat/lon points.
function M.distM(la1, lo1, la2, lo2)
  local r = math.pi / 180
  local x = (lo2 - lo1) * r * math.cos((la1 + la2) * r / 2)
  local y = (la2 - la1) * r
  return 6371000 * math.sqrt(x * x + y * y)
end

------------------------------------------------------------------------------------------
-- runtime

local st = nil            -- the state { t, w, i, h }
local seed = nil          -- what it was seeded from: { kind = 'real' | 'preset' | 'host' | 'steady', key, lat, lon, snapT }
local shown = 0           -- the puddle step shown
M.clock = function() return os.time() end     -- (tests)
M.seeds = 0

-- Every check (wetlook.lua, every CHECK_S while the setting is on): the shown weather msg (nil: none), how it is shown
-- (src: 'real' | 'preset' | 'host'; key: the preset's name and intensity), the real message's history (hist, nil)
-- and place (lat, lon), the host's snapshot (snap, nil). Seeds when the source changed, then ticks to now.
function M.sync(msg, src, key, hist, lat, lon, snap)
  local t = M.tickOf(M.clock())
  if not msg then return st end
  local r, e = M.rainOf(msg), M.et0Of(msg)
  local why = nil
  if src == 'host' and snap then
    local sn = M.fromSnapshot(snap)
    if sn and not (seed and seed.kind == 'host' and seed.snapT == sn.t) then
      st, seed, why = sn, { kind = 'host', snapT = sn.t }, 'host snapshot'
    end
  elseif src == 'real' or src == 'host' then
    local far = not (seed and seed.lat and lat) or M.distM(seed.lat, seed.lon, lat, lon) > M.RESEED_M
    if not seed or seed.kind ~= 'real' or (hist and (far or not seed.hist)) then
      local s = hist and M.fromHistory(hist, t, r, e) or nil
      st = s or M.steady(t, r, e)
      seed, why = { kind = 'real', lat = lat, lon = lon, hist = s ~= nil }, s and 'history' or 'steady (no history)'
    end
  else
    if not seed or seed.kind ~= 'preset' or seed.key ~= key then
      st, seed, why = M.steady(t, r, e), { kind = 'preset', key = key }, 'preset ' .. tostring(key)
    end
  end
  if why then M.seeds = M.seeds + 1; M.lastSeed = why end
  if st then M.advance(st, t, r, e) end
  return st
end

-- The puddle step to show now (one step a check toward the state's; 0 when frozen) and whether it changed.
function M.puddleStep(cond, tC)
  local want = (st and not M.frozen(cond, tC)) and M.fillStep(st.h) or 0
  local was = shown
  shown = M.toward(shown, want)
  return shown, shown ~= was
end

function M.wetness() return st and st.w / M.W_MAX or 0 end
function M.flowUmh() return st and st.i or 0 end
function M.standingUm() return st and floor(st.h / M.H_SCALE) or 0 end
function M.shownStep() return shown end
function M.state() return st end
-- the snapshot that rides on the host's relayed weather (BeamMP 'real-host'), nil without a state
function M.snapshot() return st and { t = st.t, w = st.w, i = st.i, h = st.h } or nil end
-- anything to keep checking for (a film, standing water, a puddle shown)?
function M.active() return st ~= nil and (st.w > 0 or st.h > 0 or st.i > 0 or shown > 0) end

function M.debugLine()
  if not st then return 'wetstate none' end
  return string.format('wetstate t=%d w=%d i=%d h=%dum step=%d/%d seed=%s seeds=%d', st.t, st.w, st.i,
    floor(st.h / M.H_SCALE), shown, M.fillStep(st.h), tostring(M.lastSeed), M.seeds)
end

function M.reset() st, seed, shown = nil, nil, 0 end

return M
