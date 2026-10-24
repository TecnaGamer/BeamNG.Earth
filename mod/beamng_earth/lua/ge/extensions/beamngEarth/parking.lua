-- Parked cars (the user, 2026-10-14: "BeamNG handles spawning traffic and parked cars pretty well"): the
-- road tiles carry curbside spots and lot stalls (server/parking.py: rows {x, y, z, heading[, 1 = lot]})
-- and this module hands them to BeamNG's own parking system (gameplay_parking) as a sites object, the way a
-- stock level's city.sites.json does. The stock system spawns the player's parked-car amount (Options >
-- Gameplay > Traffic, trafficParkedAmount) and keeps moving them to free spots near the player as the
-- player drives; the radial menu's traffic entries work as on stock maps.
--
-- One sites object per session. Its spot objects are kept by position (so a car's spot stays the same object
-- while its tile is loaded) and rebuilt at most every REBUILD_S from the spots within KEEP_M of the player
-- (the quadtree is rebuilt with them; finalizeSites). A spot a car stands on stays until the car moves on.
-- A new anchor clears them all (another frame); after a teleport the cars are scattered onto the new spots
-- once there are enough near the player (a rebase moves every vehicle with the world: nothing to do).
local M = {}
local stopwatch = require('beamngEarth/stopwatch')

M.REBUILD_S = 2.0
M.REBUILD_OFF_S = 60.0   -- ... with no parked cars on (update; turning them on rebuilds first: hookSetup)
-- spots this close to the player are in the sites: the stock system looks SEARCH 200 m round a point 200 m ahead of
-- the player (gameplay/parking.lua searchRadius / searchPointDist), ~400 m out. 900 m until 2026-10-24: a rebuild
-- (every few seconds while driving) took 24 ms on average and up to 94 ms with ~4,700 spots (I-10 Phoenix, PC)
M.KEEP_M = 500
-- ... at most this many of them, the nearest (an I-10 drive past Phoenix's lots had 3,064 within 500 m: a rebuild
-- sorted and indexed them all every couple of seconds; the stock system fills a few dozen spots)
M.MAX_SPOTS = 800
M.START_M = 400         -- the parked cars start / scatter once this many spots are within START_M ...
M.START_MIN = 24        -- ... at least this many
M.SPOT_SCL = { 2.5, 6.0, 3.0 }      -- the stock spot size (city.sites.json)
-- The only collidable ground is the terrain block (core: lattice.groundBox), which follows the player: spots
-- this far inside its edge only, and a parked car the block left behind (within GUARD_M of the edge or past
-- it) goes to a spot near the player before it can fall (checked every GUARD_S).
M.EDGE_M = 80
M.GUARD_M = 30
M.GUARD_S = 0.5
M.FALL_MS = -12         -- a parked car falling faster than this (m/s) is off the ground: moved too
M.OFF_SPOT_M = 4        -- ... and one this far from its spot (none, or another frame's) ...
M.OFF_SPOT_FAR_M = 60   -- ... once the player is this far from it (a car the player pushed stays in view)
M.enabled = true

local spots = {}        -- tile key -> { anchor, rows }
local objs = {}         -- position key -> parking spot object (current anchor)
local anchorId = nil
local dirty = false
local lastBuild = -math.huge   -- os.clock() of the last rebuild
local sites = nil
local box = nil          -- the block's ground box {x0, y0, x1, y1} the sites were built for
local lastGuard = -math.huge
local started = false   -- the parked cars were started once this level (the player may remove them)
local scatterPending = false

function M.onRows(aid, key, rows)
  if type(rows) ~= 'table' then return end
  -- (its box: rebuild passes over a tile whose box lies beyond KEEP_M without looking at its rows)
  local x0, y0, x1, y1 = math.huge, math.huge, -math.huge, -math.huge
  for _, r in ipairs(rows) do
    local x, y = tonumber(r[1]), tonumber(r[2])
    if x and y then
      if x < x0 then x0 = x end
      if x > x1 then x1 = x end
      if y < y0 then y0 = y end
      if y > y1 then y1 = y end
    end
  end
  spots[key] = { anchor = aid, rows = rows, x0 = x0, y0 = y0, x1 = x1, y1 = y1 }
  dirty = true
end

function M.dropKey(key)
  if spots[key] then
    spots[key] = nil
    dirty = true
  end
end

-- The player teleported: the parked cars go inactive (their old positions have no ground) and come back on the
-- new place's spots once it has them.
local heldActive = nil
function M.onTeleport()
  scatterPending = true
  local gp = gameplay_parking
  if gp and gp.getState() and not heldActive then
    local vars = gp.getParkingVars and gp.getParkingVars() or {}
    heldActive = vars.activeAmount or math.huge
    pcall(gp.setActiveAmount, 0)
  end
end

-- the stock extensions the parked cars need (a level without traffic support loads none of them;
-- setupVehicles spawns its group through core_multiSpawn; gameplay_walk: 0.40's traffic.lua needs it, navgraph.lua)
M.EXTENSIONS = { 'core_multiSpawn', 'gameplay_traffic', 'gameplay_parking', 'gameplay_walk' }
local function parkingSystem()
  if not (gameplay_parking and core_multiSpawn and gameplay_walk) and extensions and extensions.load then
    pcall(extensions.load, unpack(M.EXTENSIONS))
  end
  return gameplay_parking
end

local function newSites()
  return require('/lua/ge/extensions/gameplay/sites/sites')('BeamNG.Earth')
end

-- Pure: the position key of a spot row (spots from neighbouring tiles never coincide).
function M.spotKey(r) return string.format('%.1f_%.1f', r[1], r[2]) end

local function makeSpot(r)
  local ps = sites.parkingSpots.objectConstructor(sites)
  local h = math.rad(r[4])
  ps:set(vec3(r[1], r[2], r[3]), quatFromDir(vec3(math.cos(h), math.sin(h), 0), vec3(0, 0, 1)),
    vec3(M.SPOT_SCL[1], M.SPOT_SCL[2], M.SPOT_SCL[3]))
  if r[5] ~= 1 then                       -- curbside (lot stalls: either way in, not on a street)
    ps.customFields:addTag('street')      -- mapmgr tracks the car (traffic dodges it)
    ps.customFields:addTag('forwards')    -- parallel parking: nose in the direction of travel
  end
  return ps
end

-- Pure: point (x, y) inside box {x0, y0, x1, y1} shrunk by m.
function M.inside(b, x, y, m)
  return not b or (x >= b[1] + m and x <= b[3] - m and y >= b[2] + m and y <= b[4] - m)
end

-- Rebuilds the sites from the spots near the player; returns the number of spots within START_M.
local newAnchor = false   -- the next rebuild is the first of an anchor
local function rebuild(player)
  sites = sites or newSites()
  local list = sites.parkingSpots
  local keep, near = {}, 0
  local k2, s2 = M.KEEP_M * M.KEEP_M, M.START_M * M.START_M
  local kx, ky = M.KEEP_M, M.KEEP_M
  local cand, nc = {}, 0           -- rows within KEEP_M on the block, then the MAX_SPOTS nearest of them
  for _, t in pairs(spots) do
    if t.anchor == anchorId and not (t.x0 and (t.x0 > player.x + kx or t.x1 < player.x - kx
        or t.y0 > player.y + ky or t.y1 < player.y - ky)) then
      for _, r in ipairs(t.rows) do
        local dx, dy = r[1] - player.x, r[2] - player.y
        local d2 = dx * dx + dy * dy
        if d2 < k2 and M.inside(box, r[1], r[2], M.EDGE_M) then
          nc = nc + 1
          cand[nc] = { d2, r }
          if d2 < s2 then near = near + 1 end
        end
      end
    end
  end
  if nc > M.MAX_SPOTS then
    table.sort(cand, function(a, b) return a[1] < b[1] end)
  end
  for i = 1, math.min(nc, M.MAX_SPOTS) do
    local r = cand[i][2]
    local key = r.key or M.spotKey(r)   -- (the key kept on the row: one string.format per spot, not per rebuild)
    r.key = key
    keep[key] = objs[key] or makeSpot(r)
  end
  for key, ps in pairs(objs) do
    if not keep[key] and ps.vehicle then keep[key] = ps end   -- a car stands on it
  end
  objs = keep
  table.clear(list.objects)          -- (its metatable answers unknown ids with a "missing" spot)
  for _, ps in pairs(keep) do list.objects[ps.id] = ps end
  list:sort()
  sites:finalizeSites()
  local gp = parkingSystem()
  if gp then
    gp.setSites(sites)
    if newAnchor and gp.getState() and gp.onVehicleResetted and be then
      -- the stock system caches the spots it found near the player (currParkingSpots): an anchor's spots are
      -- in another frame, so it looks again (its player-reset path)
      local me = be:getPlayerVehicleID(0)
      if me and me >= 0 then pcall(gp.onVehicleResetted, me) end
    end
  end
  newAnchor = false
  return near
end

-- Pure: a parked car at p is off its spot ps (none, gone with its anchor or tile, or more than OFF_SPOT_M away)
-- and far enough from the player (pp) to be moved unseen.
function M.offSpot(p, ps, pp)
  local dx, dy = p.x - pp.x, p.y - pp.y
  if dx * dx + dy * dy < M.OFF_SPOT_FAR_M * M.OFF_SPOT_FAR_M then return false end
  if not ps or ps.missing or not ps.pos then return true end
  local sx, sy = p.x - ps.pos.x, p.y - ps.pos.y
  return sx * sx + sy * sy > M.OFF_SPOT_M * M.OFF_SPOT_M
end

-- Parked cars the block left behind go to a spot near the player (gameplay_parking.forceTeleport), and so do
-- cars off their spot: one the scatter after a teleport missed keeps its old frame's position, which is any
-- place in the new one (Fells Point 2026-10-14: two stood in a one-lane street and traffic queued behind them).
-- With no free spot within START_M (open country, a desert: none in the map data) the stock fallback is a traffic
-- teleport to any road of the graph, in view, and the guard moved it again half a second later: 16 cars flickered
-- about in the distance, ~30 moves a second (the user's session 2026-10-15, Arizona). Those cars wait hidden instead
-- and come back once spots are near (M.freeNear).
local hidden = {}       -- parked car id -> true while it waits hidden for a spot
function M.freeNear(player, rad)
  local n = 0
  local r2 = (rad or M.START_M) ^ 2
  for _, ps in pairs(objs) do
    local p = ps.pos
    if p and not ps.vehicle and (p.x - player.x) ^ 2 + (p.y - player.y) ^ 2 < r2 then n = n + 1 end
  end
  return n
end

local function guard(player)
  local gp = gameplay_parking
  if not box or not gp or not gp.getState() then return end
  local data = (not scatterPending and sites and gp.getParkedCarsData) and gp.getParkedCarsData() or nil
  local free = M.freeNear(player)
  for _, id in ipairs(gp.getParkedCarsList() or {}) do
    local o = getObjectByID(id)
    if o and hidden[id] then
      if free > 0 then                     -- spots near again: back, onto one of them
        hidden[id] = nil
        o:setActive(1)
        pcall(gp.forceTeleport, id, vec3(player.x, player.y, player.z), 0, M.START_M)
        free = free - 1
      end
    elseif o and o:getActive() then
      local p = o:getPosition()
      local falling = o:getVelocity().z < M.FALL_MS               -- off the ground (a stale spot): rescue it
      local d = data and data[id]
      local off = d and M.offSpot(p, d.parkingSpotId and sites.parkingSpots.objects[d.parkingSpotId], player)
      if falling or off or not M.inside(box, p.x, p.y, M.GUARD_M) then
        if free > 0 then
          pcall(gp.forceTeleport, id, vec3(player.x, player.y, player.z), 0, M.START_M)
          free = free - 1
        else
          hidden[id] = true
          o:setActive(0)
        end
      end
    end
  end
end

-- The stock gameplay_parking.setupVehicles (the radial menu's parked cars, our own start) rebuilds the sites first
-- when they are stale, so with the parked cars off they need not be kept up (update). Wrapped once per module table
-- (an extension reload makes a new one); the stock function runs unchanged.
local lastPlayer = nil
local function hookSetup()
  local gp = rawget(_G, 'gameplay_parking')
  if not gp or gp.__bngeEarthHooked or type(gp.setupVehicles) ~= 'function' then return end
  local orig = gp.setupVehicles
  gp.setupVehicles = function(...)
    if M.enabled and lastPlayer and next(spots) and (dirty or not sites) then
      lastBuild, dirty = os.clock(), false
      local ok, err = pcall(rebuild, lastPlayer)
      if not ok then log('W', 'beamngEarth', 'parking: ' .. tostring(err)) end
    end
    return orig(...)
  end
  gp.__bngeEarthHooked = true
end

-- blk: the block's ground box {x0, y0, x1, y1} (nil: no limit).
-- A rebuild waits for a light frame (governor roomFor, as navgraph's applies), REBUILD_WAIT_FRAMES at most
M.REBUILD_ROOM_MS = 4.0
M.REBUILD_WAIT_FRAMES = 30
local waited = 0

function M.update(aid, player, blk, gov)
  if aid ~= anchorId then
    anchorId = aid
    objs = {}
    dirty = true
    newAnchor = true
  end
  if blk and (not box or blk[1] ~= box[1] or blk[2] ~= box[2]) then
    box = { blk[1], blk[2], blk[3], blk[4] }
    dirty = true
  end
  if player and os.clock() - lastGuard >= M.GUARD_S then
    lastGuard = os.clock()
    local okG, errG = pcall(guard, player)
    if not okG then log('W', 'beamngEarth', 'parking guard: ' .. tostring(errG)) end
  end
  if player then lastPlayer = player end
  hookSetup()
  if not M.enabled or not player or not dirty or not next(spots) then return end
  -- no parked cars (the player's choice, or removed): the sites only every REBUILD_OFF_S (a rebuild cost 24 ms every
  -- few seconds while driving, used by nobody, up to 88 ms on a Steam Deck); turning them on (the radial menu: the
  -- stock setupVehicles) rebuilds them first (hookSetup)
  local gpOn = gameplay_parking and gameplay_parking.getState and gameplay_parking.getState()
  local off = not gpOn and (started or not require('beamngEarth/navgraph').choice.parked)
  local t = os.clock()
  if t - lastBuild < (off and M.REBUILD_OFF_S or M.REBUILD_S) then return end
  if gov and gov.roomFor and waited < M.REBUILD_WAIT_FRAMES and not gov:roomFor(M.REBUILD_ROOM_MS) then
    waited = waited + 1
    return
  end
  waited = 0
  lastBuild, dirty = t, false
  local sw = stopwatch.new()
  local ok, near = pcall(rebuild, player)
  if gov and gov.noteFrame then gov:noteFrame(sw()) end
  if not ok then
    log('W', 'beamngEarth', 'parking: ' .. tostring(near))
    return
  end
  local gp = gameplay_parking
  if not gp or near < M.START_MIN then return end
  if not started then
    started = true
    -- the player's level-start choice (navgraph.choice; the radial menu works at any time)
    local c = require('beamngEarth/navgraph').choice
    if c.parked and not gp.getState() then
      local amount = (c.parkedAmount and c.parkedAmount > 0) and c.parkedAmount or -1
      local okS, err = pcall(gp.setupVehicles, amount, { pos = vec3(player.x, player.y, player.z) })
      if not okS then log('W', 'beamngEarth', 'parking: setupVehicles: ' .. tostring(err)) end
    end
    scatterPending = false
  elseif scatterPending then
    scatterPending = false
    if heldActive then
      pcall(gp.setActiveAmount, heldActive)
      heldActive = nil
    end
    if gp.getState() then pcall(gp.scatterParkedCars) end
  end
end

function M.status()
  local n = 0
  for _ in pairs(objs) do n = n + 1 end
  return { tiles = (function() local c = 0 for _ in pairs(spots) do c = c + 1 end return c end)(), spots = n,
           started = started, active = gameplay_parking and gameplay_parking.getState() or false }
end

-- The level ended: the stock system forgets our sites with it.
function M.reset()
  spots, objs, anchorId, dirty, lastBuild, sites = {}, {}, nil, false, -math.huge, nil
  hidden = {}
  box, lastGuard, heldActive = nil, -math.huge, nil
  started, scatterPending = false, false
  lastPlayer = nil
end

return M
