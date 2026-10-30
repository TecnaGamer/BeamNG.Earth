-- luajit mod/tests/test_mpsession.lua  (BeamMP sessions, mpsession.lua): poses sent each POSE_S with the anchor and
-- the place (the first after joining asks for the meeting place; at once when our anchor changes); the cars of players
-- whose anchor is unknown deactivated, back on when it is known (mpanchor converts their positions); the go-to asked of
-- core; damage sent when a player comes near; reset turns the cars back on.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local ms = require('beamngEarth/mpsession')

-- pure: who is hidden
local hide = ms.toHide({ [10] = 1, [11] = 2, [12] = 3 }, function(pid) return pid == 1 or pid == 2 end)
T.check(not hide[10] and not hide[11] and hide[12], 'toHide: an unknown anchor hidden, known ones not')

-- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
-- -- pure: the sharing port: the server's (bngeConfig) when it is a usable one
-- T.check(ms.sharePort({ sharePort = 50000 }) == 50000, 'sharePort: the server\'s port')
-- T.check(ms.sharePort(nil) == nil, 'sharePort: no settings')
-- for _, bad in ipairs({ 80, 70000, 5.5, 'x' }) do
--   T.check(ms.sharePort({ sharePort = bad }) == nil, 'sharePort: a bad port left out: ' .. tostring(bad))
-- end

-- BeamMP's client API, stubbed
local handlers, sent = {}, {}
function AddEventHandler(name, fn) handlers[name] = fn end
function TriggerServerEvent(name, data) sent[#sent + 1] = { name = name, data = data } end
function jsonEncode(t)
  local parts = {}
  for k, v in pairs(t) do parts[#parts + 1] = string.format('%q:%s', k, tostring(v)) end
  table.sort(parts)
  return '{' .. table.concat(parts, ',') .. '}'
end
local decoded = {}
function jsonDecode(s) return decoded[s] end
local cars = {}
local function car(gid, owner, own)
  local o = S.newObject('BeamNGVehicle')
  o.active = 1
  function o:setActive(a) self.active = a end
  function o:queueLuaCommand(c) self.cmds = self.cmds or {}; self.cmds[#self.cmds + 1] = c end
  S.objects[gid] = o
  cars[gid] = { gameVehicleID = gid, ownerID = owner, own = own }
  return o
end
MPVehicleGE = { getVehicles = function() return cars end, isOwn = function(id) return cars[id] and cars[id].own end }
-- the ground under a car coming back on (terrainpatch.groundReady): there, unless a check below says otherwise
local tp = require('beamngEarth/terrainpatch')
local groundOk = true
tp.groundReady = function() return groundOk end
local asked = {}
beamngEarth_core = { rebaseTo = function(lat, lon) asked.rebase = { lat, lon } end,
  teleport = function(lat, lon) asked.go = { lat, lon } end }
local anchor = { lat = 36.5, lon = -118.1 }
function anchor:enuToGeodetic(x, y) return self.lat + y / 1e5, self.lon + x / 1e5, 0 end
function anchor:worldToGeodetic(x, y, z) return self:enuToGeodetic(x, y, z) end

local mine = car(20, 1, true)
local other = car(21, 2, false)
local stranger = car(22, 3, false)
ms.onPostJoin()
ms.update(0.1, anchor, { x = 100, y = 0, z = 0 })
T.check(handlers.bngeAnchors and handlers.bngeGoTo, 'the plugin\'s events handled')
T.check(#sent == 1 and sent[1].name == 'bngePose' and sent[1].data:find('"joined":true', 1, true)
  and sent[1].data:find('"aLat":36.5', 1, true), 'the first pose: anchor, place, joined')
T.check(other.active == 0 and stranger.active == 0 and mine.active == 1, 'no anchors yet: the others\' cars off')
ms.update(1, anchor, { x = 100, y = 0, z = 0 })
T.check(#sent == 1, 'the next pose after POSE_S')
ms.update(1.1, anchor, { x = 100, y = 0, z = 0 })
T.check(#sent == 2 and sent[2].data:find('"joined":false', 1, true), 'the next pose, not joining any more')

-- the plugin's anchors: player 2 known (another anchor: mpanchor converts its positions), player 3 not
decoded.g = { ['1'] = { aLat = 36.5, aLon = -118.1, lat = 36.5, lon = -118.1 },
  ['2'] = { aLat = 36.6, aLon = -118.0, lat = 36.6, lon = -118.0 } }
handlers.bngeAnchors('g')
ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
T.check(other.active == 1 and stranger.active == 0, 'a known anchor: on (converted); unknown: still off')

ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
T.check(require('beamngEarth/mpanchor').knows(2) and not require('beamngEarth/mpanchor').knows(3),
  'mpanchor told the anchors')

-- no ground where a car comes back on: it stays off, its spot a collision focus, until the ground is there (or
-- GROUND_WAIT_S passed)
local late = car(23, 4, false)
local later = car(24, 5, false)
ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
T.check(late.active == 0 and later.active == 0, 'two more players\' cars, anchors unknown: off')
decoded.g45 = { ['1'] = decoded.g['1'], ['2'] = decoded.g['2'],
  ['4'] = { aLat = 36.6, aLon = -118.0, lat = 36.6, lon = -118.0 }, ['5'] = { aLat = 36.6, aLon = -118.0, lat = 36.6, lon = -118.0 } }
groundOk = false
handlers.bngeAnchors('g45')
ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
T.check(late.active == 0 and later.active == 0 and #ms.groundFoci() == 2,
  'no ground under them: still off, their spots collision foci')
groundOk = true
ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
T.check(late.active == 1 and later.active == 1 and #ms.groundFoci() == 0, 'the ground there: on')
local last = car(25, 6, false)
ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
decoded.g6 = { ['1'] = decoded.g['1'], ['2'] = decoded.g['2'], ['4'] = decoded.g45['4'], ['5'] = decoded.g45['5'],
  ['6'] = { aLat = 36.6, aLon = -118.0, lat = 36.6, lon = -118.0 } }
groundOk = false
handlers.bngeAnchors('g6')
ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
T.check(last.active == 0, 'no ground: off')
ms.update(ms.GROUND_WAIT_S + 1, anchor, { x = 100, y = 0, z = 0 })
T.check(last.active == 1 and #ms.groundFoci() == 0, 'no ground after GROUND_WAIT_S: on anyway')
groundOk = true

-- our anchor changes (a rebase): a pose at once, not after POSE_S
local n0 = #sent
require('beamngEarth/mpanchor').announceOwn(36.7, -118.1)
ms.update(0.01, anchor, { x = 100, y = 0, z = 0 })
T.check(#sent == n0 + 1, 'our anchor changed: a pose at once')

-- the go-to asked of core (once a session, a little beside the other player)
decoded.t = { lat = 36.7, lon = -118.3 }
handlers.bngeGoTo('t')
T.check(asked.go and asked.go[1] == 36.7 and asked.go[2] > -118.3, 'go-to: beside the other player')
asked.go = nil
handlers.bngeGoTo('t')
T.check(asked.go == nil, 'go-to once a session')

-- other players' cars off our block float (gravity off, left out of the collision foci), on it land again
local blockBox = nil
tp.block = function() return blockBox end
other.pos = vec3(5000, 0, 0)
ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
T.check(not ms.isFloating(21), 'no block known: nothing floats')
blockBox = { -512, -512, 512, 512 }
other.cmds = {}
ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
T.check(ms.isFloating(21) and other.cmds[#other.cmds] == 'obj:setGravity(0)', 'off our block: floats, gravity off')
other.pos = vec3(500, 0, 0)       -- (within FLOAT_IN_M of the edge: still floats)
ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
T.check(ms.isFloating(21), 'near the edge: still floating (hysteresis)')
other.pos = vec3(0, 0, 0)
core_environment = core_environment or {}
local oldGetG = core_environment.getGravity
core_environment.getGravity = function() return -9.81 end
ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
T.check(not ms.isFloating(21) and other.cmds[#other.cmds] == 'obj:setGravity(-9.81)', 'on our block: lands, gravity back')
core_environment.getGravity = oldGetG
blockBox = nil

-- damage: due when a player comes near; after a change of DAMAGE_STEP that held DAMAGE_SETTLE_S
T.check(ms.damageDue(true, 500, 0, 0, 0.5) == true, 'damage: a newcomer sees a damaged car: sent')
T.check(ms.damageDue(true, 0, 0, 0, 0.5) == false, 'damage: a newcomer and an undamaged car: nothing to send')
T.check(ms.damageDue(true, 0, nil, 0, 0.5) == false, 'damage: a newcomer, an undamaged car never sent: nothing')
T.check(ms.damageDue(true, 0, 5000, 0, 0.5) == true, 'damage: a newcomer, our car reset since a wreck was sent: sent')
local due, settled = ms.damageDue(false, 10, 0, 0, 0.5)
T.check(due == false and settled == 0, 'damage: a small change: not sent')
due, settled = ms.damageDue(false, 500, 0, 0, 0.5)
T.check(due == false and settled == 0.5, 'damage: a big change: waits to settle')
due = ms.damageDue(false, 500, 0, ms.DAMAGE_SETTLE_S, 0.5)
T.check(due == true, 'damage: settled: sent')
-- sendDamage: our car's state asked of BeamMP's nodesVE when player 2 comes near (we are player 1)
MPConfig = { getPlayerServerID = function() return 1 end }
ms.reset()
decoded.n = { ['1'] = { aLat = 36.5, aLon = -118.1, lat = 36.5, lon = -118.1 },
  ['2'] = { aLat = 36.6, aLon = -118.0, lat = 36.5005, lon = -118.1 } }
decoded.f = { ['1'] = decoded.n['1'], ['2'] = { aLat = 36.6, aLon = -118.0, lat = 36.6, lon = -118.0 } }
local cmds = {}
function mine:queueLuaCommand(c) cmds[#cmds + 1] = c end
map = { objects = { [20] = { damage = 0 } } }
handlers.bngeAnchors('f')
ms.sendDamage(36.5, -118.1, 0.5)
T.check(#cmds == 0, 'damage: nobody near: nothing sent')
handlers.bngeAnchors('n')
ms.sendDamage(36.5, -118.1, 0.5)
T.check(#cmds == 0, 'damage: a player came near, our car undamaged: nothing to send')
map.objects[20].damage = 900
for _ = 1, 6 do ms.sendDamage(36.5, -118.1, 0.5) end
T.check(#cmds == 1 and cmds[1]:find('nodesGE.sendNodes', 1, true) and cmds[1]:find('getBeamDeformation', 1, true),
  'damage: sent once after it changed and settled (the broken and bent beams, on BeamMP\'s path): ' .. #cmds)
do  -- run it as vehicle Lua: only the broken and bent beams go out
  local got, gl = nil, nil
  local env = { pairs = pairs, tostring = tostring, math = math,
    v = { data = { beams = { { cid = 0, id1 = 0, id2 = 1 }, { cid = 1, id1 = 0, id2 = 1 }, { cid = 2, id1 = 0, id2 = 1 },
      { cid = 3, id1 = 0, id2 = 1 } }, nodes = { [0] = { pos = vec3(0, 0, 0) }, [1] = { pos = vec3(1.23456, 0, 0) } } } },
    obj = { beamIsBroken = function(_, c) return c == 0 end, getBeamDeformation = function(_, c) return c == 1 and 0.25 or 0 end,
      getBeamRestLength = function(_, c) return c == 3 and 1.1 or 1.23456 end, getID = function() return 20 end,
      queueGameEngineLua = function(_, c) gl = c end },
    jsonEncode = function(t) got = t return 'J' end }
  local fn = assert(loadstring(cmds[1]))
  setfenv(fn, env)
  fn()
  T.check(got and got.beams['1'] and got.beams['1'][2] == true and got.beams['2'] and got.beams['2'][3] == 0.25
    and got.beams['3'] == nil and next(got.nodes) == nil and got.beams['1'][1] == 1.235
    and got.beams['4'] and got.beams['4'][1] == 1.1, 'the broken and bent beams only (rest length off its jbeam length too), rounded')
  T.check(gl == "nodesGE.sendNodes('J', 20)", 'sent on BeamMP\'s full-damage path')
end
ms.sendDamage(36.5, -118.1, 0.5)
T.check(#cmds == 1, 'damage: not again while nothing changes')
handlers.bngeAnchors('f')
ms.sendDamage(36.5, -118.1, 0.5)
handlers.bngeAnchors('n')
ms.sendDamage(36.5, -118.1, 0.5)
T.check(#cmds == 2, 'damage: the damaged car to a player who came near again: ' .. #cmds)

-- others: the map's arrows, every player but us, with names and headings
decoded.o = { ['1'] = { aLat = 36.5, aLon = -118.1, lat = 36.5, lon = -118.1, name = 'me', h = 10 },
  ['2'] = { aLat = 36.6, aLon = -118.0, lat = 36.6, lon = -118.0, name = 'bob', h = 271.5 },
  ['3'] = { aLat = 36.6, aLon = -118.0, lat = 36.7, lon = -118.0 } }
handlers.bngeAnchors('o')
local o = ms.others()
T.check(#o == 2 and o[1].name == 'bob' and o[1].h == 271.5 and o[2].name == 'player 3' and o[2].h == nil,
  'others: the other players, named, with headings')
T.check(o[1].pid == '2' and o[2].pid == '3', 'others: with their server ids (the map keys and follows players by them)')
-- two players of the same name stay two (their ids), in the same order every time
decoded.same = { ['4'] = { aLat = 0, aLon = 179.9, lat = 0, lon = 179.9, name = 'bob' }, ['2'] = decoded.o['2'] }
handlers.bngeAnchors('same')
o = ms.others()
T.check(#o == 2 and o[1].pid == '2' and o[2].pid == '4' and o[1].name == o[2].name, 'others: same-named players apart, by id')
handlers.bngeAnchors('o')

-- a car the game deleted here (BeamMP's sphere) comes back once its owner is near, not again at once
local restored = {}
MPVehicleGE.restorePlayerVehicle = function(name) restored[#restored + 1] = name end
local oldGet = MPVehicleGE.getVehicles
MPVehicleGE.getVehicles = function()
  return { ['2-0'] = { isDeleted = true, ownerID = 2, ownerName = 'bob', gameVehicleID = 99 },
    ['3-0'] = { isDeleted = true, ownerID = 3, ownerName = 'far', gameVehicleID = 98 } }
end
decoded.r = { ['2'] = { aLat = 36.6, aLon = -118.0, lat = 36.5005, lon = -118.1 },
  ['3'] = { aLat = 40, aLon = -100, lat = 40, lon = -100 } }
handlers.bngeAnchors('r')
T.check(ms.restoreDeleted(36.5, -118.1) == 0, 'a car removed here on purpose (never out of our world): left removed')
require('beamngEarth/mpanchor')._noteFar('2-0')
T.check(ms.restoreDeleted(36.5, -118.1) == 1 and restored[1] == 'bob', 'deleted car of a near player that was out of our world: restored')
T.check(ms.restoreDeleted(36.5, -118.1) == 0, 'not again at once')
MPVehicleGE.getVehicles = oldGet

-- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
-- -- pickAnchor: the shared anchor near a place (Utah's three players on one, Italy's alone; a lone far one ignored)
-- local utah = { ['1'] = { aLat = 40.0, aLon = -111.9 }, ['2'] = { aLat = 40.0, aLon = -111.9 },
--   ['3'] = { aLat = 40.02, aLon = -111.9 }, ['4'] = { aLat = 45.46, aLon = 9.19 } }
-- local la, lo, n = ms.pickAnchor(utah, 40.01, -111.9)
-- T.check(la == 40.0 and lo == -111.9 and n == 2, 'pickAnchor: the one most players near use')
-- la = ms.pickAnchor(utah, 45.47, 9.19)
-- T.check(la == 45.46, 'pickAnchor: Italy keeps its own')
-- T.check(ms.pickAnchor(utah, 30, 0) == nil, 'pickAnchor: nobody near: none')
-- la = ms.pickAnchor({ ['1'] = { aLat = 40.0, aLon = -111.9 }, ['2'] = { aLat = 40.01, aLon = -111.9 } }, 40.005, -111.9)
-- T.check(la == 40.0, 'pickAnchor: a tie goes to the lowest key (every player picks the same)')
-- la = ms.pickAnchor(utah, 40.01, -111.9, '1')
-- T.check(la == 40.0 or la == 40.02, 'pickAnchor: skip leaves our own out')

T.check(ms.adoptAnchor(40, -111.9) == nil, 'adoptAnchor: none (shared world building commented out)')

-- reset: every car back on
ms.reset()
T.check(stranger.active == 1 and next(ms.state().deactivated) == nil, 'reset: the cars back on')
-- copies repaired: BeamMP's full-damage message for another player's car is kept, and put on once the car is
-- SYNC_FAR_M from our camera (near us it keeps what our game worked out); its vehicle Lua checks it first (beams
-- broken here only: reset, then the owner's state REPAIR_FRAMES frames later)
do
  local passed = {}
  nodesGE = { handle = function(raw) passed[#passed + 1] = raw end }
  MPHelpers = { b64encode = function(d) return 'B64(' .. d .. ')' end }
  local vehs = { ['2-0'] = { isLocal = false, gameVehicleID = 21, ownerID = 2 }, ['1-0'] = { isLocal = true, gameVehicleID = 20, ownerID = 1 } }
  local oldMP = MPVehicleGE
  MPVehicleGE = { getVehicles = function() return vehs end, getGameVehicleID = function(sid) return sid == '2-0' and 21 or 20 end,
    isOwn = function(id) return id == 20 end }
  handlers.bngeAnchors('g')
  T.check(ms.wrapNodes() and ms.wrapNodes(), 'wrapped once')
  other.cmds = {}
  nodesGE.handle('g:2-0:{"x":1}')
  nodesGE.handle('n:1-0:{"own":1}')
  T.check(#passed == 1 and #other.cmds == 0, 'break groups: as BeamMP does; our own car\'s full damage: never to BeamMP')
  core_camera = { getPosition = function() return vec3(100, 0, 0) end }     -- (our camera at the player)
  other.pos = vec3(150, 0, 0)          -- 50 m from the camera: we see its crash
  nodesGE.handle('n:2-0:{"beams":{}}')
  T.check(#passed == 1 and #other.cmds == 0, 'another player\'s full damage: kept, nothing put on yet')
  -- a car not known here yet (BeamMP still spawning it): never to BeamMP's applyNodes (BeamNG 0.40: a fatal error in
  -- the copy's vehicle Lua), kept by its BeamMP id and adopted once known
  nodesGE.handle('n:3-0:{"beams":{"7":1}}')
  T.check(#passed == 1, 'an unknown car\'s full damage: not to BeamMP')
  ms.adoptNodes()           -- (still unknown: kept)
  ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
  T.check(#other.cmds == 0, 'near our camera: the copy keeps what our game worked out')
  other.pos = vec3(100 + ms.SYNC_RANGE_M + 10, 0, 0)
  ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
  T.check(#other.cmds == 0, 'out of range: only kept')
  other.pos = vec3(100 + ms.SYNC_FAR_M + 10, 0, 0)
  ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
  T.check(#other.cmds == 1 and other.cmds[1]:find('requestReset(RESET_PHYSICS)', 1, true)
    and other.cmds[1]:find('"{\\"beams\\":{}}", true ', 1, true), 'far from our camera: checked in its vehicle Lua, put on (reset if beams broke here only)')
  ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
  T.check(#other.cmds == 1, 'put on once')
  ms.afterRepairReset(21, 5)
  for _ = 1, ms.REPAIR_FRAMES do ms.update(0.01, anchor, { x = 100, y = 0, z = 0 }) end
  T.check(other.cmds[#other.cmds]:find('applyDamage(d)', 1, true) ~= nil, 'after the reset: the owner\'s state applied')
  T.check(loadstring(other.cmds[1]) ~= nil and loadstring(other.cmds[#other.cmds]) ~= nil, 'the vehicle Lua compiles')
  -- run it as the vehicle Lua of BeamNG 0.40 (beamstate.onBeamDeformed; no beamDeformed): the broken beams at once,
  -- the dents grown in by the morph steps, which M.update sends MORPH_STEPS frames in a row
  do
    local broke, lens, deformedN, morphFrom = {}, { [0] = 1, [1] = 1, [2] = 2 }, 0, nil
    local env = { type = type, pairs = pairs, tonumber = tonumber, tostring = tostring, pcall = pcall, rawset = rawset, rawget = rawget,
      jsonDecode = function() return { nodes = { { { x = 0, y = 0, z = 0 } } },
        beams = { { 1.5, false, 0.2 }, { 1, true, 0 }, { 2, false, 0 } } } end,
      mime = { unb64 = function(x) return x end },
      obj = { setNodeMass = function() end, beamIsBroken = function(_, cid) return broke[cid] == true end,
        breakBeam = function(_, cid) broke[cid] = true end, setBeamLength = function(_, cid, l) lens[cid] = l end,
        getBeamRestLength = function(_, cid) return lens[cid] end, getID = function() return 21 end,
        queueGameEngineLua = function(_, c) morphFrom = c end,
        setNodePosition = function() error('no node positions') end },
      beamstate = { beamBroken = function() end, onBeamDeformed = function() deformedN = deformedN + 1 end } }
    local fn = assert(loadstring(other.cmds[#other.cmds]))
    env._G = env
    setfenv(fn, env)
    fn()
    T.check(broke[1] and lens[0] == 1 and deformedN == 0, 'the broken beams at once, the dents not yet')
    T.check(morphFrom and morphFrom:find('startMorph(21)', 1, true), 'the morph started (GE told)')
    local n = #other.cmds
    ms.startMorph(21)
    for _ = 1, ms.MORPH_STEPS do ms.update(0.01, anchor, { x = 100, y = 0, z = 0 }) end
    T.check(#other.cmds == n + ms.MORPH_STEPS and other.cmds[#other.cmds]:find('bngeMorph(1.0000)', 1, true),
      'a morph step each frame, the last at 1')
    for k = n + 1, #other.cmds do
      local step = assert(loadstring(other.cmds[k]))
      setfenv(step, env)
      step()
      if k == n + ms.MORPH_STEPS / 2 then T.near(lens[0], 1.25, 1e-3, 'half way: half the dent') end
    end
    T.check(lens[0] == 1.5 and lens[2] == 2 and deformedN == 1 and env.bngeDmg == nil, 'BeamNG 0.40: the dents on at the end')
    ms.update(0.01, anchor, { x = 100, y = 0, z = 0 })
    T.check(#other.cmds == n + ms.MORPH_STEPS, 'no more steps')
  end
  -- the same state is not put on again when the copy's damage moves afterwards (loose parts torn off by the dents);
  -- a new one is
  map = { objects = { [21] = { damage = 1000 } } }
  local function settle() for _ = 1, 40 do ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 }) end end
  settle()
  local n = #other.cmds
  map.objects[21].damage = 3000
  settle()
  T.check(#other.cmds == n, 'the same state not put on twice')
  nodesGE.handle('n:2-0:{"beams":{"1":1}}')
  settle()
  T.check(#other.cmds == n + 1 and other.cmds[#other.cmds]:find('requestReset', 1, true), 'a new state is')
  -- damage that came while the car was away goes on near us too (it teleported up to us: we never saw that crash)
  other.pos = vec3(100 + ms.SYNC_RANGE_M + 500, 0, 0)
  nodesGE.handle('n:2-0:{"beams":{"2":1}}')
  n = #other.cmds
  other.pos = vec3(130, 0, 0)
  ms.update(ms.HIDE_S, anchor, { x = 100, y = 0, z = 0 })
  T.check(#other.cmds == n + 1, 'a state that came while it was away: put on near us too')
  core_camera = nil
  map = nil
  MPVehicleGE = oldMP
  nodesGE, MPHelpers = nil, nil
end

-- a copy's own damage checked again: once it moved DAMAGE_STEP from where it settled, for COPY_SETTLE_S
T.check(ms.copyDue(30, 0, 0, 0.5) == false, 'copy check: a small change is not due')
T.check(ms.copyDue(500, nil, 0, 0.5) == false, 'copy check: no settled figure yet')
do
  local due, since = ms.copyDue(500, 0, 0, 0.5)
  T.check(due == false and since == 0.5, 'copy check: a big change starts the wait')
  due = ms.copyDue(500, 0, ms.COPY_SETTLE_S, 0.5)
  T.check(due == true, 'copy check: due after COPY_SETTLE_S')
  local _, back = ms.copyDue(510, 500, 3, 0.5)
  T.check(back == 0, 'copy check: back near its figure: the wait starts over')
end

do -- a car switched off here: its BeamMP name tag hidden with it, shown again after
  local rec = {}
  local saved = rawget(_G, 'MPVehicleGE')
  MPVehicleGE = { getVehicleByGameID = function(gid) return gid == 7 and rec or nil end }
  ms.nametag(7, true)
  T.check(rec.hideNametag == true, 'hidden with the car')
  ms.nametag(7, false)
  T.check(rec.hideNametag == nil, 'shown again')
  ms.nametag(8, true)                       -- (a car BeamMP does not know: nothing)
  MPVehicleGE = nil
  ms.nametag(7, true)                       -- (no BeamMP: nothing)
  MPVehicleGE = saved
end
do -- no car (BeamMP joins without one): the free camera on the ground, and lifted when under it. A teleport's target is
   -- core's to put the camera at (core.moveCamera; test_teleport_nocar.lua), so not the join's first spot after it
  local savedBe, savedCam, savedGet, savedQ = rawget(_G, 'be'), rawget(_G, 'core_camera'), rawget(_G, 'getPlayerVehicle'), rawget(_G, 'quatFromDir')
  local ground, put = 100, nil
  be = { getSurfaceHeightBelow = function(_, p) return ground end }
  core_camera = { setPosRot = function(_, x, y, z) put = { x, y, z } end }
  getPlayerVehicle = function() return nil end
  quatFromDir = quatFromDir or function() return { x = 0, y = 0, z = 0, w = 1 } end
  ms.onPostJoin()
  ms.placeCamera({ x = 5, y = 5, z = 0 })
  T.check(put and put[1] == 0 and put[2] == -8 and put[3] == 103, 'first: on the ground at the origin')
  put = nil
  ms.placeCamera({ x = 0, y = -8, z = 400 })
  T.check(put == nil, 'a free camera flown up stays')
  ms.placeCamera({ x = 30, y = 40, z = 50 })
  T.check(put and put[1] == 30 and put[2] == 40 and put[3] == 103, 'under the ground: lifted where it is, 3 m over it')
  -- a teleport asked before the join's first placement (the ground not in yet): core places the camera, not the join
  ms.onPostJoin()
  put = nil
  ms.onTeleport()
  ms.placeCamera({ x = 900, y = 700, z = 106 })
  T.check(put == nil, 'after a teleport: the camera core put at its target (over the ground) is left there')
  ms.placeCamera({ x = 900, y = 700, z = 60 })
  T.check(put and put[1] == 900 and put[2] == 700, 'after a teleport: still lifted where it is when under the ground')
  -- under the terrain but over collision just below: a tunnel's tube (core put it there, or flown in): left there
  local savedRay = rawget(_G, 'castRayStatic')
  local rayFrom = nil
  castRayStatic = function(from, dir, len) rayFrom = from.z; return 2.5 end
  put = nil
  ms.placeCamera({ x = 900, y = 700, z = 60 })
  T.check(put == nil, 'in the tube of a tunnel (collision 2.5 m under it): not lifted out onto the hill')
  T.check(rayFrom and rayFrom <= 60.1 + 1e-9, 'the ray from the camera (from higher it met the ground over it)')
  castRayStatic = function(from, dir, len) return len end        -- (the engine's "nothing hit": the length)
  ms.placeCamera({ x = 900, y = 700, z = 60 })
  T.check(put and put[3] == 103, 'under the terrain over nothing: lifted as before')
  castRayStatic = savedRay
  put = nil
  getPlayerVehicle = function() return { id = 1 } end
  ms.placeCamera({ x = 0, y = 0, z = -50 })
  T.check(put == nil, 'a car: the camera is never moved here')
  be, core_camera, getPlayerVehicle, quatFromDir = savedBe, savedCam, savedGet, savedQ
end
T.done('test_mpsession')
