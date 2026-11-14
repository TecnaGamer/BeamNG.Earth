-- A bus at a bus station's bay (the user, 2026-11-09: "spawn it only when traffic is on"; then "three in a row closed a lane:
-- one, somewhere in the lot, out of the way").
-- The road tiles carry the stations' bus bays as rows {x, y, z, heading, 2} of the parked-car rows (server
-- busstationpark.py; parking.lua hands those rows here and keeps them out of the parked cars' sites). At most MAX_BUSES
-- stock bus stands at the bay nearest the player, only while the traffic is on, parked (brake on, no driver, not in the
-- traffic system). Cheap by design:
--  * nothing runs with traffic off (a standing bus is deleted once), and nothing within UPDATE_S of the last pass;
--  * a pass is a loop over the few rows kept (a station sends at most BUSES_PER_STATION of them), no frame work;
--  * at most one spawn per pass, a bus the player left behind is moved to a new bay (not deleted and spawned again);
--  * every bus is deleted at a teleport / a new anchor / the end of the level (a rebase moves vehicles with the world).
local M = {}

M.MAX_BUSES = 1         -- buses standing at once (the user 2026-11-09: three in a row closed a lane: one, off the lanes)
M.UPDATE_S = 2.0        -- seconds between passes
M.SPAWN_M = 220         -- a bay this near the player may get a bus ...
M.MIN_M = 14            -- ... not nearer than this (a bus popping up beside the car) unless it stood there already
M.DESPAWN_M = 420       -- a bus this far from the player is moved to a nearer bay, or deleted
M.APART_M = 16          -- chosen bays are at least this far from each other
M.MODEL = 'citybus'     -- the stock city bus (the traffic's)
M.LIFT_M = 0.5          -- a spawn's height over the ground

local rowsByKey = {}    -- tile key -> { anchor, rows }
local buses = {}        -- vehicle id -> { bay = 'x_y', x, y }
local anchorId = nil
local lastPass = -math.huge
local failed = false    -- the model could not be spawned: no more tries this session
local warned = false

function M.onRows(aid, key, rows)
  if type(rows) ~= 'table' then return end
  rowsByKey[key] = { anchor = aid, rows = rows }
end

function M.dropKey(key)
  rowsByKey[key] = nil
end

-- Pure: the bay key of a row.
function M.bayKey(r) return string.format('%.1f_%.1f', r[1], r[2]) end

-- Pure: the bays to keep a bus at: from rows (a list of {x, y, z, heading, 2}) the ones within spawnM of (px, py), the
-- nearest first, none within minM unless it is in `held` (bay key -> true), at most `max`, none closer than apartM to a
-- chosen one. Returns a list of { key, row, d }.
function M.pick(rows, px, py, max, spawnM, minM, apartM, held)
  local cand = {}
  local s2, m2 = spawnM * spawnM, minM * minM
  for _, r in ipairs(rows) do
    local dx, dy = r[1] - px, r[2] - py
    local d2 = dx * dx + dy * dy
    local key = M.bayKey(r)
    if d2 <= s2 and (d2 >= m2 or (held and held[key])) then cand[#cand + 1] = { key = key, row = r, d = math.sqrt(d2) } end
  end
  table.sort(cand, function(a, b) return a.d < b.d end)
  local out = {}
  for _, c in ipairs(cand) do
    if #out >= max then break end
    local ok = true
    for _, o in ipairs(out) do
      local dx, dy = c.row[1] - o.row[1], c.row[2] - o.row[2]
      if dx * dx + dy * dy < apartM * apartM then ok = false break end
    end
    if ok then out[#out + 1] = c end
  end
  return out
end

-- Pure: the traffic is on. The stock traffic system's state when it is loaded ("on"), else the player's level-start choice.
function M.trafficOn(gt, choice)
  if gt and type(gt.getState) == 'function' then
    local ok, st = pcall(gt.getState)
    if ok and st ~= nil then return st == 'on' or st == true end
  end
  return choice ~= nil and choice.traffic == true
end

local function allRows()
  local out = {}
  for _, t in pairs(rowsByKey) do
    if t.anchor == anchorId then
      for _, r in ipairs(t.rows) do out[#out + 1] = r end
    end
  end
  return out
end

local function deleteBus(id)
  local o = be and be:getObjectByID(id) or nil
  if o then pcall(function() o:delete() end) end
  buses[id] = nil
end

local function deleteAll()
  for id in pairs(buses) do deleteBus(id) end
end

local function place(veh, r)
  local h = math.rad(r[4] or 0)
  local dir = vec3(math.cos(h), math.sin(h), 0)
  local z = be:getSurfaceHeightBelow(vec3(r[1], r[2], r[3] + 3))
  local pos = vec3(r[1], r[2], ((z and z > -1e6) and z or r[3]) + M.LIFT_M)
  local ok = pcall(spawn.safeTeleport, veh, pos, quatFromDir(dir, vec3(0, 0, 1)))
  if ok then veh:queueLuaCommand('input.event("parkingbrake", 1, 2)') end
  return ok
end

local function spawnAt(r)
  if failed then return nil end
  local ok, veh = pcall(core_vehicles.spawnNewVehicle, M.MODEL,
    { pos = vec3(r[1], r[2], r[3] + M.LIFT_M), autoEnterVehicle = false, vehicleName = 'bne_bus' })
  if not ok or not veh then
    failed = true
    if not warned then
      warned = true
      log('W', 'beamngEarth', 'bus stations: could not spawn ' .. M.MODEL .. ': ' .. tostring(veh))
    end
    return nil
  end
  if not place(veh, r) then deleteBus(veh:getId()) return nil end
  return veh
end

-- Every pass: player {x, y, z}; now: a clock (seconds); gov the frame governor (optional).
function M.update(aid, player, now, gov)
  if aid ~= anchorId then
    deleteAll()
    anchorId = aid
  end
  if now - lastPass < M.UPDATE_S then return end
  lastPass = now
  local nav = require('beamngEarth/navgraph')
  if not player or failed or not M.trafficOn(rawget(_G, 'gameplay_traffic'), nav.choice) then
    if next(buses) then deleteAll() end
    return
  end
  local rows = allRows()
  if #rows == 0 then return end
  -- the bays a bus stands at stay (their bus is within DESPAWN_M); the rest of the places go to the nearest free bays
  local held, standing = {}, 0
  for id, b in pairs(buses) do
    local o = be:getObjectByID(id)
    if not o then
      buses[id] = nil
    else
      local dx, dy = b.x - player.x, b.y - player.y
      if dx * dx + dy * dy <= M.DESPAWN_M * M.DESPAWN_M then
        held[b.bay] = true
        standing = standing + 1
      end
    end
  end
  local want = M.pick(rows, player.x, player.y, M.MAX_BUSES, M.SPAWN_M, M.MIN_M, M.APART_M, held)
  local free = {}
  for _, c in ipairs(want) do
    if not held[c.key] and standing < M.MAX_BUSES then free[#free + 1] = c end
  end
  if #free == 0 then return end
  if gov and gov.roomFor and not gov:roomFor(6.0) then return end
  local c = free[1]
  -- a bus the player left far behind goes to the new bay (no spawn)
  local mover = nil
  for id, b in pairs(buses) do
    local dx, dy = b.x - player.x, b.y - player.y
    if dx * dx + dy * dy > M.DESPAWN_M * M.DESPAWN_M then mover = id break end
  end
  if mover then
    local o = be:getObjectByID(mover)
    if o and place(o, c.row) then
      buses[mover] = { bay = c.key, x = c.row[1], y = c.row[2] }
    else
      deleteBus(mover)
    end
    return
  end
  if standing >= M.MAX_BUSES then return end
  local veh = spawnAt(c.row)
  if veh then buses[veh:getId()] = { bay = c.key, x = c.row[1], y = c.row[2] } end
end

function M.status()
  local n = 0
  for _ in pairs(buses) do n = n + 1 end
  return { buses = n, failed = failed }
end

function M.onTeleport()
  deleteAll()
  lastPass = -math.huge
end

function M.reset()
  deleteAll()
  rowsByKey, anchorId, lastPass, failed, warned = {}, nil, -math.huge, false, false
end

return M
