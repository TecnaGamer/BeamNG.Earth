-- luajit mod/tests/test_mpdefer.lua  (BeamMP: far players' cars not spawned here, mpdefer.lua): a far player's car
-- spawn is kept on BeamMP's own record (no spawn, no queue entry, no notice), its edits and paint go into what is
-- kept, it spawns with its latest config at its latest place once it comes near (and its ground is in), by the
-- player's own queue settings; a far spawned car's edit waits until it is near and on; our own and near cars pass;
-- a car whose place is not known spawns after UNKNOWN_WAIT_S; a stale hook hands everything back; without BeamMP's
-- functions nothing is wrapped. BeamMP's MPVehicleGE is a stand-in that follows its code (onServerVehicleSpawned /
-- Edited / Removed / ColorChanged, applyVehSpawn / applyVehEdit, the spawn queue).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local J = dofile(dir .. '/minijson.lua')
function jsonEncode(t) return J.encode(t) end
function jsonDecode(s) return J.decode(s) end
local logs = {}
function log(level, tag, msg) logs[#logs + 1] = msg end

local geodesy = require('beamngEarth/geodesy')
local ma = require('beamngEarth/mpanchor')
local md = require('beamngEarth/mpdefer')

-- pure
T.check(select(2, md.parse('s:USER:Some Name:4-0:{"a":1}')) == '4-0', 'parse: a spawn\'s vehicle id')
local c1, s1, d1, r1, n1 = md.parse('s:USER:Some Name:4-0:{"a":1}')
T.check(c1 == 's' and d1 == '{"a":1}' and r1 == 'USER' and n1 == 'Some Name', 'parse: a spawn\'s parts (BeamMP\'s pattern)')
T.check(select(2, md.parse('c:4-1:{"b":2}')) == '4-1' and select(3, md.parse('p:4-1:[1]')) == '[1]', 'parse: edit, paint')
T.check(select(2, md.parse('d:12-3')) == '12-3', 'parse: a removal')
T.check(md.parse('r:4-0:{}') == 'r' and select(2, md.parse('r:4-0:{}')) == nil and md.parse(nil) == nil,
  'parse: other packets: the code only')
T.check(md.verdict(5e6, false, 0) == 'hold' and md.verdict(1000, false, 0) == 'spawn', 'verdict: far held, near spawned')
T.check(md.verdict(27000, false, 0) == 'spawn' and md.verdict(27000, true, 0) == 'hold'
  and md.verdict(20000, true, 0) == 'spawn', 'verdict: between RELEASE_M and FAR_M as it is (no flapping)')
T.check(md.verdict(nil, false, 0) == 'hold' and md.verdict(nil, true, md.UNKNOWN_WAIT_S) == 'spawn',
  'verdict: place not known: held, spawned after UNKNOWN_WAIT_S')
T.check(md.groundDue(true, 0) and not md.groundDue(false, 1) and md.groundDue(false, md.GROUND_WAIT_S), 'groundDue')
T.check(md.holdEdit(5e6) and not md.holdEdit(100) and not md.holdEdit(nil), 'holdEdit: only known far')
T.check(md.editDue(100, false) and md.editDue(nil, false) and not md.editDue(100, true) and not md.editDue(5e6, false),
  'editDue: near (or not known) and on')
local mg = md.mergeEdit({ vid = 7, pos = { 1, 2, 3 }, rot = { 0, 0, 0, 1 }, jbm = 'pickup', vcf = { a = 1 } },
  { jbm = 'us_semi', vcf = { b = 2 }, pos = { 9, 9, 9 }, pro = '1' })
T.check(mg.jbm == 'us_semi' and mg.vcf.b == 2 and mg.vid == 7 and mg.pos[1] == 1 and mg.pro == '1',
  'mergeEdit: the edit\'s config, the spawn\'s place and remote id')
local wp = md.withPose({ pos = { 1, 2, 3 }, rot = { 0, 0, 0, 1 }, jbm = 'x' }, { x = 10, y = 20, z = 30, rot = { 0, 0, 1, 0 } })
T.check(wp.pos[1] == 10 and wp.pos[3] == 30 and wp.rot[3] == 1 and wp.jbm == 'x', 'withPose: the latest place')
T.check(md.withPaints({}, { 5 }).vcf.paints[1] == 5, 'withPaints')

-- BeamMP's MPVehicleGE, as its code does it
local sv = { enableSpawnQueue = false, queueSkipUnicycle = true, highlightQueuedPlayers = true }
local function getValue(k, d) local v = sv[k]; if v == nil then return d end; return v end
settings = { getValue = getValue }
local notes, queueUI = {}, {}
UI = { showNotification = function(t) notes[#notes + 1] = t end, updateQueue = function(s, e) queueUI[#queueUI + 1] = { s, e } end }
local MYPID = 0
MPConfig = { getPlayerServerID = function() return MYPID end }
local vehicles, players, configs = {}, {}, {}
local spawned, edited, lostEdits, colored, removed = {}, {}, 0, 0, {}
local nextGid = 1000
local function applyVehSpawn(ev)
  local d = jsonDecode(ev.data)
  local v = vehicles[ev.serverVehicleID]
  nextGid = nextGid + 1
  v.gameVehicleID, v.isSpawned, v.jbeam = nextGid, true, d.jbm
  spawned[#spawned + 1] = { sid = ev.serverVehicleID, data = d }
end
local function applyVehEdit(sid, data)
  local v = vehicles[sid]
  if not v or v.gameVehicleID < 0 then lostEdits = lostEdits + 1 return end
  edited[#edited + 1] = { sid = sid, data = jsonDecode(data) }
end
local function onServerVehicleSpawned(role, name, sid, data)
  local d = jsonDecode(data)
  local pid = tonumber(sid:match('^(%d+)'))
  if not players[pid] then players[pid] = { name = name, role = { name = role } } end
  if pid == MPConfig.getPlayerServerID() then
    vehicles[sid] = { serverVehicleString = sid, ownerID = pid, isLocal = true, isSpawned = true, gameVehicleID = d.vid }
    return
  elseif vehicles[sid] and vehicles[sid].remoteVehID == d.vid then
    return                                        -- "This ID already exists, syncing vehicles"
  end
  vehicles[sid] = { serverVehicleString = sid, ownerID = pid, ownerName = name, isSpawned = false, isLocal = false,
    gameVehicleID = -1, remoteVehID = d.vid, position = vec3(0, 0, 0) }
  configs[sid] = d
  local ev = { playerNickname = name, serverVehicleID = sid, data = data }
  if settings.getValue('enableSpawnQueue') and not (settings.getValue('queueSkipUnicycle') and d.jbm == 'unicycle') then
    vehicles[sid].spawnQueue = ev
    UI.updateQueue(1, 0)
    UI.showNotification('Spawn received and queued for ' .. name)
  else
    applyVehSpawn(ev)
  end
end
local function onServerVehicleEdited(sid, data)
  if not vehicles[sid] then return end
  local pos, rot = configs[sid].pos, configs[sid].rot          -- (indexes it: a record without a config would throw)
  configs[sid] = jsonDecode(data)
  configs[sid].pos, configs[sid].rot = pos, rot
  if settings.getValue('enableSpawnQueue') then
    vehicles[sid].editQueue = data
    UI.updateQueue(0, 1)
  else
    applyVehEdit(sid, data)
  end
end
local function onServerVehicleRemoved(sid)
  local v = vehicles[sid]
  if not v then return end
  removed[#removed + 1] = { sid = sid, queued = v.spawnQueue ~= nil }
  vehicles[sid] = nil
end
local function onServerVehicleColorChanged(sid, data)
  colored = colored + 1
  if configs[sid] then configs[sid].vcf = configs[sid].vcf or {}; configs[sid].vcf.paints = jsonDecode(data) end
end
local beammpHandle = function(raw)
  local code, rest = raw:sub(1, 1), raw:sub(3)
  if code == 's' then
    local role, name, sid, data = rest:match('^(%w+)%:([%g%s]+)%:(%d+%-%d+)%:({.+})')
    onServerVehicleSpawned(role, name, sid, data)
  elseif code == 'c' then
    local sid, data = rest:match('^(%d+%-%d+)%:({.+})')
    onServerVehicleEdited(sid, data)
  elseif code == 'd' then onServerVehicleRemoved(rest)
  elseif code == 'p' then
    local sid, data = rest:match('^(%d+%-%d+)%:(%[.+%])')
    onServerVehicleColorChanged(sid, data)
  elseif code == 'x' then error('boom')
  end
end
MPVehicleGE = { handle = beammpHandle, getVehicles = function() return vehicles end,
  getPlayers = function() return players end }
local function applyQueuedEvents()
  for sid, v in pairs(vehicles) do
    if v.spawnQueue then local q = v.spawnQueue; v.spawnQueue = nil; applyVehSpawn(q) end
    if v.editQueue then local q = v.editQueue; v.editQueue = nil; applyVehEdit(sid, q) end
  end
end
-- BeamMP's positionGE: a car not spawned takes the packet's position on its record
local placed = {}
positionGE = { handle = function(raw)
  local sid, data = raw:match('^p%:(%d+%-%d+)%:({.*})')
  placed[#placed + 1] = sid
  local v = vehicles[sid]
  local d = jsonDecode(data)
  if v and not v.isSpawned and d.pos then v.position:set(d.pos[1], d.pos[2], d.pos[3]) end
end }
MPCoreNetwork = { isMPSession = function() return true end }

-- mpsession's side (stubbed): owners' places, the ground, switched-off cars
local place = {}           -- pid -> { lat, lon }
local groundOk, off = true, {}
package.loaded['beamngEarth/mpsession'] = {
  ownerPlace = function(pid) local p = place[pid]; if p then return p[1], p[2] end end,
  ownerDist = function(pid)
    local p, la, lo = place[pid], ma.own()
    if p and la then
      local k = math.pi / 180
      return 6371000 * math.sqrt(((p[2] - lo) * k * math.cos((p[1] + la) / 2 * k)) ^ 2 + ((p[1] - la) * k) ^ 2)
    end
  end,
  groundOkAt = function() return groundOk end,
  isOff = function(gid) return off[gid] == true end,
}

local clockT = 0
ma.clock = function() return clockT end
ma.install()
local ME = { 36.5, -118.1 }
ma.announceOwn(ME[1], ME[2])
local PARIS = { 48.85, 2.35 }
ma.setRemote(4, PARIS[1], PARIS[2])
place[4] = PARIS
ma.setRemote(5, 36.52, -118.08)
place[5] = { 36.52, -118.08 }
local function spawnRaw(sid, name, vid, jbm, extra)
  return 's:USER:' .. name .. ':' .. sid .. ':' .. jsonEncode({ vid = vid, jbm = jbm, vcf = { parts = jbm },
    pos = { 1, 2, 3 }, rot = { 0, 0, 0, 1 }, pro = '0' })
end
-- a position packet of player pid's car at (lat, lon), in its owner's frame
local function posRaw(sid, aLat, aLon, lat, lon)
  local x, y, z = geodesy.newAnchor(aLat, aLon):geodeticToWorld(lat, lon, 0)
  return string.format('p:%s:{"pos":[%.6f,%.6f,%.6f],"vel":[0,0,0],"rot":[0,0,0.5,0.866],"rvel":[0,0,0]}', sid, x, y, z)
end
local function tick(n) for _ = 1, n or 1 do clockT = clockT + md.TICK_S; md.update(md.TICK_S) end end

-- not installed before the first update: BeamMP's own handle
T.check(MPVehicleGE.handle == beammpHandle, 'nothing wrapped before the first update')
tick()
T.check(MPVehicleGE.handle ~= beammpHandle, 'installed: MPVehicleGE.handle wrapped')
T.check(md.install() == true, 'install again: a no-op')

-- a far player's spawn (queue off): kept, not spawned, no queue entry, no notice; settings and UI as they were
local getBefore, notesBefore, uiBefore = settings.getValue, #notes, #queueUI
MPVehicleGE.handle(spawnRaw('4-0', 'Far Away', 11, 'pickup'))
local v4 = vehicles['4-0']
T.check(#spawned == 0, 'a far player\'s car: not spawned here')
T.check(v4 and v4.isSpawned == false and v4.gameVehicleID == -1 and v4.ownerID == 4, 'its record BeamMP\'s own (queued-car state)')
T.check(v4.spawnQueue == nil and type(v4.bngeHeld) == 'table' and v4.hideNametag == true, 'its spawn kept on it, its tag hidden')
T.check(settings.getValue == getBefore and rawget(settings, 'getValue') == getBefore, 'settings.getValue put back')
T.check(#notes == notesBefore and #queueUI == uiBefore, 'no "queued" notice, no queue count')
T.check(configs['4-0'] ~= nil, 'BeamMP kept its config (later edits find it)')
applyQueuedEvents()
T.check(#spawned == 0, 'BeamMP\'s queue does not spawn it')

-- our own car and a near player's: as before
MPVehicleGE.handle(spawnRaw('0-0', 'Me', 50, 'pickup'))
T.check(vehicles['0-0'].isLocal and not vehicles['0-0'].bngeHeld, 'our own car: untouched')
-- (Codex review 4) a near BeamNG.Earth player's car goes to BeamMP only with a place in our frame: its spawn's
-- coordinates are in its owner's frame; kept until its first packet, then spawned there
MPVehicleGE.handle(spawnRaw('5-0', 'Near By', 12, 'covet'))
T.check(#spawned == 0 and vehicles['5-0'].bngeHeld, 'a near player\'s car, no packet of its own yet: kept')
positionGE.handle(posRaw('5-0', 36.52, -118.08, 36.5202, -118.0801))
tick()
T.check(#spawned == 1 and spawned[1].sid == '5-0' and not vehicles['5-0'].bngeHeld
  and math.abs(spawned[1].data.pos[1] - ma.seen('5-0').x) < 1e-6, '... its first packet: spawned there')

-- with the player's queue on: a far car kept too; a near one queued as BeamMP does
sv.enableSpawnQueue = true
MPVehicleGE.handle(spawnRaw('4-1', 'Far Away', 13, 'unicycle'))
T.check(vehicles['4-1'].bngeHeld and vehicles['4-1'].spawnQueue == nil and #spawned == 1, 'queue on: a far car kept (a unicycle too)')
sv.enableSpawnQueue = false

-- edits and paint to a kept car: into the kept spawn (the latest config spawns later), nothing built
local editsBefore = #edited
MPVehicleGE.handle('c:4-0:' .. jsonEncode({ jbm = 'us_semi', vcf = { parts = 'semi' }, pro = '1' }))
local kept = jsonDecode(v4.bngeHeld.json)
T.check(#edited == editsBefore and lostEdits == 0 and kept.jbm == 'us_semi' and kept.vid == 11 and kept.pos[1] == 1,
  'an edit to a kept car: into its kept spawn, nothing built')
MPVehicleGE.handle('p:4-0:[{"baseColor":[1,0,0,1]}]')
kept = jsonDecode(v4.bngeHeld.json)
T.check(kept.vcf.paints[1].baseColor[1] == 1 and colored == 1, 'live paint: into the kept spawn, BeamMP\'s own as before')

-- its position packets from Paris: dropped (far), its record placed far away (never a blob at our origin)
local nPlaced = #placed
MPVehicleGE.handle('r:4-0:{}')
positionGE.handle(posRaw('4-0', PARIS[1], PARIS[2], PARIS[1], PARIS[2]))
T.check(#placed == nPlaced and ma.isFar('4-0'), 'its positions: dropped (far)')
local s4 = ma.seen('4-0')
T.check(s4 and s4.d > 5e6, string.format('seen: its distance from our origin (%.0f km)', s4 and s4.d / 1000 or -1))
tick()
T.check(v4.bngeHeld and (v4.position.x ~= 0 or v4.position.y ~= 0 or v4.position.z ~= 0), 'still kept; its record placed far')
T.check(#md.foci() == 0, 'no ground asked for a far car')

-- it comes near (its player teleported next to us): its packets settle, then the ground, then it spawns
ma.setRemote(4, 36.51, -118.09)
place[4] = { 36.51, -118.09 }
clockT = clockT + ma.SETTLE_S + 0.1
positionGE.handle(posRaw('4-0', 36.51, -118.09, 36.5005, -118.1005))
T.check(not ma.isFar('4-0') and ma.seen('4-0').d < 200, 'its packets near')
groundOk = false
tick()
T.check(v4.bngeHeld and #spawned == 1 and #md.foci() == 1, 'near: waits for our ground (a collision focus there)')
groundOk = true
positionGE.handle(posRaw('4-0', 36.51, -118.09, 36.5005, -118.1005))
tick()
local last = spawned[#spawned]
local seenNow = ma.seen('4-0')
T.check(#spawned == 2 and last.sid == '4-0', 'its ground in: spawned')
T.check(last.data.jbm == 'us_semi' and last.data.vcf.paints[1].baseColor[1] == 1, 'with its latest config and paint')
T.check(math.abs(last.data.pos[1] - seenNow.x) < 1e-6 and math.abs(last.data.pos[2] - seenNow.y) < 1e-6
  and last.data.rot[4] > 0.8, 'at its latest place in our frame')
T.check(vehicles['4-0'].isSpawned and not vehicles['4-0'].bngeHeld and not vehicles['4-0'].hideNametag,
  'BeamMP\'s record of it spawned; nothing kept')
T.check(md.stats.released == 2 and #md.foci() == 0, 'counted')

-- a far spawned car's edit: kept until it is near and on again
for _ = 1, 3 do positionGE.handle(posRaw('5-0', 36.52, -118.08, 36.52, -118.08)) end
ma.setRemote(5, PARIS[1], PARIS[2])
place[5] = PARIS
clockT = clockT + ma.SETTLE_S + 0.1
positionGE.handle(posRaw('5-0', PARIS[1], PARIS[2], PARIS[1], PARIS[2]))
local gid5 = vehicles['5-0'].gameVehicleID
off[gid5] = true                                     -- (mpsession switched it off: far)
editsBefore = #edited
MPVehicleGE.handle('c:5-0:' .. jsonEncode({ jbm = 'bx', vcf = { parts = 'bx' } }))
T.check(#edited == editsBefore and vehicles['5-0'].bngeHeldEdit, 'an edit to a far spawned car: kept, not built')
MPVehicleGE.handle('p:5-0:[{"baseColor":[0,1,0,1]}]')
tick()
T.check(#edited == editsBefore, 'kept while far')
ma.setRemote(5, 36.52, -118.08)
place[5] = { 36.52, -118.08 }
clockT = clockT + ma.SETTLE_S + 0.1
positionGE.handle(posRaw('5-0', 36.52, -118.08, 36.52, -118.08))
tick()
T.check(#edited == editsBefore, 'near but still switched off here: kept')
off[gid5] = nil
tick()
T.check(#edited == editsBefore + 1 and edited[#edited].data.jbm == 'bx' and edited[#edited].data.vcf.paints[1].baseColor[2] == 1
  and not vehicles['5-0'].bngeHeldEdit, 'near and on: the kept edit built (with the paint since)')

-- (Codex review) the player's queue on: an older edit BeamMP queued while the car was near never overwrites a newer
-- one kept after it went far; the kept (latest) edit is the one built when it is near again
sv.enableSpawnQueue = false
positionGE.handle(posRaw('5-1', 36.52, -118.08, 36.5203, -118.0802))
MPVehicleGE.handle(spawnRaw('5-1', 'Near By', 31, 'covet'))
T.check(math.abs(spawned[#spawned].data.pos[2] - ma.seen('5-1').y) < 1e-6, 'a near car with a packet first: spawned at once there')
local gid51 = vehicles['5-1'].gameVehicleID
T.check(vehicles['5-1'].isSpawned, 'a near car spawned')
sv.enableSpawnQueue = true
MPVehicleGE.handle('c:5-1:' .. jsonEncode({ jbm = 'editA', vcf = { parts = 'A' } }))
T.check(vehicles['5-1'].editQueue ~= nil, 'edit A queued by BeamMP (near, queue on)')
ma.setRemote(5, PARIS[1], PARIS[2])
place[5] = PARIS
clockT = clockT + ma.SETTLE_S + 0.1
positionGE.handle(posRaw('5-1', PARIS[1], PARIS[2], PARIS[1], PARIS[2]))
off[gid51] = true
MPVehicleGE.handle('c:5-1:' .. jsonEncode({ jbm = 'editB', vcf = { parts = 'B' } }))
tick()
local kB = vehicles['5-1'].bngeHeldEdit
T.check(kB and kB:find('editB', 1, true) and vehicles['5-1'].editQueue == nil, 'the newer edit B kept, the older A dropped')
local nE = #edited
applyQueuedEvents()
T.check(#edited == nE, 'BeamMP\'s queue builds nothing for the far car')
ma.setRemote(5, 36.52, -118.08)
place[5] = { 36.52, -118.08 }
clockT = clockT + ma.SETTLE_S + 0.1
positionGE.handle(posRaw('5-1', 36.52, -118.08, 36.52, -118.08))
off[gid51] = nil
tick()
applyQueuedEvents()
T.check(#edited == nE + 1 and edited[#edited].sid == '5-1' and edited[#edited].data.jbm == 'editB',
  'near and on: edit B built (the latest), not A')
sv.enableSpawnQueue = false

-- (Codex review 2) a kept car near and waiting for its ground whose owner teleports far before it spawns: its pose
-- from the old anchor no longer counts, it stays kept
ma.setRemote(8, PARIS[1], PARIS[2])
place[8] = PARIS
MPVehicleGE.handle(spawnRaw('8-0', 'Hopper', 41, 'pickup'))
T.check(vehicles['8-0'].bngeHeld, 'a far car kept')
ma.setRemote(8, 36.51, -118.09)
place[8] = { 36.51, -118.09 }
clockT = clockT + ma.SETTLE_S + 0.1
positionGE.handle(posRaw('8-0', 36.51, -118.09, 36.5005, -118.1005))
groundOk = false
tick()
T.check(vehicles['8-0'].bngeHeld and #md.foci() >= 1, 'near: waits for its ground')
ma.setRemote(8, PARIS[1], PARIS[2])                  -- (its owner teleports away; its packets settle, none come)
place[8] = PARIS
groundOk = true
local n8 = #spawned
tick()
T.check(vehicles['8-0'].bngeHeld and #spawned == n8, 'its owner gone far: still kept (the old near pose not used)')
T.check(ma.seen('8-0') == nil, 'its pose from the old anchor forgotten')
-- the same for an edit of a spawned car in the settling window
ma.setRemote(9, 36.52, -118.08)
place[9] = { 36.52, -118.08 }
positionGE.handle(posRaw('9-0', 36.52, -118.08, 36.52, -118.08))
MPVehicleGE.handle(spawnRaw('9-0', 'Edit Hopper', 42, 'covet'))
T.check(vehicles['9-0'].isSpawned, 'a near car spawned')
positionGE.handle(posRaw('9-0', 36.52, -118.08, 36.52, -118.08))
ma.setRemote(9, PARIS[1], PARIS[2])
place[9] = PARIS
local nE9 = #edited
MPVehicleGE.handle('c:9-0:' .. jsonEncode({ jbm = 'far_edit' }))
T.check(#edited == nE9 and vehicles['9-0'].bngeHeldEdit, 'an edit in its owner\'s settling window: kept (far by its new place)')

-- (Codex review 3) a kept car whose owner teleports next to us but whose packets do not come: never spawned at the
-- spawn's old coordinates (its owner's old frame) however long; it spawns at its first packet in our frame
ma.setRemote(12, PARIS[1], PARIS[2])
place[12] = PARIS
MPVehicleGE.handle(spawnRaw('12-0', 'Silent', 51, 'pickup'))
T.check(vehicles['12-0'].bngeHeld, 'a far car kept')
ma.setRemote(12, 36.51, -118.09)
place[12] = { 36.51, -118.09 }
groundOk = true
local n12 = #spawned
tick(math.floor(2 * md.UNKNOWN_WAIT_S / md.TICK_S) + 2)
T.check(vehicles['12-0'].bngeHeld and #spawned == n12, 'its owner near, no packet of its own: kept (not at its old coordinates)')
positionGE.handle(posRaw('12-0', 36.51, -118.09, 36.5005, -118.1005))
tick()
local s12 = ma.seen('12-0')
T.check(#spawned == n12 + 1 and spawned[#spawned].sid == '12-0' and math.abs(spawned[#spawned].data.pos[1] - s12.x) < 1e-6,
  'its first packet: spawned there')

-- (Codex review 4) a kept car handed to BeamMP's queue (the player's queue on) whose owner's anchor changes before
-- the queue spawns it (a teleport within RELEASE_M): taken back, never spawned at the pose from before; its next
-- packet in our frame spawns it there. The same when OUR anchor changes (a rebase: the pose was in our old frame).
local function queuedRelease(pid, sid, vid)
  ma.setRemote(pid, PARIS[1], PARIS[2])
  place[pid] = PARIS
  MPVehicleGE.handle(spawnRaw(sid, 'Queued' .. pid, vid, 'pickup'))
  ma.setRemote(pid, 36.51, -118.09)
  place[pid] = { 36.51, -118.09 }
  clockT = clockT + ma.SETTLE_S + 0.1
  positionGE.handle(posRaw(sid, 36.51, -118.09, 36.5005, -118.1005))
  groundOk = true
  sv.enableSpawnQueue = true
  tick()
  return vehicles[sid]
end
local q13 = queuedRelease(13, '13-0', 61)
T.check(q13.spawnQueue and not q13.bngeHeld and not q13.isSpawned, 'released into BeamMP\'s queue (queue on)')
local old13 = jsonDecode(q13.spawnQueue.data).pos
ma.setRemote(13, 36.53, -118.12)                      -- (its owner teleports 3 km on: still near)
place[13] = { 36.53, -118.12 }
local n13 = #spawned
applyQueuedEvents()
T.check(#spawned == n13 and vehicles['13-0'].bngeHeld and not vehicles['13-0'].spawnQueue,
  'its owner\'s anchor changed: the queued spawn taken back at once, BeamMP\'s queue spawns nothing')
tick(3)
applyQueuedEvents()
T.check(#spawned == n13, 'no packet since: still kept')
clockT = clockT + ma.SETTLE_S + 0.1
positionGE.handle(posRaw('13-0', 36.53, -118.12, 36.5010, -118.1010))
tick()
applyQueuedEvents()
local sp13 = spawned[#spawned]
T.check(#spawned == n13 + 1 and sp13.sid == '13-0' and math.abs(sp13.data.pos[1] - ma.seen('13-0').x) < 1e-6
  and math.abs(sp13.data.pos[1] - old13[1]) > 1, 'its packet in our frame: spawned there, not at the old pose')
-- our own anchor changes (a rebase) while a released spawn waits in BeamMP's queue
local q14 = queuedRelease(14, '14-0', 62)
T.check(q14.spawnQueue and not q14.bngeHeld, 'released into BeamMP\'s queue')
local n14 = #spawned
ma.announceOwn(36.505, -118.105)
applyQueuedEvents()
T.check(#spawned == n14 and vehicles['14-0'].bngeHeld, 'our anchor changed: the queued spawn taken back')
ma.announceOwn(ME[1], ME[2])
sv.enableSpawnQueue = false
tick()

-- (own pass after review 4) every other way an old or foreign pose could reach BeamMP:
-- a near BeamNG.Earth car's spawn sitting in BeamMP's queue without a place in our frame (queued before the hook):
-- taken in whatever its distance, spawned at its packet
ma.setRemote(15, 36.51, -118.09)
place[15] = { 36.51, -118.09 }
vehicles['15-0'] = { serverVehicleString = '15-0', ownerID = 15, ownerName = 'Early', isSpawned = false, isLocal = false,
  gameVehicleID = -1, remoteVehID = 71, position = vec3(0, 0, 0),
  spawnQueue = { playerNickname = 'Early', serverVehicleID = '15-0',
    data = jsonEncode({ vid = 71, jbm = 'van', pos = { 1, 2, 3 }, rot = { 0, 0, 0, 1 } }) } }
tick()
T.check(vehicles['15-0'].bngeHeld and not vehicles['15-0'].spawnQueue, 'a near Earth car queued without our-frame place: taken in')
positionGE.handle(posRaw('15-0', 36.51, -118.09, 36.5004, -118.1004))
sv.enableSpawnQueue = true
local n15 = #spawned
tick()
T.check(vehicles['15-0'].spawnQueue and vehicles['15-0'].bngePosed, 'its packet: handed back to BeamMP\'s queue with a place')
tick(2)
T.check(vehicles['15-0'].spawnQueue and not vehicles['15-0'].bngeHeld, 'a queued spawn with a place: left in BeamMP\'s queue')
applyQueuedEvents()
T.check(#spawned == n15 + 1 and math.abs(spawned[#spawned].data.pos[1] - ma.seen('15-0').x) < 1e-6,
  'BeamMP\'s queue spawns it at its place in our frame')
sv.enableSpawnQueue = false
-- a BeamNG.Earth player's kept car whose place is not known for long (no owner place, no packets): never spawned at
-- the spawn's coordinates by the timeout
ma.setRemote(16, PARIS[1], PARIS[2])
MPVehicleGE.handle(spawnRaw('16-0', 'Placeless', 72, 'pickup'))
T.check(vehicles['16-0'].bngeHeld, 'kept (place not known)')
local n16 = #spawned
tick(math.floor(2 * md.UNKNOWN_WAIT_S / md.TICK_S) + 2)
T.check(vehicles['16-0'].bngeHeld and #spawned == n16, 'a known Earth player\'s car: no timeout spawn at foreign coordinates')

-- (Codex review 5) a player without a known anchor: its kept spawn times out into BeamMP's queue at its raw packet;
-- its first anchor then comes (it is a BeamNG.Earth player, in Paris): the queued spawn is taken back at once, before
-- BeamMP's queue can spawn it at the raw coordinates
sv.enableSpawnQueue = true
MPVehicleGE.handle(spawnRaw('17-0', 'Late Earth', 81, 'pickup'))
positionGE.handle('p:17-0:{"pos":[500,600,7],"vel":[0,0,0],"rot":[0,0,1,0],"rvel":[0,0,0]}')
tick(math.floor(md.UNKNOWN_WAIT_S / md.TICK_S) + 1)
T.check(vehicles['17-0'].spawnQueue and not vehicles['17-0'].bngeHeld, 'timed out into BeamMP\'s queue (raw place)')
ma.setRemote(17, PARIS[1], PARIS[2])
place[17] = PARIS
local n17 = #spawned
applyQueuedEvents()
T.check(#spawned == n17 and vehicles['17-0'].bngeHeld and not vehicles['17-0'].spawnQueue,
  'its first anchor learned: taken back at once, nothing spawned at the raw coordinates')
tick()
T.check(vehicles['17-0'].bngeHeld and #spawned == n17, 'far (Paris): kept')
sv.enableSpawnQueue = false

-- BeamMP's own queue: a far car queued before the hook (or while its place was not known) taken in
ma.setRemote(4, PARIS[1], PARIS[2])
place[4] = PARIS
vehicles['4-5'] = { serverVehicleString = '4-5', ownerID = 4, ownerName = 'Far Away', isSpawned = false, isLocal = false,
  gameVehicleID = -1, remoteVehID = 99, position = vec3(0, 0, 0),
  spawnQueue = { playerNickname = 'Far Away', serverVehicleID = '4-5', data = jsonEncode({ vid = 99, jbm = 'van' }) } }
clockT = clockT + 10                                  -- (its earlier packets stale: by its owner's place)
local ui0 = #queueUI
tick()
T.check(vehicles['4-5'].bngeHeld and vehicles['4-5'].spawnQueue == nil and #queueUI == ui0 + 1 and queueUI[#queueUI][1] == 0,
  'a far car in BeamMP\'s queue: taken in, the queue count shown again')

-- removing a kept car: BeamMP removes it as a queued one
MPVehicleGE.handle('d:4-5')
T.check(vehicles['4-5'] == nil and removed[#removed].sid == '4-5' and removed[#removed].queued, 'a kept car removed: BeamMP\'s queued-car removal')

-- a player whose place is not known (no BeamNG.Earth): kept UNKNOWN_WAIT_S, then spawned as before
local n0 = #spawned
MPVehicleGE.handle(spawnRaw('7-0', 'Stranger', 21, 'pickup'))
T.check(#spawned == n0 and vehicles['7-0'].bngeHeld, 'place not known: kept for now')
-- (it drives on meanwhile: its packets, untransformed, reach BeamMP's record)
positionGE.handle('p:7-0:{"pos":[500,600,7],"vel":[0,0,0],"rot":[0,0,1,0],"rvel":[0,0,0]}')
tick(math.floor(md.UNKNOWN_WAIT_S / md.TICK_S) - 1)
T.check(#spawned == n0, '... for UNKNOWN_WAIT_S')
tick(2)
T.check(#spawned == n0 + 1 and spawned[#spawned].sid == '7-0', 'then spawned (BeamMP\'s behaviour)')
local p7 = spawned[#spawned].data
T.check(p7.pos[1] == 500 and p7.pos[2] == 600 and p7.pos[3] == 7 and p7.rot[3] == 1,
  '(Codex review) at its latest place from its packets, not the spawn\'s old one')

-- a reload of this module: what is kept is found again on BeamMP's records
MPVehicleGE.handle(spawnRaw('4-2', 'Far Away', 14, 'pickup'))
T.check(vehicles['4-2'].bngeHeld, 'kept')
md.reset()
local nS = #spawned
MPVehicleGE.handle(spawnRaw('4-3', 'Far Away', 15, 'pickup'))
T.check(#spawned == nS + 1 and not vehicles['4-3'].bngeHeld, 'after reset (the session\'s end): the hook passes through')
tick()
T.check(vehicles['4-2'].bngeHeld and #spawned == nS + 1, 'a session going on: the kept car found again, still kept')

-- the hook stale (our session code stopped): everything kept handed back at the next packet
local realTime = os.time
os.time = function() return realTime() + md.STALE_S + 5 end
MPVehicleGE.handle('m:1:4-2')
os.time = realTime
local back = {}
for i = nS + 2, #spawned do back[spawned[i].sid] = true end
local anyKept = false
for _, v in pairs(vehicles) do if v.bngeHeld then anyKept = true end end
T.check(back['4-2'] and back['4-1'] and not anyKept and vehicles['4-2'].isSpawned,
  'stale: every kept spawn handed back to BeamMP')
tick()

-- an error in BeamMP's handle during a kept spawn: settings put back, the error BeamMP's
T.check(not pcall(MPVehicleGE.handle, 'x:whatever'), 'an unparsed packet passes through (BeamMP\'s own error)')
local saved = beammpHandle
-- (a handle that throws inside the queued call)
md._forget()
MPVehicleGE = { handle = function() error('inside BeamMP') end, getVehicles = function() return vehicles end }
md.update(md.TICK_S)
local okE = pcall(MPVehicleGE.handle, spawnRaw('4-4', 'Far Away', 16, 'pickup'))
T.check(not okE and settings.getValue == getValue and rawget(UI, 'showNotification') ~= nil and rawget(UI, 'updateQueue') ~= nil,
  'an error inside BeamMP: raised as before, settings and UI put back')

-- no BeamMP functions: nothing wrapped
md._forget()
MPVehicleGE = { getVehicles = function() return {} end }
T.check(md.install() == false and MPVehicleGE.handle == nil, 'MPVehicleGE without handle: nothing wrapped')
md._forget()
local s0 = settings
settings = nil
MPVehicleGE = { handle = saved, getVehicles = function() return vehicles end }
T.check(md.install() == false and MPVehicleGE.handle == saved, 'no settings.getValue: nothing wrapped')
settings = s0

T.done('test_mpdefer')
