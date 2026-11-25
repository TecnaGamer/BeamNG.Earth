-- BeamNG.Earth: lightning and thunder (the weather plan 2026-11-24, item 10; weather phase B). Driven by
-- beamngEarth_weather (storm.update every frame while the weather's thunder class is above 0 and the setting is on;
-- nothing at all otherwise, not even this module's state).
--
-- Where and when. The strikes lie on a fixed lattice of CELL_DEG cells over the planet and SLOT_S slots of UTC time:
-- each (cell, slot) holds one strike or none, decided by a 32-bit hash of (cell, slot) alone (M.strikeAt). Every
-- player whose weather has the same thunder class therefore sees the same flashes at the same places at the same
-- wall-clock moments, with no message between them: BeamMP players near each other (the same preset, or 'real-host')
-- see one storm (to within their PCs' clock difference, usually well under a second), and the thunder of each strike
-- reaches each of them after their own distance / SOUND_MS. A higher class keeps every strike of a lower one (the hash
-- is compared with the class's share), so a storm building up only adds strikes.
--
-- Thunder class (M.thunderClass): 0 none, 1 possible, 2 a thunderstorm (WMO code 95), 3 a strong one (96 / 99, with
-- hail); from Open-Meteo's lightning potential where it exists (DWD ICON-D2, Central Europe), else from the code and
-- CAPE with rain (the weather plan's research note 3: no open global lightning feed is usable).
--
-- Each strike: a flash (FLASH pulses: the sky and the scene lit for a few tens of milliseconds, 1 to 3 times; how it is
-- drawn is storm.lua's render part below), for a cloud-to-ground strike within BOLT_M and in the open a bolt, and its
-- thunder after the distance at the speed of sound (a near crack, a rolling peal or a far rumble by distance).
-- Cost: between strikes a table walk at 4 Hz; a flash writes a few fields over 0.3 s.
local M = {}

local bit = require('bit')

M.CELL_DEG = 0.1          -- the strike lattice (degrees): ~11 km cells
M.SLOT_S = 10             -- seconds per slot; one strike at most per cell and slot
M.SEED = 0x5354524b       -- 'STRK'
-- strikes per cell per minute by thunder class (1..3): with the 3 x 3 cells around the player (~1,000 km2) a class 2
-- storm gives ~7 flashes a minute in all, one within 5 km every two minutes or so; a class 3 ~18
M.RATE = { 0.25, 0.8, 2.0 }
M.CG_SHARE = 0.3          -- cloud-to-ground strikes (a bolt); the rest flash inside the cloud (a glow only)
M.VARIANTS = 4            -- bolt shapes
M.SOUND_MS = 343          -- m/s
M.SEE_M = 30000           -- flashes seen up to this far (fading with distance)
M.HEAR_M = 22000          -- thunder heard up to this far (real: 15-25 km)
M.BOLT_M = 15000          -- bolts drawn up to this far
M.CLOSE_M, M.MID_M = 1500, 6000     -- thunder sounds: a crack nearer than CLOSE_M, a peal to MID_M, a rumble beyond
-- ... and a strike nearer than STRIKE_M (the user, 2026-11-25: "super close", scary loud): its own recordings made by a
-- strike, no delay after the flash (at 300 m the sound needs 0.9 s; the crack of a strike that near reads as at once),
-- full volume, the rain bed ducked under it for DUCK_S
M.STRIKE_M = 300
M.DUCK_S, M.DUCK_TO = 3.0, 0.35
M.LPI_MIN, M.LPI_STRONG = 0.5, 5    -- J/kg (ICON-D2's lightning potential index)
M.CAPE_MIN, M.CAPE_RAIN_MMH = 800, 0.5
M.CHECK_S = 0.25          -- the strike walk while a storm is on ...
M.LOOKAHEAD_S = 1.0       -- ... looking this far ahead

------------------------------------------------------------------------------------------
-- pure

local TWO32 = 4294967296

-- a * b mod 2^32 (exact in doubles: each partial product stays under 2^53)
local function imul(a, b)
  a, b = a % TWO32, b % TWO32
  local al = a % 65536
  local ah = (a - al) / 65536
  return ((ah * b) % 65536 * 65536 + al * b) % TWO32
end
local function u32(x) return x % TWO32 end
local function rotl(x, n) return u32(bit.rol(bit.tobit(x), n)) end
local function bxor(a, b) return u32(bit.bxor(bit.tobit(a), bit.tobit(b))) end
local function shr(x, n) return u32(bit.rshift(bit.tobit(x), n)) end

-- Pure: a 32-bit hash (MurmurHash3's mixing) of integer arguments: the same on every CPU and OS (integer maths in
-- doubles below 2^53, LuaJIT's bit ops).
function M.hash(...)
  local h = M.SEED
  for i = 1, select('#', ...) do
    local k = math.floor(tonumber((select(i, ...))) or 0) % TWO32
    k = imul(k, 0xcc9e2d51)
    k = rotl(k, 15)
    k = imul(k, 0x1b873593)
    h = bxor(h, k)
    h = rotl(h, 13)
    h = (imul(h, 5) + 0xe6546b64) % TWO32
  end
  h = bxor(h, shr(h, 16))
  h = imul(h, 0x85ebca6b)
  h = bxor(h, shr(h, 13))
  h = imul(h, 0xc2b2ae35)
  h = bxor(h, shr(h, 16))
  return h
end

-- Pure: the thunder class (0..3) of a weather message (the one shown: its preset or the real one).
function M.thunderClass(m)
  if type(m) ~= 'table' then return 0 end
  local code = tonumber(m.code) or 0
  local c = 0
  if code == 96 or code == 99 then c = 3 elseif code >= 95 and code <= 99 then c = 2 end
  local lpi = tonumber(m.lpi)
  if lpi and lpi >= M.LPI_STRONG then c = math.max(c, 3) elseif lpi and lpi >= M.LPI_MIN then c = math.max(c, 1) end
  local cape, rain = tonumber(m.capeJkg) or 0, tonumber(m.rainMmH) or 0
  if cape >= M.CAPE_MIN and rain >= M.CAPE_RAIN_MMH then c = math.max(c, 1) end
  return c
end

-- Pure: the share of (cell, slot)s that hold a strike in class c.
function M.share(c)
  c = math.floor(tonumber(c) or 0)
  if c < 1 then return 0 end
  return math.min(1, M.RATE[math.min(c, #M.RATE)] * M.SLOT_S / 60)
end

-- Pure: the strike of cell (i, j) (lat / CELL_DEG, lon / CELL_DEG, floored) in UTC slot `slot` (epoch / SLOT_S, floored)
-- for thunder class c, or nil: { t = epoch seconds, lat, lon, cg = a ground strike, pulses = 1..3, variant = 1..VARIANTS,
-- seed = a 32-bit number for the flash's own variation }.
function M.strikeAt(i, j, slot, c)
  local p = M.share(c)
  if p <= 0 or M.hash(i, j, slot, 1) / TWO32 >= p then return nil end
  local h2, h3, h4 = M.hash(i, j, slot, 2), M.hash(i, j, slot, 3), M.hash(i, j, slot, 4)
  local lo = h3 % 65536
  return {
    t = (slot + h2 / TWO32) * M.SLOT_S,
    lat = (i + lo / 65536) * M.CELL_DEG,
    lon = (j + (h3 - lo) / 65536 / 65536) * M.CELL_DEG,
    cg = (h4 % 1000) / 1000 < M.CG_SHARE,
    pulses = 1 + math.floor(h4 / 1000) % 3,
    variant = 1 + math.floor(h4 / 3000) % M.VARIANTS,
    seed = h4,
  }
end

-- Pure: the strikes around (lat, lon) (its cell and the 8 next to it) in the slots from epoch t0 to t1, sorted by time.
function M.strikesNear(lat, lon, t0, t1, c)
  local out = {}
  if M.share(c) <= 0 then return out end
  local i0, j0 = math.floor(lat / M.CELL_DEG), math.floor(lon / M.CELL_DEG)
  for slot = math.floor(t0 / M.SLOT_S), math.floor(t1 / M.SLOT_S) do
    for di = -1, 1 do
      for dj = -1, 1 do
        local s = M.strikeAt(i0 + di, j0 + dj, slot, c)
        if s and s.t >= t0 and s.t < t1 then out[#out + 1] = s end
      end
    end
  end
  table.sort(out, function(a, b) return a.t < b.t end)
  return out
end

-- Pure: the flash's pulses for a strike: { { start s, length s, peak 0..1 }, ... } (a return stroke and its
-- restrikes: bright, short, with dark gaps; each restrike a little weaker).
function M.pulses(s)
  local out, t = {}, 0
  local seed = tonumber(s.seed) or 0
  for k = 1, math.max(1, tonumber(s.pulses) or 1) do
    local r = M.hash(seed, k) / TWO32
    local len = 0.04 + 0.06 * r
    out[#out + 1] = { t, len, 1 / (1 + 0.35 * (k - 1)) }
    t = t + len + 0.04 + 0.08 * (1 - r)
  end
  return out
end

-- Pure: the flash's brightness (0..1) at time dt after the strike.
function M.flashAt(pulses, dt)
  for _, p in ipairs(pulses) do
    if dt >= p[1] and dt < p[1] + p[2] then
      local x = (dt - p[1]) / p[2]
      return p[3] * (x < 0.2 and x / 0.2 or (1 - x) / 0.8 * 0.7 + 0.3 * (1 - x))
    end
  end
  return 0
end

-- Pure: how bright a strike d m away looks (0..1): 1 overhead, falling with distance, 0 past SEE_M.
function M.flashGain(d)
  d = tonumber(d) or math.huge
  if d >= M.SEE_M then return 0 end
  return 1 / (1 + (d / 3000) ^ 1.5) * (1 - d / M.SEE_M)
end

-- Pure: the distance (m) a flash d m away is drawn at with visibility visM (nil: its own).
function M.proxyDistance(d, visM)
  d = tonumber(d) or 0
  local v = tonumber(visM)
  if not v then return d end
  return math.min(d, math.max(M.PROXY_MIN_M, M.PROXY_VIS * v))
end

-- Pure: the thunder for a strike d m away: { delay s, kind 'strike' | 'close' | 'mid' | 'far', gain 0..1 } or nil (too far).
function M.thunderFor(d)
  d = tonumber(d) or math.huge
  if d > M.HEAR_M then return nil end
  if d < M.STRIKE_M then return { delay = 0, kind = 'strike', gain = 1 } end
  local kind = d < M.CLOSE_M and 'close' or (d < M.MID_M and 'mid' or 'far')
  local gain = math.min(1, 1.2 / (1 + d / 2500)) * (1 - 0.5 * d / M.HEAR_M)
  return { delay = d / M.SOUND_MS, kind = kind, gain = gain }
end

------------------------------------------------------------------------------------------
-- runtime: the clock

-- UTC epoch seconds with a fraction: os.time() (whole seconds, never ahead of the true time) against os.clockhp(); the
-- largest os.time() - clock seen is the offset (it converges from below within a second), reset when the PC's clock
-- jumps.
local clockOff = nil
function M.now()
  local c = os.clockhp and os.clockhp() or os.clock()
  local o = os.time() - c
  if not clockOff or o > clockOff + 2 or o < clockOff - 2 then clockOff = o
  elseif o > clockOff then clockOff = o end
  return clockOff + c
end

------------------------------------------------------------------------------------------
-- runtime: the flash, the bolt, the thunder

M.ART = '/levels/beamng_earth/art/weather/'
M.MATERIALS = M.ART .. 'weather.materials.json'
M.SOUND = '/levels/beamng_earth/art/sound/weather/'
-- thunder files per kind (tools: ~/bnge-tests/weather/b/audio; CREDITS.md "Weather sounds")
M.THUNDER = { close = { 'bne_thunder_close1.ogg', 'bne_thunder_close2.ogg' }, mid = { 'bne_thunder_mid1.ogg',
  'bne_thunder_mid2.ogg' }, far = { 'bne_thunder_far1.ogg', 'bne_thunder_far2.ogg' },
  strike = { 'bne_thunder_strike1.ogg', 'bne_thunder_strike2.ogg' } }
M.THUNDER_VOL = 1.0
M.MUFFLED = 0.45          -- the thunder's volume in a tunnel or under a deck
-- the flash's light: as bright at the camera as a strike of LIGHT_LM lumens d m away (~50 lux at 3 km: a night lit up,
-- lost in daylight as a real flash is), but put LIGHT_NEAR_M from the camera towards the strike and LIGHT_UP_M up, its
-- lumens scaled by the distance squared (a light kilometres away drew nothing on the ground in the broker shots: the
-- engine culls far lights)
M.LIGHT_LM = 6e9
M.LIGHT_NEAR_M, M.LIGHT_UP_M = 250, 200
M.LIGHT_COLOR = '0.8 0.85 1 1'
M.GLOW_SIZE_M = 6000      -- the lit cloud (a glow at the cloud base over the strike) ...
M.GLOW_MAX_ELEV = math.rad(9)   -- ... no higher in the view than this (the cloud base over a far strike is low in the sky;
                                -- over a near one it was out of the frame)
M.BOLT_BOTTOM_M = -60     -- the bolt's foot under the player's ground (hidden by the horizon and buildings)
-- the stand-in distance of the bolt and the glow: no farther than PROXY_VIS of the visibility (nor nearer than
-- PROXY_MIN_M), so the fog leaves them most of their light
M.PROXY_VIS, M.PROXY_MIN_M = 0.25, 300
M.VEIL_ALPHA = 0.2        -- the lit air's opacity at a full flash overhead (0.35 turned a night street flat white, wb9)
M.CLOUD_BASE_MIN_M, M.CLOUD_BASE_MAX_M = 700, 3000
M.FLASH_S = 0.6           -- a flash's whole length (its pulses: M.pulses)

local st = nil            -- nil until the first storm; { class, lastT, flashes = {}, thunders = {}, objs }
M.strikes, M.flashes, M.thunders = 0, 0, 0
M.last = nil              -- the last strike (status, the probes)

local function camPos()
  local cam = rawget(_G, 'core_camera')
  local p = cam and cam.getPosition and cam.getPosition()
  return p
end

local function api() return rawget(_G, 'beamngEarth_api') end

local function makeStatic(shape, name)
  local o = createObject('TSStatic')
  if not o then return nil end
  o:setField('shapeName', 0, shape)
  o:setField('collisionType', 0, 'None')
  o:setField('decalType', 0, 'None')
  o:setField('castShadows', 0, 'false')
  o.useInstanceRenderData, o.canSave = true, false
  o:setField('instanceColor', 0, '1 1 1 0')
  o:setHidden(true)
  o:registerObject(name)
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
  return o
end

local function objects()
  if st.objs then return st.objs end
  if not scenetree.findObject('bngEarthBolt') and loadJsonMaterialsFile then pcall(loadJsonMaterialsFile, M.MATERIALS) end
  local objs = { bolts = {} }
  for k = 1, M.VARIANTS do
    local o = makeStatic(M.ART .. 'bne_bolt' .. k .. '.cdae', 'bngEarthStormBolt' .. k)
    objs.bolts[k] = o and o:getID()
  end
  local g = makeStatic(M.ART .. 'bne_flashglow.cdae', 'bngEarthStormGlow')   -- (not the material's name: an object named like a material shadows it)
  objs.glow = g and g:getID()
  local v = makeStatic(M.ART .. 'bne_flashveil.cdae', 'bngEarthStormVeil')
  objs.veil = v and v:getID()
  local l = createObject('PointLight')
  if l then
    l.canSave = false
    l:setField('isEnabled', 0, '0')
    l:setField('castShadows', 0, '0')
    l:setField('color', 0, M.LIGHT_COLOR)
    l:registerObject('bngEarthStormLight')
    if scenetree.MissionGroup then scenetree.MissionGroup:addObject(l) end
    objs.light = l:getID()
  end
  st.objs = objs
  return objs
end

local function obj(id) return id and scenetree.findObjectById(id) or nil end

local function yawQuat(dx, dy)            -- the quad's normal (its +y) towards (dx, dy)
  local l = math.sqrt(dx * dx + dy * dy)
  if l < 1e-6 then return 0, 0, 0, 1 end
  local th = math.atan2(-dx / l, dy / l)
  return 0, 0, math.sin(th / 2), math.cos(th / 2)
end

local function setAlpha(o, a)
  o:setField('instanceColor', 0, string.format('1 1 1 %.3f', a))
  o:updateInstanceRenderData()
end

-- A strike now: its flash (and bolt) start, its thunder is queued. ctx: { groundZ, cloudAboveM, sheltered }.
local function strike(s, now, ctx)
  local a = api()
  local p = camPos()
  if not (a and a.toGame and p) then return end
  -- (api.toGame / toGeo return plain numbers: x, y, z / lat, lon, h; the first broker run's error: "attempt to index
  -- local 'geo' (a number value)", no strike was ever looked up)
  local gx, gy = a.toGame(s.lat, s.lon, 0)
  if not (tonumber(gx) and tonumber(gy)) then return end
  local g = { x = gx, y = gy }
  local dx, dy = g.x - p.x, g.y - p.y
  local d = math.sqrt(dx * dx + dy * dy)
  M.strikes = M.strikes + 1
  M.last = { t = s.t, d = d, cg = s.cg, lat = s.lat, lon = s.lon }
  local th = M.thunderFor(d)
  if th then
    local files = M.THUNDER[th.kind]
    st.thunders[#st.thunders + 1] = { at = s.t + th.delay, file = files[1 + s.seed % #files], gain = th.gain, kind = th.kind }
  end
  local gain = M.flashGain(d)
  if gain <= 0.002 then return end
  local base = math.max(M.CLOUD_BASE_MIN_M, math.min(M.CLOUD_BASE_MAX_M, tonumber(ctx.cloudAboveM) or 1500))
  local gz = tonumber(ctx.groundZ) or p.z
  st.flashes[#st.flashes + 1] = { t0 = s.t, pulses = M.pulses(s), gain = gain, x = g.x, y = g.y, z = gz, base = base,
    bolt = s.cg and d < M.BOLT_M and not ctx.sheltered and s.variant or nil, dx = dx, dy = dy, d = d }
  M.flashes = M.flashes + 1
end

-- The flashes' look this frame: the brightest flash drives the light, the glow and its bolt.
local function drawFlashes(now, ctx)
  local best, bv = nil, 0
  local keep = {}
  for _, f in ipairs(st.flashes) do
    local dt = now - f.t0
    if dt < M.FLASH_S or f.held then
      keep[#keep + 1] = f
      local v = f.held or M.flashAt(f.pulses, dt) * f.gain
      if v > bv or not best then best, bv = f, v end
    end
  end
  st.flashes = keep
  local objs = st.objs
  if not objs then return end
  local shown = st.shown or {}
  local want = {}
  if best and bv > 0.001 then
    local l = obj(objs.light)
    local cp = camPos()
    if l and cp then
      local d = math.max(1, best.d)
      local k = math.min(1, M.LIGHT_NEAR_M / d)
      local lx, ly, lz = cp.x + best.dx * k, cp.y + best.dy * k, cp.z + M.LIGHT_UP_M
      local rl = math.sqrt((lx - cp.x) ^ 2 + (ly - cp.y) ^ 2 + M.LIGHT_UP_M ^ 2)
      l:setPosition(vec3(lx, ly, lz))
      l:setField('radius', 0, string.format('%.0f', rl * 3))
      -- (the strike's own illuminance at the camera, LIGHT_LM / d^2, from a light rl away; the pulse's level times)
      l:setField('intensity', 0, string.format('%.6g', M.LIGHT_LM * (rl / d) ^ 2 * bv / best.gain))
      if not shown.light then l:setField('isEnabled', 0, '1') end
      l:postApply()
      want.light = true
    end
    local p = camPos()
    local qx, qy, qz, qw = yawQuat(-best.dx, -best.dy)
    -- the bolt and the glow drawn at a stand-in distance r (the same picture, scaled by r / d) inside the fog: at their
    -- real distance a storm's fog (visibility 2-3 km) hid them completely in the broker shots, a real flash outshines it
    local f = math.min(1, M.proxyDistance(best.d, ctx and ctx.visM) / math.max(1, best.d))
    local px, py = p and p.x + best.dx * f, p and p.y + best.dy * f
    local gl = obj(objs.glow)
    if gl and p then
      local s = M.GLOW_SIZE_M * f
      gl:setScale(vec3(s, s, s * 0.5))
      local gz = math.min(best.z + best.base, p.z + best.d * math.tan(M.GLOW_MAX_ELEV))
      gl:setPosRot(px, py, p.z + (gz - p.z) * f, qx, qy, qz, qw)
      if not shown.glow then gl:setHidden(false) end
      setAlpha(gl, math.min(1, bv * 1.5))
      want.glow = true
    end
    if best.bolt and p then
      local b = obj(objs.bolts[best.bolt])
      if b then
        local h = (best.base - M.BOLT_BOTTOM_M) * f
        b:setScale(vec3(h, h, h))
        b:setPosRot(px, py, p.z + (best.z + M.BOLT_BOTTOM_M - p.z) * f, qx, qy, qz, qw)
        if shown.bolt ~= best.bolt then b:setHidden(false) end
        setAlpha(b, math.min(1, bv / best.gain))
        want.bolt = best.bolt
      end
    end
    -- the lit air around the camera (rain and haze light up in a flash: the whole view brightens)
    local v = obj(objs.veil)
    if v and p and not (ctx and ctx.sheltered) then
      v:setPosRot(p.x, p.y, p.z, 0, 0, 0, 1)
      if not shown.veil then v:setHidden(false) end
      setAlpha(v, math.min(1, M.VEIL_ALPHA * bv))
      want.veil = true
    end
  end
  if shown.light and not want.light then local l = obj(objs.light) if l then l:setField('isEnabled', 0, '0'); l:postApply() end end
  if shown.glow and not want.glow then local gl = obj(objs.glow) if gl then gl:setHidden(true) end end
  if shown.veil and not want.veil then local v = obj(objs.veil) if v then v:setHidden(true) end end
  if shown.bolt and shown.bolt ~= want.bolt then local b = obj(objs.bolts[shown.bolt]) if b then b:setHidden(true) end end
  st.shown = want
end

local function playThunders(now, muffled)
  if #st.thunders == 0 then return end
  local keep = {}
  for _, t in ipairs(st.thunders) do
    if now >= t.at then
      if now - t.at < 2 then          -- (a thunder missed by more than that, a pause or a hitch: dropped)
        local au = Engine and Engine.Audio
        if au and au.playOnce then
          pcall(au.playOnce, 'AudioGui', M.SOUND .. t.file, { volume = t.gain * M.THUNDER_VOL * (muffled and M.MUFFLED or 1) })
          M.thunders = M.thunders + 1
          if t.kind == 'strike' then          -- (the rain bed ducked under a strike's crack and boom)
            local amb = package.loaded['beamngEarth/ambience']
            if amb and amb.duck then pcall(amb.duck, M.DUCK_S, M.DUCK_TO) end
          end
        end
      end
    else
      keep[#keep + 1] = t
    end
  end
  st.thunders = keep
end

-- A held flash for screenshots and probes (console: beamngEarth_weather's storm.hold): M.holdSpec = { d = m ahead,
-- bearing = rad from the camera's view (0 ahead, + to the right), level = 0..1 (1: full), bolt = 1..VARIANTS or nil,
-- strobe = s (a flash every so often instead of a steady one) }. Drawn as a flash that never ends.
function M.hold(spec) M.holdSpec = spec end
local function holdFlash(now, ctx)
  local h = M.holdSpec
  local cam = rawget(_G, 'core_camera')
  local p = camPos()
  local f = cam and cam.getForward and cam.getForward()
  if not (p and f) then return end
  local fx, fy = f.x, f.y
  local l = math.sqrt(fx * fx + fy * fy)
  if l < 1e-6 then fx, fy, l = 0, 1, 1 end
  fx, fy = fx / l, fy / l
  local b = tonumber(h.bearing) or 0
  local dxv, dyv = fx * math.cos(b) + fy * math.sin(b), -fx * math.sin(b) + fy * math.cos(b)
  local d = tonumber(h.d) or 3000
  if not st.objs then objects() end
  local base = math.max(M.CLOUD_BASE_MIN_M, math.min(M.CLOUD_BASE_MAX_M, tonumber(ctx.cloudAboveM) or 1500))
  local lvl = tonumber(h.level) or 1
  if h.strobe then
    local ph = now % h.strobe
    lvl = ph < 0.3 and lvl * M.flashAt(M.pulses({ seed = math.floor(now / h.strobe), pulses = 3 }), ph) or 0
  end
  st.flashes = { { t0 = now, held = lvl, pulses = {}, gain = 1, x = p.x + dxv * d, y = p.y + dyv * d,
    z = tonumber(ctx.groundZ) or p.z, base = base, bolt = h.bolt, dx = dxv * d, dy = dyv * d, d = d } }
  if lvl <= 0 then st.flashes = {} end
end

-- Every frame from weather.lua while the thunder class c > 0 (or a flash or thunder is still running). ctx: { groundZ
-- (the player's ground, game z), cloudAboveM (the cloud base over it), sheltered (in a tunnel / under a deck) }.
-- Returns true while it has something running.
function M.update(dt, c, ctx)
  if not st then
    if (tonumber(c) or 0) < 1 and not (M.holdSpec and ctx and ctx.hold) then return false end
    st = { class = c, lastT = nil, upcoming = {}, flashes = {}, thunders = {}, objs = nil, timer = M.CHECK_S }
  end
  ctx = ctx or {}
  local now = M.now()
  st.class = c
  st.timer = st.timer + (dt or 0)
  -- the strikes of the next LOOKAHEAD_S looked up every CHECK_S (st.lastT: the end of what is looked up), each started
  -- in the frame its time comes
  if c >= 1 and st.timer >= M.CHECK_S then
    st.timer = 0
    local a, p = api(), camPos()
    local lat, lon
    if a and a.toGeo and p then lat, lon = a.toGeo(p.x, p.y, p.z) end
    local geo = tonumber(lat) and tonumber(lon) and { lat = lat, lon = lon }
    if geo then
      local t0 = st.lastT or now
      if t0 < now - 1 or t0 > now + 5 then t0 = now end    -- (back from a pause, a load: no burst of old strikes)
      local t1 = now + M.LOOKAHEAD_S
      for _, s in ipairs(M.strikesNear(geo.lat, geo.lon, t0, t1, c)) do st.upcoming[#st.upcoming + 1] = s end
      st.lastT = math.max(t0, t1)
    end
  end
  if #st.upcoming > 0 then
    local keep = {}
    for _, s in ipairs(st.upcoming) do
      if now >= s.t then
        if now - s.t < 1 then
          if not st.objs then objects() end
          strike(s, now, ctx)
        end
      else
        keep[#keep + 1] = s
      end
    end
    st.upcoming = keep
  end
  if M.holdSpec and ctx.hold then holdFlash(now, ctx)
  elseif #st.flashes > 0 and st.flashes[1].held then st.flashes = {} end
  if #st.flashes > 0 or (st.shown and next(st.shown)) then drawFlashes(now, ctx) end
  playThunders(now, ctx.sheltered)
  if c < 1 then st.upcoming = {} end
  if c < 1 and #st.flashes == 0 and #st.thunders == 0 and not (st.shown and next(st.shown)) then
    st.lastT = nil
    return false
  end
  return true
end

-- The level goes, or the storm is switched off: everything hidden, the objects deleted.
function M.reset()
  if st and st.objs then
    for _, id in ipairs(st.objs.bolts or {}) do local o = obj(id) if o then o:delete() end end
    for _, k in ipairs({ 'glow', 'veil', 'light' }) do local o = obj(st.objs[k]) if o then o:delete() end end
  end
  st = nil
end

-- One line on the objects' state (the probes' overlay, the console): ids, hidden flags, the light's fields, whether
-- the materials are loaded and the shapes' world boxes.
function M.debugLine()
  if not st or not st.objs then return 'storm objs: none' end
  local function f(o, k) local ok, v = pcall(function() return o:getField(k, 0) end) return ok and tostring(v) or '?' end
  local parts = {}
  local function one(tag, id)
    local o = obj(id)
    if not o then parts[#parts + 1] = tag .. '=missing' return end
    local okh, h = pcall(function() return o:isHidden() end)
    local okb, b = pcall(function() local wb = o:getWorldBox() return wb and string.format('%.0f..%.0f', wb.minExtents.z, wb.maxExtents.z) end)
    parts[#parts + 1] = string.format('%s#%s h=%s ic=%s box=%s', tag, tostring(id), tostring(okh and h), f(o, 'instanceColor'),
      tostring(okb and b))
  end
  one('glow', st.objs.glow)
  local sb = st.shown and st.shown.bolt
  if sb then one('bolt' .. sb, st.objs.bolts[sb]) end
  local l = obj(st.objs.light)
  if l then
    parts[#parts + 1] = string.format('light en=%s r=%s int=%s bri=%s', f(l, 'isEnabled'), f(l, 'radius'), f(l, 'intensity'),
      f(l, 'brightness'))
  end
  parts[#parts + 1] = 'mat=' .. tostring(scenetree.findObject('bngEarthBolt') ~= nil) .. '/' ..
    tostring(scenetree.findObject('bngEarthFlashGlow') ~= nil)
  return table.concat(parts, ' | ')
end

function M.status()
  return { class = st and st.class or 0, strikes = M.strikes, flashes = M.flashes, thunders = M.thunders,
    queued = st and #st.thunders or 0, last = M.last }
end

return M
