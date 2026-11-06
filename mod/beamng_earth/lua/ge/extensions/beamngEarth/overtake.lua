-- Overtaking on multi-lane highways (Tann2019, 2026-11-05, I-94 in Montana: "the slow thing is a truck. can traffic pass
-- these guys?"). The stock AI never overtakes: it changes lanes only for a turn or an exit, so traffic (and the map's
-- autodrive, the same AI) queued for miles behind a truck in the right lane while the left lane was empty.
--
-- Every SCAN_S, for each traffic car and the player's car while it autodrives (gps.lua): on a one-way carriageway of 2+
-- lanes, held HELD_S behind a slower vehicle in its lane (within LOOK_M, slower than HELD_SHARE of the limit), with the
-- lane to its left clear (CLEAR_BACK_M behind to CLEAR_AHEAD_M ahead) and no junction within JUNCTION_M: the car's AI
-- is told to move one lane left (vehicle ai.laneChange: its plan's nodes shifted sideways, kept within the lanes going its
-- way); its plan's later nodes come in its own lane again, so it drifts back right by itself (keep right except to
-- pass). One shift per pass: a second one is added to the first (laneChange moves the plan's nodes from where they are,
-- clamped only at the lanes' outer edge) and in the live test (I-90 at Billings, 2026-11-05) the repeated shifts took a
-- car past its lane; one that drifts back too soon is held again and passes after COOLDOWN_S. Right-hand traffic: left
-- to pass; left-hand: right.
-- Not the player's car (PLAYER = false): the autodrive's AI drives a given path (gps.lua, ai.driveUsingPath), whose plan
-- the shift is not clamped to the carriageway in: in the same test it ran 10 m left of its lane into the median.
local M = {}

M.enabled = true
M.PLAYER = false
M.SCAN_S = 0.5
M.LOOK_M = 50
M.HELD_S = 2.0
M.HELD_SHARE = 0.85
M.MIN_MS = 8            -- the passer at least this fast (no passing in a queue at a standstill)
M.LANE_TOL_M = 1.6      -- the vehicle ahead counts as in the lane within this sideways
M.CLEAR_BACK_M = 25
M.CLEAR_AHEAD_M = 70
M.JUNCTION_M = 120
M.SHIFT_S = 2.5         -- the move over takes this long at the car's speed (ai.laneChange's distance), at least SHIFT_MIN_M
M.SHIFT_MIN_M = 30
M.PAST_M = 15
M.COOLDOWN_S = 8
M.GIVE_UP_S = 25

local held = {}         -- car id -> os.clock() it started being held
local passing = {}      -- car id -> { lead, at }: passing it
local lastPass = {}     -- car id -> os.clock() of its last pass
local lastScan = -math.huge
local stats = { passes = 0 }

-- Pure: the lanes going the car's way on an edge (lanes string: '+' along in -> out, '-' against; research §2.2),
-- whether it drives along in -> out.
function M.lanesMyWay(lanes, along)
  if type(lanes) ~= 'string' then return 0 end
  local n = 0
  for ch in lanes:gmatch(along and '%+' or '%-') do n = n + 1 end
  return n
end

-- Pure: the car's lane index (0 = leftmost going its way) and the lane width on a one-way carriageway of half width
-- radius with n lanes, the car at lat m right of its centre line (looking the car's way).
function M.laneOf(lat, radius, n)
  if not lat or not radius or radius <= 0 or not n or n < 1 then return nil end
  local w = 2 * radius / n
  local k = math.floor((lat + radius) / w)
  return math.max(0, math.min(n - 1, k)), w
end

-- Pure: the sideways shift (m, + right) that takes a car in lane k of n one lane over to pass (left in right-hand
-- traffic, right in left-hand traffic), or nil when there is no lane that side.
function M.passShift(k, n, w, leftHand)
  if not k or not n or n < 2 then return nil end
  if leftHand then return k < n - 1 and w or nil end
  return k > 0 and -w or nil
end

-- Pure: is a car (speed v m/s, road limit m/s) held by a vehicle ahead in its lane at gap m doing leadV m/s?
function M.isHeld(v, limit, gap, leadV)
  if not v or not limit or not gap or not leadV then return false end
  return gap <= M.LOOK_M and v >= M.MIN_MS * 0.5 and leadV < limit * M.HELD_SHARE and leadV < v + 1.5
end

-- Pure: others [{ahead = m along the car, side = m right of it, v = m/s}]: is the band [lat - tol, lat + tol] sideways
-- (relative to the car) clear from back m behind to ahead m ahead? A car closing fast from behind needs more room.
function M.laneClear(others, shift, back, ahead, v)
  for _, o in ipairs(others) do
    if math.abs(o.side - shift) < M.LANE_TOL_M then
      local room = back + math.max(0, (o.v or 0) - (v or 0)) * 3
      if o.ahead > -room and o.ahead < ahead then return false end
    end
  end
  return true
end

local function stockGraphNode(m, name) return m and m.nodes and m.nodes[name] end

-- The car's edge on the game's map: lanes its way, its lane, lane width, the edge's limit, its heading along the edge.
local function onRoad(m, o, p, f)
  local n1, n2 = map.findClosestRoad(p)
  local a, c = stockGraphNode(m, n1), stockGraphNode(m, n2)
  local e = a and c and (a.links[n2] or c.links[n1])
  if not e or not e.oneWay then return nil end
  local inN, outN = m.nodes[e.inNode or n1], m.nodes[e.outNode or n2]
  if not inN or not outN then return nil end
  local d = outN.pos - inN.pos
  d.z = 0
  if d:length() < 0.1 then return nil end
  d = d:normalized()
  local along = d:dot(f) > 0
  if not along then return nil end           -- (a one-way road driven the wrong way: not ours to sort out)
  local n = M.lanesMyWay(e.lanes, true)
  if n < 2 then return nil end
  local r = vec3(d.y, -d.x, 0)
  local lat = (p - inN.pos):dot(r)
  local radius = math.min(inN.radius or e.inRadius or 0, outN.radius or e.outRadius or 0)
  if radius <= 0 then radius = e.inRadius or 0 end
  local k, w = M.laneOf(lat, radius, n)
  if not k then return nil end
  return { n = n, k = k, w = w, lat = lat, limit = e.speedLimit or 0, dir = d, right = r }
end

-- Is a scan due (navgraph's overtakeScan asks first: the traffic list and the snapshots it builds for a scan are
-- worth making only every SCAN_S, not every frame)?
function M.due(now)
  return M.enabled and now - lastScan >= M.SCAN_S
end

-- One scan (navgraph.update calls it every frame; it runs every SCAN_S). cars: {id = true} of the traffic cars; player:
-- the player's car id while it autodrives, else nil. nearJunction(x, y, r) -> bool. leftHand: traffic on the left.
function M.update(now, carIds, playerAuto, nearJunction, leftHand, controlledFn)
  if not M.enabled or now - lastScan < M.SCAN_S then return end
  lastScan = now
  if simTimeAuthority and simTimeAuthority.getPause and simTimeAuthority.getPause() then return end
  local m = map and map.getMap and map.getMap()
  if not m or not m.nodes then return end
  -- every vehicle that may block or be passed: positions, headings, speeds
  local all = {}
  local function add(id, passer)
    local o = getObjectByID(id)
    if o and o:getActive() then
      local f = o:getDirectionVector()
      f.z = 0
      if f:length() > 0.1 then
        all[#all + 1] = { id = id, o = o, p = o:getPosition(), f = f:normalized(), v = o:getVelocity():length(),
                          passer = passer }
      end
    end
  end
  for id in pairs(carIds) do add(id, not (controlledFn and controlledFn(id))) end
  if playerAuto then add(playerAuto, M.PLAYER) end
  for _, c in ipairs(all) do
    local ok, err = pcall(function()
      if not c.passer then return end
      local road = onRoad(m, c.o, c.p, c.f)
      if not road then held[c.id], passing[c.id] = nil, nil return end
      -- the others round it, in its frame
      local others, lead, gap = {}, nil, nil
      for _, o in ipairs(all) do
        if o ~= c then
          local d = o.p - c.p
          local ahead = d:dot(road.dir)
          local side = d:dot(road.right)
          if math.abs(ahead) < 120 and o.f:dot(c.f) > 0.5 then
            others[#others + 1] = { ahead = ahead, side = side, v = o.v, id = o.id }
            if ahead > 0 and math.abs(side) < M.LANE_TOL_M and (not gap or ahead < gap) then lead, gap = o, ahead end
          end
        end
      end
      local p = passing[c.id]
      if p then
        local l
        for _, o in ipairs(others) do if o.id == p.lead then l = o end end
        if not l or l.ahead < -M.PAST_M or now - p.at > M.GIVE_UP_S then
          passing[c.id] = nil                 -- past it (or it is gone)
        end
        return
      end
      if not lead or not M.isHeld(c.v, road.limit, gap, lead.v) or c.v < M.MIN_MS * 0.5 then
        held[c.id] = nil
        return
      end
      held[c.id] = held[c.id] or now
      if now - held[c.id] < M.HELD_S or now - (lastPass[c.id] or -math.huge) < M.COOLDOWN_S then return end
      local shift = M.passShift(road.k, road.n, road.w, leftHand)
      if not shift or c.v < M.MIN_MS then return end
      if nearJunction and nearJunction(c.p.x + road.dir.x * 60, c.p.y + road.dir.y * 60, M.JUNCTION_M) then return end
      if not M.laneClear(others, shift, M.CLEAR_BACK_M, M.CLEAR_AHEAD_M, c.v) then return end
      c.o:queueLuaCommand(string.format('ai.laneChange(nil, %.1f, %.2f)', math.max(M.SHIFT_MIN_M, c.v * M.SHIFT_S),
        shift))
      passing[c.id] = { lead = lead.id, at = now }
      lastPass[c.id], held[c.id] = now, nil
      stats.passes = stats.passes + 1
    end)
    if not ok then log('W', 'beamngEarth', 'overtake: ' .. tostring(err)) end
  end
  -- forget the cars that are gone
  for id in pairs(lastPass) do
    if not carIds[id] and id ~= playerAuto then lastPass[id], held[id], passing[id] = nil, nil, nil end
  end
end

function M.status() return { passes = stats.passes } end

function M.reset()
  held, passing, lastPass, lastScan = {}, {}, {}, -math.huge
end

return M
