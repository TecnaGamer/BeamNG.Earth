-- BeamNG.Earth GE-side per-frame test driver (docs/V1_4_SPEC.md 3.0, M0). tools/drive.py sends this
-- file through the game's MCP run_lua; there is no mod file. Loading the chunk defines the global
-- BNGE_DRIVE (first uninstalling a driver that a dead harness left behind) and returns 'ok'. Then:
--   BNGE_DRIVE.install(cfgJson)  -> JSON {ok = true, ...} or {error = text}
--   BNGE_DRIVE.poll()            -> JSON: the frames since the last poll, the driver state and one
--                                   mod / perfmon snapshot (the harness calls it at most once a second)
--   BNGE_DRIVE.uninstall()       -> 'uninstalled' (drive.py calls it in a finally block)
--
-- Per frame, from the onPreRender of an extension proxy (lua/common/extensions.lua:1081-1164
-- newExtensionProxy + submitEventSinks, the mechanism flowgraph/manager.lua:222 uses; removed with
-- proxy:destroy(); docs/research/2026-10-10-origin-distance.md section 6):
--   - advance the profile by speed * dtSim (arc length s, profile time t), heading from the profile;
--   - place the player with veh:setClusterPosRelRot(-1, x, y, z, q) (no reset: the probes/p6 rebase
--     move kept 20.0 -> 20.1 m/s), q = the yaw from the heading commanded last frame to this one
--     (a relative, world-frame rotation as tech/techCore.lua:909-912 and rebase.lua use it), and set
--     the velocity with veh:applyClusterVelocityScaleAdd(-1, 0, vx, vy, vz) (scale 0 = replace,
--     spawn.lua:534; funstuff.lua adds with scale 1);
--   - height: ground profiles ray down with castRayStatic from RAY_UP above the last ground (only
--     static collision: the TerrainBlock, never the render-only far tiles) and add the clearance
--     measured at install; a miss is a 'fall' (logged, the last ground height is kept). Flight
--     profiles (points with `alt`, m AGL) fly at the start ground height + alt until M5's
--     pose_reply.groundZ exists;
--   - inputs are zeroed at install and at uninstall; the camera is never touched (it stays on the
--     player);
--   - records the frame: wall ms, flags (FLAG_*), the tile level under the player (the mod's
--     farfield.exactState(), else a key lookup like tools/ball_drive.py).
-- A rebase moves the player itself (rebase.moveVehicles, wrapped while installed): the driver's own
-- state (position, heading, ground) goes through the same transform, and that frame places nothing.
-- A jump of the player that is not a rebase (a teleport, a reset by hand) ends the run ('jump').
-- Error paths: a failed install undoes every step it made; an error in a frame stops the run and
-- parks the car (velocity 0, inputs zero); with no poll for WATCHDOG_MS (the harness died without its
-- uninstall) the driver stops the same way and restores rebase.moveVehicles. The proxy itself is
-- only destroyed by uninstall() (not from inside its own event); loading this chunk again always
-- uninstalls the previous driver, also a half-installed or stopped one.

local D = rawget(_G, 'BNGE_DRIVE')
if type(D) == 'table' and D.uninstall then pcall(D.uninstall) end
D = {}
_G.BNGE_DRIVE = D

local floor, abs, sqrt, min, max = math.floor, math.abs, math.sqrt, math.min, math.max
local sin, cos, atan2, rad, deg = math.sin, math.cos, math.atan2, math.rad, math.deg

D.RAY_UP = 1000          -- m above the last ground height where the ground ray starts
D.RAY_LEN = 3000         -- m of ray (a miss: castRayStatic returns >= the length)
D.MAX_VZ = 30            -- m/s; |vz| clamp of a ground profile (slope changes, rebase frames)
D.JUMP_M = 500           -- m between the placed and the actual position that is not a rebase: stop
D.REBASE_GRACE = 5       -- frames after a rebase in which the actual position may still be the old one
D.RESYNC_DEG = 5         -- the measured heading this far from the commanded one (collision): resync
D.RESYNC_EVERY = 30      -- frames between heading checks
D.MAX_FRAMES = 20000     -- frame records kept between two polls (more: counted as dropped)
D.WATCHDOG_MS = 60000    -- no poll for this long (the harness polls every second): stop and park
D.NO_TILE = 99           -- `under` value of a frame with no shown tile under the player
D.EXACT_LEVEL = -2       -- fallback exact level (v1.3.1 layout) when the mod has no exactState
D.MAX_LEVEL = 9
D.L0 = 256
-- frame flags (bit values)
D.FLAG_HEAVY = 1         -- the mod's governor ran a heavy op this frame (terrain commit, rebase, teleport)
D.FLAG_REBASE = 2        -- rebase phase 1 (the vehicle move) ran this frame
D.FLAG_HARNESS = 4       -- the harness did MCP work in this frame interval (poll)
D.FLAG_FALL = 8          -- no ground under the placed position
D.FLAG_MISS = 16         -- exact miss (mod metric, mesh mode)
D.FLAG_OLD = 32          -- the tile under the player belongs to a previous anchor
D.FLAG_SKIP = 64         -- nothing placed this frame (rebase frame, paused, done)
D.ZERO_INPUTS = "input.event('throttle', 0, 2); input.event('brake', 0, 2); input.event('steering', 0, 2); "
  .. "input.event('clutch', 0, 2); input.event('parkingbrake', 0, 2)"

------------------------------------------------------------------------------------------
-- pure helpers (unit-tested under luajit, mod/tests/test_drive_driver.lua)

-- Heading in degrees (0 = north = +y, 90 = east = +x) of a direction.
function D.headingOf(dx, dy) return deg(atan2(dx, dy)) % 360 end

-- a - b wrapped to (-180, 180].
function D.angleDiff(a, b)
  local d = (a - b) % 360
  if d > 180 then d = d - 360 end
  return d
end

-- BeamNG quaternion of a turn about +z that changes a heading by dh degrees (clockwise from
-- above). BeamNG quaternions rotate by the conjugate of the standard formula (geodesy.lua
-- beamngQuat), so the standard (0, 0, sin(-dh/2), cos(-dh/2)) becomes (0, 0, sin(dh/2), cos(dh/2)).
function D.yawQuat(dh)
  local h = rad(dh) / 2
  return 0, 0, sin(h), cos(h)
end

-- A profile from the config: { key = 't'|'s', points = {{u, speed, heading, alt?, vz?}, ...},
-- flight = any point has alt, total = last u }. points: list of {t|s, speed, heading?, alt?, vz?};
-- a missing heading is `heading0` (the vehicle's heading at install). Returns nil, error on bad input.
function D.makeProfile(points, heading0)
  if type(points) ~= 'table' or #points == 0 then return nil, 'profile has no points' end
  local key = points[1].t ~= nil and 't' or 's'
  local out, flight = {}, false
  for i, p in ipairs(points) do
    local u, v = tonumber(p[key]), tonumber(p.speed)
    if not u then return nil, 'point ' .. i .. ': every point needs ' .. key end
    if not v or v < 0 then return nil, 'point ' .. i .. ': speed must be >= 0' end
    if i > 1 and u <= out[i - 1].u then return nil, 'point ' .. i .. ': ' .. key .. ' must increase' end
    local alt = tonumber(p.alt)
    if alt then flight = true end
    out[i] = { u = u, speed = v, heading = tonumber(p.heading) or heading0 or 0, alt = alt, vz = tonumber(p.vz) }
  end
  return { key = key, points = out, flight = flight, total = out[#out].u, i = 1 }
end

-- Speed, heading, alt, vz of a profile at u (t or s), linear between points (heading along the
-- shorter arc), held after the last point. Keeps a cursor: u only grows during a drive.
function D.profileAt(prof, u)
  local pts = prof.points
  local i = prof.i or 1
  if u < pts[i].u then i = 1 end
  while i < #pts and u >= pts[i + 1].u do i = i + 1 end
  prof.i = i
  local a, b = pts[i], pts[i + 1]
  if not b or u <= a.u then return a.speed, a.heading, a.alt, a.vz end
  local f = (u - a.u) / (b.u - a.u)
  local alt = (a.alt and b.alt) and (a.alt + f * (b.alt - a.alt)) or a.alt
  local vz = (a.vz and b.vz) and (a.vz + f * (b.vz - a.vz)) or a.vz
  return a.speed + f * (b.speed - a.speed), (a.heading + f * D.angleDiff(b.heading, a.heading)) % 360, alt, vz
end

-- Minimal JSON encoder (the GE has jsonEncode; this one is for plain luajit).
local function encode(v)
  local t = type(v)
  if t == 'nil' then return 'null' end
  if t == 'boolean' then return tostring(v) end
  if t == 'number' then
    if v ~= v or v == math.huge or v == -math.huge then return 'null' end
    if v == floor(v) and abs(v) < 1e15 then return string.format('%d', v) end
    return string.format('%.6g', v)
  end
  if t == 'string' then
    return '"' .. v:gsub('[%c"\\]', function(c) return string.format('\\u%04x', c:byte()) end) .. '"'
  end
  local parts = {}
  if #v > 0 or next(v) == nil then
    for i = 1, #v do parts[i] = encode(v[i]) end
    return '[' .. table.concat(parts, ',') .. ']'
  end
  for k, x in pairs(v) do parts[#parts + 1] = encode(tostring(k)) .. ':' .. encode(x) end
  table.sort(parts)
  return '{' .. table.concat(parts, ',') .. '}'
end
D.encode = encode

local function toJson(v)
  local je = rawget(_G, 'jsonEncode')
  if je then return je(v) end
  return encode(v)
end

-- A monotonic wall clock in ms (hptimer in the GE, as the mod's stopwatch.lua; else socket/os).
function D.newClock()
  local hp = rawget(_G, 'hptimer')
  if hp then
    local ok, t = pcall(hp)
    if ok and t and t.stopAndReset then
      local acc = 0
      t:stopAndReset()
      return function() acc = acc + t:stopAndReset(); return acc end
    end
  end
  local okS, socket = pcall(require, 'socket')
  local c = (okS and type(socket) == 'table' and socket.gettime) or os.clock
  local t0 = c()
  return function() return (c() - t0) * 1000 end
end

------------------------------------------------------------------------------------------
-- engine side

local function vehicle()
  local f = rawget(_G, 'getPlayerVehicle')
  return f and f(0)
end

-- Ground height under (x, y) by a static-collision ray from zTop down, or nil.
local function groundAt(x, y, zTop)
  local d = castRayStatic(vec3(x, y, zTop), vec3(0, 0, -1), D.RAY_LEN)
  if d and d < D.RAY_LEN then return zTop - d end
end

local function governor()
  local core = rawget(_G, 'beamngEarth_core')
  return core and core._governor and core._governor()
end

-- Tile level under (x, y), whether it is a previous anchor's, and the exact-miss flag.
local function underState(x, y)
  local ff = package.loaded['beamngEarth/farfield']
  if not ff then return nil, false, false end
  if ff.exactState then return ff.exactState() end
  -- a v1.3.1 mod: current-anchor tiles by key (keys L_i_j, i = floor(x / S)), no miss flag
  if not ff._tile then return nil, false, false end
  for l = D.EXACT_LEVEL, D.MAX_LEVEL do
    local s = D.L0 * 2 ^ l
    local t = ff._tile(l .. '_' .. floor(x / s) .. '_' .. floor(y / s))
    if t and t.shown and not t.failed then return l, false, l ~= D.EXACT_LEVEL end
  end
  return nil, false, true
end

local function newFrames() return { ms = {}, flags = {}, under = {}, n = 0, dropped = 0 } end

-- Velocity 0 and inputs zero (the car stays where it is).
local function park(veh)
  if not veh then return end
  veh:applyClusterVelocityScaleAdd(-1, 0, 0, 0, 0)
  veh:queueLuaCommand(D.ZERO_INPUTS)
end

-- Give rebase.moveVehicles back to the mod (when it is still our wrapper).
local function unwrap()
  local w = D.wrapped
  if w and w.mod.moveVehicles == w.fn then w.mod.moveVehicles = w.orig end
  D.wrapped = nil
end

-- End the run from inside a frame (error, watchdog): park, unwrap; the proxy stays, inert, until
-- uninstall() or the next load of this chunk.
local function stop(reason, err)
  local s = D.state
  if not s or s.done then return end
  s.done, s.reason, s.endMs = true, reason, D.clock() - s.installMs
  if err and not s.error then s.error = err end
  unwrap()
  pcall(park, vehicle())
end

local function record(ms, flags, under)
  local f = D.frames
  if f.n >= D.MAX_FRAMES then f.dropped = f.dropped + 1; return end
  f.n = f.n + 1
  f.ms[f.n] = floor(ms * 100 + 0.5) / 100
  f.flags[f.n] = flags
  f.under[f.n] = under or D.NO_TILE
end

-- Rebase phase 1 (called by the wrapped rebase.moveVehicles, inside the mod's onUpdate, before the
-- tick of the same frame): the driver's frame changes with the world's.
function D.onRebase(xf)
  local s = D.state
  if not s then return end
  local x, y, z = xf:apply(s.x, s.y, s.z)
  local hx, hy = sin(rad(s.cmd)), cos(rad(s.cmd))
  local nx, ny = xf:apply(s.x + hx * 1000, s.y + hy * 1000, s.z)
  local dh = D.angleDiff(D.headingOf(nx - x, ny - y), s.cmd)
  local dz = z - s.z
  s.x, s.y, s.z = x, y, z
  s.ground, s.base = s.ground + dz, s.base + dz
  s.hOff, s.cmd = s.hOff + dh, (s.cmd + dh) % 360
  s.rebases = s.rebases + 1
  s.rebaseFrame, s.skip = s.frame + 1, true -- the tick of this frame comes next
end

-- One frame. dtSim = 0: paused (nothing moves).
-- A velocity physics can still produce for a profile speed: finite, horizontal speed not wildly
-- above the profile's, vertical speed below a hard bound (flight profiles command large climbs).
D.SANE_FACTOR, D.SANE_SLACK, D.SANE_VZ = 3, 30, 1e5
function D.sane(v, speed)
  local x, y, z = v.x, v.y, v.z
  if x ~= x or y ~= y or z ~= z then return false end -- NaN
  if abs(z) >= D.SANE_VZ then return false end           -- also +-inf
  return sqrt(x * x + y * y) < D.SANE_FACTOR * (speed or 0) + D.SANE_SLACK
end

function D.tick(dtReal, dtSim)
  local s = D.state
  if not s then return end
  s.frame = s.frame + 1
  local now = D.clock()
  local ms = now - s.lastTick
  s.lastTick = now
  local flags = 0
  if D.harnessTag then D.harnessTag = false; flags = flags + D.FLAG_HARNESS end
  if not s.done and now - (s.lastPoll or s.installMs) > D.WATCHDOG_MS then stop('watchdog') end
  local gov = governor()
  if gov and gov.isHeavyFrame and gov:isHeavyFrame() then flags = flags + D.FLAG_HEAVY end
  if s.rebaseFrame == s.frame then flags = flags + D.FLAG_REBASE end

  local veh = vehicle()
  dtSim = tonumber(dtSim) or 0
  if s.done or not veh or dtSim <= 0 then
    local lvl, isOld, miss = underState(s.x, s.y)
    if miss then flags = flags + D.FLAG_MISS end
    if isOld then flags = flags + D.FLAG_OLD end
    record(ms, flags + D.FLAG_SKIP, lvl)
    return
  end
  -- a rebase frame: the mod's vehicle move is this frame's placement (the state moved with it)
  local place = not s.skip
  s.skip = false

  -- a jump that is not a rebase: stop (the profile's frame is lost)
  local p = veh:getPosition()
  if place and s.frame - (s.rebaseFrame or -1e9) > D.REBASE_GRACE
    and (p.x - s.x) ^ 2 + (p.y - s.y) ^ 2 > D.JUMP_M * D.JUMP_M then
    s.done, s.reason, s.endMs = true, 'jump', now - s.installMs
    record(ms, flags + D.FLAG_SKIP, nil)
    return
  end

  -- advance along the profile (also in a rebase frame: the car keeps moving at its velocity)
  local prof = s.prof
  local speed, heading, alt, vz = D.profileAt(prof, prof.key == 't' and s.t or s.s)
  local h = (heading + s.hOff) % 360
  local ds = speed * dtSim
  local hx, hy = sin(rad(h)), cos(rad(h))
  s.x, s.y = s.x + hx * ds, s.y + hy * ds
  s.s, s.t = s.s + ds, s.t + dtSim

  if place then
    local z
    if alt then
      z = s.base + alt
      vz = vz or (z - s.z) / dtSim
    else
      local g = groundAt(s.x, s.y, s.ground + D.RAY_UP)
      if g then
        s.ground = g
      else
        s.falls = s.falls + 1
        flags = flags + D.FLAG_FALL
      end
      z = s.ground + s.clearance
      vz = max(-D.MAX_VZ, min(D.MAX_VZ, (z - s.z) / dtSim))
    end
    s.z = z

    -- heading: the turn since last frame's command; now and then, follow the measured heading if
    -- a collision turned the car
    if s.frame % D.RESYNC_EVERY == 0 then
      local d = veh:getDirectionVector()
      if d and (d.x ~= 0 or d.y ~= 0) then
        local mh = D.headingOf(d.x, d.y)
        if abs(D.angleDiff(mh, s.cmd)) > D.RESYNC_DEG then s.cmd, s.resyncs = mh, s.resyncs + 1 end
      end
    end
    -- the speed physics reports (the motion since the last placement) against the profile's
    s.placed = s.placed + 1
    local v = s.placed > 1 and veh:getVelocity()
    -- a physics blow-up (in-game 2026-10-10: the user's lifted Hopper at 67 m/s went unstable and the
    -- engine removed it; velocity read inf and the old driver kept going): stop at once
    if v and not D.sane(v, speed) then
      s.done, s.reason, s.endMs = true, 'unstable', now - s.installMs
      s.error = s.error or string.format('vehicle unstable: v = (%s, %s, %s)', tostring(v.x), tostring(v.y), tostring(v.z))
      unwrap()
      record(ms, flags + D.FLAG_SKIP, nil)
      return
    end
    if v then
      s.vSum, s.vProfSum, s.vN = s.vSum + sqrt(v.x * v.x + v.y * v.y), s.vProfSum + s.lastSpeed, s.vN + 1
    end
    s.lastSpeed = speed
    local qx, qy, qz, qw = D.yawQuat(D.angleDiff(h, s.cmd))
    s.cmd = h
    veh:setClusterPosRelRot(-1, s.x, s.y, z, qx, qy, qz, qw)
    veh:applyClusterVelocityScaleAdd(-1, 0, hx * speed, hy * speed, vz)
  else
    flags = flags + D.FLAG_SKIP
  end

  local lvl, isOld, miss = underState(p.x, p.y)
  if miss then flags = flags + D.FLAG_MISS end
  if isOld then flags = flags + D.FLAG_OLD end
  record(ms, flags, lvl)

  local limit = s.km and s.km * 1000 or nil
  if (limit and s.s >= limit) or (not limit and (prof.key == 't' and s.t or s.s) >= prof.total) then
    s.done, s.reason, s.endMs = true, 'end', now - s.installMs
    veh:applyClusterVelocityScaleAdd(-1, 0, 0, 0, 0) -- park: the harness uninstalls within a second
  end
end

------------------------------------------------------------------------------------------
-- install / poll / uninstall

local function fail(text) return toJson({ error = text }) end

-- cfg (JSON text or a table): { points = {...} (see makeProfile), km = total km (optional: else the
-- profile's end), clearance = m (optional: measured), exactLevel = n (optional, v1.3.1 mod only) }
function D.install(cfg)
  if D.installed then return fail('already installed') end
  if type(cfg) == 'string' then cfg = jsonDecode(cfg) end
  local veh = vehicle()
  if not veh then return fail('no player vehicle') end
  if not rawget(_G, 'newExtensionProxy') then return fail('newExtensionProxy missing') end
  local p = veh:getPosition()
  local g = groundAt(p.x, p.y, p.z + D.RAY_UP)
  if not g then return fail('no ground under the player') end
  local d = veh:getDirectionVector()
  local h0 = D.headingOf(d.x, d.y)
  local prof, err = D.makeProfile(cfg.points, h0)
  if not prof then return fail(err) end
  D.EXACT_LEVEL = tonumber(cfg.exactLevel) or D.EXACT_LEVEL
  D.clock = D.clock or D.newClock()
  D.frames = newFrames()
  D.harnessTag = true -- the rest of this MCP call (encoding, the HTTP answer) lands in the first frame
  D.state = {
    prof = prof, km = tonumber(cfg.km), x = p.x, y = p.y, z = p.z, s = 0, t = 0,
    ground = g, base = g, clearance = tonumber(cfg.clearance) or max(0.05, min(5, p.z - g)),
    cmd = h0, hOff = 0, frame = 0, lastTick = D.clock(), installMs = D.clock(), lastPoll = D.clock(),
    falls = 0, resets = 0, rebases = 0, resyncs = 0, vSum = 0, vProfSum = 0, vN = 0, placed = 0, lastSpeed = 0,
    vehId = veh:getID(), done = false, reason = nil, skip = false, rebaseFrame = nil,
  }
  -- from here on uninstall() undoes every step, also when one of them fails
  D.installed = true
  local okI, errI = pcall(function()
    veh:queueLuaCommand(D.ZERO_INPUTS)

    -- the mod's rebase moves the player: follow it (restored at uninstall)
    local rb = package.loaded['beamngEarth/rebase']
    if rb and rb.moveVehicles then
      local orig = rb.moveVehicles
      D.wrapped = { mod = rb, orig = orig }
      D.wrapped.fn = function(xf, ...)
        local moved, playerId = orig(xf, ...)
        local ok, e = pcall(D.onRebase, xf)
        if not ok and D.state then D.state.error = 'onRebase: ' .. tostring(e) end
        return moved, playerId
      end
      rb.moveVehicles = D.wrapped.fn
    end

    local sink = {}
    function sink:onPreRender(dtReal, dtSim)
      local ok, e = pcall(D.tick, dtReal, dtSim)
      if not ok then pcall(stop, 'error', tostring(e)) end
    end
    function sink:onVehicleResetted(vid)
      if D.state and vid == D.state.vehId then D.state.resets = D.state.resets + 1 end
    end
    D.proxy = newExtensionProxy(nil, 'beamngEarthDrive')
    D.proxy:submitEventSinks({ sink }, {})
  end)
  if not okI then
    pcall(D.uninstall)
    return fail('install: ' .. tostring(errI))
  end
  return toJson({ ok = true, x = p.x, y = p.y, z = p.z, ground = g, clearance = D.state.clearance, heading = h0,
    key = prof.key, total = prof.total, flight = prof.flight, points = #prof.points })
end

-- The poll as a table (poll() encodes it). Tags the frame interval that will contain this MCP work.
function D.pollTable()
  D.harnessTag = true
  local pm = rawget(_G, 'beamngEarth_perfmon')
  if pm and pm.tagHarness then pm.tagHarness() end
  local s = D.state or {}
  if D.clock then s.lastPoll = D.clock() end
  local f = D.frames or newFrames()
  D.frames = newFrames()
  local out = {
    wallMs = D.clock and s.installMs and (D.clock() - s.installMs) or 0,
    frames = { ms = f.ms, flags = f.flags, under = f.under, dropped = f.dropped },
    drive = { s = s.s, t = s.t, x = s.x, y = s.y, z = s.z, heading = s.cmd, done = s.done, reason = s.reason,
      error = s.error, endMs = s.endMs, falls = s.falls, resets = s.resets, rebases = s.rebases, resyncs = s.resyncs,
      vSum = s.vSum, vProfSum = s.vProfSum, vN = s.vN },
  }
  local core = rawget(_G, 'beamngEarth_core')
  if core and core.status then
    local ok, st = pcall(function() return loadstring('return ' .. core.status())() end)
    if ok then out.mod = st else out.modError = tostring(st) end
  end
  if pm and pm.snapshot then out.perf = pm.snapshot() end
  if core and core.hitches then out.hitches = core.hitches() end   -- what each frame over 50 ms went to (core.lua)
  local okLimit, limit = pcall(function() return Engine.getCurrentFpsLimitReason() end)
  if okLimit and type(limit) == 'table' then out.fpsLimit = limit end
  return out
end

function D.poll() return toJson(D.pollTable()) end

function D.uninstall()
  if D.proxy then pcall(function() D.proxy:destroy() end) end
  D.proxy = nil
  unwrap()
  local veh = vehicle()
  if veh and D.installed then
    pcall(function()
      -- park on the ground under the car, at rest, inputs zero
      local p = veh:getPosition()
      local g = groundAt(p.x, p.y, p.z + D.RAY_UP)
      if g and D.state then veh:setClusterPosRelRot(-1, p.x, p.y, g + D.state.clearance, 0, 0, 0, 1) end
      park(veh)
    end)
  end
  D.installed = false
  return 'uninstalled'
end

return 'ok'
