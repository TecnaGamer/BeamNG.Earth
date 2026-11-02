-- BeamMP sessions, with BeamNG.Earth's server plugin (beammp/Server/BeamNGEarth/main.lua; the user, 2026-10-22:
-- BeamMP support). BeamMP sends every car's position in its driver's world coordinates, metres from the driver's own
-- anchor; mpanchor.lua (Thai-v4's PR #88) converts the other players' positions into ours, given their anchors. Each
-- POSE_S (and at once when our anchor changes) this sends the plugin our anchor and place (bngePose); it answers with
--   - bngeAnchors { pid -> { aLat, aLon, lat, lon, name, h } }: every player's anchor (to mpanchor), place, name and
--     heading (others(): the world map's arrows); the cars of a
--     player whose anchor is not known (no BeamNG.Earth, or not heard yet) are deactivated here: they would stand in
--     the wrong place;
--   - bngeGoTo { lat, lon }: on joining, a teleport to where the others are.
-- Damage: BeamMP sends parts that break off (break groups) but not dents; its own full-damage message (nodesVE.getNodes:
-- every node and beam, applied on the other players' copies by its nodesGE) is never sent. This sends our own cars'
-- damage on BeamMP's full-damage path (nodesGE.sendNodes), as only the beams broken or bent (DAMAGE_VE: tens of KB for
-- a wreck instead of ~200 KB), when another player comes within DAMAGE_NEAR_M (they see the car as it is) and
-- DAMAGE_SETTLE_S after our car's damage changed by DAMAGE_STEP while one is near.
-- Shared generation (the user, 2026-10-22: "3 players in Utah and 1 in Italy ... the Utah players split generation
-- between each other"): players near each other converge onto one anchor (pickAnchor: within ADOPT_M, the one most of
-- them use; a teleport next to them lands on it, core.teleportTo / adoptAnchor; otherwise a smooth rebaseTo), and
-- our world server serves its group (the "share" message, on the port the plugin's bngeConfig names: its port and code
-- go out in our poses); the plugin sends
-- each player the others on its anchor (bngePeers), handed to the world server ("peers"), which splits the far tiles
-- among them (server/peerwork.py). Commented out below (2026-10-27; off since 2026-10-23, the user: not coming back for
-- a while): each player builds their own world; the cars still show in the right place (mpanchor converts positions).
-- BeamMP's files are not changed: its client events, its own functions, and mpanchor's wrap of positionGE.handle.
local M = {}

M.PROTO = 2                 -- as the plugin's G.PROTO
-- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
-- -- Shared generation (below) on? Off for now (the user, 2026-10-23: "disable the shared gen. think is causing problems"):
-- -- no group service (no "share", so no port in our poses and no bngePeers for us), no converging onto the others'
-- -- anchor, no landing on it after a teleport. Each player builds their own world; the cars still show in the right
-- -- place (mpanchor converts positions between anchors).
-- M.SHARE_GEN = false
M.POSE_S = 2
M.HIDE_S = 0.5
-- A car switched off here (far, or its owner's anchor unknown) comes back on only over ground (terrainpatch.groundReady
-- at its latest position): switched on over none, it fell, the ground came in around it and its physics blew up
-- (instabilities, which BeamMP turns into the car switched off or deleted; a Windows test PC, 2026-10-24, the other
-- player back from a teleport across the world while this one had driven 3 km off). Its spot is a collision focus
-- meanwhile (groundFoci), so the ground gets made; on after GROUND_WAIT_S whatever.
M.GROUND_WAIT_S = 20
-- Other players' cars off our terrain block float: gravity off, and no collision made round them (collisionfoci skips
-- them: no solid roads, trees or terrain patches). Each player drives their own car; a copy off our block only has to
-- follow it, and on terrain patches it could not keep up: in a three-player test (2026-10-24) a car driven at 104-120
-- mph 3 miles from the others outran its patches on their screens and broke, 187-234 broken beams there against 46 on
-- its own. Off the block when FLOAT_OUT_M from its edge or beyond, on it again FLOAT_IN_M inside (gravity back).
M.FLOAT_OUT_M = 32
M.FLOAT_IN_M = 96
M.GOTO_OFFSET_M = 12        -- a joining player lands this far from the one it goes to (not on top of it)
M.DAMAGE_STEP = 50          -- (the game's damage figure, map.objects[id].damage)
M.DAMAGE_SETTLE_S = 2
M.DAMAGE_NEAR_M = 2000      -- a player this near sees our cars' damage (sendDamage)
M.STATUS_S = 5              -- the session's state to the log and STATUS_FILE this often
M.STATUS_FILE = '/temp/beamngEarth/mpsession_status.json'
-- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
-- M.CONFIG_WAIT_S = 10        -- the server's settings waited for this long before sharing without them (an older plugin
--                             -- sends none)
-- M.ADOPT_M = 7500            -- another player's anchor this near is ours to share (half the rebase distance: no loop)
-- M.CONVERGE_S = 5            -- the anchor checked this often (converge)
-- M.SETTLE_S = 10             -- ... and not within this of our own anchor's last change

local handlers = false
local anchors = {}          -- tostring(server player id) -> { aLat, aLon, lat, lon } (bngeAnchors)
local deactivated = {}      -- game vehicle id -> true: deactivated here
local waitGround = {}       -- game vehicle id -> { x, y, since }: to come back on, waiting for its ground
local floating = {}         -- game vehicle id -> true: floating here (off our block, gravity off)
local poseIn, hideIn = 0, 0
local joined = false        -- BeamMP joined and no pose sent yet: the first pose asks for the meeting place
local wentTo = false        -- the session's one bngeGoTo done
local camPlaced = false     -- the free camera put on the ground once (BeamMP joins without a car)
local lastPlayers = -1      -- (the log: the player count as last logged)
local stats = { anchors = 0, go = 0, poses = 0, converged = 0, peers = 0 }
-- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
-- local share = nil           -- { port, code } of our world server's group service (onShare); false: asked
-- local config = nil          -- the server's settings (bngeConfig { sharePort }), nil until they come
-- local configWait = 0        -- s waited for them
-- local convergeIn = 0
-- local anchorSince = 0       -- seconds our anchor has been the same (converge's settle)
-- local lastAnchorKey = nil
local statusIn = 5
local members = {}          -- tostring(pid) -> true: the other players within DAMAGE_NEAR_M at the last check
local sentDamage = {}       -- own game vehicle id -> the damage its state was last sent at
local damageSince = {}      -- own game vehicle id -> seconds its damage has differed from sentDamage

function M.key(aLat, aLon) return string.format('%.6f,%.6f', aLat, aLon) end

-- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
-- -- Pure: the anchor a player at (lat, lon) should share: of the players' anchors (bngeAnchors' table) within ADOPT_M of
-- -- it, the one the most of them use, ties to the lowest key; skip: a player id left out (ourselves, for a teleport).
-- -- -> aLat, aLon, players on it (nil: none in reach)
-- function M.pickAnchor(list, lat, lon, skip)
--   local count, ll = {}, {}
--   for pid, a in pairs(list) do
--     local aLat, aLon = type(a) == 'table' and tonumber(a.aLat), type(a) == 'table' and tonumber(a.aLon)
--     if pid ~= skip and aLat and aLon then
--       local k = M.key(aLat, aLon)
--       count[k], ll[k] = (count[k] or 0) + 1, { aLat, aLon }
--     end
--   end
--   local best, bestN = nil, 0
--   for k, n in pairs(count) do
--     if M.dist(lat, lon, ll[k][1], ll[k][2]) <= M.ADOPT_M and (n > bestN or (n == bestN and k < best)) then
--       best, bestN = k, n
--     end
--   end
--   if best then return ll[best][1], ll[best][2], bestN end
--   return nil
-- end

-- Pure: the remote vehicles to deactivate: owners { game vehicle id -> owner's server player id }, known (owner's id
-- -> true when its anchor is known). -> { game vehicle id -> true }
function M.toHide(owners, known)
  local out = {}
  for gid, owner in pairs(owners) do
    if not known(owner) then out[gid] = true end
  end
  return out
end

-- Metres between two latitude / longitude points (equirectangular: a few km).
function M.dist(lat1, lon1, lat2, lon2)
  local k = math.pi / 180
  local x = (lon2 - lon1) * k * math.cos((lat1 + lat2) / 2 * k)
  return 6371000 * math.sqrt(x * x + ((lat2 - lat1) * k) ^ 2)
end

-- Pure: should our car's full damage go out now? newcomer: a player came near; damage, sent: now and at the last
-- send (nil: never); settled: seconds it has differed. -> send, settled (updated)
function M.damageDue(newcomer, damage, sent, settled, dt)
  -- (a newcomer gets our car's full state only when it has damage to show: an undamaged car is the one BeamMP spawned
  -- there already, and every join sent every nearby player's every car to every client, 2026-10-23: "the game freezes
  -- when players join")
  -- (... or it changed since our last send: a car reset while no one was near would otherwise stay wrecked on the
  -- copies of the players who saw it last, 2026-10-24)
  if newcomer then
    return (damage or 0) >= M.DAMAGE_STEP or (sent ~= nil and math.abs((damage or 0) - sent) >= M.DAMAGE_STEP), 0
  end
  if not damage or math.abs(damage - (sent or 0)) < M.DAMAGE_STEP then return false, 0 end
  settled = (settled or 0) + dt
  return settled >= M.DAMAGE_SETTLE_S, settled
end

local function mp() return rawget(_G, 'MPVehicleGE') end
local function core() return rawget(_G, 'beamngEarth_core') end
local remoteOwners          -- (below; the test switch uses it)

-- A vehicle by its game id, as the game's vehicle class (setActive, queueLuaCommand; scenetree.findObjectById's
-- object has no setActive in the native Linux build: the session stopped at the first car to hide)
local function vehicle(gid)
  local get = rawget(_G, 'getObjectByID')
  local o = get and get(gid)
  if not o and be and be.getObjectByID then o = be:getObjectByID(gid) end
  o = o or scenetree.findObjectById(gid)
  return o and o.setActive and o or nil         -- (vehicles only)
end

-- BeamMP's name tag of a car switched off here hidden with it (BeamMP draws the tags of every car it knows, switched
-- off or not: a "guest" tag floated over an empty road, Discord 2026-10-24). Its own per-car flag, hideNametag
-- (MPVehicleGE's tag loop skips it), on its own record of the car: no file of BeamMP's changed.
function M.nametag(gid, hidden)
  local mp = rawget(_G, 'MPVehicleGE')
  local ok, v = pcall(function() return mp and mp.getVehicleByGameID and mp.getVehicleByGameID(gid) end)
  if ok and type(v) == 'table' then v.hideNametag = hidden or nil end
end

-- Test switch (two BeamMP players, .claude/work/mp_two.sh): the game's start arguments -bngetest car,damage
-- spawn a car TEST_CAR_S after the first pose with none, damage it TEST_DAMAGE_S after that; the status file then
-- lists the other players' cars' broken beams (the damage they show here). 'switch': TEST_SWITCH_S after the car,
-- into another player's car (the game's vehicle switch) for TEST_SWITCH_HOLD_S, then back; 'perf': the frame times
-- (perfmon.lua) since the last status in the status file. 'away': TEST_AWAY_S after the car, a teleport 130 degrees
-- of longitude away (about California to Italy) for TEST_AWAY_HOLD_S, then back (a player across the world and back:
-- the other's copy of our car must survive). 'drive': TEST_DRIVE_S after the car, the game's AI drives it (traffic
-- mode) for the frame times while the world streams. 'goto' (with -bngegoto LAT,LON): TEST_GOTO_S after the first
-- pose, a teleport there (a fresh place: the shared generation's timing); -bngegotoat UNIXTIME: at that time instead
-- (two machines arriving together). 'map': TEST_MAP_S after the first pose, the world map opened (the other players'
-- arrows, for a screenshot).
M.TEST_CAR_S, M.TEST_DAMAGE_S = 5, 25
M.TEST_SWITCH_S, M.TEST_SWITCH_HOLD_S = 40, 30
M.TEST_AWAY_S, M.TEST_AWAY_HOLD_S = 40, 60
M.TEST_DRIVE_S = 20
M.TEST_GOTO_S = 3
M.TEST_MAP_S = 30
local test = {}
do
  local ok, args = pcall(function() return Engine.getStartingArgs() end)
  if ok and type(args) == 'table' then
    for i, a in ipairs(args) do
      if a == '-bngetest' and type(args[i + 1]) == 'string' then
        for w in args[i + 1]:gmatch('[^,]+') do test[w] = true end
      end
      if a == '-bngegoto' and type(args[i + 1]) == 'string' then
        local la, lo = args[i + 1]:match('^([%-%d%.]+),([%-%d%.]+)$')
        test.gotoLat, test.gotoLon = tonumber(la), tonumber(lo)
      end
      if a == '-bngegotoat' then test.gotoAt = tonumber(args[i + 1]) end
    end
  end
end
local testClock, testCarAt, testDamaged = 0, nil, false
local testOwnId = nil       -- (test) our test car's game id
local anchorRef = nil       -- (test) the anchor of the latest update
local switchLog = {}        -- (test 'switch') { { t, what, active, ownThere } }
local awayLog = {}          -- (test 'away') { { t, what, lat, lon } }
local lateGoTo = nil         -- (test 'late': the go-to held until TEST_LATE_S)
M.TEST_LATE_S = 45
M.TEST_DELREMOTE_S = 40
M.TEST_DROP_S, M.TEST_DROP_M = 40, 2500
M.TEST_LEAVE_S = 40         -- ('leave') after the car, the server left as with BeamMP's Leave button
local brokenOf = {}         -- (test) remote game vehicle id -> broken beams

-- (test) a remote car's broken beams, from its vehicle Lua
local deformedOf = {}
function M.onBroken(gid, n, d) brokenOf[tonumber(gid)] = tonumber(n); deformedOf[tonumber(gid)] = tonumber(d) end

local onGoToRef = nil
local function testStep(dt)
  if not next(test) then return end
  testClock = testClock + (dt or 0)
  if lateGoTo and testClock >= M.TEST_LATE_S and onGoToRef then
    local d = lateGoTo
    lateGoTo = nil
    log('I', 'beamngEarth', 'BeamMP test: going to the others now')
    onGoToRef(d)
  end
  if test['goto'] and test.gotoLat and not test.wentGoto and stats.poses > 0 and testClock > M.TEST_GOTO_S
      and (not test.gotoAt or os.time() >= test.gotoAt) then
    test.wentGoto = true
    local c = core()
    if c then
      log('I', 'beamngEarth', string.format('BeamMP test: to %.5f, %.5f', test.gotoLat, test.gotoLon))
      c.teleport(test.gotoLat, test.gotoLon, nil, { record = false })
    end
  end
  if test.map and not test.mapOpened and stats.poses > 0 and testClock > M.TEST_MAP_S then
    test.mapOpened = true
    local st = rawget(_G, 'beamngEarth_settings')
    if st and st.toggleMap then st.toggleMap(); log('I', 'beamngEarth', 'BeamMP test: map opened') end
  end
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if test.car and not testCarAt and not veh and stats.poses > 0 and testClock > M.TEST_CAR_S then
    testCarAt = testClock
    local c = core_camera and core_camera.getPosition and core_camera.getPosition()
    if c then
      local z = be:getSurfaceHeightBelow(vec3(c.x, c.y + 6, c.z + 50))
      core_vehicles.spawnNewVehicle('pickup', { pos = vec3(c.x, c.y + 6, (z or c.z) + 0.5),
        rot = quatFromDir(vec3(0, 1, 0), vec3(0, 0, 1)), autoEnterVehicle = true })
      log('I', 'beamngEarth', 'BeamMP test: car spawned')
    end
  end
  if testCarAt and not testOwnId and veh then testOwnId = veh:getId() end
  if test.away and testOwnId and anchorRef then
    local c = core()
    local function note(what, lat, lon)
      awayLog[#awayLog + 1] = { t = testClock, what = what, lat = lat, lon = lon }
      log('I', 'beamngEarth', 'BeamMP test: ' .. what)
    end
    if #awayLog == 0 and testClock - testCarAt > M.TEST_AWAY_S and veh then
      local q = veh:getPosition()
      local lat, lon = anchorRef:worldToGeodetic(q.x, q.y, q.z)
      note('away', lat, lon)
      c.teleport(lat, (lon + 130 + 180) % 360 - 180, nil, { record = false })
    elseif #awayLog == 1 and testClock - awayLog[1].t > M.TEST_AWAY_HOLD_S then
      note('back', awayLog[1].lat, awayLog[1].lon)
      c.teleport(awayLog[1].lat, awayLog[1].lon, nil, { record = false })
    end
  end
  if test.drive and testOwnId and not test.driving and veh and testClock - testCarAt > M.TEST_DRIVE_S then
    test.driving = true
    veh:queueLuaCommand('ai.setMode("traffic") ai.setSpeedMode("legal")')
    log('I', 'beamngEarth', 'BeamMP test: AI driving')
  end
  if test.switch and testOwnId then
    local phase = #switchLog
    local function note(what)
      local a = getPlayerVehicle and getPlayerVehicle(0)
      switchLog[#switchLog + 1] = { t = testClock, what = what, active = a and a:getId() or nil,
        ownThere = vehicle(testOwnId) ~= nil }
      log('I', 'beamngEarth', 'BeamMP test: ' .. what)
    end
    if phase == 0 and testClock - testCarAt > M.TEST_SWITCH_S then
      local target = nil
      for gid in pairs(remoteOwners()) do
        if not deactivated[gid] and vehicle(gid) then target = gid; break end
      end
      if target then
        be:enterVehicle(0, vehicle(target))
        note('into the other player\'s car ' .. target)
      end
    elseif phase == 1 and testClock - switchLog[1].t > M.TEST_SWITCH_HOLD_S then
      note('still in the other car (before switching back)')
      local own = vehicle(testOwnId)
      if own then be:enterVehicle(0, own) end
      note('back into our car')
    elseif phase == 3 and testClock - switchLog[3].t > 10 then
      note('10 s after switching back')
    end
  end
  -- 'delremote': TEST_DELREMOTE_S after the car, the other players' cars removed here, as a player does for frames (the
  -- restore must leave them removed; 'oldrestore': 1.6.0's restore rule, for the before)
  if test.delremote and testCarAt and not test.deleted and testClock - testCarAt > M.TEST_DELREMOTE_S and remoteOwners then
    local owners = remoteOwners()
    for gid in pairs(owners) do
      local o = vehicle(gid)
      if o then
        test.deleted = true
        o:delete()
        log('I', 'beamngEarth', 'BeamMP test: removed the other player\'s car ' .. tostring(gid))
      end
    end
  end
  -- 'leave': TEST_LEAVE_S after the car, the server left (BeamMP's Leave button; MCP is off in a session)
  if test.leave and testCarAt and not test.left and testClock - testCarAt > M.TEST_LEAVE_S then
    test.left = true
    log('I', 'beamngEarth', 'BeamMP test: leaving the server')
    local mp = rawget(_G, 'MPCoreNetwork')
    if mp and mp.leaveServer then pcall(mp.leaveServer, true) end
  end
  -- 'drop': TEST_DROP_S after the car, our car moved TEST_DROP_M east (BeamMP's teleport to a player far off our block)
  if test.drop and testCarAt and not test.dropped and veh and testClock - testCarAt > M.TEST_DROP_S then
    test.dropped = true
    local p = veh:getPosition()
    veh:setPosition(vec3(p.x + M.TEST_DROP_M, p.y, p.z + 5))
    log('I', 'beamngEarth', 'BeamMP test: car moved ' .. M.TEST_DROP_M .. ' m east')
  end
  if test.damage and testCarAt and not testDamaged and veh and testClock - testCarAt > M.TEST_DAMAGE_S then
    testDamaged = true
    -- a real crash (dropped from 20 m): dents, which BeamMP itself does not send; the other player's copy shows them
    -- only through the full-damage message (sendDamage)
    local p = veh:getPosition()
    veh:setPosition(vec3(p.x, p.y, p.z + 20))
    log('I', 'beamngEarth', 'BeamMP test: car damaged')
  end
end

local COUNT = [[local n,d=0,0 for _,b in pairs(v.data.beams) do if obj:beamIsBroken(b.cid) then n=n+1 ]]
  .. [[elseif obj:getBeamDeformation(b.cid) > 0.01 then d=d+1 end end ]]
  .. [[obj:queueGameEngineLua("local m=package.loaded['beamngEarth/mpsession'] if m then m.onBroken(%d,"..n..","..d..") end")]]

-- The other players' vehicles: { game vehicle id -> owner's server player id }, { game vehicle id -> BeamMP's
-- serverVehicleString }
remoteOwners = function()
  local out, sids = {}, {}
  local v = mp()
  if not v or type(v.getVehicles) ~= 'function' then return out, sids end
  local ok, all = pcall(v.getVehicles)
  if not ok or type(all) ~= 'table' then return out, sids end
  for _, veh in pairs(all) do
    -- (spawned ones only: a car BeamMP has queued has a placeholder game id, not a vehicle)
    if type(veh) == 'table' and veh.isSpawned ~= false and tonumber(veh.gameVehicleID) and veh.gameVehicleID > 0
      and veh.ownerID and not v.isOwn(veh.gameVehicleID) then
      out[veh.gameVehicleID] = veh.ownerID
      sids[veh.gameVehicleID] = veh.serverVehicleString
    end
  end
  return out, sids
end

-- Cars of other players that the game deleted here (BeamMP: isDeleted, drawn as its placeholder sphere; before
-- mpanchor.FAR_M a teleport put them thousands of km out, where the physics broke) come back with BeamMP's own
-- restorePlayerVehicle once their owner is within RESTORE_M of us, at most every RESTORE_AGAIN_S per player. Only a car
-- our world could have broken (mpanchor.wasFar: it was beyond FAR_M) and RESTORE_MAX times at most: isDeleted is also a
-- car this player removed on purpose (fewer cars, more frames), and respawning every one of those every 15 s (each a
-- spawn: a freeze, and BeamMP's "Trying to respawn" notice) fits the 1.6.0 players' "multiplayer freezing" (2026-10-23).
M.RESTORE_M, M.RESTORE_AGAIN_S, M.RESTORE_MAX = 20000, 15, 2
local restoredAt = {}       -- owner name -> clock of the last restore
local restores = {}         -- server vehicle string -> restores asked for it
local clock = 0
function M.restoreDeleted(lat, lon)
  local mpanchor = require('beamngEarth/mpanchor')
  local v = mp()
  if not v or type(v.getVehicles) ~= 'function' or type(v.restorePlayerVehicle) ~= 'function' then return 0 end
  local ok, all = pcall(v.getVehicles)
  if not ok or type(all) ~= 'table' then return 0 end
  local n = 0
  for sid, veh in pairs(all) do
    local a = type(veh) == 'table' and veh.isDeleted == true and not veh.isLocal and veh.ownerName
      and (test.oldrestore or (mpanchor.wasFar(tostring(sid)) and (restores[tostring(sid)] or 0) < M.RESTORE_MAX))
      and anchors[tostring(veh.ownerID)]
    if a and tonumber(a.lat) and tonumber(a.lon) and M.dist(lat, lon, tonumber(a.lat), tonumber(a.lon)) <= M.RESTORE_M
        and clock - (restoredAt[veh.ownerName] or -math.huge) >= M.RESTORE_AGAIN_S then
      restoredAt[veh.ownerName] = clock
      restores[tostring(sid)] = (restores[tostring(sid)] or 0) + 1
      stats.restored = (stats.restored or 0) + 1
      log('I', 'beamngEarth', 'BeamMP: ' .. tostring(veh.ownerName) .. '\'s deleted car brought back')
      pcall(v.restorePlayerVehicle, veh.ownerName)
      n = n + 1
    end
  end
  return n
end


-- The heading of the car the player is in, degrees clockwise from the frame's +y (north at the anchor); nil without one.
local function carHeading()
  local gpv = rawget(_G, 'getPlayerVehicle')
  local veh = gpv and gpv(0)
  if not veh then return nil end
  local ok, dir = pcall(function() return veh:getDirectionVector() end)
  if not ok or not dir or math.abs(dir.x) + math.abs(dir.y) < 1e-6 then return nil end
  return (math.deg(math.atan2(dir.x, dir.y)) + 360) % 360
end

-- Our own server player id (BeamMP's MPConfig), as a string; nil unknown.
local function myPid()
  local cfg = rawget(_G, 'MPConfig')
  if type(cfg) == 'table' and type(cfg.getPlayerServerID) == 'function' then
    local ok, id = pcall(cfg.getPlayerServerID)
    if ok and id ~= nil then return tostring(id) end
  end
  return nil
end

local function onAnchors(data)
  local ok, d = pcall(jsonDecode, data)
  if not ok or type(d) ~= 'table' then return end
  stats.anchors = stats.anchors + 1
  anchors = d
  local mpanchor = require('beamngEarth/mpanchor')
  local n = 0
  for pid, a in pairs(d) do
    n = n + 1
    if type(a) == 'table' then mpanchor.setRemote(pid, a.aLat, a.aLon) end
  end
  if n ~= lastPlayers then
    lastPlayers = n
    log('I', 'beamngEarth', string.format('BeamMP: %d player(s) with BeamNG.Earth', n))
  end
end

-- core.teleportTo: the anchor of other players near (lat, lon) to teleport onto (aLat, aLon), nil if none: none while
-- shared world building is commented out (its pickAnchor above).
function M.adoptAnchor(lat, lon) return nil end

-- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
-- -- Our world server's group service is up (core: the "share" answer): { port, code }.
-- function M.onShare(msg)
--   local port = tonumber(msg.port)
--   share = port and { port = port, code = tostring(msg.code or ''), weight = tonumber(msg.weight) } or false
--   log('I', 'beamngEarth', 'BeamMP: sharing generation on port ' .. tostring(port))
-- end

-- The server's settings (the plugin's bngeConfig, at our first pose): its time and weather.
local function onConfig(data)
  local ok, d = pcall(jsonDecode, data)
  if not ok or type(d) ~= 'table' then return end
  M.applyServerSky(d)
end

-- The server's time and weather (its plugin's G.TIME / G.WEATHER, in bngeConfig): { time = { hours, speed, epoch } or
-- nil (the real clock), weather = a preset name or nil (the real weather) }. A player who set Sky or Weather to
-- Manual keeps their own (the sky and weather only follow the server in their 'real' mode).
function M.applyServerSky(d)
  local sk, wx = rawget(_G, 'beamngEarth_sky'), rawget(_G, 'beamngEarth_weather')
  local time = type(d) == 'table' and type(d.time) == 'table' and d.time or nil
  local weather = type(d) == 'table' and type(d.weather) == 'string' and d.weather or nil
  if sk and sk.setServerClock then pcall(sk.setServerClock, time) end
  if wx and wx.setPreset then pcall(wx.setPreset, weather) end
  if time or weather then
    log('I', 'beamngEarth', string.format('BeamMP: the server\'s time %s, weather %s',
      time and string.format('%05.2f h x%g', tonumber(time.hours) or 0, tonumber(time.speed) or 0) or 'real',
      weather or 'real'))
  end
end

-- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
-- -- Pure: the port our world server shares on: the BeamMP server's (bngeConfig) when it is a usable one, else nil.
-- function M.sharePort(cfg)
--   local port = cfg and tonumber(cfg.sharePort)
--   if port and (port ~= math.floor(port) or port < 1024 or port > 65535) then port = nil end
--   return port
-- end

-- local function onPeers(data)
--   if not M.SHARE_GEN then return end
--   local ok, d = pcall(jsonDecode, data)
--   if not ok or type(d) ~= 'table' or type(d.members) ~= 'table' then return end
--   stats.peers = #d.members
--   require('beamngEarth/net').send({ type = 'peers', me = tostring(d.me), members = d.members })
--   log('I', 'beamngEarth', string.format('BeamMP: %d player(s) share our anchor\'s generation', #d.members))
-- end

-- -- Onto the anchor of the players near us when it is not ours (pickAnchor, everyone's anchors with ours): a smooth
-- -- rebase (core.rebaseTo). Every player applies the same rule, so a group ends on one anchor; a car ahead that
-- -- rebased takes the ones following onto its new anchor.
-- local function converge(dt, anchor, lat, lon)
--   if not M.SHARE_GEN then return end
--   local k = M.key(anchor.lat, anchor.lon)
--   if k ~= lastAnchorKey then lastAnchorKey, anchorSince = k, 0 end
--   anchorSince = anchorSince + (dt or 0)
--   convergeIn = convergeIn - (dt or 0)
--   if convergeIn > 0 or anchorSince < M.SETTLE_S then return end
--   convergeIn = M.CONVERGE_S
--   local mine = myPid()
--   if not mine or not anchors[mine] then return end   -- (ours not heard back yet: the counts would be off)
--   local aLat, aLon = M.pickAnchor(anchors, lat, lon)
--   if not aLat or M.key(aLat, aLon) == k then return end
--   local c = core()
--   local ok, why = false, 'no core'
--   if c and c.rebaseTo then ok, why = c.rebaseTo(aLat, aLon) end
--   if ok then
--     stats.converged = stats.converged + 1
--     log('I', 'beamngEarth', string.format('BeamMP: onto the shared anchor %s', M.key(aLat, aLon)))
--   end
-- end

local function onGoTo(data)
  local ok, d = pcall(jsonDecode, data)
  local c = core()
  stats.go = stats.go + 1
  if test.late and testClock < M.TEST_LATE_S then lateGoTo = data; return end
  if wentTo or not ok or type(d) ~= 'table' or not c then return end
  local lat, lon = tonumber(d.lat), tonumber(d.lon)
  if not lat or not lon then return end
  wentTo = true
  -- beside the other player, not on it (12 m east)
  lon = lon + M.GOTO_OFFSET_M / (111320 * math.max(0.01, math.cos(math.rad(lat))))
  log('I', 'beamngEarth', string.format('BeamMP: to the other players at %.5f, %.5f', lat, lon))
  c.teleport(lat, lon, nil, { record = false })
end

onGoToRef = onGoTo

-- No car yet (BeamMP joins without one): the free camera, left at the level's spawn point under the generated
-- ground, put once on the ground at the origin (where the vehicle selector spawns the car); and lifted back over the
-- ground whenever it is under it (a teleport moves the world under a free camera: from a valley to a mountain it
-- ended 1400 m underground, a black screen). A teleport (the join's to the other players, or the map's) puts the
-- camera at its target itself (core.lua moveCamera / placePlayer, in single player too: onto lower ground it was left
-- hundreds of metres up, and the car the vehicle selector spawns there fell from it, the user 2026-10-24: "sometimes i
-- spawn high above the ground in beammp").
M.TUBE_RAY_M = 8
local function placeCamera(player)
  if (getPlayerVehicle and getPlayerVehicle(0)) or not be or not be.getSurfaceHeightBelow then return end
  local x, y = 0, -8
  if camPlaced then
    if not player or not player.x then return end
    x, y = player.x, player.y
  end
  local z = be:getSurfaceHeightBelow(vec3(x, y, 9000))
  if type(z) ~= 'number' or z < -1e5 then return end      -- (the ground not there yet)
  if camPlaced and player.z >= z - 0.5 then return end
  -- (under the terrain over collision a few metres down: in a tunnel's tube, where a teleport put it (core floorCheck)
  -- or it was flown in; only a camera over nothing is lost underground)
  -- (the ray from the camera: from higher, a camera just under the ground met that ground and counted as in a tube;
  -- a hit only under the surface)
  local ray = camPlaced and rawget(_G, 'castRayStatic')
  local from = player.z + 0.1
  local d = ray and ray(vec3(x, y, from), vec3(0, 0, -1), M.TUBE_RAY_M)
  if d and d < M.TUBE_RAY_M and from - d < z - 0.5 then return end
  camPlaced = true
  local q = quatFromDir(vec3(0, 1, -0.15), vec3(0, 0, 1))
  if core_camera and core_camera.setPosRot then core_camera.setPosRot(0, x, y, z + 3, q.x, q.y, q.z, q.w) end
end

-- A teleport asked (core.teleportTo): core puts the camera at its target (no car), so the join's first placement at
-- the origin does not follow it (a teleport can be asked before the ground at the origin is in). A rebase does not
-- come here.
function M.onTeleport()
  camPlaced = true
end
M.placeCamera = placeCamera     -- (tests)

-- BeamMP's join done (core.lua onBeamMPPostJoin): the first pose asks the plugin where the others are.
-- (and where to get help: the BeamMP team, 2026-10-27, players kept asking them about BeamNG.Earth)
M.SUPPORT = 'BeamNG.Earth on BeamMP: for help join the TGForever Discord, https://discord.gg/tgf. BeamNG.Earth is '
  .. 'not like a normal BeamNG mod: if something is not working, it is almost certainly a BeamNG.Earth problem, not a '
  .. 'BeamMP one, so please do not contact the BeamMP team or open BeamMP support tickets about it.'
function M.onPostJoin()
  joined, wentTo, camPlaced = true, false, false
  anchors = {}
  log('I', 'beamngEarth', M.SUPPORT)
end

-- Is (x, y) m or more inside our terrain block (nil: no block known)?
-- Copies repaired by the owner's full damage (the user, 2026-10-24: "do the fixes"). Two cars that hit each other are
-- each worked out by every game on its own, so a copy breaks other beams than its owner's car: in a three-player test a
-- car with 9 broken beams showed 72 and 30 on the others, and BeamMP's full-damage message (nodesVE.applyNodes) breaks
-- and dents but never mends. BeamMP's nodesGE.handle is wrapped (as mpanchor wraps positionGE.handle; BeamMP's files
-- are not changed): another player's car keeps the owner's state, and once it is far from our camera (checkCopies)
-- its vehicle Lua counts the beams broken here but not in the owner's state; none: the state is put on (APPLY_VE);
-- some: the car is reset (RESET_PHYSICS: BeamMP's positionVE puts it back on its next packet) and the state put on
-- REPAIR_FRAMES frames later.
M.REPAIR_FRAMES = 3
M.BACK_ON_SETTLE_S = 2.0    -- a car switched off while far comes back on once its packets have come this long
local nodesWrapped = nil   -- the nodesGE table wrapped (BeamMP reloading it makes a new one)
local nodesBySid = {}       -- BeamMP vehicle id -> a full damage that came before its car was known here
local pendingNodes = {}     -- game vehicle id -> { raw, frames } (the owner's state, applied after the reset)
-- Copies checked again after damage of their own (2026-10-24, three players: a copy took 24 broken beams after its
-- owner's full damage had come, the two cars still pushing into each other; the owner's damage barely moved, so it sent
-- nothing more and the copy stayed broken). A copy whose damage figure moved DAMAGE_STEP from where it had settled,
-- for COPY_SETTLE_S, is stale again: checked against the owner's last state once far. No state yet: the owner's car had
-- less than DAMAGE_STEP damage while we were near, so beams broken here mean a reset to new.
M.COPY_SETTLE_S = 4
M.SYNC_FAR_M = 150          -- a copy takes its owner's damage only this far from our camera (m) ...
M.SYNC_RANGE_M = 1000       -- ... and this near: once, as it comes in range (the user, 2026-10-24: "we really need to
                            -- only update the damage once the car is in range"); a farther copy only keeps the state
local lastNodes = {}        -- game vehicle id -> its owner's last full damage (its JSON as it came)
local stale = {}            -- game vehicle id -> true: may differ from its owner (a new state, or damage of its own)
local morphs = {}           -- game vehicle id -> steps done: a copy's dents growing in (APPLY_VE's bngeMorph)
local applied = {}          -- game vehicle id -> the owner's state last put on it: put on once
local witnessed = {}        -- game vehicle id -> true: its stale damage came while it was near our camera (we saw it)
local copyBase = {}         -- game vehicle id -> the copy's damage figure once settled (nil: to be taken)
local copySince = {}        -- game vehicle id -> seconds it has differed from copyBase
local copyQuiet = {}        -- game vehicle id -> seconds before copyBase is taken (after a state, a check, a reset)
local PRISTINE_VE = [[local n = 0 for _, b in pairs(v.data.beams) do if obj:beamIsBroken(b.cid) then n = n + 1 end end ]]
  .. [[if n > 0 then obj:requestReset(RESET_PHYSICS) ]]
  .. [[obj:queueGameEngineLua("local m = package.loaded['beamngEarth/mpsession'] if m then m.afterRepairReset(%d, " .. n .. ") end") end]]
-- The owner's damage put on a copy (vehicle Lua; d: the decoded state): its broken beams and node masses at once,
-- its dents (beam rest lengths) grown in over MORPH_STEPS frames (bngeMorph, stepped by M.update). Measured on a
-- test bench (a pickup wrecked by a 30 m drop, 156-225 broken beams, its state put on a fresh one, 2026-10-24): the
-- broken beams alone came out exact; BeamMP's nodesVE.applyNodes way (every node set where it stood, then the rest
-- lengths) broke 84-97 more (loose parts torn off, and on a car that had turned since, twisted apart: 49 beams on
-- copies of a car with none, an instability); the rest lengths in one step 159 more; grown in over 60 steps 39 more
-- (the user chose this: "Breaks + morphed dents"). BeamNG 0.40 renamed beamstate.beamDeformed to onBeamDeformed.
M.MORPH_STEPS = 60
-- (its globals by rawset: a plain assignment of a new global in vehicle Lua logs a warning with a stack trace, every
-- repair on a busy server)
local APPLY_VE = [[local function applyDamage(d) if type(d) ~= 'table' then return end ]]
  .. [[for cid, n in pairs(d.nodes or {}) do if type(n) == 'table' and #n > 1 then obj:setNodeMass(tonumber(cid) - 1, n[2]) end end ]]
  .. [[local l0 = {} for cid, b in pairs(d.beams or {}) do local c = tonumber(cid) - 1 ]]
  .. [[if b[2] == true then if not obj:beamIsBroken(c) then obj:breakBeam(c) beamstate.beamBroken(c, 1) end ]]
  .. [[elseif not obj:beamIsBroken(c) then l0[c] = obj:getBeamRestLength(c) end end ]]
  .. [[rawset(_G, 'bngeDmg', { beams = d.beams or {}, l0 = l0 }) ]]
  .. [[obj:queueGameEngineLua("local m = package.loaded['beamngEarth/mpsession'] if m then m.startMorph(" .. obj:getID() .. ") end") end ]]
  .. [[rawset(_G, 'bngeMorph', function(f) local s = rawget(_G, 'bngeDmg') if not s then return end ]]
  .. [[local deformed = beamstate.onBeamDeformed or beamstate.beamDeformed ]]
  .. [[for cid, b in pairs(s.beams) do local c = tonumber(cid) - 1 local l0 = s.l0[c] ]]
  .. [[if l0 and not obj:beamIsBroken(c) then obj:setBeamLength(c, l0 + (b[1] - l0) * f) ]]
  .. [[if f >= 1 and b[3] > 0 and deformed then deformed(c, b[3]) end end end ]]
  .. [[if f >= 1 then rawset(_G, 'bngeDmg', nil) end end) ]]
M.APPLY_VE = APPLY_VE        -- (tests)
local REPAIR_VE = APPLY_VE .. [[local raw, apply = %s, %s local ok, d = pcall(jsonDecode, raw) local extra = 0 ]]
  .. [[if ok and type(d) == 'table' and type(d.beams) == 'table' then for _, b in pairs(v.data.beams) do ]]
  .. [[if obj:beamIsBroken(b.cid) then local s = d.beams[tostring(b.cid + 1)] or d.beams[b.cid + 1] ]]
  .. [[if not (type(s) == 'table' and s[2] == true) then extra = extra + 1 end end end end ]]
  .. [[if extra > 0 then obj:requestReset(RESET_PHYSICS) ]]
  .. [[obj:queueGameEngineLua("local m = package.loaded['beamngEarth/mpsession'] if m then m.afterRepairReset(%d, " .. extra .. ") end") ]]
  .. [[elseif apply and ok then applyDamage(d) end]]

local function camPos()
  local cam = rawget(_G, 'core_camera')
  return cam and cam.getPosition and cam.getPosition() or nil
end

-- Is game vehicle gid within m of our camera (cam: its position; false when either is unknown)?
local function within(gid, m, cam)
  local veh = cam and vehicle(gid)
  local q = veh and veh:getPosition()
  return q ~= nil and (q.x - cam.x) ^ 2 + (q.y - cam.y) ^ 2 + (q.z - cam.z) ^ 2 < m ^ 2
end

function M.wrapNodes()
  local ng = rawget(_G, 'nodesGE')
  if not ng or type(ng.handle) ~= 'function' then return false end
  if nodesWrapped == ng then return true end
  local orig = ng.handle
  ng.handle = function(rawData)
    local code, sid, data = string.match(rawData or '', '^(%a)%:(%d+%-%d+)%:(.*)')
    if code ~= 'n' then return orig(rawData) end
    local mp = rawget(_G, 'MPVehicleGE')
    local v = mp and mp.getVehicles and mp.getVehicles()[sid]
    if v and v.isLocal then return end          -- (our own car's: nothing to put on)
    -- never BeamMP's own applyNodes: on BeamNG 0.40 it calls beamstate.beamDeformed, renamed, and the copy's vehicle Lua
    -- stopped with a fatal error (a player's log, 2026-10-24: the message came as a far car came back on, before it
    -- could be told apart here); a car not known yet keeps its state by its BeamMP id till it is (adoptNodes)
    local gid = v and mp.getGameVehicleID and mp.getGameVehicleID(sid) or -1
    if not vehicle(gid) then nodesBySid[sid] = data return end
    lastNodes[gid], stale[gid] = data, true   -- (put on once it is in range: checkCopies; nothing done with it till then)
    witnessed[gid] = within(gid, M.SYNC_FAR_M, camPos()) or nil
  end
  nodesWrapped = ng
  return true
end

-- The full damages that came before their cars were known here, put with their cars once they are (each frame).
local function adoptNodes()
  if not next(nodesBySid) then return end
  local mp = rawget(_G, 'MPVehicleGE')
  if not (mp and mp.getGameVehicleID) then return end
  for sid, data in pairs(nodesBySid) do
    local gid = mp.getGameVehicleID(sid) or -1
    if vehicle(gid) then
      nodesBySid[sid] = nil
      lastNodes[gid], stale[gid] = data, true
    end
  end
end
M.adoptNodes = adoptNodes       -- (tests)

-- (the copy's vehicle Lua) gid was reset for a repair: its owner's state goes on REPAIR_FRAMES frames later
function M.afterRepairReset(gid, extra)
  local p = pendingNodes[tonumber(gid)]
  if p then p.frames = M.REPAIR_FRAMES end
  log('I', 'beamngEarth', string.format('BeamMP: car %s repaired (%s beams broken here only): its owner\'s damage again',
    tostring(gid), tostring(extra)))
end

-- Pure: is a copy's own damage due a check? damage now, base: where it settled, since: seconds it has differed.
-- -> due, since (updated)
function M.copyDue(damage, base, since, dt)
  if not damage or not base or math.abs(damage - base) < M.DAMAGE_STEP then return false, 0 end
  since = (since or 0) + dt
  return since >= M.COPY_SETTLE_S, since
end

-- (every HIDE_S) owners: the other players' cars here; cam: our camera's position (nil: unknown, nothing is far).
-- Near us a copy keeps what our game worked out (the user, 2026-10-24: "we should really only sync damage for cars far
-- away": a crash we watched is the one our game simulated, and putting the owner's damage on there reset the car in
-- front of us); a stale copy takes its owner's last state once it is SYNC_FAR_M from the camera.
local function checkCopies(owners, dt, cam)
  local objs = rawget(_G, 'map') and map.objects or {}
  for gid in pairs(owners) do
    local o = objs[gid]
    local dmg = o and tonumber(o.damage)
    local p = pendingNodes[gid]
    if not dmg or deactivated[gid] or (p and p.frames) or morphs[gid] then
      copyBase[gid] = nil
    elseif (copyQuiet[gid] or 0) > 0 then
      copyQuiet[gid] = copyQuiet[gid] - dt
    elseif not copyBase[gid] then
      copyBase[gid], copySince[gid] = dmg, 0
    else
      if dmg < M.DAMAGE_STEP and copyBase[gid] >= M.DAMAGE_STEP then lastNodes[gid], applied[gid] = nil, nil end   -- (reset)
      local due
      due, copySince[gid] = M.copyDue(dmg, copyBase[gid], copySince[gid], dt)
      -- (its damage moved after the owner's state went on: checked again once far, but not against the same state: the
      -- dents growing in tear a loose part or two off, and a Windows test copy was repaired 7 times in 90 s for it)
      if due then
        stale[gid], copyBase[gid] = applied[gid] == nil or applied[gid] ~= lastNodes[gid], nil
        witnessed[gid] = within(gid, M.SYNC_FAR_M, cam) or nil
      end
    end
    local veh = stale[gid] and cam and not deactivated[gid] and vehicle(gid)
    local q = veh and veh:getPosition()
    local d2 = q and (q.x - cam.x) ^ 2 + (q.y - cam.y) ^ 2 + (q.z - cam.z) ^ 2
    -- (nearer than SYNC_FAR_M too when the damage came while it was away: a car teleported up to us, or one that
    -- jumped from out of range to near within a check, was never seen crashing here)
    if d2 and d2 <= M.SYNC_RANGE_M ^ 2 and (d2 >= M.SYNC_FAR_M ^ 2 or not witnessed[gid]) then
      if lastNodes[gid] then
        pendingNodes[gid], applied[gid] = { raw = lastNodes[gid] }, lastNodes[gid]
        veh:queueLuaCommand(string.format(REPAIR_VE, string.format('%q', lastNodes[gid]), 'true', gid))
      else
        pendingNodes[gid] = nil
        veh:queueLuaCommand(string.format(PRISTINE_VE, gid))
      end
      stale[gid], witnessed[gid], copyBase[gid], copyQuiet[gid] = nil, nil, nil, M.COPY_SETTLE_S
    end
  end
  for gid in pairs(lastNodes) do
    if not owners[gid] then lastNodes[gid], copyBase[gid], copySince[gid], copyQuiet[gid], applied[gid] = nil, nil, nil, nil, nil end
  end
  for gid in pairs(stale) do if not owners[gid] then stale[gid], witnessed[gid] = nil, nil end end
end

-- (the copy's vehicle Lua) gid's broken beams are on: its dents grow in from the next frame
function M.startMorph(gid) morphs[tonumber(gid)] = 0 end

local function stepMorphs()
  for gid, k in pairs(morphs) do
    k = k + 1
    local veh = vehicle(gid)
    if veh then veh:queueLuaCommand(string.format('if bngeMorph then bngeMorph(%.4f) end', k / M.MORPH_STEPS)) end
    if not veh or k >= M.MORPH_STEPS then
      morphs[gid] = nil
      copyBase[gid], copyQuiet[gid] = nil, M.COPY_SETTLE_S     -- (its damage figure moved with the dents: settles anew)
    else
      morphs[gid] = k
    end
  end
end

local function applyPendingNodes()
  for gid, p in pairs(pendingNodes) do
    if p.frames then
      p.frames = p.frames - 1
      if p.frames <= 0 then
        pendingNodes[gid] = nil
        local veh = vehicle(gid)
        if veh then
          veh:queueLuaCommand(APPLY_VE .. 'local ok, d = pcall(jsonDecode, ' .. string.format('%q', p.raw) .. ') if ok then applyDamage(d) end')
        end
      end
    end
  end
end

local function onBlock(x, y, m)
  local b = require('beamngEarth/terrainpatch').block()
  if not b then return nil end
  return x >= b[1] + m and y >= b[2] + m and x <= b[3] - m and y <= b[4] - m
end

-- Is this other player's car floating here (collisionfoci leaves it out)?
function M.isFloating(gid) return floating[tonumber(gid)] == true end

local function gravityNow()
  local ce = rawget(_G, 'core_environment')
  local g = ce and ce.getGravity and ce.getGravity()
  return tonumber(g) or -9.81
end

-- The other players' cars on (not switched off): float off our block, land on it. Gravity sent again each time to the
-- floating ones (the real gravity, weather.lua, sets every vehicle's when it changes).
local function updateFloating(owners)
  for gid in pairs(owners) do
    local veh = not deactivated[gid] and vehicle(gid)
    if veh then
      local p = veh:getPosition()
      if floating[gid] then
        if onBlock(p.x, p.y, M.FLOAT_IN_M) then
          floating[gid] = nil
          veh:queueLuaCommand('obj:setGravity(' .. gravityNow() .. ')')
          log('I', 'beamngEarth', string.format('BeamMP: car %s on our block again: gravity back', tostring(gid)))
        else
          veh:queueLuaCommand('obj:setGravity(0)')
        end
      elseif onBlock(p.x, p.y, M.FLOAT_OUT_M) == false then
        floating[gid] = true
        veh:queueLuaCommand('obj:setGravity(0)')
        log('I', 'beamngEarth', string.format('BeamMP: car %s off our block: floats', tostring(gid)))
      end
    end
  end
  for gid in pairs(floating) do
    if not owners[gid] then floating[gid] = nil end       -- (gone)
  end
end

-- Each frame (core.lua) in a BeamMP session in our level: anchor (core's st.anchor), player { x, y, z } in its frame
-- (the car; the camera when there is none).
function M.update(dt, anchor, player)
  if not handlers and rawget(_G, 'AddEventHandler') then
    AddEventHandler('bngeAnchors', onAnchors, 'beamngEarth')
    AddEventHandler('bngeGoTo', onGoTo, 'beamngEarth')
    -- AddEventHandler('bngePeers', onPeers, 'beamngEarth')     -- (shared world building, commented out)
    AddEventHandler('bngeConfig', onConfig, 'beamngEarth')
    handlers = true
  end
  local mpanchor = require('beamngEarth/mpanchor')
  mpanchor.install()            -- (a no-op once installed, and until BeamMP's positionGE has loaded)
  M.wrapNodes()
  adoptNodes()
  applyPendingNodes()
  stepMorphs()
  clock = clock + (dt or 0)
  statusIn = statusIn - (dt or 0)
  if statusIn <= 0 then
    statusIn = M.STATUS_S
    local n = 0
    for _ in pairs(anchors) do n = n + 1 end
    log('I', 'beamngEarth', string.format('BeamMP status: anchor %s, %d player(s) known, events anchors/goto %d/%d, '
      .. 'poses %d, %s', anchor and M.key(anchor.lat, anchor.lon) or 'none', n, stats.anchors, stats.go, stats.poses,
      mpanchor.status()))
    -- the same as a file (the game's log reaches the disk late; BeamMP turns the game's MCP off in a session)
    local hidden = 0
    for _ in pairs(deactivated) do hidden = hidden + 1 end
    local remote = nil
    local own = nil
    if next(test) then
      local v = getPlayerVehicle and getPlayerVehicle(0)
      if v then
        local gid = v:getId()
        v:queueLuaCommand(string.format(COUNT, gid))
        own = { broken = brokenOf[gid], deformed = deformedOf[gid], damage = rawget(_G, 'map') and map.objects[gid] and map.objects[gid].damage,
          sent = sentDamage[gid] }
        if anchor then
          local q = v:getPosition()
          own.lat, own.lon = anchor:worldToGeodetic(q.x, q.y, q.z)
        end
      end
      remote = {}
      for gid in pairs(remoteOwners()) do
        local o = vehicle(gid)
        if o then o:queueLuaCommand(string.format(COUNT, gid)) end
        local r = { broken = brokenOf[gid], deformed = deformedOf[gid], hidden = deactivated[gid] == true }
        if o and anchor then        -- (where it stands here, as latitude / longitude: the conversion's check)
          local q = o:getPosition()
          r.lat, r.lon = anchor:worldToGeodetic(q.x, q.y, q.z)
          -- (its height over our ground: a car that looks to float; floating: off our block on purpose)
          r.z = q.z
          r.gz = be and be.getSurfaceHeightBelow and be:getSurfaceHeightBelow(vec3(q.x, q.y, q.z + 2)) or nil
          r.float = floating[gid] == true
        end
        remote[tostring(gid)] = r
      end
    end
    local perf = nil
    if test.perf then
      local pm = rawget(_G, 'beamngEarth_perfmon')
      if not pm and extensions and extensions.load then extensions.load('beamngEarth_perfmon') end
      pm = rawget(_G, 'beamngEarth_perfmon')
      local ok, snap = pcall(function() return pm and pm.snapshot() end)
      if ok and type(snap) == 'table' then
        perf = { frames = snap.frames, fps = snap.fps, p50 = snap.p50, p99 = snap.p99, maxMs = snap.maxMs,
          over33 = snap.over33, over50 = snap.over50, over100 = snap.over100 }
        local gov = require('beamngEarth/governor')
        if next(gov.slow) then perf.slow, gov.slow = gov.slow, {} end     -- (the slow frames since the last)
      end
    end
    -- (the terrain block where it stands and the far field's box for it: the two should agree)
    local blk = nil
    local tb = scenetree and scenetree.findObject and scenetree.findObject('bngEarthTerrain')
    local ff = package.loaded['beamngEarth/farfield']
    local cb = ff and ff.coverBlock and ff.coverBlock()
    if tb or cb then
      local tp = tb and tb:getPosition()
      blk = { tx = tp and tp.x, ty = tp and tp.y, tz = tp and tp.z, cx0 = cb and cb.x0, cy0 = cb and cb.y0,
        cx1 = cb and cb.x1, cy1 = cb and cb.y1, near = ff and ff.nearVisual and ff.nearVisual() }
      local ok, fs = pcall(function() return ff and ff.stats() end)
      if ok and type(fs) == 'table' then
        blk.partial, blk.partialSink, blk.partialLevel, blk.over = fs.partial, fs.partialSink, fs.partialLevel, fs.overBlock
      end
    end
    -- (the real time and weather in the session: the sky's and weather's own state)
    local skyNow, wxNow = nil, nil
    local sk, wx = rawget(_G, 'beamngEarth_sky'), rawget(_G, 'beamngEarth_weather')
    if sk and sk.status then local okS, v = pcall(sk.status) if okS then skyNow = v; v.manualBy = sk.manualBy end end
    if wx and wx.getMode then
      local okW, m = pcall(wx.getMode)
      local okL, line = pcall(function() return wx.statusLine and wx.statusLine() end)
      wxNow = { mode = okW and m or nil, line = okL and line or nil, manualBy = wx.manualBy }
    end
    pcall(jsonWriteFile, M.STATUS_FILE, { t = os.time(), block = blk, sky = skyNow, weather = wxNow, anchor = anchor and M.key(anchor.lat, anchor.lon) or nil,
      perf = perf, switch = next(switchLog) and switchLog or nil, away = next(awayLog) and awayLog or nil,
      anchors = anchors, stats = stats, hidden = hidden, remote = remote, own = own, player = player and { x = player.x,
      y = player.y, z = player.z, camera = player.camera } or nil }, true)
  end
  -- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
  -- configWait = configWait + (dt or 0)
  -- if share == nil and M.SHARE_GEN and (config or configWait >= M.CONFIG_WAIT_S) then
  --   share = false               -- our world server's group service, asked once a session (its answer: onShare), on
  --   require('beamngEarth/net').send({ type = 'share', port = M.sharePort(config) })     -- the server's port
  -- end
  if not anchor then return end
  placeCamera(player)
  anchorRef = anchor
  testStep(dt)
  if not player then return end
  local lat, lon = anchor:worldToGeodetic(player.x, player.y, player.z or 0)
  poseIn = poseIn - (dt or 0)
  if (poseIn <= 0 or mpanchor.ownChanged) and rawget(_G, 'TriggerServerEvent') then
    poseIn = M.POSE_S
    mpanchor.ownChanged = false
    local h = carHeading()
    if h and anchor.northAt then        -- (true north: the frame's +y drifts from it away from the anchor)
      local ok, nx, ny = pcall(anchor.northAt, anchor, player.x, player.y)
      if ok and tonumber(nx) and tonumber(ny) then h = (h - math.deg(math.atan2(nx, ny)) + 360) % 360 end
    end
    TriggerServerEvent('bngePose', jsonEncode({ v = M.PROTO, aLat = anchor.lat, aLon = anchor.lon, lat = lat,
      lon = lon, joined = joined, h = h and math.floor(h * 10 + 0.5) / 10 or nil }))   -- (sp / sc / sw: shared world
      -- building's port, code and weight, commented out)
    stats.poses = stats.poses + 1
    if joined then log('I', 'beamngEarth', string.format('BeamMP: first pose sent (%.5f, %.5f)', lat, lon)) end
    joined = false
  end
  -- converge(dt, anchor, lat, lon)     -- (shared world building, commented out)
  hideIn = hideIn - (dt or 0)
  if hideIn > 0 then return end
  hideIn = M.HIDE_S
  local owners, sids = remoteOwners()
  local hide = M.toHide(owners, mpanchor.knows)
  for gid, sid in pairs(sids) do
    if mpanchor.isFar(sid) then hide[gid] = true end     -- (not placed: beyond our world)
  end
  M.sendDamage(lat, lon, M.HIDE_S)
  local cam = rawget(_G, 'core_camera') and core_camera.getPosition and core_camera.getPosition() or player
  checkCopies(owners, M.HIDE_S, cam)
  M.restoreDeleted(lat, lon)
  for gid in pairs(hide) do
    if not deactivated[gid] then
      local veh = vehicle(gid)
      if veh then
        veh:setActive(0)
        M.nametag(gid, true)
        deactivated[gid] = true
        log('I', 'beamngEarth', string.format('BeamMP: car %s (%s) switched off here (%s)', tostring(gid),
          tostring(sids[gid]), mpanchor.isFar(sids[gid]) and 'far' or 'its owner\'s anchor not known'))
      end
    end
  end
  local tp = require('beamngEarth/terrainpatch')
  local now = clock
  for gid in pairs(deactivated) do
    if not vehicle(gid) then
      deactivated[gid], waitGround[gid] = nil, nil      -- (gone: removed by the server or its owner)
    elseif hide[gid] then
      waitGround[gid] = nil
    elseif mpanchor.nearFor(sids[gid]) < M.BACK_ON_SETTLE_S then
      -- (its packets in reach again only just: BeamMP's position sync settles first. Back on 0.5 s after its owner's
      -- jump back, a pickup went unstable 0.9 s later on a 19-player server, 2026-10-24)
    else
      local veh = vehicle(gid)
      local x, y = mpanchor.lastPos(sids[gid])
      if not x and veh then local p = veh:getPosition(); x, y = p.x, p.y end
      local w = waitGround[gid]
      local floats = x and onBlock(x, y, M.FLOAT_OUT_M) == false      -- (it floats there: no ground needed)
      if not x or floats or tp.groundReady(x, y) or (w and now - w.since > M.GROUND_WAIT_S) then
        if w and x and not tp.groundReady(x, y) then
          log('W', 'beamngEarth', 'BeamMP: car ' .. tostring(gid) .. ' back on without ground under it (waited too long)')
        elseif w then
          log('I', 'beamngEarth', string.format('BeamMP: car %s back on, its ground in after %.1f s', tostring(gid),
            now - w.since))
        else
          -- (its packet's height against our ground there: a remote car came back on two instabilities later in a
          -- three-player test, cause not pinned down, 2026-10-24)
          local _, _, pz = mpanchor.lastPos(sids[gid])
          local gz = x and be and be.getSurfaceHeightBelow and be:getSurfaceHeightBelow(vec3(x, y, (pz or 0) + 50))
          log('I', 'beamngEarth', string.format('BeamMP: car %s back on at %.0f, %.0f, z %.2f (our ground %.2f)',
            tostring(gid), x or 0, y or 0, pz or -1, gz or -1))
        end
        M.nametag(gid, false)
        if veh then
          veh:setActive(1)
          -- BeamMP's own reset of its position sync (smoothers, the remote clock, a clean teleport onto the next
          -- packet): switched off a minute while its player teleported away and back (their car reset, its clock
          -- restarted), the car came back on with that state stale and two instabilities followed within 0.2 s, on the
          -- right spot and height (a three-player test, 2026-10-24). Its physics reset first, as a freshly spawned car
          -- (which BeamMP places stably): back on without it, a car floating off our block went unstable 0.8 s later,
          -- every time (a player's 19-player session and the away test, 2026-10-24); the owner's damage goes on again
          -- (lastNodes, once in range)
          veh:queueLuaCommand('obj:requestReset(RESET_PHYSICS) if positionVE and positionVE.onReset then '
            .. 'positionVE.onReset() end')
          if lastNodes[gid] then stale[gid], applied[gid] = true, nil end
        end
        deactivated[gid], waitGround[gid] = nil, nil
      else
        if not w then
          w = { since = now }
          waitGround[gid] = w
          log('I', 'beamngEarth', string.format('BeamMP: car %s waits for its ground at %.0f, %.0f', tostring(gid), x, y))
        end
        w.x, w.y = x, y
      end
    end
  end
  updateFloating(owners)
end

-- Collision foci (collisionfoci.lua) where cars switched off here are coming back on (waitGround): their ground made
-- before they are. [{ x, y, r, trees, id, kind }]
function M.groundFoci()
  local out = {}
  local cf = require('beamngEarth/collisionfoci')
  for gid, w in pairs(waitGround) do
    out[#out + 1] = { x = w.x, y = w.y, r = cf.PLAYER_R, trees = true, id = gid, kind = 'player' }
  end
  return out
end

-- Our own car's damage as BeamMP's full-damage message (nodesGE.sendNodes, as its nodesVE.getNodes sends it) with only
-- the beams broken or bent: { nodes = {}, beams = { ["cid+1"] = { rest length, broken, deformation } } }. (BeamMP's
-- getNodes also lists every node's position and every beam: ~200 KB for a pickup, decoded on every other player's
-- game; it also called a global Round that BeamNG 0.40's vehicle Lua lacks, so it was never sent.) A copy without
-- our mod applies the same message with BeamMP's applyNodes: the listed beams only. (Vehicle Lua)
-- A beam is bent when its rest length is 2 mm or more off its length in the car's jbeam (node positions): BeamNG's
-- getBeamDeformation flags few of them (17 beams on a copy with 134 broken), and its dents were lost.
local DAMAGE_VE = [[local s = { nodes = {}, beams = {} } local nodes = v.data.nodes or {} ]]
  .. [[for _, b in pairs(v.data.beams) do local c = b.cid local br, df = obj:beamIsBroken(c), obj:getBeamDeformation(c) ]]
  .. [[local bent = df > 0.001 if not br and not bent then local n1, n2 = nodes[b.id1], nodes[b.id2] ]]
  .. [[if n1 and n2 and n1.pos and n2.pos then bent = math.abs(obj:getBeamRestLength(c) - (n1.pos - n2.pos):length()) > 0.002 end end ]]
  .. [[if br or bent then ]]
  .. [[s.beams[tostring(c + 1)] = { math.floor(obj:getBeamRestLength(c) * 1000 + 0.5) / 1000, br, math.floor(df * 1000 + 0.5) / 1000 } end end ]]
  .. [[obj:queueGameEngineLua("nodesGE.sendNodes('" .. jsonEncode(s) .. "', " .. obj:getID() .. ")")]]

M.DAMAGE_VE = DAMAGE_VE      -- (tests)

-- Our own cars' damage to the players near (see the top). (lat, lon): our place.
function M.sendDamage(lat, lon, dt)
  local now, newcomer, any = {}, false, false
  local mine = myPid()
  for k, a in pairs(anchors) do
    if k ~= mine and type(a) == 'table' and tonumber(a.lat) and tonumber(a.lon)
        and M.dist(lat, lon, tonumber(a.lat), tonumber(a.lon)) <= M.DAMAGE_NEAR_M then
      now[k] = true
      any = true
      if not members[k] then newcomer = true end
    end
  end
  members = now
  local v = mp()
  if not any or not v or type(v.getVehicles) ~= 'function' then return end
  local objs = rawget(_G, 'map') and map.objects or {}
  local ok, all = pcall(v.getVehicles)
  if not ok or type(all) ~= 'table' then return end
  for _, veh in pairs(all) do
    local gid = type(veh) == 'table' and veh.gameVehicleID
    if gid and v.isOwn(gid) then
      local o = objs[gid]
      local dmg = o and tonumber(o.damage)
      local send
      send, damageSince[gid] = M.damageDue(newcomer, dmg, sentDamage[gid], damageSince[gid], dt)
      local obj = send and vehicle(gid)
      if obj then
        obj:queueLuaCommand(DAMAGE_VE)
        sentDamage[gid] = dmg or 0
      end
    end
  end
end

-- The session's end (BeamMP left, the level's end): the deactivated cars back on, everything forgotten.
function M.reset()
  for gid in pairs(deactivated) do
    local veh = vehicle(gid)
    if veh then veh:setActive(1) end
    M.nametag(gid, false)
  end
  for gid in pairs(floating) do
    local veh = vehicle(gid)
    if veh then veh:queueLuaCommand('obj:setGravity(' .. gravityNow() .. ')') end
  end
  floating = {}
  pendingNodes, lastNodes, copyBase, copySince, copyQuiet, stale, morphs, applied, witnessed = {}, {}, {}, {}, {}, {}, {}, {}, {}
  nodesBySid = {}
  M.applyServerSky(nil)       -- (the real time and weather again after a session with the server's)
  deactivated, anchors, poseIn, hideIn, joined, wentTo, camPlaced, lastPlayers = {}, {}, 0, 0, false, false, false, -1
  waitGround = {}
  members, sentDamage, damageSince, restoredAt, restores = {}, {}, {}, {}, {}
  -- share, config, configWait, convergeIn, anchorSince, lastAnchorKey = nil, nil, 0, 0, 0, nil   -- (shared world building)
  stats, statusIn = { anchors = 0, go = 0, poses = 0, converged = 0, peers = 0 }, M.STATUS_S
  require('beamngEarth/mpanchor').reset()
end

-- The other players for the world map: { { pid, name, lat, lon, h } } (pid: the server's player id as a string, what
-- the map keys and follows them by: two players can have the same name; h: heading in degrees from true north, nil
-- when they have no car), from the plugin's latest bngeAnchors (POSE_S old at most).
function M.others()
  local out, mine = {}, myPid()
  for k, a in pairs(anchors) do
    if k ~= mine and type(a) == 'table' and tonumber(a.lat) and tonumber(a.lon) then
      out[#out + 1] = { pid = k, name = type(a.name) == 'string' and a.name or ('player ' .. k), lat = tonumber(a.lat),
        lon = tonumber(a.lon), h = tonumber(a.h) }
    end
  end
  table.sort(out, function(x, y) if x.name ~= y.name then return x.name < y.name end return x.pid < y.pid end)
  return out
end

-- (tests, the dev tools)
function M.state() return { anchors = anchors, deactivated = deactivated, joined = joined, wentTo = wentTo } end

return M
