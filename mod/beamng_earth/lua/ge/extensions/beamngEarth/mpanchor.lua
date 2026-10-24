-- BeamMP co-op: remote players' vehicle positions made correct across independent floating origins. From Thai-v4's
-- PR #88 (the conversion and the hook are theirs); in BeamNG.Earth's BeamMP support it gets the anchors from the
-- server plugin's bngeAnchors (mpsession.lua) instead of its own relay.
--
-- Every player rebases on their own schedule (core.lua, rebase.lua); BeamMP's position sync assumes every client's
-- Lua-space vec3 means the same real-world point. Each player's anchor is its real-world origin (lat, lon), and any
-- anchor converts into any other with geodesy.Anchor:transformTo (exact, doubles; rebase.lua's own transform). When
-- a position packet for a remote vehicle arrives and its owner's anchor differs from ours, the packet (position,
-- velocity, orientation, angular velocity) is transformed into our frame before BeamMP's own code (prediction,
-- smoothing, the teleport-distance correction) sees it. The transform is cached per remote player, rebuilt when
-- either anchor changes.
--
-- Hook point: positionGE.handle(rawData), the single entry point for every incoming position packet whatever the
-- position-smoother setting (positionGE.lua: handle() branches to smoothPosExec or applyPos, both fed from the same
-- raw "p:<serverVehicleID>:{json}" string). Wrapped once here, at run time; BeamMP's files are not changed.

local geodesy = require('beamngEarth/geodesy')

local M = {}

-- A remote car whose position lands farther than this from our origin is not placed: our world is not there, and
-- thousands of km out the game's physics go non-finite and the game deletes the car (BeamMP then draws its
-- placeholder sphere; the user, 2026-10-22: one player teleported across the world, then the other, and each saw the
-- other's car as a sphere). Its packets are dropped (the car stays where it was) and mpsession hides it.
M.FAR_M = 30000

local myLat, myLon = nil, nil            -- our own current anchor (core.lua: announceOwn)
local remote = {}                        -- [playerId] = { lat, lon }, the plugin's latest (setRemote)
M.ownChanged = false                     -- our anchor changed since mpsession last sent a pose (it sends one at once)
local xfCache = {}                       -- [playerId] = { myLat, myLon, rLat, rLon, xf } (lazy, see transformFor)
local installed = false
local far = {}                           -- [serverVehicleString] = true: its last position was beyond FAR_M (isFar)
local wasFar = {}                        -- [serverVehicleString] = true: it was beyond FAR_M at some time (wasFar)
local lastPos = {}                       -- [serverVehicleString] = { x, y, z }: its latest position placed (lastPos)

local function isMPSession()
  return MPCoreNetwork ~= nil and MPCoreNetwork.isMPSession ~= nil and MPCoreNetwork.isMPSession()
end

-- Called by core.lua right after st.anchor is set (commitTeleport, finishRebase): every anchor
-- change, including the player's very first one. A no-op outside a BeamMP session.
-- core.lua: our anchor is now (lat, lon) (a teleport's or a rebase's commit).
function M.announceOwn(lat, lon)
  if lat ~= myLat or lon ~= myLon then M.ownChanged = true end
  myLat, myLon = lat, lon
end

-- A player's position packets name no anchor: those it sent from its old place can arrive after the plugin told us
-- its new anchor, and converted with the new one they put the car at the new place at the old one's height (a
-- three-player test, 2026-10-24: a car back from a teleport across the world arrived at z -80, ~1.2 km under the
-- ground, and its physics blew up: two instabilities, on Windows, a second after the teleport). For SETTLE_S after a
-- player's anchor changes its packets are dropped (the car keeps its last good position).
-- The other order (packets from the new place converted with the old anchor) shows as a jump: a car more than JUMP_M
-- from where its packet of less than JUMP_WINDOW_S ago put it starts the same wait; after it the next packet is
-- taken as the car's place.
M.SETTLE_S = 1.5
M.JUMP_M = 2000
M.JUMP_WINDOW_S = 2
local settleUntil = {}                   -- [playerId] = os.clock() until which its packets are dropped
local lastAt = {}                        -- [serverVehicleString] = os.clock() of its latest packet let through
M.clock = os.clock                       -- (tests)

-- mpsession.lua: player pid's anchor, from the plugin's bngeAnchors.
function M.setRemote(pid, lat, lon)
  pid, lat, lon = tonumber(pid), tonumber(lat), tonumber(lon)
  if not pid or not lat or not lon then return end
  local old = remote[pid]
  if old and (old.lat ~= lat or old.lon ~= lon) then
    settleUntil[pid] = M.clock() + M.SETTLE_S
  end
  remote[pid] = { lat = lat, lon = lon }
end

-- Do we know player pid's anchor (its cars' positions can be converted)?
function M.knows(pid) return remote[tonumber(pid)] ~= nil end

-- Was this remote car's last position (BeamMP's serverVehicleString "pid-vid") beyond FAR_M?
function M.isFar(sid) return far[sid] == true end
-- Was this remote car ever beyond FAR_M (where the game's physics break and the game deletes it)? (mpsession's restore)
function M.wasFar(sid) return wasFar[sid] == true end
function M._noteFar(sid) wasFar[sid] = true end       -- (tests)
-- The latest position (our frame) a packet of this remote car was let through with: x, y, z (nil: none yet). Where a car
-- hidden while far is coming back to (mpsession waits for the ground there).
function M.lastPos(sid)
  local p = lastPos[sid]
  if p then return p[1], p[2], p[3] end
end

-- (the session's end) the remote anchors forgotten
function M.reset() remote, xfCache, far, wasFar, lastPos, settleUntil, lastAt = {}, {}, {}, {}, {}, {}, {} end

-- A log line once per car and kind every NOTE_S (the session's log tells what happened to a car before an instability)
M.NOTE_S = 10
local noted = {}
local function note(sid, kind, text)
  local k = tostring(sid) .. ':' .. kind
  local now = os.clock()
  if noted[k] and now - noted[k] < M.NOTE_S then return end
  noted[k] = now
  log('I', 'beamngEarth', text)
end

local function transformFor(pid)
  if not myLat then return nil end
  local r = remote[pid]
  if not r then return nil end
  local c = xfCache[pid]
  if c and c.myLat == myLat and c.myLon == myLon and c.rLat == r.lat and c.rLon == r.lon then
    return c.xf
  end
  local theirs, ours = geodesy.newAnchor(r.lat, r.lon), geodesy.newAnchor(myLat, myLon)
  local xf = theirs:transformTo(ours)
  xfCache[pid] = { myLat = myLat, myLon = myLon, rLat = r.lat, rLon = r.lon, xf = xf }
  return xf
end

-- Transform one decoded position packet's pos/vel/rot/rvel in place: pos is a world point
-- (xf:apply), vel and rvel are directions/rates (xf:rotate, no translation), rot is the vehicle's
-- orientation quaternion, composed with the frame's own rotation the same way rebase.lua's
-- updateResetState composes a vehicle's orientation across a rebase ("vehicle's rotation first,
-- then R"): newRot = quatMul(rot, xf:beamngQuat()).
local function applyTransform(decoded, xf)
  if decoded.pos then
    decoded.pos[1], decoded.pos[2], decoded.pos[3] = xf:apply(decoded.pos[1], decoded.pos[2], decoded.pos[3])
  end
  if decoded.vel then
    decoded.vel[1], decoded.vel[2], decoded.vel[3] = xf:rotate(decoded.vel[1], decoded.vel[2], decoded.vel[3])
  end
  if decoded.rvel then
    decoded.rvel[1], decoded.rvel[2], decoded.rvel[3] = xf:rotate(decoded.rvel[1], decoded.rvel[2], decoded.rvel[3])
  end
  if decoded.rot then
    local bx, by, bz, bw = xf:beamngQuat()
    decoded.rot[1], decoded.rot[2], decoded.rot[3], decoded.rot[4] =
      geodesy.quatMul(decoded.rot[1], decoded.rot[2], decoded.rot[3], decoded.rot[4], bx, by, bz, bw)
  end
end

-- A position packet BeamMP's smoothing must not see: a non-finite number anywhere (after a player's teleport across
-- the world, 2026-10-24: "velocityVE.setAngularVelocity(nan, nan, nan, ...)" on another player's game, then the car's
-- physics blew up: an instability, which BeamMP turns into the car switched off (more than 3 in 4 s) or deleted (more
-- than 10)). Dropped: the car keeps its last good position. A speed over MAX_SPEED (a teleport's artefact, no car is
-- that fast) keeps the position but its velocities are zeroed, so BeamMP's prediction does not fling the car.
M.MAX_SPEED = 2000          -- m/s
local function finite(v) return type(v) == 'number' and v == v and v > -math.huge and v < math.huge end
function M.sanitize(decoded)
  for _, k in ipairs({ 'pos', 'vel', 'rvel', 'rot' }) do
    local t = decoded[k]
    if t ~= nil then
      if type(t) ~= 'table' then return false end
      for i = 1, (k == 'rot' and 4 or 3) do
        if t[i] ~= nil and not finite(t[i]) then return false end
      end
    end
  end
  local v = decoded.vel
  if type(v) == 'table' and (v[1] or 0) ^ 2 + (v[2] or 0) ^ 2 + (v[3] or 0) ^ 2 > M.MAX_SPEED ^ 2 then
    v[1], v[2], v[3] = 0, 0, 0
    if type(decoded.rvel) == 'table' then decoded.rvel[1], decoded.rvel[2], decoded.rvel[3] = 0, 0, 0 end
    return true, true
  end
  return true, false
end

-- Wraps positionGE.handle: corrects a 'p' (position) packet's embedded JSON before handing the
-- (possibly rewritten) raw string to the original handle, which then runs completely unmodified.
local function wrapHandle(original)
  return function(rawData)
    local code, serverVehicleID, data = string.match(rawData, '^(%a)%:(%d+%-%d+)%:({.*})')
    if code ~= 'p' or not serverVehicleID then return original(rawData) end
    local pid = tonumber(serverVehicleID:match('^(%d+)%-'))
    local now = M.clock()
    if pid and settleUntil[pid] then
      if now < settleUntil[pid] then
        note(serverVehicleID, 'settle', 'BeamMP: car ' .. serverVehicleID .. '\'s packets dropped for ' .. M.SETTLE_S
          .. ' s: its owner\'s anchor just changed (or the car jumped)')
        return
      end
      settleUntil[pid] = nil
      for sid in pairs(lastAt) do
        if tonumber(sid:match('^(%d+)%-')) == pid then lastAt[sid] = nil end   -- (the next packet: the car's place)
      end
    end
    local xf = pid and transformFor(pid) or nil
    local ok, decoded = pcall(jsonDecode, data)
    if not ok or type(decoded) ~= 'table' then return original(rawData) end
    if not xf then                          -- (no conversion: the packet as sent, if it is sane)
      local sane, changed = M.sanitize(decoded)
      if not sane then return end
      if type(decoded.pos) == 'table' then lastPos[serverVehicleID] = decoded.pos end
      if not changed then return original(rawData) end
      local okE, enc = pcall(jsonEncode, decoded)
      return original(okE and (code .. ':' .. serverVehicleID .. ':' .. enc) or rawData)
    end
    applyTransform(decoded, xf)
    local sane, changed = M.sanitize(decoded)
    if not sane then
      note(serverVehicleID, 'insane', 'BeamMP: a position packet of car ' .. serverVehicleID .. ' with a non-finite number dropped')
      return
    end
    if changed then
      note(serverVehicleID, 'fast', 'BeamMP: car ' .. serverVehicleID .. ' reported over MAX_SPEED: its speeds zeroed')
    end
    local p = decoded.pos
    if p and tonumber(p[1]) and tonumber(p[2]) and p[1] * p[1] + p[2] * p[2] > M.FAR_M * M.FAR_M then
      if not far[serverVehicleID] then
        log('I', 'beamngEarth', string.format('BeamMP: car %s far (%.0f km): its packets dropped', serverVehicleID,
          math.sqrt(p[1] * p[1] + p[2] * p[2]) / 1000))
      end
      far[serverVehicleID], wasFar[serverVehicleID] = true, true
      return
    end
    local lp = lastPos[serverVehicleID]
    if p and lp and lastAt[serverVehicleID] and now - lastAt[serverVehicleID] < M.JUMP_WINDOW_S and pid
        and (p[1] - lp[1]) ^ 2 + (p[2] - lp[2]) ^ 2 > M.JUMP_M * M.JUMP_M then
      settleUntil[pid] = now + M.SETTLE_S
      note(serverVehicleID, 'jump', string.format('BeamMP: car %s jumped %.0f m between packets: its owner\'s packets '
        .. 'dropped for %.1f s', serverVehicleID, math.sqrt((p[1] - lp[1]) ^ 2 + (p[2] - lp[2]) ^ 2), M.SETTLE_S))
      return
    end
    if far[serverVehicleID] then
      log('I', 'beamngEarth', string.format('BeamMP: car %s near again at %.0f, %.0f, %.0f (speed %.1f m/s)',
        serverVehicleID, p and p[1] or 0, p and p[2] or 0, p and p[3] or 0, decoded.vel and math.sqrt((decoded.vel[1] or 0) ^ 2
        + (decoded.vel[2] or 0) ^ 2 + (decoded.vel[3] or 0) ^ 2) or -1))
    end
    far[serverVehicleID] = nil
    if p then lastPos[serverVehicleID], lastAt[serverVehicleID] = p, now end
    local ok2, reencoded = pcall(jsonEncode, decoded)
    if not ok2 then return original(rawData) end
    return original(code .. ':' .. serverVehicleID .. ':' .. reencoded)
  end
end

-- Installs the hooks once positionGE and the BeamMP event globals exist. BeamNG's extension loader
-- exposes every loaded GE extension as a bare global of its module name too (BeamMP's own code
-- calls it the same way, e.g. MPGameNetwork.lua: "positionGE.handle(params)"), so this patches the
-- same table everyone else calls through, not a local copy.
-- Safe to call repeatedly (every onUpdate, see core.lua): a no-op after the first success, and a
-- no-op entirely in singleplayer (positionGE never loads there).
function M.install()
  if installed then return end
  local pg = rawget(_G, 'positionGE')
  if pg == nil or type(pg.handle) ~= 'function' then return end
  pg.handle = wrapHandle(pg.handle)
  installed = true
  log('I', 'beamngEarth', 'mpanchor: installed (BeamMP cross-anchor position fixups active)')
end

-- Console diagnostic (beamngEarth_core.mpStatus(), see core.lua): whether the hook installed, our
-- own last-announced anchor, and every peer anchor we have heard about so far. An empty peer list
-- after both players have spawned and moved means the announce/relay chain is broken somewhere
-- (not installed, not in an MP session, the server plugin not loaded, or not yet relayed).
function M.status()
  local parts = {
    'installed=' .. tostring(installed),
    'isMPSession=' .. tostring(isMPSession()),
    'myAnchor=' .. tostring(myLat) .. ',' .. tostring(myLon),
  }
  local n = 0
  for pid, r in pairs(remote) do
    n = n + 1
    parts[#parts + 1] = string.format('peer#%s=%.5f,%.5f', tostring(pid), r.lat, r.lon)
  end
  parts[#parts + 1] = 'knownPeers=' .. n
  return table.concat(parts, ' | ')
end

return M
