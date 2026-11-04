-- Far player cars saved and taken out, and put back when the player comes near or switches to them (BeamMP support,
-- the user, 2026-10-22: "If the active car gets too far away from other player spawned cars save their state and
-- unload them. If a player gets near enough or switches back to that car load the state and load and resume the map
-- around it").
--
-- A player's unoccupied own car (not traffic, not parked, in BeamMP not another player's) more than UNLOAD_M from
-- the active car for UNLOAD_AFTER_S is saved with the game's own state save (vehicle Lua beamstate.save: every node, beam deformation
-- and break, the hydros and the Lua state), its place kept as latitude / longitude / height (anchors change on the
-- way), and deleted. Within NEAR_M of the active car again it is a collision focus first (foci(), collisionfoci.lua:
-- the roads, trees and ground under it are made, terrainpatch.lua); within LOAD_M and with the static ground under its
-- place it is spawned there (model, parts, paint) and its state loaded (beamstate.load). switchTo(uid) puts the player
-- in one: near, once it is back; far, the car driven until then is saved where it is, then taken there by a teleport
-- and taken out when the stored car is back and entered. A teleport saves the unoccupied other cars (onTeleport).
-- The records live as long as the level.
local M = {}
local localplayers = require('beamngEarth/localplayers')
local controlled, otherControlled = {}, {}

M.UNLOAD_M = 2500
M.LOAD_M = 1500
M.NEAR_M = 1800             -- a stored car this near is a collision focus (its ground made before it comes back)
M.UNLOAD_AFTER_S = 5
M.SAVE_WAIT_S = 8           -- the state file not written by then: the car is left as it is
M.GROUND_M = 6              -- the static ground must be within this of the saved height before the car comes back
M.LOAD_DELAY_S = 1.0        -- after the spawn: its state loaded
M.SWITCH_WAIT_S = 120       -- a switch whose car has not come back by then is given up
M.UPDATE_S = 0.5
M.DIR = '/temp/beamngEarth_farcars/'

local stored = {}       -- uid -> { uid, model, config, paint, lat, lon, h, rot, path, name }
local saving = {}       -- vehicle id -> { rec, t, keep }  (keep: the switch's car, not deleted)
local loading = {}      -- vehicle id -> { rec, t }
local farFor = {}       -- vehicle id -> seconds beyond UNLOAD_M
local written = {}      -- path -> true: the state files asked for (deleted at the level's end)
local switch = nil      -- { uid, oldId, oldUid, t, teleported } while a switch waits for its car
local serial = 0
local clock = 0
local since = math.huge

-- Pure: unload a car this far (m) from the active one, far for this long (s)?
function M.shouldUnload(d, far) return d > M.UNLOAD_M and far >= M.UNLOAD_AFTER_S end

-- Pure: the uids of the records within r of (ax, ay), sorted; toXY(rec) -> x, y (nil: unknown).
function M.within(recs, ax, ay, r, toXY)
  local out = {}
  for uid, rec in pairs(recs) do
    local x, y = toXY(rec)
    if x and (x - ax) ^ 2 + (y - ay) ^ 2 <= r * r then out[#out + 1] = uid end
  end
  table.sort(out)
  return out
end

-- Is the game vehicle id this player's (single player: yes; BeamMP: MPVehicleGE.isOwn)?
local function own(id)
  local mp = rawget(_G, 'MPVehicleGE')
  if type(mp) == 'table' and type(mp.isOwn) == 'function' then
    local ok, v = pcall(mp.isOwn, id)
    return ok and v == true
  end
  return true
end

-- Is vehicle id coupled (a trailer, the car pulling it) to the active car aid? (core_trailerRespawn's train head)
local function inTrain(id, aid)
  local tr = rawget(_G, 'core_trailerRespawn')
  if not tr or not tr.getVehicleTrainHead then return false end
  local ok, a = pcall(tr.getVehicleTrainHead, id)
  local ok2, b = pcall(tr.getVehicleTrainHead, aid)
  return ok and ok2 and a ~= nil and a == b
end

-- This player's unoccupied cars, other than the active one's train, not being saved or spawned and not carrying a
-- switching player. Reset a car's far timer as soon as a local player enters it.
local function candidates(aid)
  local out = {}
  local cf = require('beamngEarth/collisionfoci')
  for _, v in ipairs(cf.vehicles()) do
    local id = v.id
    if v.controlled then farFor[id] = nil end
    if v.kind == 'player' and not v.controlled and id ~= aid and not saving[id] and not loading[id] and own(id)
      and not (switch and switch.oldId == id) and not inTrain(id, aid) then
      out[#out + 1] = v
    end
  end
  return out
end

local function anchorOf()
  local core = rawget(_G, 'beamngEarth_core')
  local st = core and core._state and core._state()
  return st and st.anchor
end

-- Is a teleport under way (core.lua requestAnchor: the player is still at the old place)?
local function teleporting()
  local core = rawget(_G, 'beamngEarth_core')
  local st = core and core._state and core._state()
  return st ~= nil and st.pending ~= nil and st.pending.purpose == 'teleport'
end

-- A record's place in the active frame: x, y, z (nil: no anchor).
local function toXYZ(rec)
  local a = anchorOf()
  if not a then return nil end
  return a:geodeticToWorld(rec.lat, rec.lon, rec.h)
end

-- Save vehicle veh; update takes it out (or, keep, hands it on to the switch) once its state file is written.
local function startSave(veh, keep)
  local a = anchorOf()
  if not a then return nil end
  local p = veh:getPosition()
  local lat, lon, h = a:worldToGeodetic(p.x, p.y, p.z)
  local q = quat(0, 0, 1, 0) * quat(veh:getRefNodeRotation())       -- (as core_vehicles' keepOtherVehRotation)
  serial = serial + 1
  local uid = string.format('%d_%d', os.time(), serial)
  local name = veh:getField('name', '')
  local okP, metal = pcall(function() return veh:getMetallicPaintData() end)
  local rec = { uid = uid, model = veh.JBeam, config = veh.partConfig, lat = lat, lon = lon, h = h,
    rot = { q.x, q.y, q.z, q.w }, path = M.DIR .. uid .. '.json', name = name ~= '' and name or nil,
    paint = { veh.color, veh.colorPalette0, veh.colorPalette1 }, metal = okP and metal or nil }
  if FS:fileExists(rec.path) then FS:removeFile(rec.path) end
  written[rec.path] = true
  veh:queueLuaCommand(string.format('beamstate.save(%q)', rec.path))
  saving[veh:getId()] = { rec = rec, t = clock, keep = keep }
  return rec
end

-- Spawn a stored car at its place and load its state LOAD_DELAY_S later (update). enter: the switch's car: the car
-- that carried the player there goes first (it may stand where this one comes back), this one is entered.
local function startLoad(rec, enter)
  if enter and switch and switch.oldId then
    localplayers.snapshot(controlled, otherControlled)
    if otherControlled[switch.oldId] then
      -- Another local player entered the car being used for the switch. It must not be deleted or duplicated.
      if switch.oldUid then stored[switch.oldUid] = nil end
      farFor[switch.oldId] = nil
      switch = nil
      return false
    end
  end
  local x, y, z = toXYZ(rec)
  if not x then return false end
  local r = rec.rot
  local opt = { config = rec.config, pos = vec3(x, y, z), rot = quat(r[1], r[2], r[3], r[4]),
    autoEnterVehicle = enter == true, vehicleName = rec.name }
  if enter and switch and switch.oldId then
    local old = scenetree.findObjectById(switch.oldId)
    if old then old:delete() end
    switch.oldId = nil
  end
  local paint = rawget(_G, 'createVehiclePaint')
  if paint and rec.paint and rec.paint[1] then
    local m = rec.metal or {}
    opt.paint, opt.paint2, opt.paint3 = paint(rec.paint[1], m[1]), paint(rec.paint[2], m[2]), paint(rec.paint[3], m[3])
  end
  local ok, veh = pcall(core_vehicles.spawnNewVehicle, rec.model, opt)
  if not ok or not veh then
    log('W', 'beamngEarth', 'far car: could not spawn ' .. tostring(rec.model) .. ': ' .. tostring(veh))
    return false
  end
  stored[rec.uid] = nil
  loading[veh:getId()] = { rec = rec, t = clock }
  return true
end

-- Is the static ground (terrain block, terrain patch, road) under a record's place yet?
local function groundAt(rec)
  local x, y, z = toXYZ(rec)
  if not x then return false end
  local d = castRayStatic(vec3(x, y, z + M.GROUND_M), vec3(0, 0, -1), 2 * M.GROUND_M)
  return d ~= nil and d < 2 * M.GROUND_M
end

-- The collision foci of the stored cars near the active one (collisionfoci.update's extra: the roads, trees and the
-- ground under them made before they come back). [{ x, y, r, trees, kind, stored }]
function M.foci()
  local out = {}
  if next(stored) == nil then return out end
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if not veh then return out end
  local p = veh:getPosition()
  local cf = require('beamngEarth/collisionfoci')
  for _, uid in ipairs(M.within(stored, p.x, p.y, M.NEAR_M, toXYZ)) do
    local x, y = toXYZ(stored[uid])
    if (x - p.x) ^ 2 + (y - p.y) ^ 2 > cf.MIN_SEPARATION_M ^ 2 then
      out[#out + 1] = { x = x, y = y, r = cf.PLAYER_R, trees = true, kind = 'player', stored = uid }
    end
  end
  return out
end

local function finishSwitch(veh)
  if not (getPlayerVehicle and getPlayerVehicle(0) == veh) then be:enterVehicle(0, veh) end
  local old = switch.oldId and scenetree.findObjectById(switch.oldId)
  if old then old:delete() end
  switch = nil
end

-- Each frame (core.lua) in our level.
function M.update(dt)
  clock = clock + (dt or 0)
  since = since + (dt or 0)
  if since < M.UPDATE_S then return end
  local step = math.min(since, 1)       -- (the first update: since is huge)
  since = 0
  local active = getPlayerVehicle and getPlayerVehicle(0)
  if not active or not anchorOf() then return end
  local ap = active:getPosition()
  if next(saving) or switch then
    localplayers.snapshot(controlled, otherControlled)
    if switch and switch.oldId and otherControlled[switch.oldId] then
      saving[switch.oldId], farFor[switch.oldId] = nil, nil
      if switch.oldUid then stored[switch.oldUid] = nil end
      switch = nil
    end
  end
  -- saves written: the car goes (the switch's car: taken to the stored one); too slow: it stays as it is
  for id, s in pairs(saving) do
    local veh = scenetree.findObjectById(id)
    if not veh then
      saving[id] = nil
    elseif (not s.keep and localplayers.controlled(id, veh, controlled)) or (s.keep and not switch) then
      -- A player entered while beamstate.save was in flight, or a kept-car switch was cancelled.
      saving[id], farFor[id] = nil, nil
    elseif FS:fileExists(s.rec.path) then
      stored[s.rec.uid] = s.rec
      saving[id], farFor[id] = nil, nil
      if s.keep and switch then
        switch.oldUid = s.rec.uid
        local target = stored[switch.uid]
        if target then
          local core = rawget(_G, 'beamngEarth_core')
          core.teleport(target.lat, target.lon, nil, { record = false })
          switch.teleported = true
        end
      else
        veh:delete()
      end
    elseif clock - s.t > M.SAVE_WAIT_S then
      saving[id] = nil
      if s.keep then switch = nil end
      log('W', 'beamngEarth', 'far car: state not saved, kept: ' .. tostring(id))
    end
  end
  -- spawned ones: their state loaded (and entered for a switch)
  for id, l in pairs(loading) do
    local veh = scenetree.findObjectById(id)
    if not veh then
      loading[id] = nil
    elseif clock - l.t >= M.LOAD_DELAY_S then
      veh:queueLuaCommand(string.format('beamstate.load(%q)', l.rec.path))
      loading[id] = nil
      if switch and switch.uid == l.rec.uid then finishSwitch(veh) end
    end
  end
  if switch and clock - switch.t > M.SWITCH_WAIT_S then
    log('W', 'beamngEarth', 'far car: switch to ' .. tostring(switch.uid) .. ' given up')
    switch = nil
  end
  if teleporting() then return end      -- (the player still where it was: nothing far, nothing near)
  -- watching another BeamMP player's car (the game's vehicle switch takes the camera to it): our cars stay as they
  -- are, the player comes back to them; taking them out would remove them for every player
  if not own(active:getId()) then
    for id in pairs(farFor) do farFor[id] = 0 end
    return
  end
  -- far own cars: saved and out
  for _, v in ipairs(candidates(active:getId())) do
    local id = v.id
    local d = math.sqrt((v.x - ap.x) ^ 2 + (v.y - ap.y) ^ 2)
    farFor[id] = d > M.UNLOAD_M and (farFor[id] or 0) + step or 0
    if M.shouldUnload(d, farFor[id]) then
      local veh = scenetree.findObjectById(id)
      if veh then startSave(veh) end
    end
  end
  -- stored ones near again, the ground under them made: back
  for _, uid in ipairs(M.within(stored, ap.x, ap.y, M.LOAD_M, toXYZ)) do
    local rec = stored[uid]
    if not (switch and switch.oldUid == uid) and groundAt(rec) then startLoad(rec, switch and switch.uid == uid) end
  end
end

-- A teleport (core.lua teleportTo, before the anchor changes): unoccupied own cars stay where they are, saved
-- with their place in the old frame and taken out. Locally occupied cars are never taken out; the active car's trailer
-- comes along.
function M.onTeleport()
  local active = getPlayerVehicle and getPlayerVehicle(0)
  if not active or not anchorOf() then return end
  for _, v in ipairs(candidates(active:getId())) do
    local veh = scenetree.findObjectById(v.id)
    if veh then startSave(veh) end
  end
end

-- The stored cars: [{ uid, model, name, lat, lon }] (a UI's list; switchTo).
function M.list()
  local out = {}
  for uid, r in pairs(stored) do
    out[#out + 1] = { uid = uid, model = r.model, name = r.name, lat = r.lat, lon = r.lon }
  end
  table.sort(out, function(a, b) return a.uid < b.uid end)
  return out
end

-- Put the player in stored car uid: near, it is entered once it is back; far, the car driven until then is saved
-- where it is first (it becomes a stored car), then taken there and taken out once the stored car is entered.
-- True if under way.
function M.switchTo(uid)
  local rec = stored[uid]
  local active = getPlayerVehicle and getPlayerVehicle(0)
  if not rec or not active or switch then return false end
  local x, y = toXYZ(rec)
  if not x then return false end
  local ap = active:getPosition()
  local near = (x - ap.x) ^ 2 + (y - ap.y) ^ 2 <= M.LOAD_M * M.LOAD_M
  if not near then
    localplayers.snapshot(controlled, otherControlled)
    if otherControlled[active:getId()] then return false end
  end
  switch = { uid = uid, t = clock }
  if near then return true end
  if not startSave(active, true) then switch = nil; return false end
  switch.oldId = active:getId()
  return true
end

-- The level's end (core.lua): everything forgotten, the state files deleted (left over from a crash too).
function M.reset()
  local paths = {}
  for p in pairs(written) do paths[#paths + 1] = p end
  if FS.findFiles then
    local ok, found = pcall(FS.findFiles, FS, M.DIR, '*.json', 0, false, false)
    if ok and type(found) == 'table' then for _, p in ipairs(found) do paths[#paths + 1] = p end end
  end
  for _, p in ipairs(paths) do
    if FS:fileExists(p) then FS:removeFile(p) end
  end
  written = {}
  stored, saving, loading, farFor, switch, clock, since = {}, {}, {}, {}, nil, 0, math.huge
end

-- (tests, the dev tools)
function M.state() return { stored = stored, saving = saving, loading = loading, switch = switch } end

return M
