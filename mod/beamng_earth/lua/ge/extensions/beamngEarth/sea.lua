-- The sea: WaterBlock tiles that follow the Earth's curve, from the server's 'sea' messages (server
-- seatiles.py / seacarve.py, --sea on; docs/research/2026-10-13-water-probe.md). Each tile is a square
-- at the sea level of its centre, tilted to the local vertical there; the server carves the ground
-- under the sea. A WaterBlock's surface is at its position (the engine caps its volume at the box's
-- centre), its volume DEPTH_M / 2 below.
local M = {}
local waterguard = require('beamngEarth/waterguard')
M.GUARD_VERSION = 1

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
local rawWant = nil      -- server geometry before finite-column tunnel exclusions
local guardOwners, guardRecords = {}, {} -- road tile instances, retained until their last mesh is deleted
local segments, fluidDirty = {}, false
M.riversVersion = 0      -- counts changes of want's rivers (ambience.lua's river sounds follow them)
local made = {}         -- key -> { id, t }
local madeRivers = {}   -- key -> id
local riverData = {}    -- key -> definition used for its live object
local madeAnchor = nil
local phys = nil        -- { id, x, y, z } of the physics block
local physStale = false -- tiles were made since it was: made again once a frame makes none
M.created, M.nudges, M.physMade, M.riversMade, M.riverErrors = 0, 0, 0, 0, 0
M.RIVER_PREFIX = 'bngEarthRiver_'
-- Rivers farther than RIVER_HIDE_M from the camera (their box) are hidden, shown again within RIVER_SHOW_M: each River
-- is a WaterObject with its own per-frame render and reflection setup, ~18 % of the game's main thread at 70 mph on
-- the A10 west of Genoa (404 Rivers in the 3 x 3 map cells, half of them over 2.4 km away; 2026-11-05). Far off, the
-- ground photo shows the water. Checked every RIVER_CULL_S.
M.RIVER_SHOW_M, M.RIVER_HIDE_M, M.RIVER_CULL_S = 1100, 1200, 0.5
M.riversHidden = 0
local riverBox = {}     -- key -> { x0, y0, z0, x1, y1, z1 } (the River's world box, read once)
local riverHidden = {}  -- key -> true while hidden
local lastCull = -1e9
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
    local o = m.id and scenetree.findObjectById(m.id)
    if o then o:delete() end
  end
  for _, id in pairs(madeRivers) do
    local o = scenetree.findObjectById(id)
    if o then o:delete() end
  end
  made, madeRivers, riverData, madeAnchor = {}, {}, {}, nil
  riverBox, riverHidden = {}, {}
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
  riverData[key] = r
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

local function makePhys(p, z, size, depth)
  removePhys()
  local o = createObject('WaterBlock')
  setGridSize(o, size)
  for _, f in ipairs(FIELDS) do o:setField(f[1], f[2], f[3]) end
  -- the textures too, though it is never drawn: without them each move logged "Texture missing: " (an empty name)
  -- twice (342 log errors in a 33 min drive, BeamNG 0.40.4, 2026-10-30)
  for _, x in ipairs(TEXTURES) do o:setField(x[1], 0, exists(x[2]) and x[2] or x[3]) end
  o:setField('gridElementSize', 0, tostring(size / 16))
  o:setPosition(vec3(p.x, p.y, z))
  o:setScale(vec3(size, size, 2 * depth))
  o.canSave = false
  o:registerObject(M.PHYS_NAME)
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
  if o.setRenderEnabled then o:setRenderEnabled(false) end
  phys = { id = o:getId(), x = p.x, y = p.y, z = z, size = size, depth = depth }
  M.physMade = M.physMade + 1
end

local function create(key, t)
  if (t.depth or M.DEPTH_M / 2) <= .01 then made[key] = { t = t }; return end
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
  o:setScale(vec3(t.size + M.TILE_OVERLAP_M, t.size + M.TILE_OVERLAP_M, 2 * (t.depth or M.DEPTH_M / 2)))
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

local function sameTile(a, b)
  return b and a.x == b.x and a.y == b.y and a.z == b.z and a.size == b.size and a.nx == b.nx and a.ny == b.ny
    and (a.depth or M.DEPTH_M / 2) == (b.depth or M.DEPTH_M / 2)
end
local function sameRiver(a, b)
  if a == b then return true end
  if not a or not b or a.flow ~= b.flow or a.kind ~= b.kind or #a.nodes ~= #b.nodes then return false end
  for i, n in ipairs(a.nodes) do for j = 1, 5 do if n[j] ~= b.nodes[i][j] then return false end end end
  return true
end
local function replan(protectionChanged)
  if not rawWant then return end
  want = { anchorId = rawWant.anchorId, shores = rawWant.shores,
    tiles = waterguard.blocks(rawWant.tiles, segments, M.DEPTH_M / 2),
    rivers = waterguard.rivers(rawWant.rivers, segments) }
  -- Synchronous removal is intentional: roads.update runs before sea.update. No old deep fluid volume may
  -- survive until after the new tunnel collision has been published, even when creations span many frames.
  for key, m in pairs(made) do
    if not sameTile(m.t, want.tiles[key]) then
      local o = m.id and scenetree.findObjectById(m.id); if o then o:delete() end
      made[key] = nil
      fluidDirty = true
    end
  end
  for key, id in pairs(madeRivers) do
    if not sameRiver(riverData[key], want.rivers[key]) then
      local o = scenetree.findObjectById(id); if o then o:delete() end
      madeRivers[key], riverData[key], riverBox[key], riverHidden[key] = nil, nil, nil, nil
      fluidDirty = true
    end
  end
  if protectionChanged or fluidDirty then removePhys(); physStale, fluidDirty = true, true end
  M.riversVersion = M.riversVersion + 1
end
local function rebuildGuards()
  local records = {}; for id, r in pairs(guardRecords) do records[id] = r.record end
  segments = waterguard.segments(records)
  replan(true)
end

-- token identifies a road tile INSTANCE, not its reusable tile key. Object references retain it through
-- time-sliced deletion, content swaps and disconnects. Records carry geodetic floor points from the server.
function M.protect(token, records, anchor)
  if not anchor or type(records) ~= 'table' or #records == 0 or guardOwners[token] then return end
  local ids, changed = {}, false
  for _, r in ipairs(records) do
    if type(r.nodes) == 'table' and #r.nodes >= 2 and type(r.id) == 'string' and r.r and r.h then
      local entry = guardRecords[r.id]
      if not entry then
        local nodes = {}
        for _, p in ipairs(r.nodes) do nodes[#nodes + 1] = { anchor:geodeticToWorld(p[1], p[2], p[3]) } end
        entry = { refs = 0, geo = r, record = { nodes = nodes, r = r.r, h = r.h } }
        guardRecords[r.id], changed = entry, true
      end
      if not ids[r.id] then ids[r.id] = true; entry.refs = entry.refs + 1 end
    end
  end
  guardOwners[token] = { refs = 1, ids = ids }
  if changed then rebuildGuards() end
  return token
end
function M.retain(token)
  if guardOwners[token] then guardOwners[token].refs = guardOwners[token].refs + 1 end
end
function M.release(token)
  local owner = guardOwners[token]
  if not owner then return end
  owner.refs = owner.refs - 1
  if owner.refs > 0 then return end
  guardOwners[token] = nil
  local changed = false
  for id in pairs(owner.ids) do
    local entry = guardRecords[id]; entry.refs = entry.refs - 1
    if entry.refs == 0 then guardRecords[id], changed = nil, true end
  end
  if changed then rebuildGuards() end
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
  rawWant = { anchorId = msg.anchorId, tiles = tiles, rivers = rivers, shores = shores }
  replan(false)
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
-- The distance (m) from p to the box b ({ x0, y0, z0, x1, y1, z1 }): 0 inside it. Pure.
function M.boxDistance(b, p)
  local dx = math.max(b[1] - p.x, 0, p.x - b[4])
  local dy = math.max(b[2] - p.y, 0, p.y - b[5])
  local dz = math.max(b[3] - p.z, 0, p.z - b[6])
  return math.sqrt(dx * dx + dy * dy + dz * dz)
end

-- Hide / show the made Rivers by their distance from the camera (RIVER_HIDE_M / RIVER_SHOW_M).
local function cullRivers(now)
  if now - lastCull < M.RIVER_CULL_S then return end
  lastCull = now
  local cam = getCameraPosition and getCameraPosition()
  if not cam then return end
  local hidden = 0
  for key, id in pairs(madeRivers) do
    local o = scenetree.findObjectById(id)
    if o then
      local b = riverBox[key]
      if not b and o.getWorldBox then
        local wb = o:getWorldBox()
        if wb and wb.minExtents then
          local lo, hi = wb.minExtents, wb.maxExtents
          b = { lo.x, lo.y, lo.z, hi.x, hi.y, hi.z }
          riverBox[key] = b
        end
      end
      if b then
        local d = M.boxDistance(b, cam)
        if riverHidden[key] then
          if d < M.RIVER_SHOW_M then o:setHidden(false); riverHidden[key] = nil end
        elseif d > M.RIVER_HIDE_M then
          o:setHidden(true); riverHidden[key] = true
        end
      end
      if riverHidden[key] then hidden = hidden + 1 end
    end
  end
  M.riversHidden = hidden
end

function M.update(anchorId, player)
  if want == nil or want.anchorId ~= anchorId then
    if madeAnchor ~= nil then removeAll() end
    return
  end
  if madeAnchor ~= anchorId then removeAll(); madeAnchor = anchorId end
  for key, m in pairs(made) do
    if not want.tiles[key] then
      local o = m.id and scenetree.findObjectById(m.id)
      if o then o:delete() end
      made[key] = nil
    end
  end
  for key, id in pairs(madeRivers) do
    if not want.rivers[key] then
      local o = scenetree.findObjectById(id)
      if o then o:delete() end
      madeRivers[key], riverBox[key], riverHidden[key] = nil, nil, nil
    end
  end
  -- (a rebuilt water volume re-places a slow car only when it is in that water: with "nudge = fluidDirty" every
  -- tunnel or water-guard change of the streamed road tiles reset a stopped car in place, wherever it was, every few
  -- seconds or minutes: players' reports on the dev builds since PR #822, 2026-11-08)
  local n, nudge = 0, false
  if fluidDirty and player then
    for _, t in pairs(want.tiles) do
      if inside(t, player) and player.z < t.z + 1 then nudge = true; break end
    end
  end
  fluidDirty = false
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
      local size = M.PHYS_SIZE
      local depth = waterguard.cap({ x = player.x, y = player.y, z = z, size = size }, segments, M.DEPTH_M / 2)
      while depth <= .01 and size > 4 do
        size = size / 2
        depth = waterguard.cap({ x = player.x, y = player.y, z = z, size = size }, segments, M.DEPTH_M / 2)
      end
      if depth <= .01 then removePhys()
      elseif (physStale and n == 0) or not phys or math.abs(player.x - phys.x) > math.max(1, phys.size / 4)
          or math.abs(player.y - phys.y) > math.max(1, phys.size / 4) or math.abs(z - phys.z) > 0.05
          or depth < phys.depth - .001 or size ~= phys.size then
        makePhys(player, z, size, depth)
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
  cullRivers(os.clock())
end

-- Origin rebase (core.lua finishRebase): the water moves into the new frame now instead of going until the server's
-- next 'sea' message (2026-10-17: every rebase left no sea, and no water physics under a car in it, for seconds).
-- The wanted tiles and rivers are mapped (p_new = R*p + t, a tile's normal turned by R) and become newAnchorId's; the
-- made blocks take their new place and tilt, the physics block its new place; rivers (splines in world coordinates)
-- are made again from the mapped nodes. The server's next 'sea' message replaces the set as usual.
function M.transform(xf, newAnchorId, anchor)
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
  if want and want.anchorId ~= newAnchorId then
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
    local o = m.id and scenetree.findObjectById(m.id)
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
  madeRivers, riverBox, riverHidden = {}, {}, {}
  if phys then
    phys.x, phys.y, phys.z = xf:apply(phys.x, phys.y, phys.z)
    local o = scenetree.findObjectById(phys.id)
    if o then o:setPosition(vec3(phys.x, phys.y, phys.z)) end
  end
  if madeAnchor ~= nil then madeAnchor = newAnchorId end
  if rawWant and rawWant.anchorId ~= newAnchorId then
    for _, t in pairs(rawWant.tiles) do
      -- With no exclusions want shares these tables and they were already mapped above.
      local shared = false; for _, w in pairs(want and want.tiles or {}) do if w == t then shared = true; break end end
      if not shared then mapTile(t) end
    end
    for key, r in pairs(rawWant.rivers) do
      if r ~= (want and want.rivers or {})[key] then
        for _, p in ipairs(r.nodes) do p[1], p[2], p[3] = xf:apply(p[1], p[2], p[3]) end
      end
    end
    rawWant.anchorId = newAnchorId
  end
  for _, r in pairs(guardRecords) do
    for i, p in ipairs(r.record.nodes) do
      if anchor then r.record.nodes[i] = { anchor:geodeticToWorld(unpack(r.geo.nodes[i])) }
      else p[1], p[2], p[3] = xf:apply(p[1], p[2], p[3]) end
    end
  end
  if next(guardRecords) then rebuildGuards() end
end

-- Disconnect, a new server session, mod unload: no tiles until the server says so again.
function M.clear()
  want, rawWant = nil, nil
  M.riversVersion = M.riversVersion + 1
  removeAll()
end

-- Level end: the level deletes its objects itself.
function M.reset()
  want, rawWant, made, madeRivers, riverData, madeAnchor, phys, physStale = nil, nil, {}, {}, {}, nil, nil, false
  guardOwners, guardRecords, segments, fluidDirty = {}, {}, {}, false
  riverBox, riverHidden = {}, {}
  M.riversVersion = M.riversVersion + 1
end

function M.status()
  local n = 0
  for _ in pairs(made) do n = n + 1 end
  local w = 0
  for _ in pairs(want and want.tiles or {}) do w = w + 1 end
  local rv = 0
  for _ in pairs(madeRivers) do rv = rv + 1 end
  local guards, owners = 0, 0
  for _ in pairs(guardRecords) do guards = guards + 1 end
  for _ in pairs(guardOwners) do owners = owners + 1 end
  return { tiles = n, want = w, guards = guards, guardOwners = owners, created = M.created, nudges = M.nudges, physMade = M.physMade,
    phys = phys and phys.z or nil, rivers = rv, riversMade = M.riversMade, riverErrors = M.riverErrors,
    riversHidden = M.riversHidden }
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
