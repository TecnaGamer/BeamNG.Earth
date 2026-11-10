-- luajit mod/tests/test_splitscreen.lua
-- Issue #499: two local seats, stock traffic registration/pooling, and the far-car save lifecycle. The fake pool
-- models the installed game's setActiveAmount and its deferred forceTeleport on reactivation; no engine is launched.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
table.clear = table.clear or function(t) for k in pairs(t) do t[k] = nil end end

local rawQuat = quat
local qmt = { __mul = function(a, b)
  return quat(a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y, a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
    a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w, a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z)
end }
function quat(...) return setmetatable(rawQuat(...), qmt) end

local objects, order, slots, nativeControlled = {}, {}, {}, {}
local data, aiIds, pool, forceTeleport = {}, {}, nil, nil
local teleports, bulk, activeAmount = {}, {}, 2
local function car(x)
  local o = S.newObject('BeamNGVehicle')
  o.JBeam, o.partConfig, o.color, o.pos = 'pickup', 'vehicles/pickup/x.pc', '1 0 0 1', vec3(x, 0, 10)
  o.active, o.commands = true, {}
  function o:getId() return self.id end
  function o:getJBeamFilename() return self.JBeam end
  function o:getActive() return self.active end
  function o:getVelocity() return vec3(0, 0, 0) end
  function o:getDirectionVector() return vec3(1, 0, 0) end
  function o:isPlayerControlled() return nativeControlled[self.id] == true end
  function o:setMeshAlpha(alpha) self.alpha = alpha end
  function o:setActive(value)
    local active = value == true or value == 1
    if active == self.active then return end
    self.active = active
    if pool and pool.allVehs[self.id] ~= nil then pool.allVehs[self.id] = active and 1 or 0 end
    local t = data[self.id]
    if t and t.isAi then
      if not active then
        t.alpha, t._teleport = 0, t._teleport or 'default'
      elseif t._teleport then
        forceTeleport(self.id) -- stock's onVehicleActiveChanged
      end
    end
  end
  function o:getRefNodeRotation() return quat(0, 0, 0, 1) end
  function o:getMetallicPaintData() return {} end
  function o:queueLuaCommand(cmd) self.commands[#self.commands + 1] = cmd end
  local delete = o.delete
  function o:delete() objects[self.id] = nil; delete(self) end
  objects[o.id], S.objects[o.id] = o, o
  order[#order + 1] = o.id
  return o
end
local first, second, ai, ai2, spare, remote, parked = car(0), car(4000), car(900), car(100), car(4500), car(5000), car(200)
slots[0], slots[7] = first.id, second.id -- the player indices are deliberately sparse
local roster = { 0, 7 }
core_multiseat = { getPlayers = function(result)
  result = result or {}
  table.clear(result)
  for i, player in ipairs(roster) do result[i] = player end
  return result
end }
be = {}
function be:getPlayerVehicleID(player) return slots[player] or -1 end
function be:getObjectCount()
  local n = 0
  for _, id in ipairs(order) do if objects[id] then n = n + 1 end end
  return n
end
function be:getObject(i)
  local n = -1
  for _, id in ipairs(order) do
    if objects[id] then n = n + 1; if n == i then return objects[id] end end
  end
end
function be:queueAllObjectLua() end
function be:enterVehicle(player, o) slots[player] = o and o.id or nil end
function getPlayerVehicle(player) return objects[slots[player or 0]] end
function getObjectByID(id) return objects[id] end

pool = { allVehs = { [ai.id] = 1, [ai2.id] = 1 }, maxActiveAmount = activeAmount }
local function activeCount()
  local n = 0
  for _, state in pairs(pool.allVehs) do if state == 1 then n = n + 1 end end
  return n
end
function pool:removeVeh(id)
  if self.allVehs[id] == nil then return false end
  self.allVehs[id] = nil
  return true
end
function pool:insertVeh(id, force)
  local o = objects[id]
  if not o or self.allVehs[id] ~= nil then return false end
  local active = o:getActive()
  if active and activeCount() >= self.maxActiveAmount then
    if force then self.maxActiveAmount = self.maxActiveAmount + 1 else active = false end
  end
  self.allVehs[id] = active and 1 or 0
  o:setActive(active and 1 or 0)
  return true
end
core_vehicleActivePooling = { getPool = function(name) if name == 'autoTraffic' then return pool end end }
local function setActiveAmount(amount)
  activeAmount, pool.maxActiveAmount = amount, amount
  for _, id in ipairs(aiIds) do
    if pool.allVehs[id] == 1 and activeCount() > amount then objects[id]:setActive(0) end
  end
  for _, id in ipairs(aiIds) do
    if pool.allVehs[id] == 0 and activeCount() < amount then objects[id]:setActive(1) end
  end
end
forceTeleport = function(id)
  local t = data[id]
  if t and t.ignoreForceTeleport then return end
  teleports[id] = (teleports[id] or 0) + 1
  if t then t._teleport = nil end -- also represents stock onRespawn's repaint
end
local function aiData()
  return { isAi = true, enableRespawn = true, state = 'active', alpha = 1 }
end
-- Player cars are in the full traffic data, but not in the AI list (as in gameplay/traffic.lua).
data[first.id], data[second.id], data[spare.id] = { isAi = false }, { isAi = false }, { isAi = false }
data[ai.id], data[ai2.id], aiIds = aiData(), aiData(), { ai.id, ai2.id }
gameplay_traffic = {
  getState = function() return 'on' end,
  getTrafficData = function() return data end,
  getTrafficList = function() return aiIds end,
  getTrafficVars = function() return { activeAmount = activeAmount } end,
  setActiveAmount = setActiveAmount,
  forceTeleport = forceTeleport,
  forceTeleportAll = function(ids)
    bulk = {}
    for _, id in ipairs(ids or aiIds) do bulk[id] = true; forceTeleport(id) end
  end,
}
gameplay_parking = { getParkedCarsList = function() return { second.id, parked.id } end }

local files = {}
FS = { fileExists = function(_, p) return files[p] ~= nil end, removeFile = function(_, p) files[p] = nil end }
local anchor = {}
function anchor:worldToGeodetic(x, y, z) return y, x, z end
function anchor:geodeticToWorld(lat, lon, h) return lon, lat, h end
beamngEarth_core = { _state = function() return { anchor = anchor } end, teleport = function() end }
function castRayStatic() return 12 end -- stored cars do not reload during this fixture
MPVehicleGE = { isOwn = function(id) return id ~= remote.id end }

local graph = { nodes = { a = { 0, 0, 10, 4 }, b = { 100, 0, 10, 4 } },
  edges = { { 'a', 'b', 0, 1, 1, 10, 1, 4 } } }
function readFile() return 'graph' end
function jsonDecode() return graph end
function jsonEncode() return '{}' end
function writeFile() end
path = { getPathLevelInfo = function() return '' end }
local gameMap = { nodes = {} }
map = { getMap = function() return gameMap end, reset = function(nodes) gameMap.nodes = nodes end }

local N = require('beamngEarth/navgraph')
local CF = require('beamngEarth/collisionfoci')
local FC = require('beamngEarth/farcars')
N.GUARD_S, N.MIN_GAP_S = 0, 0
local box = { -500, -500, 500, 500 }
local function applyGraph(serial)
  T.check(N.onMessage({ anchorId = 1, path = 'graph', serial = serial }, 1), 'graph accepted')
  N.update(1, box)
end
local function switched(old, new, player)
  -- This fixture also runs against current main, before the new switch hook exists.
  if N.onVehicleSwitched then N.onVehicleSwitched(old, new, player) end
end
local function kinds()
  local out = {}
  for _, v in ipairs(CF.vehicles()) do out[v.id] = v end
  return out
end

applyGraph(1)
N.update(1, box) -- the periodic out-of-block guard
T.check((teleports[ai.id] or 0) > 0, 'ordinary AI beyond the block is recycled')
T.check(not teleports[first.id] and not teleports[second.id] and not teleports[spare.id],
  'both occupied cars and an unoccupied player car in traffic data are not recycled')
local v = kinds()
T.check(v[second.id] and v[second.id].kind == 'player', 'the second seat overrides traffic and parking classification')
T.check(v[spare.id] and v[spare.id].kind == 'player' and v[ai.id].kind == 'traffic',
  'a previous player car is distinct from ordinary AI')
local focus = CF.compute(CF.vehicles(), first.id, 0, 0)
local secondFocus
for _, f in ipairs(focus) do if f.id == second.id then secondFocus = f end end
T.check(secondFocus and secondFocus.r == CF.PLAYER_R and secondFocus.trees, 'the second driver gets a player collision island')

-- Joining an AI car while the graph is being rebuilt: the roster has not caught up yet. The stock pool already
-- made both AI cars inactive; entering must reactivate the selected car without its deferred teleport/repaint.
N.onTeleport()
T.check(activeAmount == 0 and not ai:getActive() and not ai2:getActive() and second:getActive(),
  'teleport holds ordinary AI without deactivating the second player')
roster, slots[7] = { 0 }, ai.id
local beforeAI = teleports[ai.id] or 0
switched(second.id, ai.id, 7)
T.check(ai:getActive() and pool.allVehs[ai.id] == nil and not data[ai.id].enableRespawn
  and data[ai.id].ignoreForceTeleport, 'joined AI removed from the hold and protected immediately')
T.check((teleports[ai.id] or 0) == beforeAI, 'joining an inactive AI car does not trigger its deferred teleport')
T.check(kinds()[ai.id].kind == 'player', 'a newly switched sparse slot is remembered until the roster catches up')
applyGraph(2)
T.check(activeAmount == 2 and ai:getActive() and (teleports[ai.id] or 0) == beforeAI,
  'restoring the pool does not move the second driver')
T.check(bulk[ai2.id] and not bulk[ai.id] and not bulk[first.id], 'the all-forced path scatters only unoccupied AI')
T.check((teleports[ai2.id] or 0) > 0, 'unoccupied AI is recycled after the new graph')

roster, slots[7] = { 0, 7 }, second.id
switched(ai.id, second.id, 7)
T.check(pool.allVehs[ai.id] ~= nil and data[ai.id].enableRespawn == true and not data[ai.id].ignoreForceTeleport,
  'leaving returns the AI car to its pool and original respawn policy')
beforeAI = teleports[ai.id] or 0
N.update(1, box)
T.check((teleports[ai.id] or 0) > beforeAI, 'the released AI is recycled again')

-- A car already occupied at teleport time stays active; leaving it during the hold makes it ordinary held AI again.
slots[7] = ai.id
switched(second.id, ai.id, 7)
beforeAI = teleports[ai.id] or 0
N.onTeleport()
T.check(activeAmount == 0 and ai:getActive() and not ai2:getActive(), 'pool hold excludes an already occupied AI car')
slots[7] = second.id
switched(ai.id, second.id, 7)
T.check(pool.allVehs[ai.id] == 0 and not ai:getActive(), 'an AI car abandoned during the hold joins the hold')
applyGraph(3)
T.check(activeAmount == 2 and (teleports[ai.id] or 0) > beforeAI and second:getActive(),
  'abandoned AI resumes normally after the new graph')

-- Slot 0 is protected by exactly the same rule.
slots[0] = ai.id
switched(first.id, ai.id, 0)
beforeAI = teleports[ai.id] or 0
N.onTeleport()
T.check(ai:getActive() and activeAmount == 0, 'the primary driver in an AI car also survives the pool hold')
applyGraph(4)
T.check(ai:getActive() and (teleports[ai.id] or 0) == beforeAI, 'the primary driver is excluded from bulk scattering')
slots[0] = first.id
switched(ai.id, first.id, 0)

-- A native control flag protects a seat even before either the roster or a switch hook exposes its slot.
roster, slots[7], nativeControlled[second.id] = { 0 }, nil, true
T.check(kinds()[second.id].kind == 'player', 'native live control overrides a temporarily missing roster seat')
nativeControlled[second.id] = nil
T.check(kinds()[second.id].kind == 'parked', 'an exited seat no longer protects an unoccupied parked car')
roster, slots[7] = { 0, 7 }, second.id

-- Far-car saves are asynchronous. A player entering after the save request, including just before its file appears,
-- must cancel deletion and restart the far timer when they leave. Other BeamMP owners and AI remain untouched.
FC.reset()
local function tick() FC.update(0.5) end
for _ = 1, 12 do tick() end
local save = FC.state().saving[spare.id]
T.check(save ~= nil, 'an unoccupied previous player car is saved after the far delay')
T.check(not FC.state().saving[second.id] and not FC.state().saving[ai.id] and not FC.state().saving[remote.id],
  'the second driver, AI and another BeamMP owner are not saved')
second.pos = vec3(50, 0, 10)
slots[7] = spare.id
switched(second.id, spare.id, 7)
if save then files[save.rec.path] = true end
tick()
T.check(objects[spare.id] and not FC.state().saving[spare.id] and #FC.list() == 0,
  'a car entered while its save finishes remains live and is not duplicated')
for _ = 1, 12 do tick() end
T.check(objects[spare.id] and not FC.state().saving[spare.id], 'a controlled far car stays live')
slots[7] = second.id
switched(spare.id, second.id, 7)
tick()
T.check(not FC.state().saving[spare.id], 'leaving starts a new far delay, not the old accumulated one')
for _ = 1, 12 do tick() end
save = FC.state().saving[spare.id]
T.check(save ~= nil, 'the released car becomes eligible for normal far saving again')
if save then files[save.rec.path] = true end
tick()
local saved = FC.list()
T.check(not objects[spare.id] and #saved == 1 and objects[remote.id], 'unoccupied own car stored; BeamMP car untouched')

local near = car(60)
FC.onTeleport()
local nearSave = FC.state().saving[near.id]
T.check(nearSave and not FC.state().saving[second.id] and not FC.state().saving[remote.id],
  'teleport saves an unoccupied own car, not a local driver or another BeamMP owner')
slots[7] = near.id
switched(second.id, near.id, 7)
if nearSave then files[nearSave.rec.path] = true end
tick()
T.check(objects[near.id] and not FC.state().saving[near.id] and #FC.list() == 1,
  'entering a teleport save before it finishes cancels unloading')

-- A deliberate far switch cannot delete a car still occupied by a second player, including a late join.
local uid = saved[1] and saved[1].uid
slots[7] = first.id
switched(near.id, first.id, 7)
T.check(not FC.switchTo(uid), 'a shared active car cannot be deleted for a far-car switch')
slots[7] = second.id
switched(first.id, second.id, 7)
T.check(FC.switchTo(uid), 'the ordinary far-car switch still starts')
local keep = FC.state().saving[first.id]
slots[7] = first.id
switched(second.id, first.id, 7)
if keep then files[keep.rec.path] = true end
tick()
T.check(objects[first.id] and not FC.state().switch and #FC.list() == 1,
  'joining a car during a far switch cancels its deletion and duplicate record')

FC.reset()
N.reset()
-- A user can already have traffic on when entering the level: the first graph must release an initial hold too.
slots[7] = second.id
switched(first.id, second.id, 7)
N.onTeleport()
T.check(activeAmount == 0, 'traffic can be held before the first graph is applied')
applyGraph(5)
T.check(activeAmount == 2 and second:getActive(), 'the first graph also restores held ordinary AI')
N.reset()
T.done('test_splitscreen')
