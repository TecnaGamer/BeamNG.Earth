-- The sea: WaterBlock tiles that follow the Earth's curve, from the server's 'sea' messages (server
-- seatiles.py / seacarve.py, --sea on; docs/research/2026-10-13-water-probe.md). Each tile is a square
-- at the sea level of its centre, tilted to the local vertical there; the server carves the ground
-- under the sea. A WaterBlock's surface is at its position (the engine caps its volume at the box's
-- centre), its volume DEPTH_M / 2 below.
local M = {}

M.DEPTH_M = 200         -- the blocks' z scale: water down to 100 m below the surface (sea floors <= 80 m)
M.CREATE_PER_FRAME = 4  -- tiles made per frame (a full set of ~50 in under a second, no hitch)
M.NUDGE_SPEED = 1.0     -- m/s: a car slower than this inside a new tile is re-placed where it is, so its
                        -- physics sees the water (a parked car only notices water when it moves)
M.PREFIX = 'bngEarthSea_'
-- A vehicle takes one water object at a time, the one made last that it is in (2026-10-13: on the edge
-- between two tiles only the nodes over one of them were in water). So one more block, not drawn,
-- PHYS_SIZE m square around the player at the level of the tile under it, is made again after any
-- new tile (it must be the newest) and when the player nears its edge or the level under it changes.
M.TILE_OVERLAP_M = 1.0  -- each tile this much larger than its square (half each side)
M.PHYS_SIZE = 256
M.PHYS_MARGIN = 64      -- m: made again around the player when it is closer than this to its edge
M.PHYS_NAME = 'bngEarthSeaPhys'

-- East Coast USA's ocean ("Ocean", levels/east_coast_usa/main), its colours made murkier (a bay, not
-- the open Atlantic); core textures where that level's own may be missing
local FIELDS = {
  { 'baseColor', 0, '46 98 96 255' }, { 'underwaterColor', 0, '60 96 88 240' },
  { 'clarity', 0, '0.15' }, { 'fresnelBias', 0, '-0.5' }, { 'fresnelPower', 0, '0.2' },
  { 'specularPower', 0, '1000' }, { 'reflectDetailAdjust', 0, '0' }, { 'reflectMaxRateMs', 0, '1' },
  { 'fullReflect', 0, '0' }, { 'gridElementSize', 0, '0.8' },
  { 'depthGradientMax', 0, '5' }, { 'distortEndDist', 0, '15' }, { 'distortFullDepth', 0, '5' },
  { 'waterFogDensity', 0, '0.4' }, { 'waterFogDensityOffset', 0, '0.1' },
  { 'wetDarkening', 0, '0.3' }, { 'wetDepth', 0, '0.5' }, { 'viscosity', 0, '0.001' },
  { 'overallRippleMagnitude', 0, '0.2' }, { 'overallWaveMagnitude', 0, '0.1' },
  { 'overallFoamOpacity', 0, '3.5' }, { 'foamMaxDepth', 0, '0.75' }, { 'foamAmbientLerp', 0, '1' },
  { 'foamRippleInfluence', 0, '0.005' },
  { 'rippleDir', 0, '0 -1' }, { 'rippleDir', 1, '0.707 0.707' }, { 'rippleDir', 2, '-0.5 0.86' },
  { 'rippleMagnitude', 0, '0.5' }, { 'rippleMagnitude', 1, '0.5' }, { 'rippleMagnitude', 2, '0.35' },
  { 'rippleSpeed', 0, '0.008' }, { 'rippleSpeed', 1, '0.05' }, { 'rippleSpeed', 2, '0.003' },
  { 'rippleTexScale', 0, '10 10' }, { 'rippleTexScale', 1, '2 2' }, { 'rippleTexScale', 2, '120 120' },
  { 'waveDir', 0, '0 1' }, { 'waveDir', 1, '-0.707 -0.707' }, { 'waveDir', 2, '0.5 0.86' },
  { 'waveMagnitude', 0, '0.5' }, { 'waveMagnitude', 1, '0.105' }, { 'waveMagnitude', 2, '0.1' },
  { 'waveSpeed', 0, '2' }, { 'waveSpeed', 1, '1' }, { 'waveSpeed', 2, '2' },
  { 'foamDir', 0, '0 1' }, { 'foamDir', 1, '0 -1' }, { 'foamSpeed', 0, '0.01' }, { 'foamSpeed', 1, '0.01' },
  { 'foamOpacity', 1, '5' }, { 'foamTexScale', 1, '4 4' },
}
-- Lakes and rivers (the server's "L..." keys, and the Rivers): fresh water, greener and browner than the bay
local INLAND = {
  { 'baseColor', 0, '44 74 60 255' }, { 'underwaterColor', 0, '52 78 58 240' }, { 'clarity', 0, '0.12' },
  { 'overallWaveMagnitude', 0, '0.15' }, { 'overallRippleMagnitude', 0, '0.6' },
  -- the ripples drift visibly (with 0.12 and the bay's slow speeds a lake looked frozen, 2026-10-13)
  { 'rippleSpeed', 0, '0.03' }, { 'rippleSpeed', 1, '0.09' }, { 'rippleSpeed', 2, '0.012' },
}
-- Rivers along centrelines, styled like the stock maps' (Italy's main rivers, Utah, Jungle Rock Island;
-- 2026-10-13 research): no foam (white fringes at shallow edges); waves small (they fade out at
-- LowLODDistance and popped where a segment switched meshes); the near high-detail mesh to 120 m;
-- 4 m slices (less stretching at bends: the texture runs along the centreline). The ripples run
-- downstream (flowMagnitudePhysics only pushes things; the shader never sees it): tiles longer along the
-- river than across, each layer moving at the river's flow (riverLook below).
local RIVER_LOOK = {
  { 'overallFoamOpacity', 0, '0' }, { 'foamMaxDepth', 0, '0.1' }, { 'depthGradientMax', 0, '6' },
  { 'overallRippleMagnitude', 0, '1.2' }, { 'overallWaveMagnitude', 0, '0.05' },
  { 'rippleMagnitude', 0, '1' }, { 'rippleMagnitude', 1, '1' }, { 'rippleMagnitude', 2, '0.3' },
  { 'rippleDir', 0, '0 1' }, { 'rippleDir', 1, '0.2 1' }, { 'rippleDir', 2, '-0.2 1' },
  { 'rippleTexScale', 0, '2 4' }, { 'rippleTexScale', 1, '5 10' }, { 'rippleTexScale', 2, '20 40' },
  { 'SegmentLength', 0, '4' }, { 'LowLODDistance', 0, '120' },
}
-- ripple layer speeds (tiles per second) so their patterns drift at the river's flow (m/s) / 1, 2, 5
local function riverSpeeds(flow)
  return { flow / 4, flow / 10 / 2, flow / 40 / 5 }
end

local TEXTURES = {      -- field, East Coast's file, the stock fallback (0.40 moved them from /core/art/water to /assets)
  { 'rippleTex', '/levels/east_coast_usa/art/water/ripple3.dds', '/assets/materials/tileable/water/water_effects/ripple_nm.normal.dds' },
  -- (2026-10-15: the ramp is a .png in East Coast USA, and neither fallback existed: ~1000 "texture missing" log
  -- errors per session that filled the log's 15000 lines before the end of a drive)
  { 'depthGradientTex', '/levels/east_coast_usa/art/water/depthcolor_ramp.png',
    '/assets/materials/tileable/water/depthcolor_ramp/river_depthcolor_ramp_b.png' },
  { 'foamTex', '/levels/east_coast_usa/art/water/foam.dds', '/assets/materials/tileable/water/water_effects/foam_b.color.dds' },
}

local want = nil         -- { anchorId, tiles = { key -> tile }, rivers = { key -> river } } from the server
M.riversVersion = 0      -- counts changes of want's rivers (ambience.lua's river sounds follow them)
local made = {}         -- key -> { id, t }
local madeRivers = {}   -- key -> id
local madeAnchor = nil
local phys = nil        -- { id, x, y, z } of the physics block
local physStale = false -- tiles were made since it was: made again once a frame makes none
M.created, M.nudges, M.physMade, M.riversMade, M.riverErrors = 0, 0, 0, 0, 0
M.RIVER_PREFIX = 'bngEarthRiver_'
M.SUBDIVIDE_MIN, M.SUBDIVIDE_COLUMNS = 6, 8    -- a River's near mesh: at most 8 columns, cells >= 6 m

local function exists(p)
  return not FS or FS:fileExists(p)
end

local function removePhys()
  if phys then
    local o = scenetree.findObjectById(phys.id)
    if o then o:delete() end
    phys = nil
  end
end

local function removeAll()
  for _, m in pairs(made) do
    local o = scenetree.findObjectById(m.id)
    if o then o:delete() end
  end
  for _, id in pairs(madeRivers) do
    local o = scenetree.findObjectById(id)
    if o then o:delete() end
  end
  made, madeRivers, madeAnchor = {}, {}, nil
  removePhys()
end

-- A River along a river's or stream's centreline (server riverlines.py): its nodes {x, y, z, width,
-- depth} downstream, flowing at flow m/s (flowMagnitudePhysics pushes what floats in it), made from a
-- definition as the editor does (Sim.deserializeObjectFromText).
local function createRiver(key, r)
  local nodes = {}
  for _, n in ipairs(r.nodes) do nodes[#nodes + 1] = { n[1], n[2], n[3], n[4], n[5], 0, 0, 1 } end
  local def = { name = M.RIVER_PREFIX .. key, class = 'River', position = { nodes[1][1], nodes[1][2], nodes[1][3] },
    nodes = nodes, flowMagnitudePhysics = r.flow, cubemap = 'BNG_Sky_01_cubemap',
    __parent = scenetree.MissionGroup and 'MissionGroup' or nil }
  -- the textures in the definition: registered without them, every River logged three "texture missing" and a
  -- "no parent" error (thousands a session, the log's 15000 lines were full before a drive ended; 2026-10-15)
  for _, x in ipairs(TEXTURES) do def[x[1]] = exists(x[2]) and x[2] or x[3] end
  local ok = pcall(Sim.deserializeObjectFromText, jsonEncode(def), true, true)
  local o = ok and scenetree.findObject(M.RIVER_PREFIX .. key)
  if not o then
    M.riverErrors = M.riverErrors + 1
    return
  end
  for _, f in ipairs(FIELDS) do o:setField(f[1], f[2], f[3]) end
  for _, f in ipairs(INLAND) do o:setField(f[1], f[2], f[3]) end
  for _, f in ipairs(RIVER_LOOK) do o:setField(f[1], f[2], f[3]) end
  for i, v in ipairs(riverSpeeds(r.flow)) do o:setField('rippleSpeed', i - 1, string.format('%.4f', v)) end
  -- the near mesh's columns are its widest node / SubdivideLength: too many and the engine draws no near
  -- mesh at all (a 48 m creek vanished near the camera at 3 m, drew at 6 m; 2026-10-13)
  local wmax = 0
  for _, nd in ipairs(r.nodes) do wmax = math.max(wmax, tonumber(nd[4]) or 0) end
  o:setField('SubdivideLength', 0, string.format('%.1f', math.max(M.SUBDIVIDE_MIN, wmax / M.SUBDIVIDE_COLUMNS)))
  for _, x in ipairs(TEXTURES) do o:setField(x[1], 0, exists(x[2]) and x[2] or x[3]) end
  o:setField('cubemap', 0, 'BNG_Sky_01_cubemap')
  o:setField('flowMagnitudePhysics', 0, tostring(r.flow))
  o.canSave = false
  if scenetree.MissionGroup and o.getGroup and not o:getGroup() then scenetree.MissionGroup:addObject(o) end
  -- styled after it was made: its textures and its near (high-detail) mesh are built again now (without,
  -- it drew "NO MATERIAL" and vanished within LowLODDistance of the camera, 2026-10-13)
  if o.reloadTextures then o:reloadTextures() end
  if o.regenerate then o:regenerate() end
  madeRivers[key] = o:getId()
  M.riversMade = M.riversMade + 1
end

-- The tile of the server's set the point is in (the nearest centre if on an edge), or nil.
local function tileAt(p)
  local best, bd
  for _, t in pairs(want and want.tiles or {}) do
    if math.abs(p.x - t.x) <= t.size / 2 and math.abs(p.y - t.y) <= t.size / 2 then
      local d = (p.x - t.x) ^ 2 + (p.y - t.y) ^ 2
      if not bd or d < bd then best, bd = t, d end
    end
  end
  return best
end

-- gridSize, set first: the engine logs a warning when gridSize exceeds the scale, checked against the scale the block
-- has at that moment (100 m until setScale; and the default gridSize, 128, warned at our setScale)
local function setGridSize(o, size)
  -- (at most 100 m, that default scale; the surface mesh's density is gridElementSize)
  o:setField('gridSize', 0, tostring(math.max(1, math.floor(math.min(100, size)))))
end

local function makePhys(p, z)
  removePhys()
  local o = createObject('WaterBlock')
  setGridSize(o, M.PHYS_SIZE)
  for _, f in ipairs(FIELDS) do o:setField(f[1], f[2], f[3]) end
  o:setField('gridElementSize', 0, tostring(M.PHYS_SIZE / 16))
  o:setPosition(vec3(p.x, p.y, z))
  o:setScale(vec3(M.PHYS_SIZE, M.PHYS_SIZE, M.DEPTH_M))
  o.canSave = false
  o:registerObject(M.PHYS_NAME)
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
  if o.setRenderEnabled then o:setRenderEnabled(false) end
  phys = { id = o:getId(), x = p.x, y = p.y, z = z }
  M.physMade = M.physMade + 1
end

local function create(key, t)
  local o = createObject('WaterBlock')
  setGridSize(o, t.size)
  for _, f in ipairs(FIELDS) do o:setField(f[1], f[2], f[3]) end
  if key:sub(1, 1) == 'L' then
    for _, f in ipairs(INLAND) do o:setField(f[1], f[2], f[3]) end
  end
  for _, x in ipairs(TEXTURES) do o:setField(x[1], 0, exists(x[2]) and x[2] or x[3]) end
  o:setField('cubemap', 0, 'BNG_Sky_01_cubemap')
  -- the local vertical: a tilt of |n| radians about (-ny, nx, 0)
  local n = math.sqrt(t.nx * t.nx + t.ny * t.ny)
  if n > 1e-9 then
    o:setField('rotation', 0, string.format('%.9f %.9f 0 %.6f', -t.ny / n, t.nx / n, math.deg(math.asin(math.min(n, 1)))))
  end
  o:setPosition(vec3(t.x, t.y, t.z))
  -- a little larger than its square: tilted neighbours meet without a hairline gap (2026-10-13)
  o:setScale(vec3(t.size + M.TILE_OVERLAP_M, t.size + M.TILE_OVERLAP_M, M.DEPTH_M))
  -- after the scale: the engine warns ("gridSize cannot be > scale", meaning this one) when it exceeds the scale, 100 m
  -- before setScale (an 8 km tile's 128 m, 2026-10-17)
  o:setField('gridElementSize', 0, tostring(math.max(4, t.size / 64)))
  o.canSave = false
  o:registerObject(M.PREFIX .. key)
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
  made[key] = { id = o:getId(), t = t }
  M.created = M.created + 1
end

local function inside(t, p)
  return math.abs(p.x - t.x) <= t.size / 2 and math.abs(p.y - t.y) <= t.size / 2
end

-- A 'sea' message: { anchorId, tiles = { {key, x, y, z, size, nx, ny}, ... } }.
function M.onMessage(msg)
  local tiles = {}
  for _, r in ipairs(type(msg.tiles) == 'table' and msg.tiles or {}) do
    if type(r) == 'table' and #r >= 7 then
      tiles[tostring(r[1])] = { x = tonumber(r[2]), y = tonumber(r[3]), z = tonumber(r[4]), size = tonumber(r[5]),
        nx = tonumber(r[6]), ny = tonumber(r[7]) }
    end
  end
  local rivers = {}
  for _, r in ipairs(type(msg.rivers) == 'table' and msg.rivers or {}) do
    if type(r) == 'table' and type(r[4]) == 'table' and #r[4] >= 2 then
      rivers[tostring(r[1])] = { kind = r[2], flow = tonumber(r[3]) or 0, nodes = r[4] }
    end
  end
  local shores = {}        -- (the shore sound's lines: server seacarve.shore_plan, ambience.lua)
  for _, r in ipairs(type(msg.shores) == 'table' and msg.shores or {}) do
    if type(r) == 'table' and type(r[3]) == 'table' and #r[3] >= 2 then
      shores[tostring(r[1])] = { kind = r[2], nodes = r[3] }
    end
  end
  want = { anchorId = msg.anchorId, tiles = tiles, rivers = rivers, shores = shores }
  M.riversVersion = M.riversVersion + 1
end

-- The server's rivers (key -> { kind, flow, nodes = { {x, y, z, width, depth}, ... } }) and their anchor, or nil.
function M.rivers()
  if want then return want.rivers, want.anchorId end
end

-- The server's shorelines (key -> { kind = 'sea' | 'lake', nodes = { {x, y, z}, ... } }) and their anchor, or nil.
function M.shores()
  if want then return want.shores or {}, want.anchorId end
end

-- Every frame: the tiles of the server's set for the current anchor exist, the others are gone.
-- player: { x, y, z, vx, vy, vz } in the anchor frame (or nil).
function M.update(anchorId, player)
  if want == nil or want.anchorId ~= anchorId then
    if madeAnchor ~= nil then removeAll() end
    return
  end
  if madeAnchor ~= anchorId then removeAll(); madeAnchor = anchorId end
  for key, m in pairs(made) do
    if not want.tiles[key] then
      local o = scenetree.findObjectById(m.id)
      if o then o:delete() end
      made[key] = nil
    end
  end
  for key, id in pairs(madeRivers) do
    if not want.rivers[key] then
      local o = scenetree.findObjectById(id)
      if o then o:delete() end
      madeRivers[key] = nil
    end
  end
  local n, nudge = 0, false
  for key, r in pairs(want.rivers) do
    if not madeRivers[key] then
      if n >= M.CREATE_PER_FRAME then break end
      createRiver(key, r)
      n = n + 1
    end
  end
  for key, t in pairs(want.tiles) do
    if not made[key] then
      if n >= M.CREATE_PER_FRAME then break end
      create(key, t)
      n = n + 1
      if player and inside(t, player) and player.z < t.z + 1 then nudge = true end
    end
  end
  if player then
    local t = tileAt(player)
    if not t then
      if phys then removePhys() end
    else
      -- the tile's (tilted) surface under the player: a river falls across its tile
      local z = t.z - (t.nx or 0) * (player.x - t.x) - (t.ny or 0) * (player.y - t.y)
      if (physStale and n == 0) or not phys or math.abs(player.x - phys.x) > M.PHYS_SIZE / 2 - M.PHYS_MARGIN
          or math.abs(player.y - phys.y) > M.PHYS_SIZE / 2 - M.PHYS_MARGIN or math.abs(z - phys.z) > 0.05 then
        makePhys(player, z)
        physStale = false
        if player.z < z + 1 then nudge = true end      -- in the water: its physics must take this one
      end
    end
    if n > 0 then physStale = true end
  end
  if nudge and player then
    local speed = math.sqrt((player.vx or 0) ^ 2 + (player.vy or 0) ^ 2 + (player.vz or 0) ^ 2)
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    if veh and speed < M.NUDGE_SPEED then
      local p, r = veh:getPosition(), quat(veh:getRotation())
      veh:setPositionRotation(p.x, p.y, p.z, r.x, r.y, r.z, r.w)
      M.nudges = M.nudges + 1
    end
  end
end

-- Origin rebase (core.lua finishRebase): the water moves into the new frame now instead of going until the server's
-- next 'sea' message (2026-10-17: every rebase left no sea, and no water physics under a car in it, for seconds).
-- The wanted tiles and rivers are mapped (p_new = R*p + t, a tile's normal turned by R) and become newAnchorId's; the
-- made blocks take their new place and tilt, the physics block its new place; rivers (splines in world coordinates)
-- are made again from the mapped nodes. The server's next 'sea' message replaces the set as usual.
function M.transform(xf, newAnchorId)
  local ox, oy, oz = xf:apply(0, 0, 0)
  local function turn(nx, ny)
    local nz = math.sqrt(math.max(0, 1 - nx * nx - ny * ny))
    local x, y = xf:apply(nx, ny, nz)
    return x - ox, y - oy
  end
  local function mapTile(t)
    t.x, t.y, t.z = xf:apply(t.x, t.y, t.z)
    t.nx, t.ny = turn(t.nx or 0, t.ny or 0)
  end
  if want and want.anchorId == madeAnchor then
    for _, t in pairs(want.tiles) do mapTile(t) end
    for _, r in pairs(want.rivers) do
      for _, n in ipairs(r.nodes) do n[1], n[2], n[3] = xf:apply(n[1], n[2], n[3]) end
    end
    for _, r in pairs(want.shores or {}) do
      for _, n in ipairs(r.nodes) do n[1], n[2], n[3] = xf:apply(n[1], n[2], n[3]) end
    end
    want.anchorId = newAnchorId
    M.riversVersion = M.riversVersion + 1
  end
  for key, m in pairs(made) do
    if m.t ~= (want and want.tiles or {})[key] then mapTile(m.t) end   -- (the same table as want's: mapped above)
    local o = scenetree.findObjectById(m.id)
    if o then
      local t = m.t
      local n = math.sqrt(t.nx * t.nx + t.ny * t.ny)
      if n > 1e-9 then
        o:setField('rotation', 0, string.format('%.9f %.9f 0 %.6f', -t.ny / n, t.nx / n, math.deg(math.asin(math.min(n, 1)))))
      end
      o:setPosition(vec3(t.x, t.y, t.z))
    end
  end
  for _, id in pairs(madeRivers) do
    local o = scenetree.findObjectById(id)
    if o then o:delete() end
  end
  madeRivers = {}
  if phys then
    phys.x, phys.y, phys.z = xf:apply(phys.x, phys.y, phys.z)
    local o = scenetree.findObjectById(phys.id)
    if o then o:setPosition(vec3(phys.x, phys.y, phys.z)) end
  end
  if madeAnchor ~= nil then madeAnchor = newAnchorId end
end

-- Disconnect, a new server session, mod unload: no tiles until the server says so again.
function M.clear()
  want = nil
  M.riversVersion = M.riversVersion + 1
  removeAll()
end

-- Level end: the level deletes its objects itself.
function M.reset()
  want, made, madeRivers, madeAnchor, phys, physStale = nil, {}, {}, nil, nil, false
  M.riversVersion = M.riversVersion + 1
end

function M.status()
  local n = 0
  for _ in pairs(made) do n = n + 1 end
  local w = 0
  for _ in pairs(want and want.tiles or {}) do w = w + 1 end
  local rv = 0
  for _ in pairs(madeRivers) do rv = rv + 1 end
  return { tiles = n, want = w, created = M.created, nudges = M.nudges, physMade = M.physMade,
    phys = phys and phys.z or nil, rivers = rv, riversMade = M.riversMade, riverErrors = M.riverErrors }
end

-- A reloaded module (extensions.reload) deletes the tiles (and the old WaterPlane) the old one left.
do
  if scenetree and scenetree.findClassObjects then
    for _, cls in ipairs({ 'WaterBlock', 'WaterPlane', 'River' }) do
      for _, name in ipairs(scenetree.findClassObjects(cls) or {}) do
        if tostring(name):sub(1, #M.PREFIX) == M.PREFIX or name == 'bngEarthSea' or name == M.PHYS_NAME
            or tostring(name):sub(1, #M.RIVER_PREFIX) == M.RIVER_PREFIX then
          local o = scenetree.findObject(name)
          if o then o:delete() end
        end
      end
    end
  end
end

return M
