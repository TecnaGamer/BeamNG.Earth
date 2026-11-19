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
-- Every active parked car costs frame time even when it sleeps: the engine's physics frame waits for each vehicle,
-- and while the world server streams (driving) those waits grow. Steam Deck, Des Moines at 45 mph, the player's 5
-- parked cars: PhysicsAndVLUA 0.7 -> 5.1 ms a frame, 26.8 -> 24.4 fps; standing still with the CPU busy, 5 cars 13 ms,
-- 2 cars 3 ms, none 0.2 (2026-11-14; the cars' own vehicle Lua steps were not the cost). So:
-- - a parked car the stock system left behind (no free spot ahead for it: a highway, open country) goes inactive
--   (hidden) once it is HIDE_BEHIND_M behind a moving player (more than 120 degrees off its heading), or HIDE_M away
--   in any direction, and only while it is outside the camera's view (its field of view plus VIEW_MARGIN_DEG: a
--   rear-facing or orbit camera looking at it keeps it); the stock pool uses inactive cars first for its next spot
--   ahead, and with every car hidden one comes back on a free spot UNHIDE_MIN_M .. START_M from the player;
-- - the low quality presets keep fewer of the player's parked cars active at once (ACTIVE_CAP, the stock pool's
--   activeAmount: the rest wait pooled and take turns on the spots ahead), only while the game's parked-car amount
--   is its default (an amount the player chose, in the options or at level start, is kept on every preset), and only
--   while the active amount is still the one this module set (changed by anyone else: theirs, the cap suspended
--   until a new fleet or level).
-- Deck A/B (ABBA, same route, the player's 5 cars, medium): physics 4.1 -> 1.6 ms a frame, 23.4 -> 24.5 fps, frames
-- over 50 ms 1297 -> 1124, physics-led slow frames 283 -> 42 a drive (parked cars off: 27.5 fps, 0.3 ms).
M.HIDE_M = 600          -- (the stock system places cars up to ~400 m ahead: searchPointDist + searchRadius)
M.HIDE_BEHIND_M = 150   -- (it moves a car behind the player on after ~70 m: 50 m + the speed)
M.HIDE_MIN_SPEED = 3    -- m/s: slower, no direction (HIDE_M only)
M.UNHIDE_MIN_M = 120    -- (the stock keepClearRadius: parked cars avoid appearing nearer)
M.HIDE_HOLD_S = 10      -- a car that came back stays at least this long before it can hide again
M.BEHIND_COS = -0.5     -- behind: the cosine of its angle off the player's heading below this (over 120 degrees)
M.VIEW_MARGIN_DEG = 20  -- the camera's half field of view (horizontal, at VIEW_ASPECT) widened by this
M.VIEW_ASPECT = 2.4     -- (the widest screens: 21:9)
M.DEFAULT_PARKED_AMOUNT = 5   -- the game's trafficParkingAmount default (settings/defaults.json) when unreadable
M.ACTIVE_CAP = { low = 2, medium = 2 }   -- (Deck drive: 5 active 5.1 ms, 3: 4.2, 2: 3.0, none 0.7)
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
  -- the bus stations' bays ({x, y, z, heading, 2}: server busstationpark.py) are the buses' (busparked.lua), not spots
  local bus, cars = nil, nil
  for i, r in ipairs(rows) do
    if r[5] == 2 then
      bus = bus or {}
      bus[#bus + 1] = r
      if not cars then cars = {} for j = 1, i - 1 do cars[j] = rows[j] end end
    elseif cars then
      cars[#cars + 1] = r
    end
  end
  if bus then
    require('beamngEarth/busparked').onRows(aid, key, bus)
    rows = cars
  else
    require('beamngEarth/busparked').dropKey(key)
  end
  if #rows == 0 then spots[key] = nil dirty = true return end
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
  require('beamngEarth/busparked').dropKey(key)
  if spots[key] then
    spots[key] = nil
    dirty = true
  end
end

-- The player teleported: the parked cars go inactive (their old positions have no ground) and come back on the
-- new place's spots once it has them.
local heldActive = nil
function M.onTeleport()
  require('beamngEarth/busparked').onTeleport()
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
    vec3(r[6] or M.SPOT_SCL[1], r[7] or M.SPOT_SCL[2], M.SPOT_SCL[3]))
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
  local keep, near, mappedNear = {}, 0, 0
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
          if d2 < s2 then
            near = near + 1
            if r[6] and r[7] then mappedNear = mappedNear + 1 end
          end
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
  return near, mappedNear
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
local hidden = {}       -- parked car id -> 'stranded' (no free spot for it) or 'behind' (left behind) while hidden
local backAt = {}       -- parked car id -> os.clock() it came back (HIDE_HOLD_S)
-- Free spots within rad (START_M) of the player, at least minRad (0) from it.
function M.freeNear(player, rad, minRad)
  local n = 0
  local r2, m2 = (rad or M.START_M) ^ 2, (minRad or 0) ^ 2
  for _, ps in pairs(objs) do
    local p = ps.pos
    if p and not ps.vehicle then
      local d2 = (p.x - player.x) ^ 2 + (p.y - player.y) ^ 2
      if d2 < r2 and d2 >= m2 then n = n + 1 end
    end
  end
  return n
end

-- Pure: a parked car at p left behind by the stock system: HIDE_M from the player pp, or HIDE_BEHIND_M behind it
-- while it moves (pp.vx, pp.vy at least HIDE_MIN_SPEED).
function M.leftBehind(p, pp)
  local dx, dy = p.x - pp.x, p.y - pp.y
  local d2 = dx * dx + dy * dy
  if d2 > M.HIDE_M * M.HIDE_M then return true end
  if not M.moving(pp) or d2 < M.HIDE_BEHIND_M * M.HIDE_BEHIND_M then return false end
  local vx, vy = pp.vx or 0, pp.vy or 0
  return dx * vx + dy * vy < M.BEHIND_COS * math.sqrt(d2 * (vx * vx + vy * vy))
end

-- The current camera as { pos, fwd, cosHalf } (cosHalf: the cosine of the half field of view widened by
-- VIEW_MARGIN_DEG), nil when unknown (then nothing hides).
function M.cameraView()
  local cam = rawget(_G, 'core_camera')
  if not (cam and cam.getPosition and cam.getForward) then return nil end
  local okP, pos = pcall(cam.getPosition)
  local okF, fwd = pcall(cam.getForward)
  if not (okP and okF and pos and fwd) then return nil end
  local vfov = 75
  if cam.getFovDeg then
    local okV, v = pcall(cam.getFovDeg)
    if okV and tonumber(v) then vfov = tonumber(v) end
  end
  local half = math.deg(math.atan(math.tan(math.rad(vfov) / 2) * M.VIEW_ASPECT)) + M.VIEW_MARGIN_DEG
  return { pos = pos, fwd = fwd, cosHalf = half >= 180 and -1 or math.cos(math.rad(half)) }
end

-- Pure: point p inside the camera view v (M.cameraView); no view: in view.
function M.inView(p, v)
  if not v then return true end
  local dx, dy, dz = p.x - v.pos.x, p.y - v.pos.y, (p.z or 0) - (v.pos.z or 0)
  local d = math.sqrt(dx * dx + dy * dy + dz * dz)
  if d < 1 then return true end
  local f = v.fwd
  local fl = math.sqrt(f.x * f.x + f.y * f.y + (f.z or 0) * (f.z or 0))
  if fl == 0 then return true end
  return (dx * f.x + dy * f.y + dz * (f.z or 0)) / (d * fl) > v.cosHalf
end

-- Pure: the player moves (HIDE_MIN_SPEED or faster).
function M.moving(pp)
  local vx, vy = pp.vx or 0, pp.vy or 0
  return vx * vx + vy * vy >= M.HIDE_MIN_SPEED * M.HIDE_MIN_SPEED
end

-- Pure: the parked-car amount is the game's default: the option (getValue('trafficParkingAmount')) at its default
-- (defaultValue, else DEFAULT_PARKED_AMOUNT) and no amount picked at level start (choice.parkedAmount -1: auto).
function M.amountIsDefault(getValue, defaultValue, choice)
  local picked = choice and tonumber(choice.parkedAmount)
  if picked and picked ~= -1 then return false end
  local v = getValue and getValue('trafficParkingAmount')
  if defaultValue == nil then defaultValue = M.DEFAULT_PARKED_AMOUNT end
  return v == nil or v == defaultValue
end

-- The quality preset's cap on active parked cars (nil: the player's amount as it is).
function M.activeCap()
  local ok, s = pcall(require, 'beamngEarth/settings')
  local q = ok and type(s) == 'table' and s.get and s.get('quality')
  local cap = q and M.ACTIVE_CAP[q] or nil
  if not cap then return nil end
  local gs = rawget(_G, 'settings')
  local getValue = gs and gs.getValue and function(k) return gs.getValue(k) end
  local defaults = gs and type(gs.impl) == 'table' and type(gs.impl.defaultValues) == 'table' and gs.impl.defaultValues
  local okN, ng = pcall(require, 'beamngEarth/navgraph')
  if not M.amountIsDefault(getValue, defaults and defaults.trafficParkingAmount, okN and ng.choice or nil) then return nil end
  return cap
end
-- The cap is ours only while the stock activeAmount is the value we set (applied); then capped is the amount it had
-- before. Anyone else changing it (the player, a mission, another mod) ends that and suspends the cap (theirs, any
-- value, kept) until a new fleet (setupVehicles) or a new level; a new fleet also ends ownership.
local capped, applied, suspended = nil, nil, false
local function applyCap(gp)
  if heldActive or suspended or not (gp.getParkingVars and gp.setActiveAmount) then return end
  local cur = tonumber((gp.getParkingVars() or {}).activeAmount)
  if applied ~= nil and cur ~= applied then                 -- changed from outside: theirs from now on
    capped, applied, suspended = nil, nil, true
    return
  end
  local cap = M.activeCap()
  if cap and cur and cur > cap then
    if applied == nil then capped = cur end
    pcall(gp.setActiveAmount, cap)
    applied = cap
  elseif applied ~= nil and capped and (not cap or cap > applied) then
    local back = cap and math.min(cap, capped) or capped     -- (the cap gone or raised: theirs back, as far as allowed)
    pcall(gp.setActiveAmount, back)
    if back == capped then capped, applied = nil, nil else applied = back end
  end
end

local function guard(player)
  local gp = gameplay_parking
  if not box or not gp or not gp.getState() then return end
  applyCap(gp)
  local now = os.clock()
  local data = (not scatterPending and sites and gp.getParkedCarsData) and gp.getParkedCarsData() or nil
  local free = M.freeNear(player, M.START_M, M.UNHIDE_MIN_M)
  local list = gp.getParkedCarsList() or {}
  local anyActive = false
  for _, id in ipairs(list) do
    local o = getObjectByID(id)
    if o and not hidden[id] and o:getActive() then anyActive = true break end
  end
  local here = vec3(player.x, player.y, player.z)
  local view = nil        -- the camera view, read once a pass when needed (false: unknown)
  for _, id in ipairs(list) do
    local o = getObjectByID(id)
    if o and hidden[id] then
      if o:getActive() then                -- the stock pool took it for a spot ahead: back
        hidden[id], backAt[id] = nil, now
      elseif free > 0 and (hidden[id] == 'stranded' or not anyActive) then
        -- spots near again (for a car left behind: only with no parked car out, else the stock pool brings it)
        hidden[id], backAt[id] = nil, now
        o:setActive(1)
        pcall(gp.forceTeleport, id, here, M.UNHIDE_MIN_M, M.START_M)
        free, anyActive = free - 1, true
      end
    elseif o and o:getActive() then
      local p = o:getPosition()
      local falling = o:getVelocity().z < M.FALL_MS               -- off the ground (a stale spot): rescue it
      local d = data and data[id]
      local off = d and M.offSpot(p, d.parkingSpotId and sites.parkingSpots.objects[d.parkingSpotId], player)
      if falling or off or not M.inside(box, p.x, p.y, M.GUARD_M) then
        if free > 0 then
          pcall(gp.forceTeleport, id, here, M.UNHIDE_MIN_M, M.START_M)
          free = free - 1
        else
          hidden[id] = 'stranded'
          o:setActive(0)
        end
      elseif now - (backAt[id] or -math.huge) >= M.HIDE_HOLD_S and M.leftBehind(p, player) then
        view = view == nil and (M.cameraView() or false) or view
        if view and not M.inView(p, view) then         -- (seen: it waits until it is out of the camera's view)
          hidden[id] = 'behind'
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
    -- a new fleet: the amount we capped goes back to the player's first, and the cap is worked out again for it
    if applied ~= nil and gp.getParkingVars and gp.setActiveAmount
        and tonumber((gp.getParkingVars() or {}).activeAmount) == applied and capped then
      pcall(gp.setActiveAmount, capped)
    end
    capped, applied, suspended = nil, nil, false
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
-- While the player moves, a rebuild also waits until it is REBUILD_MOVE_M from the last one (driving, new tiles make
-- the sites dirty all the time: a rebuild every REBUILD_S cost 5-59 ms frames on a Steam Deck, 33 a 6.6 km drive);
-- the stock search looks at most ~400 m ahead, KEEP_M covers it with this margin. A new anchor, block or scatter
-- rebuilds at once.
M.REBUILD_MOVE_M = 100
local waited = 0
local builtAt = nil     -- the player's {x, y} at the last rebuild; nil: the next one is due at once

function M.update(aid, player, blk, gov)
  if aid ~= anchorId then
    anchorId = aid
    objs = {}
    dirty = true
    newAnchor = true
    builtAt = nil
  end
  if blk and (not box or blk[1] ~= box[1] or blk[2] ~= box[2]) then
    box = { blk[1], blk[2], blk[3], blk[4] }
    dirty = true
    builtAt = nil
  end
  if player and os.clock() - lastGuard >= M.GUARD_S then
    lastGuard = os.clock()
    local okG, errG = pcall(guard, player)
    if not okG then log('W', 'beamngEarth', 'parking guard: ' .. tostring(errG)) end
  end
  if player then lastPlayer = player end
  require('beamngEarth/busparked').update(aid, player, os.clock(), gov)
  hookSetup()
  if not M.enabled or not player or not dirty or not next(spots) then return end
  -- no parked cars (the player's choice, or removed): the sites only every REBUILD_OFF_S (a rebuild cost 24 ms every
  -- few seconds while driving, used by nobody, up to 88 ms on a Steam Deck); turning them on (the radial menu: the
  -- stock setupVehicles) rebuilds them first (hookSetup)
  local gpOn = gameplay_parking and gameplay_parking.getState and gameplay_parking.getState()
  local off = not gpOn and (started or not require('beamngEarth/navgraph').choice.parked)
  local t = os.clock()
  if t - lastBuild < (off and M.REBUILD_OFF_S or M.REBUILD_S) then return end
  if builtAt and started and not scatterPending and M.moving(player)
      and (player.x - builtAt[1]) ^ 2 + (player.y - builtAt[2]) ^ 2 < M.REBUILD_MOVE_M * M.REBUILD_MOVE_M then
    return
  end
  if gov and gov.roomFor and waited < M.REBUILD_WAIT_FRAMES and not gov:roomFor(M.REBUILD_ROOM_MS) then
    waited = waited + 1
    return
  end
  waited = 0
  lastBuild, dirty, builtAt = t, false, { player.x, player.y }
  local sw = stopwatch.new()
  local ok, near, mappedNear = pcall(rebuild, player)
  if gov and gov.noteFrame then gov:noteFrame(sw()) end
  if not ok then
    log('W', 'beamngEarth', 'parking: ' .. tostring(near))
    return
  end
  local gp = gameplay_parking
  if not gp or (near < M.START_MIN and mappedNear == 0) then return end
  if not started then
    started = true
    -- the player's level-start choice (navgraph.choice; the radial menu works at any time)
    local c = require('beamngEarth/navgraph').choice
    if c.parked and not gp.getState() then
      local amount = (c.parkedAmount and c.parkedAmount > 0) and c.parkedAmount or -1
      -- A small village can have just a few known driveways. Start within their actual capacity.
      if near < M.START_MIN then
        local small = math.max(1, math.floor(near * 0.6))
        amount = amount > 0 and math.min(amount, small) or small
      end
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
  require('beamngEarth/busparked').reset()
  spots, objs, anchorId, dirty, lastBuild, sites = {}, {}, nil, false, -math.huge, nil
  hidden, backAt, capped, applied, builtAt, suspended = {}, {}, nil, nil, nil, false
  box, lastGuard, heldActive = nil, -math.huge, nil
  started, scatterPending = false, false
  lastPlayer = nil
end

return M
