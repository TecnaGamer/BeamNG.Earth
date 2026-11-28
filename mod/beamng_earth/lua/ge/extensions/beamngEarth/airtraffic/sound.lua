-- From devo343's Live Air Traffic add-on (with permission; CREDITS.md), moved into BeamNG.Earth for the native
-- live air traffic (beamngEarth/airtraffic.lua).
-- Live Air Traffic: aircraft sounds, heard when an aircraft is near enough.
--
-- Each audible aircraft gets two looping 3D emitters (tools/build_aircraft_sounds.py): "near" (the full roar, whine,
-- propeller or rotor buzz) and "far" (the low rumble the air lets through over distance). The mod sets their loudness
-- and pitch itself every frame:
--   * where the sound comes from: sound takes distance / 343 s to arrive, so it is heard from where the aircraft
--     WAS (a jet 10 km up is heard ~30 s behind where it is seen); the emitter is put 50 m from the listener in
--     that direction (the game pans it; the mod does the distance itself, so the engine's own fall-off cannot
--     double it)
--   * loudness: spreading (-6 dB per doubling of distance) plus the air soaking up the high frequencies (much
--     faster for the near layer than for the far one), by the aircraft's kind and what it is doing (climbing loud,
--     descending quieter, idling at the gate, a parked light aircraft silent)
--   * jets far off or high up: a third layer, "distant", the deep rolling rumble you hear from a jet at cruise
--     straight overhead, taking over from ~4 km out; sound low on the horizon loses much to the ground on its way
--     (ground effect), from overhead it does not
--   * engine load: pitch and loudness follow what the engines are doing: near idle parked and taxiing (a little more
--     the faster it taxis), full power on the take-off roll and in a climb, reverse thrust dying away on a landing
--     rollout, throttled back in a descent; spooling up and down over seconds
--   * a jet passing close overhead (airborne, within 2.5 km): a recorded big-jet roar (lat_jet_close.wav) takes over
--     from the synthesised near layer
--   * Doppler: the pitch rises as it comes and drops as it goes, from its speed toward the listener at the moment
--     the sound left it
-- At most MAX_VOICES aircraft at once, the loudest; voices fade in and out, never click on.
local M = {}

local predict = require('beamngEarth/airtraffic/predict')

M.SOUND_DIR = '/levels/beamng_earth/art/sound/airtraffic/'
M.C = 343                -- m/s
M.MAX_VOICES = 6
M.PROXY_M = 50           -- emitters this far from the listener, in the sound's direction
M.MIN_GAIN = 0.003       -- quieter than this: not heard
M.HEAR_M = 30000         -- not even considered past this
M.FADE_S = 0.35
M.CREATE_PER_FRAME = 2

-- Per kind: sound file, playback pitch (bigger engines lower), loudness 300 m off at climb power (dB).
M.KINDS = {
  jumbo     = { file = 'jet',    pitch = 0.82, db = 97 },
  heavy     = { file = 'jet',    pitch = 0.88, db = 95 },
  narrow    = { file = 'jet',    pitch = 1.00, db = 91 },
  regional  = { file = 'jet',    pitch = 1.12, db = 89 },
  bizjet    = { file = 'jet',    pitch = 1.28, db = 87 },
  turboprop = { file = 'prop',   pitch = 1.00, db = 88 },
  light     = { file = 'piston', pitch = 1.00, db = 80 },
  heli      = { file = 'heli',   pitch = 1.00, db = 90 },
  fighter   = { file = 'jet',    pitch = 1.40, db = 102 },
}
-- (gliders and balloons: no engine sound)

------------------------------------------------------------------------------------------ pure
-- Engine load, 0 (idle) .. 1 (take-off power), from what the aircraft is doing. nil: engine off (a parked light
-- aircraft or helicopter). gsPrev: its ground speed one report earlier (speeding up: a take-off roll; slowing: a
-- landing rollout). agl: height over the ground (m), when known.
function M.engineLoad(kind, ground, gs, vs, gsPrev, agl)
  gs, vs = gs or 0, vs or 0
  if ground then
    if gs < 3 then
      if kind == 'light' or kind == 'heli' then return nil end    -- parked, engine off
      return 0.05                                                 -- airliner at the gate: idle
    end
    local taxiMax = (kind == 'light' or kind == 'heli') and 15 or 30   -- (a Cessna lifts off at ~55 kt)
    if gs <= taxiMax then return 0.10 + 0.12 * (gs - 3) / (taxiMax - 3) end   -- taxiing: little more than idle
    local slowing = gsPrev and gsPrev > gs + 2
    if slowing then
      -- landing rollout: reverse thrust while fast, dying away to idle as it slows to taxi speed
      return 0.15 + 0.55 * predict.smooth((gs - taxiMax - 10) / (taxiMax * 2))
    end
    return 0.25 + 0.75 * predict.smooth((gs - taxiMax) / (taxiMax * 1.3))   -- speeding up: take-off roll, full power
  end
  if vs > 300 then return math.min(1, 0.80 + 0.20 * (vs - 300) / 2200) end  -- climbing: climb to take-off power
  if vs < -300 then
    -- descending throttled back; low and slow on approach (flaps, gear) it needs some power again
    local approach = (agl and agl < 900 and gs < 190) and 0.25 or 0
    return math.max(0.30, 0.42 - 0.12 * math.min(1, (-vs - 300) / 1700)) + approach
  end
  return 0.72                                                     -- cruising / level
end

-- How load sounds per engine: pitch (factor) and loudness (dB) at idle and at full power. Jets swing the most (the
-- fans spool from idle whine to a full-power howl); pistons go from a ~1,000 rpm taxi to 2,500; turboprop propellers
-- hold their speed (some change); a helicopter's rotor turns at one speed (almost none).
M.LOAD_SOUND = {
  jet    = { p0 = 0.68, p1 = 1.06, db0 = -15, db1 = 3 },
  prop   = { p0 = 0.84, p1 = 1.04, db0 = -10, db1 = 3 },
  piston = { p0 = 0.58, p1 = 1.04, db0 = -14, db1 = 2 },
  heli   = { p0 = 0.95, p1 = 1.03, db0 = -6,  db1 = 2 },
}

-- Loudness change (dB) and pitch factor for an engine load of the given sound file.
function M.loadSound(file, load)
  local l = M.LOAD_SOUND[file] or M.LOAD_SOUND.jet
  load = math.max(0, math.min(1, load))
  return l.db0 + (l.db1 - l.db0) * load, l.p0 + (l.p1 - l.p0) * load
end

M.SPOOL_UP_S, M.SPOOL_DOWN_S = 2.5, 4.0

-- (Kept for anything calling it: dB and pitch straight from the state, without spooling.)
function M.powerState(kind, ground, gs, vs, gsPrev, agl)
  local load = M.engineLoad(kind, ground, gs, vs, gsPrev, agl)
  if not load then return nil end
  local k = M.KINDS[kind]
  return M.loadSound(k and k.file or 'jet', load)
end

-- Sound level (dB) of the near and far layers at distance d (m) for an aircraft of loudness db (at 300 m).
function M.levels(db, d)
  d = math.max(20, d)
  local spread = 20 * math.log10(d / 300)
  local over = math.max(0, d - 300)
  return db - spread - 0.0045 * over, db - 3 - spread - 0.0008 * over
end

-- Level (dB) of a jet's "distant" layer: the deep rumble the air carries farthest (it loses almost nothing to
-- absorption), from an aircraft of loudness db at distance d.
function M.distantLevel(db, d)
  d = math.max(20, d)
  return db - 4 - 20 * math.log10(d / 300) - 0.00025 * math.max(0, d - 300)
end

-- How much of a jet's sound is the distant rumble (0 close, 1 from ~12 km): it takes over from the far layer.
function M.distantWeight(d)
  return predict.smooth((d - 4000) / 8000)
end

-- The distant layer's gain: tuned so a jet at cruise straight overhead is a clear but very distant rumble.
function M.distantGain(level)
  return math.max(0, math.min(1, 10 ^ ((level - 82) / 24)))
end

-- How much of an airborne jet's sound is the "close" recording (lat_jet_close.wav): none past 2.5 km, all of it from
-- 1 km in. It takes over from the synthesised near layer for a jet passing close overhead; taxiing, take-off rolls
-- and the gate keep the synthesised sound (which follows the engine's load).
M.CLOSE_FROM_M, M.CLOSE_FULL_M = 2500, 1000
function M.closeWeight(d, airborne)
  if not airborne then return 0 end
  return 1 - predict.smooth((d - M.CLOSE_FULL_M) / (M.CLOSE_FROM_M - M.CLOSE_FULL_M))
end

-- Ground effect (dB lost): sound from an aircraft low on the horizon skims the ground and the terrain on its way and
-- loses much of itself; from overhead it comes straight down. 12 dB lost at the horizon, none from 20 degrees up.
function M.groundEffect(elevDeg)
  return 12 * (1 - predict.smooth((elevDeg or 90) / 20))
end

-- A level (dB) to an emitter gain (0..1): 90 dB is full, a little gentler than the physical curve so far aircraft
-- stay a faint presence instead of nothing.
function M.gain(level)
  return math.max(0, math.min(1, 10 ^ ((level - 90) / 26)))
end

-- Doppler: the pitch factor for a source moving with radial speed vr (m/s, + toward the listener).
function M.doppler(vr)
  return math.max(0.6, math.min(1.6, M.C / (M.C - math.max(-300, math.min(300, vr)))))
end

-- When the sound now heard left the aircraft: t such that |pos(t) - listener| = C (now - t). posAt(t) -> x, y, z.
-- A few fixed-point steps (converges fast: aircraft fly well below the speed of sound).
-- guess: last frame's answer (it moves little between frames, so one or two steps then do).
function M.emissionTime(now, posAt, lx, ly, lz, guess)
  local t = guess or now
  for _ = 1, 24 do
    local x, y, z = posAt(t)
    if not x then return nil end
    local d = math.sqrt((x - lx) ^ 2 + (y - ly) ^ 2 + (z - lz) ^ 2)
    local nt = now - d / M.C
    if math.abs(nt - t) < 0.01 then return nt end
    t = nt
  end
  return t
end

------------------------------------------------------------------------------------------ emitters
local voices = {}        -- aircraft id -> { near, far = emitter ids, file, g = {near, far}, want = {..}, pitch }
local listener = nil
local lisPrev, lisVel = nil, nil   -- the listener's last position and its (smoothed) velocity
M.LISTENER_VEL_S = 0.3   -- listener velocity smoothing (s)
M.DIR_SMOOTH_S = 0.15    -- a voice's direction eases over this long (no flutter from frame-to-frame noise)

-- Inside the aircraft the camera rides in: its engines through the cabin walls, a steady drone (no Doppler, no
-- distance), lower and duller for jets, the propellers' beat for props and helicopters.
function M.cabinGains(file, load)
  load = load or 0.5
  if file == 'jet' then return 0.10 + 0.08 * load, 0.40 + 0.25 * load end
  return 0.30 + 0.20 * load, 0.30 + 0.10 * load
end

local function emitterObj(id)
  if not id then return nil end
  local o = scenetree and scenetree.findObjectById and scenetree.findObjectById(id)
  return o
end

local function deleteEmitter(id)
  local o = emitterObj(id)
  if o then pcall(o.delete, o) end
end

local function makeEmitter(file)
  local e = createObject('SFXEmitter')
  if Sim and Sim.upcast then e = Sim.upcast(e) or e end
  e:setTransform(MatrixF(true))
  e:setField('fileName', 0, file)
  e:setField('is3D', 0, '1')
  e:setField('isLooping', 0, '1')
  e:setField('playOnAdd', 0, '1')
  e:setField('volume', 0, '0')
  e:setField('referenceDistance', 0, '60')
  e:setField('maxDistance', 0, '3000')
  e.canSave = false
  e:registerObject('')
  pcall(e.play, e)
  return e:getID()
end

local function dropVoice(id)
  local v = voices[id]
  if not v then return end
  deleteEmitter(v.near); deleteEmitter(v.far); deleteEmitter(v.distant); deleteEmitter(v.close)
  voices[id] = nil
end

function M.clear()
  for id in pairs(voices) do dropVoice(id) end
  voices = {}
  lisPrev, lisVel = nil, nil
end

local function listenerPos(fallback)
  local S = rawget(_G, 'SFXSystem')
  if S and S.getPositionXYZ then
    local ok, x, y, z = pcall(S.getPositionXYZ)
    if ok and tonumber(x) then return vec3(x, y, z) end
  end
  return fallback
end

-- Every frame. ctx: items (id -> aircraft), clock, toGame(lat, lon, h) -> x, y, z, camPos, dt, volume (0..2),
-- on (sounds wanted).
function M.update(ctx)
  local dt = ctx.dt or 0
  if not ctx.on then
    if next(voices) then M.clear() end
    return
  end
  -- following an aircraft the camera moves fast: hear from where it is this frame (the sound system's own listener
  -- position can lag a frame behind)
  listener = (ctx.following and ctx.camPos) or listenerPos(ctx.camPos)
  if not listener then return end
  local lx, ly, lz = listener.x, listener.y, listener.z
  -- the listener's velocity (for the Doppler shift: riding along with an aircraft there is none)
  if lisPrev and dt > 0 then
    local vx, vy, vz = (lx - lisPrev.x) / dt, (ly - lisPrev.y) / dt, (lz - lisPrev.z) / dt
    if vx * vx + vy * vy + vz * vz > 700 * 700 then
      lisVel = { x = 0, y = 0, z = 0 }                  -- (a jump: a teleport or a camera switch)
    else
      lisVel = lisVel or { x = vx, y = vy, z = vz }
      local k = math.min(1, dt / M.LISTENER_VEL_S)
      lisVel = { x = lisVel.x + (vx - lisVel.x) * k, y = lisVel.y + (vy - lisVel.y) * k, z = lisVel.z + (vz - lisVel.z) * k }
    end
  end
  lisPrev = { x = lx, y = ly, z = lz }
  local lv = lisVel or { x = 0, y = 0, z = 0 }
  -- who can be heard, and how loud
  local cands = {}
  for id, it in pairs(ctx.items) do
    local k = M.KINDS[it.kind]
    if k and ctx.cabinId == id and it.base and not it.missing then
      -- the one the camera sits in
      local target = M.engineLoad(it.kind, it.base.ground, it.base.gs, it.vs or it.base.vs, it.base.gsPrev, it.agl)
      if target then
        local cur = it.sndLoad or target
        local tau = target > cur and M.SPOOL_UP_S or M.SPOOL_DOWN_S
        cur = cur + (target - cur) * math.min(1, (ctx.dt or 0) / tau)
        it.sndLoad = cur
        local _, pf = M.loadSound(k.file, cur)
        local gN, gF = M.cabinGains(k.file, cur)
        local f, u = it.fF, it.fU
        local dir = (f and u) and (f * -0.6 + u * -0.25) or vec3(0, 0, -1)
        dir = dir * (1 / math.max(1e-6, dir:length()))
        cands[#cands + 1] = { id = id, it = it, gN = gN, gF = gF, gD = 0, gC = 0, loud = 10, closePitch = 1,
          dir = dir, pitch = k.pitch * (pf or 1), file = k.file, dist = 10 }
      end
    elseif k and it.dist and it.dist < M.HEAR_M and it.base and not it.missing then
      -- engine load, spooled: engines take seconds to speed up and longer to wind down
      local target = M.engineLoad(it.kind, it.base.ground, it.base.gs, it.vs or it.base.vs, it.base.gsPrev, it.agl)
      local dB, pf = nil, nil
      if target then
        local cur = it.sndLoad or target
        local tau = target > cur and M.SPOOL_UP_S or M.SPOOL_DOWN_S
        cur = cur + (target - cur) * math.min(1, (ctx.dt or 0) / tau)
        it.sndLoad = cur
        dB, pf = M.loadSound(k.file, cur)
      else
        it.sndLoad = nil
      end
      -- (a quick bound: where the sound came from is at least half as far as the aircraft is now)
      if dB then
        local bound = math.max(M.gain(select(2, M.levels(k.db + dB, it.dist * 0.5))),
          k.file == 'jet' and M.distantGain(M.distantLevel(k.db + dB, it.dist * 0.5)) or 0)
        if bound * (ctx.volume or 1) < M.MIN_GAIN then dB = nil end
      end
      if dB then
        local b = it.base
        local function posAt(t)
          local la, lo, alt = predict.predictAt(b, t)
          local x, y, z = ctx.toGame(la, lo, alt * predict.FT)
          if x and b.ground and it.groundZ then z = it.groundZ + 2 end
          return x, y, z
        end
        -- (heard from where it is drawn: the drawn aircraft eases from its old prediction to each new one, and the
        -- sound must follow it, not jump when the data does)
        local ox, oy, oz = 0, 0, 0
        if it.fPos then
          local px, py, pz = posAt(ctx.clock)
          if px then
            ox, oy, oz = it.fPos.x - px, it.fPos.y - py, it.fPos.z - pz
            if ox * ox + oy * oy + oz * oz > 4e6 then ox, oy, oz = 0, 0, 0 end
          end
        end
        local function posAtD(t)
          local x, y, z = posAt(t)
          if not x then return nil end
          return x + ox, y + oy, z + oz
        end
        local te = M.emissionTime(ctx.clock, posAtD, lx, ly, lz, it.soundT and it.soundT + (ctx.dt or 0))
        it.soundT = te
        if te then
          local x, y, z = posAtD(te)
          local x2, y2, z2 = posAtD(te + 0.5)
          if x and x2 then
            local dx, dy, dz = x - lx, y - ly, z - lz
            local d = math.sqrt(dx * dx + dy * dy + dz * dz)
            local elev = math.deg(math.asin(math.max(-1, math.min(1, dz / math.max(1, d)))))
            local ge = M.groundEffect(elev)
            local nearL, farL = M.levels(k.db + dB - ge, d)
            local gN, gF = M.gain(nearL), M.gain(farL)
            local gD, gC = 0, 0
            if k.file == 'jet' then
              local w = M.distantWeight(d)
              gD = w * M.distantGain(M.distantLevel(k.db + dB - ge, d))
              gF = gF * (1 - 0.6 * w)
              local cw = M.closeWeight(d, not it.base.ground)
              gC = cw * gN
              gN = gN * (1 - 0.85 * cw)
            end
            if math.max(gN, gF, gD, gC) * (ctx.volume or 1) >= M.MIN_GAIN then
              -- radial speed toward the listener at emission, relative to the listener (riding along: none)
              local vx, vy, vz = (x2 - x) / 0.5 - lv.x, (y2 - y) / 0.5 - lv.y, (z2 - z) / 0.5 - lv.z
              local vr = -(vx * dx + vy * dy + vz * dz) / math.max(1, d)
              cands[#cands + 1] = { id = id, it = it, gN = gN, gF = gF, gD = gD, gC = gC,
                loud = math.max(gN, gF, gD, gC), closePitch = k.pitch ^ 0.4 * M.doppler(vr),
                dir = vec3(dx, dy, dz) * (1 / math.max(1, d)), pitch = k.pitch * pf * M.doppler(vr), file = k.file,
                dist = d }
            end
          end
        end
      end
    end
  end
  table.sort(cands, function(a, b) return a.loud > b.loud end)
  local want = {}
  for i = 1, math.min(#cands, M.MAX_VOICES) do want[cands[i].id] = cands[i] end
  -- start new voices (a few per frame), set the rest
  local created = 0
  for id, c in pairs(want) do
    local v = voices[id]
    if v and v.file ~= c.file then dropVoice(id); v = nil end
    if not v and created < M.CREATE_PER_FRAME then
      local okN, n = pcall(makeEmitter, M.SOUND_DIR .. 'lat_' .. c.file .. '_near.ogg')
      local okF, f = pcall(makeEmitter, M.SOUND_DIR .. 'lat_' .. c.file .. '_far.ogg')
      local okD, dd, okC, cc = true, nil, true, nil
      if c.file == 'jet' then
        okD, dd = pcall(makeEmitter, M.SOUND_DIR .. 'lat_jet_distant.ogg')
        okC, cc = pcall(makeEmitter, M.SOUND_DIR .. 'lat_jet_close.ogg')
      end
      if okN and okF and okD and okC then
        v = { near = n, far = f, distant = dd, close = cc, file = c.file, gN = 0, gF = 0, gD = 0, gC = 0,
          pitch = c.pitch, cPitch = c.closePitch or 1 }
        voices[id] = v
      else
        if okN then deleteEmitter(n) end
        if okF then deleteEmitter(f) end
        if okD and dd then deleteEmitter(dd) end
        if okC and cc then deleteEmitter(cc) end
      end
      created = created + 1
    end
    if v then
      v.tN, v.tF, v.tD, v.tC, v.dir, v.tPitch, v.dist = c.gN, c.gF, c.gD, c.gC, c.dir, c.pitch, c.dist
      v.tCPitch = c.closePitch
    end
  end
  local vol = ctx.volume or 1
  local k = math.min(1, dt / M.FADE_S)
  for id, v in pairs(voices) do
    if not want[id] then v.tN, v.tF, v.tD, v.tC = 0, 0, 0, 0 end
    v.gN = v.gN + ((v.tN or 0) - v.gN) * k
    v.gF = v.gF + ((v.tF or 0) - v.gF) * k
    v.gD = (v.gD or 0) + ((v.tD or 0) - (v.gD or 0)) * k
    v.gC = (v.gC or 0) + ((v.tC or 0) - (v.gC or 0)) * k
    v.cPitch = (v.cPitch or 1) + ((v.tCPitch or v.cPitch or 1) - (v.cPitch or 1)) * math.min(1, dt / 0.15)
    v.pitch = v.pitch + ((v.tPitch or v.pitch) - v.pitch) * math.min(1, dt / 0.15)
    if not want[id] and v.gN < 0.0005 and v.gF < 0.0005 and (v.gD or 0) < 0.0005 and (v.gC or 0) < 0.0005 then
      dropVoice(id)
    else
      local want_ = v.dir or vec3(0, 0, 1)
      if v.sdir then
        local sd = v.sdir + (want_ - v.sdir) * math.min(1, dt / M.DIR_SMOOTH_S)
        local l = sd:length()
        v.sdir = l > 1e-6 and sd * (1 / l) or want_
      else
        v.sdir = want_
      end
      local p = listener + v.sdir * M.PROXY_M
      -- (the close recording already sounds at full power: it gets the Doppler and the aircraft's size, not the
      -- engine-load pitch the synthesised layers follow)
      for _, pair in ipairs({ { v.near, v.gN, v.pitch }, { v.far, v.gF, v.pitch }, { v.distant, v.gD or 0, v.pitch },
          { v.close, v.gC or 0, v.cPitch or 1 } }) do
        local o = emitterObj(pair[1])
        if o then
          pcall(o.setPosition, o, p)
          pcall(o.setVolumePitchCT, o, math.min(1, pair[2] * vol), pair[3], 0, 0)
        end
      end
    end
  end
end

function M.voices() return voices end
function M.count() local n = 0 for _ in pairs(voices) do n = n + 1 end return n end

return M
