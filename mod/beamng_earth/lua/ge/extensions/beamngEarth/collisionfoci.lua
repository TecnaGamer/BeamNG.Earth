-- Collision islands for more than one vehicle (the user, 2026-10-22, BeamMP support: "Update the collision LOD to
-- support multiple vehicles at once. As cars spread apart they can have their own island of collision. As they get
-- closer the islands merge. Traffic cars can have smaller/simpler islands").
--
-- The active car keeps its own collision as before (roads.lua COLLIDE_M by speed, trees.lua SOLID_M, the terrain
-- block). Every other vehicle adds a focus { x, y, r, trees }: other players' and player-spawned cars get the active
-- car's slow radius and solid trees, traffic a small radius and no solid trees (each solid-tree change costs the engine
-- time per tree of that shape, trees.lua; traffic stays on the roads). Parked cars none (they do not move). roads.lua
-- and trees.lua make what lies near any focus solid, so overlapping circles are one island; islands() groups them
-- (the terrain patches for islands off the terrain block, later).
local M = {}

M.PLAYER_R = 320        -- m: another player's or a player-spawned car (roads.lua COLLIDE_M min, the active car's slow radius)
M.TRAFFIC_R = 80        -- m: an AI traffic car
M.MIN_SEPARATION_M = 30 -- a vehicle this close to the active car adds no focus (its island is the active car's)
M.UPDATE_S = 0.5

-- Pure: the foci of vehicles { id, x, y, kind = 'player' | 'traffic' | 'parked' } other than the active one at
-- (ax, ay) (nil: none active). [{ x, y, r, trees, id, kind }]
function M.compute(vehicles, activeId, ax, ay)
  local out = {}
  for _, v in ipairs(vehicles) do
    if v.id ~= activeId and (v.kind == 'player' or v.kind == 'traffic') then
      local far = not ax or ((v.x - ax) ^ 2 + (v.y - ay) ^ 2) > M.MIN_SEPARATION_M ^ 2
      if far then
        local player = v.kind == 'player'
        out[#out + 1] = { x = v.x, y = v.y, r = player and M.PLAYER_R or M.TRAFFIC_R, trees = player, id = v.id,
          kind = v.kind }
      end
    end
  end
  return out
end

-- Pure: is (x, y) within extra metres of any focus' circle? (only the foci with trees when trees is set)
function M.near(foci, x, y, extra, trees)
  for _, f in ipairs(foci) do
    if not trees or f.trees then
      local r = f.r + (extra or 0)
      if (x - f.x) ^ 2 + (y - f.y) ^ 2 <= r * r then return true end
    end
  end
  return false
end

-- Pure: the foci (and the active car as { x, y, r }, optional) grouped into islands: circles that overlap are one
-- island. [{ members = { focus... }, x0, y0, x1, y1 }]
function M.islands(foci, active)
  local all = {}
  if active then all[1] = active end
  for _, f in ipairs(foci) do all[#all + 1] = f end
  local n = #all
  local parent = {}
  for i = 1, n do parent[i] = i end
  local function root(i)
    while parent[i] ~= i do parent[i] = parent[parent[i]]; i = parent[i] end
    return i
  end
  for i = 1, n do
    for j = i + 1, n do
      local a, b = all[i], all[j]
      local rr = a.r + b.r
      if (a.x - b.x) ^ 2 + (a.y - b.y) ^ 2 <= rr * rr then parent[root(i)] = root(j) end
    end
  end
  local byRoot, out = {}, {}
  for i = 1, n do
    local r = root(i)
    local isl = byRoot[r]
    if not isl then
      isl = { members = {}, x0 = math.huge, y0 = math.huge, x1 = -math.huge, y1 = -math.huge }
      byRoot[r] = isl
      out[#out + 1] = isl
    end
    local f = all[i]
    isl.members[#isl.members + 1] = f
    isl.x0, isl.y0 = math.min(isl.x0, f.x - f.r), math.min(isl.y0, f.y - f.r)
    isl.x1, isl.y1 = math.max(isl.x1, f.x + f.r), math.max(isl.y1, f.y + f.r)
  end
  return out
end

-- The game's vehicles as { id, x, y, kind }: AI traffic (gameplay_traffic), parked cars (gameplay_parking), the rest
-- 'player' (spawned by a player; in BeamMP another player's car is also a vehicle of this game).
function M.vehicles()
  local out = {}
  if not be or not be.getObjectCount then return out end
  local traffic, parked = {}, {}
  local tr = rawget(_G, 'gameplay_traffic')
  if tr and tr.getTrafficData then
    local ok, d = pcall(tr.getTrafficData)
    if ok and type(d) == 'table' then for id in pairs(d) do traffic[id] = true end end
  end
  local pk = rawget(_G, 'gameplay_parking')
  if pk and pk.getParkedCarsList then
    local ok, d = pcall(pk.getParkedCarsList)
    if ok and type(d) == 'table' then for _, id in pairs(d) do parked[id] = true end end
  end
  -- (other BeamMP players' cars floating off our block, mpsession.lua: nothing is made solid round them)
  local ms = package.loaded['beamngEarth/mpsession']
  local floats = ms and ms.isFloating
  for i = 0, be:getObjectCount() - 1 do
    local o = be:getObject(i)
    if o and o.getPosition and not (floats and floats(o:getId())) then
      local id = o:getId()
      local p = o:getPosition()
      out[#out + 1] = { id = id, x = p.x, y = p.y, kind = traffic[id] and 'traffic' or parked[id] and 'parked' or 'player' }
    end
  end
  return out
end

local since = math.huge
-- Each frame (core.lua): every UPDATE_S the foci of the vehicles other than the active car, plus extra() (the stored
-- far cars about to come back, farcars.lua), handed to the modules that make collision (consumers: functions taking
-- the foci list).
function M.update(dt, consumers, extra)
  since = since + (dt or 0)
  if since < M.UPDATE_S then return end
  since = 0
  local active = getPlayerVehicle and getPlayerVehicle(0)
  local ap = active and active:getPosition()
  local foci = M.compute(M.vehicles(), active and active:getId(), ap and ap.x, ap and ap.y)
  if extra then
    for _, f in ipairs(extra()) do foci[#foci + 1] = f end
  end
  M.last = foci
  for _, f in ipairs(consumers or {}) do f(foci) end
end

return M
