-- Roads near the player (docs/ROADMAP.md v1.6 "Roads", server/roads.py): the server announces road
-- tiles (`roads_tile`: a JSON file of chunks in the anchor frame) and drops them (`roads_drop`); each
-- chunk becomes one DecalRoad drawn on the physics ground (engine probes P1-P3, 2026-10-11: they
-- render on the hidden block, re-clip by themselves when it is re-imported or moved, and cost
-- ~0.45 ms per 100-node road with insertNodeNoRegen + regenerate). Creation and deletion run under the
-- frame governor (kinds roadChunk / roadDelete). A new anchor makes every road of the old one go
-- (the server sends the new frame's tiles again).
--
-- Materials are stock ones from official levels, loaded at runtime from the player's own install
-- (nothing copied): West Coast USA's asphalt strip, sidewalks and lines, Johnson Valley's dirt road
-- tracks, Italy's cobblestone.
--
-- Raised sidewalks (curbs, sidewalk slabs): one mesh per tile (server/sidewalks.py, the way West
-- Coast USA builds its sidewalks: one modelled mesh per district, with its `curbs` and `sidewalk1`
-- materials), announced as the tile's "walk" {shape, pos} and shown as a TSStatic that collides with
-- its visible mesh. A TSStatic only collides after be:reloadCollision() (asynchronous: in effect a
-- frame later; ~1.8 ms), so that runs once per RELOAD_EVERY_S at most while sidewalks come and go.
-- (First cut, replaced: a MeshRoad per sidewalk piece, 3 draw calls each and no curb profile.)
-- Storm drains are DecalRoad chunks like the rest (Driver Training's m_metal_grid_drain).
-- Console: beamngEarth_roads.status(), .setEnabled(bool)

local stopwatch = require('beamngEarth/stopwatch')
local shadowctl = require('beamngEarth/shadowctl')

local M = {}

M.MATERIAL_FILES = { '/levels/west_coast_usa/art/road/main.materials.json',
  '/levels/west_coast_usa/art/shapes/roads/main.materials.json',   -- curbs, sidewalk1 (the sidewalk mesh)
  '/levels/johnson_valley/art/decalroad/main.materials.json',
  '/levels/italy/art/shapes/buildings/main.materials.json',        -- italy_cobblestone (paving stones)
  '/levels/driver_training/art/road/main.materials.json',          -- m_metal_grid_drain (storm drains)
  '/levels/beamng_earth/art/shapes/beamngEarth/buildings.materials.json' }  -- buildings (server/buildings.py)
M.FADE = '450 150'          -- distanceFade: start, range (m)
M.enabled = true
-- be:reloadCollision() at most this often: every rebuild is a ~20 ms physics frame (2026-10-13, the
-- engine's frame metrics), and while driving new sidewalk tiles arrive all the time. They arrive ~640 m
-- ahead (11 s at 58 m/s), so a few seconds before they collide do not matter. A terrain commit rebuilds
-- collision anyway: M.collisionRebuilt() clears the pending rebuild.
-- 2026-10-14: a rebuild had grown to ~56 ms (1.1 M collision vertices with the buildings) and ran every 3 s
-- while driving, most of the hitches of a 34 mph drive: new colliders now wait up to 6 s (still 11 s of margin
-- at 130 mph), and colliders deleted far behind the car (their stale collision harms nobody) up to 30 s.
M.RELOAD_EVERY_S = 6
M.RELOAD_DRIVE_MAX_S = 15.0 -- maximum deferral window (s) before forcing a collision reload when dirty
M.RELOAD_HORIZON_RATIO = 0.55 -- reload once vehicle travels > 55% of collideRadius since last reload
M.DECK_URGENT_M = 200       -- urgent lookahead floor (m) for unflushed collidable decks / tunnels
M.DECK_URGENT_S = 4.0       -- urgent lookahead time (s) for unflushed collidable decks / tunnels
-- Collision only near the car (2026-10-15: the rebuild cost scales with every colliding triangle loaded, ~40 ms in
-- Fells Point with all 36 tiles' buildings and sidewalks): a tile's colliders (sidewalks, decks, the buildings'
-- lean collision mesh "bldcol") are made once its centre is within COLLIDE_LEAD_S of driving (COLLIDE_M min / max)
-- and dropped again UNCOLLIDE_M beyond that. The drawn building meshes only render when the tile has a bldcol.
M.TILE_M = 256
M.COLLIDE_M = { 320, 2000 }
M.COLLIDE_LEAD_S = 14
M.UNCOLLIDE_M = 350
M.COLLIDE_CHECK_S = 0.25
M.RELOAD_DELETES_S = 30
M.DELETE_NEAR_M = 200       -- a collider deleted this close to the player counts as new (no ghost walls)
-- High speed (the user, 2026-10-19: "I always want the ground and any bridges/buildings to have collision ... going at
-- high speeds"; at 180 mph a bridge's collider was solid only 32 m ahead, held back by RELOAD_EVERY_S): above
-- FAST_MS the COLLIDE_LEAD_S reach is ahead of the car only (COLLIDE_M max), beside and behind it COLLIDE_M min;
-- and a collider the car reaches within URGENT_S is made solid at once (URGENT_GAP_S between those rebuilds at most).
M.FAST_MS = 25
M.URGENT_S = 6
M.URGENT_GAP_S = 1
M.SIDE_PENALTY = 4          -- 1 m off the path of a fast car counts as this many along it in nextWalk
M.LEAD_MAX_M = 2000         -- a tile ahead on the path is prioritised up to this far ahead
-- No be:reloadCollision for SWAP_QUIET_FRAMES frames and SWAP_QUIET_MS after a terrain .ter swap (M.terrainSwapped): the
-- swapped block's collision follows by itself a frame later, and a rebuild in between took the block before its
-- heights were in: flat ground at z 0 for a second, and a ball at 180 mph sank under the real ground and fell through
-- it for good (in-game 2026-10-19, once the urgent rebuilds above made that coincidence common).
M.SWAP_QUIET_FRAMES = 3
M.SWAP_QUIET_MS = 150

local newColliders = {}    -- { id, walkQ entry } of the colliders made since the last reloadCollision
local parked = {}          -- walkQ entries of colliders whose tile is not near enough yet
local sinceCollideCheck = stopwatch.new()
local held = {}            -- walkQ entries of colliding meshes held while the player is in their boxes
local lastReloadX, lastReloadY = nil, nil
local noteNewColliders, playerIn, releaseHeld, hasUrgentStructure
local tiles = {}            -- key "a:tx_ty" -> { anchor, objs = { DecalRoad ids }, queued = n, walk = TSStatic id,
                            --   road = TSStatic id }
local walkQ = {}            -- { key, tile, walk = {shape, pos}, what = 'walk' | 'road' } FIFO
local createQ = {}          -- { key, chunk } FIFO
local deleteQ = {}          -- object ids
local loadedMaterials = false
local anchorId = nil
local far = {}              -- far buildings (server buildings.far_tile): key "a:tx_ty" -> { anchor, id, shape, pos, hidden, deferred }
local farQ = {}             -- keys of far tiles to make (ring queue: farH..#farQ)
local farH = 1
local lastError = nil
local sinceChange = stopwatch.new()  -- ms since a tile arrived / went or a road was made / deleted
local sinceReload = stopwatch.new()
local collisionDirty = false
local lastReloadAt = -math.huge   -- now() of the last be:reloadCollision (ours or another's): M.ahead
local deletesDirty = false  -- only far colliders deleted since the last rebuild (RELOAD_DELETES_S)
local unitStats = {}        -- kind -> { n, ms, maxMs, maxShape } (profiled by status().units)
local unitLog = {}          -- last M.UNIT_LOG { kind, ms, shape } entries (where the long frames go)
M.UNIT_LOG = 400

local function noteUnit(kind, ms, shape)
  local s = unitStats[kind]
  if not s then s = { n = 0, ms = 0, maxMs = 0 }; unitStats[kind] = s end
  s.n, s.ms = s.n + 1, s.ms + ms
  if ms > s.maxMs then s.maxMs, s.maxShape = ms, shape end
  if ms >= 5 or kind == 'reload' or kind == 'prewarm' then
    unitLog[#unitLog + 1] = { kind, math.floor(ms * 100 + 0.5) / 100, shape }
    if #unitLog > M.UNIT_LOG then table.remove(unitLog, 1) end
  end
end

-- Every road object lives in one SimGroup. A Lua reload (Lua:requestReload) rebuilds the VM without
-- unloading the extensions, so nothing deletes the old objects: 2026-10-11, after a few reloads, 3,558
-- DecalRoads and 120 sidewalk MeshRoads were in the scene for 1,011 + 51 tracked (the user saw fps drop).
-- The group is emptied when this module loads (see the end of the file).
M.GROUP = 'bngEarthRoads'

local function group()
  local g = scenetree.findObject(M.GROUP)
  if not g then
    g = createObject('SimGroup')
    g.canSave = false
    g:registerObject(M.GROUP)
    scenetree.MissionGroup:addObject(g)
  end
  return g
end

-- Objects of a previous Lua state deleted. Returns how many.
function M.sweep()
  local g = scenetree.MissionGroup and scenetree.findObject(M.GROUP)
  if not g then return 0 end
  local n = g:getCount()
  if n > 0 then
    g:deleteAllObjects()
    be:reloadCollision()
  end
  return n
end

-- The road surfaces drawn opaque (2026-10-14): the stock decal materials are all translucent, and inside
-- one road mesh the markings showed only while the engine happened to draw them after the asphalt; with
-- many more translucent objects in view (buildings' windows, signals) they vanished. Opaque asphalt is
-- drawn first, always, and the translucent lines, crossings and gutters over it.
M.OPAQUE = { 'road_asphalt_2lane', 'italy_cobblestone' }
-- Ground-hugging road ribbons, markings, drains, and raised sidewalks/curbs should never cast shadows
-- into PSSM cascades (per-object shadow flags on TSStatic are no-ops in BeamNG 0.40; Material.castShadows is real).
M.NO_SHADOW = {
  'road_asphalt_2lane', 'italy_cobblestone', 'm_dirt_road', 'sidewalk_concrete',
  'm_metal_grid_drain', 'gutter1', 'line_white', 'line_yellow', 'line_dashed_long', 'crossing_white',
  'curbs', 'sidewalk1', 'tactile_indicator',
}

-- Stock levels reload their own .materials.json on level load, so session-wide Material edits here do not leak.
local function loadMaterials()
  if loadedMaterials then return end
  for _, f in ipairs(M.MATERIAL_FILES) do loadJsonMaterialsFile(f) end
  for _, name in ipairs(M.OPAQUE) do
    local m = scenetree.findObject(name)
    if m then
      pcall(function()
        local cur = m.getField and m:getField('translucent', 0)
        if cur ~= '0' and cur ~= 'false' then
          m:setField('translucent', 0, '0'); m:setField('castShadows', 0, '0'); m:flush(); m:reload()
        end
      end)
    end
  end
  local pad = scenetree.findObject('tactile_indicator')    -- curb ramp pads: US truncated domes are yellow
  if pad then
    pcall(function()
      if not pad.getField or pad:getField('baseColorFactor', 0) ~= '0.95 0.78 0.18 1' then
        pad:setField('baseColorFactor', 0, '0.95 0.78 0.18 1'); pad:setField('castShadows', 0, '0'); pad:flush(); pad:reload()
      end
    end)
  end
  for _, name in ipairs(M.NO_SHADOW) do
    local m = scenetree.findObject(name)
    if m then
      pcall(function()
        local cur = m.getField and m:getField('castShadows', 0)
        if cur ~= '0' and cur ~= 'false' then
          m:setField('castShadows', 0, '0'); m:flush(); m:reload()
        end
      end)
    end
  end
  loadedMaterials = true
end

local function tileKey(aid, k) return tostring(aid) .. ':' .. tostring(k) end

-- Pooled shape files (server/matslots.py, 2026-10-16: the engine's frame cost grows with every distinct shape file it
-- ever loaded): roads_tile / bldfar_tile carry `slots` {what = {path, serial}}, shape files the server re-uses for
-- later tiles once we report [slot, serial] gone (core.lua pose.slotsGone). A slot goes when its tile is dropped AND
-- its last object is deleted (a collider deleted and made again while the tile lives keeps it).
local session = nil        -- the server session the slots belong to (setSession)
local objSlot = {}         -- object id -> slot number of the pooled file it loaded
local slotLive = {}        -- slot number -> objects alive with it
local slotDropped = {}     -- slot number -> { serial, session } of a dropped tile's slot, reported at its last delete
local slotsGone = {}       -- { n, serial, session } to report
local function slotOf(path) return tonumber(type(path) == 'string' and path:match('/slot_(%d+)[_%d]*%.cdae$') or nil) end

function M.setSession(s) session = s end

-- An object with a pooled shape was made / is deleted.
local function slotMade(id, path)
  local n = slotOf(path)
  if not n then return end
  objSlot[id] = n
  slotLive[n] = (slotLive[n] or 0) + 1
end
local function slotObjGone(id)
  local n = objSlot[id]
  if not n then return end
  objSlot[id] = nil
  slotLive[n] = (slotLive[n] or 1) - 1
  if slotLive[n] <= 0 then
    slotLive[n] = nil
    local d = slotDropped[n]
    if d then slotDropped[n] = nil; slotsGone[#slotsGone + 1] = { n, d[1], d[2] } end
  end
end
-- A tile's slot { n, serial, session } is let go: at once if no object has it, else at its last delete.
local function slotDrop(sl)
  if not sl then return end
  if (slotLive[sl[1]] or 0) > 0 then slotDropped[sl[1]] = { sl[2], sl[3] }
  else slotsGone[#slotsGone + 1] = { sl[1], sl[2], sl[3] } end
end
-- The slots of msg.slots[what] as { n, serial, session } (nil: not pooled).
local function msgSlot(msg, what)
  local e = type(msg.slots) == 'table' and msg.slots[what]
  if type(e) ~= 'table' then return nil end
  local n, serial = slotOf(e[1]), tonumber(e[2])
  if not n or not serial then return nil end
  return { n, serial, session, path = e[1] }
end

-- The [slot, serial] pairs gone since the last call, for server session `sess`.
function M.takeSlotsGone(sess)
  if #slotsGone == 0 then return {} end
  local out = {}
  for _, g in ipairs(slotsGone) do if g[3] ~= nil and g[3] == sess then out[#out + 1] = { g[1], g[2] } end end
  slotsGone = {}
  return out
end

-- Every [slot, serial] held for session `sess` (hello after a reconnect): live tiles' slots and dropped ones whose
-- objects are not deleted yet.
function M.slotsHeld(sess)
  local out = {}
  for _, t in pairs(tiles) do
    for _, sl in pairs(t.slots or {}) do if sl[3] == sess then out[#out + 1] = { sl[1], sl[2] } end end
  end
  for _, f in pairs(far) do if f.slot and f.slot[3] == sess then out[#out + 1] = { f.slot[1], f.slot[2] } end end
  for n, d in pairs(slotDropped) do if d[2] == sess then out[#out + 1] = { n, d[1] } end end
  return out
end

local function now() return os.clock() * 1000 end

-- Event-driven sync of a far building tile against its square's near road tile `bld`.
-- Avoids scanning the entire `far` table every frame in updateFar.
local function syncFar(key)
  local f = far[key]
  if not f then return end
  local nt = tiles[key]
  local hide = nt ~= nil and nt.bld ~= nil
  if f.id then
    if hide ~= (f.hidden or false) then
      local o = scenetree.findObjectById(f.id)
      if o then o:setHidden(hide) end
      f.hidden = hide
    end
  elseif not hide and f.deferred then
    f.deferred = false
    farQ[#farQ + 1] = key
  end
end

local function dropTile(key)
  local t = tiles[key]
  if not t then return end
  for _, sl in pairs(t.slots or {}) do slotDrop(sl) end
  for _, id in ipairs(t.objs) do deleteQ[#deleteQ + 1] = id end
  if t.walk then deleteQ[#deleteQ + 1] = t.walk end
  if t.road then deleteQ[#deleteQ + 1] = t.road end
  if t.deck then deleteQ[#deleteQ + 1] = t.deck end
  for _, w in ipairs({ 'bld', 'bld2', 'bld3', 'bld4', 'bldcol', 'walkcol', 'tunnel' }) do if t[w] then deleteQ[#deleteQ + 1] = t[w] end end
  if t.wire then deleteQ[#deleteQ + 1] = t.wire end
  -- Any mesh or chunk still queued for this tile must go too: its slot paths were just reported gone above, so if a
  -- new copy of the same key arrives before walkQ drains (PR #64: re-sent tiles), the old queue entries would be made
  -- into the new tile after the server had already reused their slot files for another tile (houses floating near
  -- Cleburne, AL, 2026-10-19).
  local function purge(list)
    local keep = {}
    for _, q in ipairs(list) do if q.key ~= key then keep[#keep + 1] = q end end
    return keep
  end
  parked, walkQ, held, createQ = purge(parked), purge(walkQ), purge(held), purge(createQ)
  if t.props then require('beamngEarth/trees').dropKey((key:gsub(':', ':p', 1))) end
  if t.park then require('beamngEarth/parking').dropKey(key) end
  tiles[key] = nil
  syncFar(key)
end

-- Every road not of anchor aid goes (a new anchor: the server re-sends its tiles).
local function switchAnchor(aid)
  if aid == anchorId then return end
  anchorId = aid
  for key, t in pairs(tiles) do
    if t.anchor ~= aid and not t.ghost then dropTile(key) end
  end
  for key, f in pairs(far) do
    if f.anchor ~= aid and not f.ghost then
      if f.id then deleteQ[#deleteQ + 1] = f.id end
      slotDrop(f.slot)
      far[key] = nil
    end
  end
  local keep = {}
  for _, q in ipairs(createQ) do if tiles[q.key] then keep[#keep + 1] = q end end
  createQ = keep
  keep = {}
  for _, q in ipairs(walkQ) do if tiles[q.key] then keep[#keep + 1] = q end end
  walkQ = keep
end

-- Pure: the DecalRoad fields of a chunk (name, value), in order.
function M.fieldsFor(chunk)
  return {
    { 'material', chunk.m }, { 'textureLength', tostring(chunk.tl or 10) },
    { 'renderPriority', tostring(chunk.p or 20) }, { 'drivability', '-1' }, { 'improvedSpline', 'true' },
    { 'overObjects', 'false' }, { 'distanceFade', M.FADE }, { 'decalBias', '0.1' },
  }
end

-- The collision radius around the player (m): COLLIDE_LEAD_S of driving at its speed, within COLLIDE_M.
function M.collideRadius(speed)
  return math.max(M.COLLIDE_M[1], math.min(M.COLLIDE_M[2], (speed or 0) * M.COLLIDE_LEAD_S))
end

local lastRadius = M.COLLIDE_M[1]
local lastSpeed = 0
local holdActive = false
local swapQuiet = nil       -- { frames left, stopwatch } after a terrain swap (M.terrainSwapped)
local lastDir = nil         -- unit { x, y } of the player's motion at FAST_MS or more, else nil

local function nearPlayerXY(x, y, r, px, py)
  if not x or not y then return true end
  if not px or not py then
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    if not veh then return true end
    local q = veh:getPosition()
    if not q then return true end
    px, py = q.x, q.y
  end
  local dx, dy = x - px, y - py
  return dx * dx + dy * dy <= r * r
end

-- The collision radius towards tile centre (cx, cy) from the player at p: lastRadius ahead of a fast car (within 45
-- degrees of its heading, and its own tile), COLLIDE_M min beside and behind it; lastRadius all round when slow.
function M.radiusToward(p, cx, cy, radius, dir)
  if not dir or not p then return radius end
  local dx, dy = cx - p.x, cy - p.y
  local d = math.sqrt(dx * dx + dy * dy)
  if d <= M.TILE_M or (dx * dir.x + dy * dir.y) >= 0.7071 * d then return radius end
  return math.min(radius, M.COLLIDE_M[1])
end

-- Is tile t's centre within the collision radius of the player? (no car or no centre: yes)
-- Zero per-frame vec3 allocations.
function M.collideNear(t, extra, px, py)
  if not t or not t.cx then return true end
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  local pos = veh and veh.getPosition and veh:getPosition()
  local r = (pos and lastDir) and M.radiusToward(pos, t.cx, t.cy, lastRadius, lastDir) or lastRadius
  r = r + (extra or 0) + M.TILE_M * 0.71
  return nearPlayerXY(t.cx, t.cy, r, px or (pos and pos.x), py or (pos and pos.y))
end

-- Every COLLIDE_CHECK_S: parked colliders whose tile came near queued; far tiles' colliders dropped.
local function updateCollideRange()
  if sinceCollideCheck() < M.COLLIDE_CHECK_S * 1000 then return end
  sinceCollideCheck = stopwatch.new()
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  local v = veh and veh.getVelocity and veh:getVelocity()
  local sp = v and math.sqrt(v.x * v.x + v.y * v.y) or 0
  lastRadius = M.collideRadius(sp)
  lastSpeed = sp
  lastDir = (sp >= (M.FAST_MS or 25)) and { x = v.x / sp, y = v.y / sp } or nil
  if #parked > 0 then
    local keep = {}
    for _, q in ipairs(parked) do
      local t = tiles[q.key]
      if t then
        if M.collideNear(t, 0, px, py) then walkQ[#walkQ + 1] = q else keep[#keep + 1] = q end
      end
    end
    parked = keep
  end
  for key, t in pairs(tiles) do
    if t.cq and not M.collideNear(t, M.UNCOLLIDE_M, px, py) then
      for what, q in pairs(t.cq) do
        if t[what] then deleteQ[#deleteQ + 1] = t[what]; t[what] = nil end
        if not t.ghost then parked[#parked + 1] = q end   -- a ghost's queue entries are the old frame's
      end
      t.cq = nil
    end
  end
end

local function isCollider(o)
  local c = o.class or (o.getClassName and o:getClassName())
  if c == 'MeshRoad' then return true end
  return c == 'TSStatic' and o:getField('collisionType', 0) ~= 'None'
end

-- collide: true ('Visible Mesh Final'), a collision type name, or false / nil (render only)
-- shadow: true to keep shadow casting enabled, false/nil to disable when non-colliding
local function createWalk(walk, collide, shadow)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, walk.shape)
  -- 'Visible Mesh Final' (the engine's own suggestion: 'Performance: Mesh is using visible collision ... switch the
  -- mesh to Visible Mesh Final'); the same collision (in-game rays, 2026-10-17)
  local colType = type(collide) == 'string' and collide or (collide and 'Visible Mesh Final' or 'None')
  o:setField('collisionType', 0, colType)
  o:setField('decalType', 0, 'None')
  local castShadow = shadow
  if castShadow == nil then
    castShadow = colType == 'Visible Mesh Final'
  end
  if not castShadow then
    shadowctl.excludeObject(o, 'road_static')
  end
  o.canSave = false
  o:setPosition(vec3(walk.pos[1], walk.pos[2], walk.pos[3]))
  o:registerObject('')
  group():addObject(o)
  return o:getID()
end

local function spawnFar(f)
  return createWalk({ shape = f.shape, pos = f.pos }, false, false)
end

local function spawnWalk(q)
  local isRoad = q.what == 'road' or q.what == 'wire' or q.renderOnly   -- render-only meshes
  -- the buildings' and sidewalks' lean meshes are a collision detail (server cdae collision_only): never drawn
  local isColOnly = (q.what == 'bldcol' or q.what == 'walkcol')
  local collide = isColOnly and 'Collision Mesh' or not isRoad
  -- Only keep shadow casting enabled on near 3D building meshes (bld) and bridge decks (deck)
  local castShadow = (q.what:sub(1, 3) == 'bld' and q.what ~= 'bldcol')
    or q.what == 'deck'
  local id = createWalk(q.walk, collide, castShadow)
  if isColOnly and id then
    local o = scenetree.findObjectById(id)
    if o then
      if o.setRenderEnabled then o:setRenderEnabled(false)
      elseif o.setField then o:setField('isRenderEnabled', 0, 'false') end
    end
  end
  return id
end

local function createChunk(chunk)
  local r = createObject('DecalRoad')
  for _, f in ipairs(M.fieldsFor(chunk)) do r:setField(f[1], 0, f[2]) end
  r.canSave = false
  r:registerObject('')
  group():addObject(r)
  for _, n in ipairs(chunk.n) do r:insertNodeNoRegen(vec3(n[1], n[2], n[3]), chunk.w, -1) end
  r:regenerate()
  return r:getId()
end

-- Server message `roads_tile` {anchorId, key, path, n}.
function M.onTile(msg, currentAnchor)
  if not M.enabled then return false end
  switchAnchor(currentAnchor)
  if msg.anchorId ~= currentAnchor then return false end
  local key = tileKey(msg.anchorId, msg.key)
  if tiles[key] then dropTile(key) end
  local text = readFile(msg.path)
  local ok, data = pcall(jsonDecode, text or '')
  if not ok or type(data) ~= 'table' or (type(data.chunks) ~= 'table' and type(data.road) ~= 'table') then
    lastError = 'unreadable road tile ' .. tostring(msg.path)
    return false
  end
  -- pooled shape files: the server's slot paths instead of the tile's own files
  local slots = {}
  for _, what in ipairs({ 'road', 'deck', 'walk', 'walkcol', 'bld', 'bld2', 'bld3', 'bld4', 'bldcol', 'wire', 'tunnel' }) do
    local sl = msgSlot(msg, what)
    if sl then
      if type(data[what]) == 'table' and type(data[what].pos) == 'table' then
        data[what] = { shape = sl.path, pos = data[what].pos }
        slots[what] = sl
      else
        slotDrop(sl)                -- nothing will load it
      end
    end
  end
  local road = type(data.road) == 'table' and data.road.shape and type(data.road.pos) == 'table' and data.road
  local chunks = type(data.chunks) == 'table' and data.chunks or {}
  tiles[key] = { anchor = msg.anchorId, objs = {}, queued = road and 0 or #chunks,
    colliders = type(data.colliders) == 'table' and data.colliders or {}, slots = slots, at = now(), colWant = {} }
  sinceChange = stopwatch.new()
  if road then                -- the tile's road mesh replaces its DecalRoads (server/roadmesh.py)
    table.insert(walkQ, 1, { key = key, tile = tiles[key], walk = road, what = 'road' })
  else
    for _, c in ipairs(chunks) do createQ[#createQ + 1] = { key = key, chunk = c } end
  end
  if type(data.props) == 'table' and type(data.props.items) == 'table' then   -- street lights, poles (trees.lua)
    require('beamngEarth/trees').onRows(msg.anchorId, 'p' .. tostring(msg.key), data.props.items, data.props.groups)
    tiles[key].props = true
  end
  if type(data.park) == 'table' and data.park[1] then      -- curbside parking spots (parking.lua)
    require('beamngEarth/parking').onRows(msg.anchorId, key, data.park)
    tiles[key].park = true
  end
  if type(data.wire) == 'table' and data.wire.shape and type(data.wire.pos) == 'table' then    -- pole wires
    walkQ[#walkQ + 1] = { key = key, tile = tiles[key], walk = data.wire, what = 'wire' }
  end
  -- sidewalks, bridge decks, buildings (split in parts under the engine's 16-bit index limit), the buildings'
  -- lean collision mesh: colliders only near the car (M.COLLIDE_M); with a bldcol the drawn buildings only render
  local tx, ty = tostring(msg.key):match('^(%-?%d+)_(%-?%d+)$')
  tiles[key].cx = tx and (tonumber(tx) + 0.5) * M.TILE_M or nil
  tiles[key].cy = ty and (tonumber(ty) + 0.5) * M.TILE_M or nil
  -- a lean collision mesh ("bldcol", "walkcol": server cdae collision_only) makes the drawn ones render-only
  local lean = type(data.bldcol) == 'table' and data.bldcol.shape ~= nil
  local leanWalk = type(data.walkcol) == 'table' and data.walkcol.shape ~= nil
  -- tunnels (server roadmesh.tunnel: tubes and portals) collide like the decks
  for _, what in ipairs({ 'walk', 'deck', 'bld', 'bld2', 'bld3', 'bld4', 'bldcol', 'walkcol', 'tunnel' }) do
    local w = data[what]
    if type(w) == 'table' and w.shape and type(w.pos) == 'table' then
      local q = { key = key, tile = tiles[key], walk = w, what = what,
        renderOnly = (lean and what:sub(1, 3) == 'bld' and what ~= 'bldcol') or (leanWalk and what == 'walk') }
      if not q.renderOnly then tiles[key].colWant[what] = true end
      if q.renderOnly or M.collideNear(tiles[key]) then walkQ[#walkQ + 1] = q else parked[#parked + 1] = q end
    end
  end
  return true
end

-- Server message `bldfar_tile` {anchorId, key, path, n, ...}: a far tile's buildings (render-only, the lite mesh);
-- shown only while the road tile of the same square has no building mesh of its own.
-- Uses inlined metadata (cx, cy, cz, shape, cdae, tx, ty, rev, pos) directly without reading msg.path when present.
function M.onFarTile(msg, currentAnchor)
  if not M.enabled then return false end
  switchAnchor(currentAnchor)
  if msg.anchorId ~= currentAnchor then return false end
  local m = msg
  local kx, ky = tostring(msg.key or ''):match('^(%-?%d+)_(%-?%d+)$')
  local tx = m.tx ~= nil and tonumber(m.tx) or tonumber(kx)
  local ty = m.ty ~= nil and tonumber(m.ty) or tonumber(ky)
  local inlinedShape = msg.shape or m.cdae
  local inlinedPos = (type(msg.pos) == 'table' and msg.pos)
    or (msg.cx ~= nil and msg.cy ~= nil and { tonumber(msg.cx), tonumber(msg.cy), tonumber(m.cz) or 0 })
    or (tx ~= nil and ty ~= nil and { tx * M.TILE_M, ty * M.TILE_M, tonumber(m.cz) or 0 })
  local data
  if inlinedShape ~= nil or type(msg.pos) == 'table' or (msg.cx ~= nil and msg.cy ~= nil) then
    data = {
      shape = inlinedShape,
      cdae = m.cdae or inlinedShape,
      pos = inlinedPos,
      cx = msg.cx,
      cy = msg.cy,
      cz = m.cz,
      tx = tx,
      ty = ty,
      rev = m.rev,
    }
  else
    local ok, decoded = pcall(jsonDecode, readFile(msg.path) or '')
    if not ok or type(decoded) ~= 'table' then lastError = 'unreadable far building tile ' .. tostring(msg.path); return false end
    data = decoded
  end
  local key = tileKey(msg.anchorId, msg.key)
  local old = far[key]
  if old and old.id then deleteQ[#deleteQ + 1] = old.id end
  if old then slotDrop(old.slot) end
  far[key] = nil
  local sl = msgSlot(msg, 'shape')
  local shape = sl and sl.path or data.shape or data.cdae
  local pos = type(data.pos) == 'table' and data.pos or inlinedPos
  if type(shape) == 'string' and shape ~= '' and type(pos) == 'table' then
    local coveredByNear = tiles[key] ~= nil and tiles[key].bld ~= nil
    far[key] = { anchor = msg.anchorId, shape = shape, pos = pos, slot = sl, tx = tx, ty = ty, rev = m.rev,
      hidden = coveredByNear, deferred = coveredByNear }
    if not coveredByNear then
      farQ[#farQ + 1] = key
    end
  else
    slotDrop(sl)
  end
  return true
end

-- Server message `bldfar_drop` {anchorId, key}.
function M.onFarDrop(msg)
  local key = tileKey(msg.anchorId, msg.key)
  local f = far[key]
  if f and f.ghost then return end
  if f and f.id then deleteQ[#deleteQ + 1] = f.id end
  if f then slotDrop(f.slot) end
  far[key] = nil
end

-- A far tile is hidden while its square's road tile shows buildings (they are the same, colliding).
-- Visibility sync is event-driven via syncFar(key) instead of scanning `far` every frame.
local function updateFar(gov)
  while farH <= #farQ and #walkQ == 0 do
    local key = farQ[farH]
    local f = far[key]
    if not f or f.id then
      farQ[farH] = nil; farH = farH + 1
      if farH > #farQ then farH = 1; for k in pairs(farQ) do farQ[k] = nil end end
    elseif tiles[key] ~= nil and tiles[key].bld ~= nil then
      f.deferred, f.hidden = true, true
      farQ[farH] = nil; farH = farH + 1
      if farH > #farQ then farH = 1; for k in pairs(farQ) do farQ[k] = nil end end
    else
      if not gov:try('roadWalk') then break end
      farQ[farH] = nil; farH = farH + 1
      if farH > #farQ then farH = 1; for k in pairs(farQ) do farQ[k] = nil end end
      local t0 = now()
      loadMaterials()
      local ok, id = pcall(spawnFar, f)
      if ok then
        f.id = id
        f.hidden = false
        slotMade(id, f.shape)
        syncFar(key)
      else
        lastError = tostring(id)
      end
      local dt = now() - t0
      gov:charge('roadWalk', dt)
      noteUnit('far', dt, f.shape)
    end
  end
end

-- Server message `roads_drop` {anchorId, key}. (A ghost goes when covered: releaseGhosts.)
function M.onDrop(msg)
  local t = tiles[tileKey(msg.anchorId, msg.key)]
  if t and t.ghost then return end
  dropTile(tileKey(msg.anchorId, msg.key))
  sinceChange = stopwatch.new()
end

-- Origin rebase (core.lua finishRebase): the shown tiles move into the new frame (p_new = R*p + t: their TSStatics
-- turned by R, as farfield.transform) and stay as "ghosts" until the new anchor's tiles over them are made
-- (ghostsCovered). Before (2026-10-17, forced rebase at Fells Point) every building, road mesh and sidewalk went at
-- once and came back over ~26 s as the server re-sent the tiles. DecalRoads (no rotation) and anything still queued go
-- now: their positions are the old frame's. A ghost's colliders are not made again (updateCollideRange).
M.GHOST_MAX_S = 120         -- ghosts left after this long go anyway
M.GHOST_FAR_M = 960         -- ... and once farther from the player than this (the server's roads DROP_M)
M.GHOST_FAR_WAIT_S = 2      -- ... from this long after the rebase on
local ghostSince = nil      -- stopwatch since the last rebase that left ghosts

local WHATS = { 'walk', 'road', 'deck', 'bld', 'bld2', 'bld3', 'bld4', 'bldcol', 'walkcol', 'wire', 'tunnel' }

function M.transform(xf, geodesy)
  local bx, by, bz, bw = xf:beamngQuat()
  local function moveObj(id, q)
    local o = id and scenetree.findObjectById(id)
    if not o then return end
    local p = o:getPosition()
    local x, y, z = xf:apply(p.x, p.y, p.z)
    o:setPosRot(x, y, z, q[1], q[2], q[3], q[4])
  end
  local function newQ(q)
    local x, y, z, w = geodesy.quatMul(q[1], q[2], q[3], q[4], bx, by, bz, bw)
    return { x, y, z, w }
  end
  local any = false
  for key, t in pairs(tiles) do
    t.q = newQ(t.q or { 0, 0, 0, 1 })
    for _, what in ipairs(WHATS) do moveObj(t[what], t.q) end
    for _, id in ipairs(t.objs) do deleteQ[#deleteQ + 1] = id end
    t.objs, t.queued, t.cq = {}, 0, nil
    if t.cx then t.cx, t.cy = xf:apply(t.cx, t.cy, 0) end
    t.ghost = true
    any = true
  end
  for _, f in pairs(far) do
    if f.id then
      f.q = newQ(f.q or { 0, 0, 0, 1 })
      moveObj(f.id, f.q)
      f.ghost = true
      any = true
    end
  end
  if lastReloadX and lastReloadY then
    lastReloadX, lastReloadY = xf:apply(lastReloadX, lastReloadY, 0)
  end
  local function keepNew(list)
    local keep = {}
    for _, q in ipairs(list) do if tiles[q.key] and not tiles[q.key].ghost then keep[#keep + 1] = q end end
    return keep
  end
  walkQ, createQ, parked = keepNew(walkQ), keepNew(createQ), keepNew(parked)
  local fq = {}
  for i = farH, #farQ do
    local k = farQ[i]
    if k and far[k] and not far[k].ghost then fq[#fq + 1] = k end
  end
  farQ, farH = fq, 1
  if any then ghostSince = stopwatch.new(); sinceChange = stopwatch.new(); collisionDirty = true end
end

-- Is the square of side M.TILE_M around (cx, cy) covered by made tiles of the current anchor (every one of the new
-- grid's squares it overlaps has a tile with nothing queued)?
-- done: the new grid's squares whose tile is made (madeSquares).
local function madeSquares(aid, busy)
  local n, done = M.TILE_M, {}
  for key, t in pairs(tiles) do
    if t.anchor == aid and not t.ghost and t.cx then
      done[math.floor(t.cx / n) .. '_' .. math.floor(t.cy / n)] = t.queued <= 0 and not busy[key]
    end
  end
  return done
end
-- True once the road tile of anchor aid under (x, y) is made, no other anchor's road (a rebase's ghost too) is near
-- it, and the collision is rebuilt after (core.floorCheck: a ray found the old session's tunnel floor at a teleport's
-- spot, the car went down onto it and fell when that mesh went, 2026-10-17). Must come after madeSquares.
function M.settledFor(aid, x, y)
  if anchorId ~= aid or #deleteQ > 0 or collisionDirty then return false end
  local n = M.TILE_M
  for _, t in pairs(tiles) do
    if t.anchor ~= aid or t.ghost then
      if not t.cx or (math.abs(t.cx - x) < 1.5 * n and math.abs(t.cy - y) < 1.5 * n) then return false end
    end
  end
  local busy = {}
  for _, q in ipairs(walkQ) do busy[q.key] = true end
  return madeSquares(aid, busy)[math.floor(x / n) .. '_' .. math.floor(y / n)] == true
end

local function covered(done, cx, cy)
  local h, n = M.TILE_M / 2, M.TILE_M
  for gx = math.floor((cx - h) / n), math.floor((cx + h - 1e-6) / n) do
    for gy = math.floor((cy - h) / n), math.floor((cy + h - 1e-6) / n) do
      if not done[gx .. '_' .. gy] then return false end
    end
  end
  return true
end

local function releaseGhosts(player)
  if not ghostSince then return end
  local busyKeys = {}
  for _, q in ipairs(walkQ) do busyKeys[q.key] = true end
  local all = ghostSince() > M.GHOST_MAX_S * 1000
  local left = false
  local done = madeSquares(anchorId, busyKeys)
  local p = player             -- core's player (the camera's focus: a test drive's vehicle is not the player's car)
  for key, t in pairs(tiles) do
    if t.ghost then
      -- (not in the rebase's first GHOST_FAR_WAIT_S: in its frame the player is still read in the old frame, every
      -- ghost looked kilometres away and all went: 46 road tiles -> 0 at each rebase, 2026-10-17)
      local far = p and t.cx and ghostSince() > M.GHOST_FAR_WAIT_S * 1000
        and math.max(math.abs(p.x - t.cx), math.abs(p.y - t.cy)) - M.TILE_M / 2 > M.GHOST_FAR_M
      if all or far or (t.cx and covered(done, t.cx, t.cy)) then
        dropTile(key)
        collisionDirty = true
      else left = true end
    end
  end
  for key, f in pairs(far) do
    if f.ghost then
      if all then
        if f.id then deleteQ[#deleteQ + 1] = f.id end
        slotDrop(f.slot)
        far[key] = nil
      else left = true end
    end
  end
  if not left then ghostSince = nil end
end

local viewAt = nil          -- core.viewAway() (a free camera or the map away from the car), else nil
local prewarmId, prewarmPath, prewarmWant = nil, nil, nil
local prewarmNow

-- Once per frame (core onUpdate): deletions, then creations, within the governor's budget.
function M.update(gov, currentAnchor, player, view)
  viewAt = view
  if prewarmWant then prewarmNow()
  elseif prewarmId and not scenetree.findObjectById(prewarmId) then   -- (the level loaded again: gone with it)
    prewarmId, prewarmWant = nil, prewarmPath
  end
  if currentAnchor then switchAnchor(currentAnchor) end
  releaseGhosts(player)
  while #deleteQ > 0 and gov:try('roadDelete') do
    local t0 = now()
    local did = table.remove(deleteQ)
    local o = scenetree.findObjectById(did)
    if o then
      if isCollider(o) then
        if M.nearPlayer(o:getPosition(), M.DELETE_NEAR_M) then collisionDirty = true else deletesDirty = true end
      end
      o:delete()
    end
    slotObjGone(did)
    local dt = now() - t0
    gov:charge('roadDelete', dt)
    noteUnit('delete', dt)
    sinceChange = stopwatch.new()
  end
  while #createQ > 0 and gov:try('roadChunk') do
    local q = table.remove(createQ, 1)
    local t = tiles[q.key]
    if t then
      local t0 = now()
      loadMaterials()
      local ok, id = pcall(createChunk, q.chunk)
      if ok then t.objs[#t.objs + 1] = id else lastError = tostring(id) end
      t.queued = t.queued - 1
      local dt = now() - t0
      gov:charge('roadChunk', dt)
      noteUnit('chunk', dt)
      sinceChange = stopwatch.new()
    end
  end
  releaseHeld()
  updateCollideRange()
  while #walkQ > 0 do
    local idx = M.nextWalk and M.nextWalk(walkQ) or 1
    local q = walkQ[idx]
    local isUrgentRoad = q.what == 'road' or q.what == 'deck' or q.what == 'tunnel'
    local urgentAllow = isUrgentRoad and gov.frameUnits == 0 and not gov.blocked and not gov.heavyPending
      and gov.heavyFrame ~= gov.frame and (gov.b or 0) > 0
    if not (gov:try('roadWalk') or urgentAllow) then break end
    table.remove(walkQ, idx)
    local t = tiles[q.key]
    if t and q.tile and q.tile ~= t then t = nil end     -- a replaced copy of the tile
    -- (only a colliding mesh waits: a render-only one (q.renderOnly: the drawn sidewalks and buildings beside their lean
    -- collision meshes) cannot catch the car, and holding it hid a whole tile's sidewalks while the car stood in their
    -- box: a roundabout's island band was never drawn, 2026-10-17 Harbor Point)
    if t and q.what ~= 'road' and not q.renderOnly and playerIn(t.colliders[q.what], q.what) then
      held[#held + 1] = q           -- the car is on it: made once the car is clear (playerIn)
      t = nil
    end
    if t then
      local t0 = now()
      loadMaterials()
      local isRoad = q.what == 'road' or q.what == 'wire' or q.renderOnly   -- render-only meshes
      local ok, id = pcall(spawnWalk, q)
      if ok then slotMade(id, q.walk.shape) end
      if ok then
        if not isRoad then t.cq = t.cq or {}; t.cq[q.what] = q end
        if isRoad then t[q.what] = id else
          t[q.what] = id; collisionDirty = true; newColliders[#newColliders + 1] = { id, q }
          t.colAt = t.colAt or {}; t.colAt[q.what] = now()
        end
        if q.what == 'bld' then syncFar(q.key) end
      else lastError = tostring(id) end
      local dt = now() - t0
      gov:charge('roadWalk', dt)
      noteUnit(q.what, dt, q.walk.shape)
      sinceChange = stopwatch.new()
    end
  end
  updateFar(gov)                -- far buildings: only with the near meshes all made (they share the budget)
  if swapQuiet then
    swapQuiet.frames = swapQuiet.frames - 1
    if swapQuiet.frames <= 0 and swapQuiet.since() >= M.SWAP_QUIET_MS then swapQuiet = nil end
  end
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  local v = veh and veh.getVelocity and veh:getVelocity()
  local p = veh and veh.getPosition and veh:getPosition()
  local px = (p and p.x) or (player and player.x)
  local py = (p and p.y) or (player and player.y)
  local speed = (v and (v.length and v:length() or math.sqrt(v.x * v.x + v.y * v.y))) or (player and player.speed) or 0
  local collideR = M.collideRadius(speed)
  lastRadius = collideR
  local traveledSq = 0
  if px and py then
    if lastReloadX and lastReloadY then
      local dx, dy = px - lastReloadX, py - lastReloadY
      traveledSq = dx * dx + dy * dy
    else
      lastReloadX, lastReloadY = px, py
    end
  end
  local timeSinceLastReload = sinceReload() / 1000
  local colDirty = (collisionDirty and timeSinceLastReload >= M.RELOAD_EVERY_S)
      or (deletesDirty and timeSinceLastReload >= M.RELOAD_DELETES_S)
  local horizonM = M.RELOAD_HORIZON_RATIO * collideR
  local traveledHorizon = collisionDirty and (traveledSq > horizonM * horizonM)
  local urgentStructure = collisionDirty and (holdActive or hasUrgentStructure(px, py, math.max(M.DECK_URGENT_M, M.DECK_URGENT_S * speed)))
  local shouldReload = not swapQuiet and (traveledHorizon
      or urgentStructure
      or ((speed <= 3.0 or M.RELOAD_EVERY_S <= 0) and colDirty)
      or (timeSinceLastReload > (M.RELOAD_DRIVE_MAX_S or 15.0) and colDirty))
  if shouldReload then
    local t0 = now()
    noteNewColliders()
    be:reloadCollision()
    local dt = now() - t0
    noteUnit('reload', dt)
    lastReloadAt = now()
    if px and py then lastReloadX, lastReloadY = px, py end
    collisionDirty, deletesDirty, sinceReload = false, false, stopwatch.new()
    if gov then
      if gov.spend then
        gov:spend(gov.b or 0)
      elseif gov.chargeMotion then
        gov:chargeMotion(math.max(gov.b or 0, dt))
      end
      gov.tokens = 0
      gov.blocked = true
      gov.quietAfter = 2
      gov.quiet = math.max(gov.quiet or 0, 2)
      if gov.chargeEngine then gov:chargeEngine(dt) end
    end
  end
end

-- Any unflushed collidable deck (bridge) or tunnel within r m (horizontally) of (px, py)?
hasUrgentStructure = function(px, py, r)
  if #newColliders == 0 or not px or not py then return false end
  local rSq = r * r
  for i = 1, #newColliders do
    local q = newColliders[i][2]
    if (q.what == 'deck' or q.what == 'tunnel') and not q.renderOnly then
      local t = tiles[q.key]
      if t and t[q.what] then
        local boxes = t.colliders and t.colliders[q.what]
        if type(boxes) == 'table' and #boxes > 0 then
          for j = 1, #boxes do
            local b = boxes[j]
            local dx = math.max(b[1] - px, 0, px - b[4])
            local dy = math.max(b[2] - py, 0, py - b[5])
            if dx * dx + dy * dy <= rSq then return true end
          end
        else
          local pos = q.walk and q.walk.pos
          if pos and pos[1] and pos[2] then
            local dx, dy = px - pos[1], py - pos[2]
            if dx * dx + dy * dy <= rSq then return true end
          end
          if t.cx and t.cy then
            local dx, dy = px - t.cx, py - t.cy
            if dx * dx + dy * dy <= rSq then return true end
          end
        end
      end
    end
  end
  return false
end

-- A colliding mesh (sidewalks, bridge decks) never turns solid around the player's car (2026-10-13: the
-- intermittent NaN drives; a late curb or deck appearing around a vehicle). The server's road tile lists
-- boxes around its pieces (roads.collider_boxes); a mesh the car is in is not made (held) or, made since
-- the last reloadCollision, is removed again before it, and made once the car is clear.
M.PLAYER_R = 3.0    -- m around the player's position (half a car's length, and some)
M.PLAYER_DZ = 2.5   -- m above / below
M.DECK_BOX_BELOW_M = 2.2   -- server/roads.py collider_boxes: the box starts this far below the deck's surface
M.DECK_ON_M = 0.2          -- at or above (surface - this): on or above the deck, not under it

-- Is world position p within r m (horizontally) of the player's car? (no car: yes, to be safe)
function M.nearPlayer(p, r)
  if not p then return true end
  return nearPlayerXY(p.x, p.y, r)
end

playerIn = function(boxes, what)
  -- (a drop hold: the car does not move until its ground and colliders are in, then it is set on top of them)
  if holdActive or type(boxes) ~= 'table' or #boxes == 0 then return false end
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if not veh then return false end
  local p = veh:getPosition()
  local r, dz = M.PLAYER_R, M.PLAYER_DZ
  for _, b in ipairs(boxes) do
    if p.x >= b[1] - r and p.x <= b[4] + r and p.y >= b[2] - r and p.y <= b[5] + r then
      -- A bridge deck is a thin slab with railings on its edges; a car at or above its surface (driving onto it from
      -- the approach, where the terrain is already at the deck's height) needs the deck under its wheels, and only a
      -- car below the surface (under the bridge, or inside the slab's 0.45 m thickness) would be caught by it (in-game
      -- 2026-10-20: a late deck was held while the car was in its box on the approach and the car fell through).
      if what == 'deck' then
        if p.z >= b[3] - dz and p.z < b[3] + M.DECK_BOX_BELOW_M - M.DECK_ON_M then return true end
      elseif p.z >= b[3] - dz and p.z <= b[6] + dz then
        return true
      end
    end
  end
  return false
end

releaseHeld = function()
  if #held == 0 then return end
  local keep = {}
  for _, q in ipairs(held) do
    local t = tiles[q.key]
    if t then
      if playerIn(t.colliders[q.what], q.what) then keep[#keep + 1] = q else walkQ[#walkQ + 1] = q end
    end
  end
  held = keep
end

noteNewColliders = function()
  local removed = 0
  for _, c in ipairs(newColliders) do
    local q = c[2]
    local t = tiles[q.key]
    if t and t[q.what] == c[1] and playerIn(t.colliders[q.what], q.what) then
      local o = scenetree.findObjectById(c[1])
      if o then o:delete() end
      t[q.what] = nil
      held[#held + 1] = q
      removed = removed + 1
      M.solidAround = (M.solidAround or 0) + 1
      log('I', 'beamngEarth', 'roads: a ' .. tostring(q.what) .. ' mesh around the player held until it is clear')
    end
  end
  newColliders = {}
  return removed
end
M._noteNewColliders = noteNewColliders

-- The player's position and motion now: p (vec3) and its unit horizontal direction at FAST_MS or more, else nil.
local function playerNow()
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if not veh then return nil end
  return veh:getPosition(), lastDir
end

-- Which walkQ entry to make next (index): the road meshes and colliders before the render-only meshes (drawn
-- buildings, wires), each by the distance to its tile's centre (behind a fast car counting three times): the tiles
-- the car reaches first (the user, 2026-10-19: high speeds, the road texture struggling to keep up). A render-only
-- mesh by its distance to the camera instead when that is nearer (viewAt: what is drawn follows the camera).
function M.nextWalk(q)
  if #q <= 1 then return 1 end
  local p, dir = playerNow()
  if not p then return 1 end
  local best, bestKey = 1, math.huge
  for i, e in ipairs(q) do
    local t = tiles[e.key]
    local k = 1e9
    if t and t.cx then
      local dx, dy = t.cx - p.x, t.cy - p.y
      k = math.sqrt(dx * dx + dy * dy)
      if dir then         -- fast: by the way along the path ahead, sideways counting SIDE_PENALTY times (the server's
        local along = dx * dir.x + dy * dir.y               -- roads.path_key); behind the car after all of those
        local side = math.abs(dx * dir.y - dy * dir.x)
        if along < -M.TILE_M then k = k * 3 + M.LEAD_MAX_M
        else k = math.max(0, along) + M.SIDE_PENALTY * math.max(0, side - M.TILE_M / 2) end
      end
      if viewAt and (e.renderOnly or e.what == 'wire') then
        local vx, vy = t.cx - viewAt.x, t.cy - viewAt.y
        k = math.min(k, math.sqrt(vx * vx + vy * vy))
      end
    end
    if e.renderOnly or e.what == 'wire' then k = k + 1e6 end
    if k < bestKey then best, bestKey = i, k end
  end
  return best
end

-- Is a collider made since the last rebuild one the car reaches soon (its tile within URGENT_S of driving, 150 m at
-- least)? Then the rebuild runs now, not after RELOAD_EVERY_S.
function M.urgentColliders()
  if #newColliders == 0 then return false end
  local p = playerNow()
  if not p then return true end
  local reach = math.max(150, lastSpeed * M.URGENT_S) + M.TILE_M * 0.71
  for _, c in ipairs(newColliders) do
    local t = tiles[c[2].key]
    if t and t.cx then
      local dx, dy = t.cx - p.x, t.cy - p.y
      if dx * dx + dy * dy <= reach * reach then return true end
    end
  end
  return false
end

-- core swapped the terrain block's .ter this frame (terrain.swap): no collision rebuild until it has settled.
function M.terrainSwapped() swapQuiet = { frames = M.SWAP_QUIET_FRAMES, since = stopwatch.new() } end
function M.swapQuiet() return swapQuiet ~= nil end

-- core's drop hold on / off: while it holds, colliders around the frozen car are made (not held for it) and rebuilt
-- at once, so the hold can wait for them (M.readyAround) and set the car on top.
function M.setHold(on) holdActive = on and true or false end

-- Are the road tiles of anchor aid within r of (x, y) all in, their colliders (decks, tunnels, sidewalks, buildings)
-- made and solid (made before the last collision rebuild)? Only what collides counts: the drawn road surface, building
-- facades and wires come when they come (in Manhattan they kept a drop hold 20 s). Roads off: yes.
function M.readyAround(aid, x, y, r)
  if not M.enabled then return true end
  if anchorId ~= aid then return false end
  local busy = {}
  for _, q in ipairs(walkQ) do
    if not q.renderOnly and q.what ~= 'road' and q.what ~= 'wire' then busy[q.key] = true end
  end
  local n = M.TILE_M
  for gx = math.floor((x - r) / n), math.floor((x + r) / n) do
    for gy = math.floor((y - r) / n), math.floor((y + r) / n) do
      local key = tileKey(aid, gx .. '_' .. gy)
      local t = tiles[key]
      if not t or t.ghost or busy[key] then return false end
      for what in pairs(t.colWant or {}) do
        if not t[what] or ((t.colAt and t.colAt[what]) or math.huge) >= lastReloadAt then return false end
      end
    end
  end
  return true
end

-- Something else made colliding objects near the player (trees.lua's Forest items): rebuild collision at the
-- next throttled slot.
function M.requestCollision() collisionDirty = true end

function M.hasWork() return #createQ > 0 or #deleteQ > 0 or #walkQ > 0 or farH <= #farQ or collisionDirty end

-- Server hello.prewarm: a tiny shape listing every material the server's shapes use, loaded once when it connects
-- (server/prewarm.py): a material's first shape in a session set it up (a tunnel's first tile 65 ms, later ones ~2 ms),
-- now while the level starts. Kept far below the map (one per path; nil: an older server, nothing).
prewarmNow = function()
  local path = prewarmWant
  -- (the hello comes at the main menu, before the level's objects and with the roads off outside our level)
  if not path or not M.enabled or not scenetree or not scenetree.MissionGroup then return end
  prewarmWant = nil
  if path == prewarmPath and prewarmId and scenetree.findObjectById(prewarmId) then return end   -- (made already)
  if prewarmId then
    local o = scenetree.findObjectById(prewarmId)
    if o then o:delete() end
    prewarmId = nil
  end
  loadMaterials()
  local t = stopwatch.new()
  local ok, id = pcall(createWalk, { shape = path, pos = { 0, 0, -5000 } }, false)
  if not ok then lastError = tostring(id); return end
  local po = id and scenetree.findObjectById(id)
  if po and po.setHidden then po:setHidden(true) end
  prewarmId, prewarmPath = id, path
  local dt = t()
  noteUnit('prewarm', dt, path)
  log('I', 'beamngEarth', string.format('roads: materials prewarmed in %.0f ms (%s)', dt, path))
end

function M.prewarm(path)
  if type(path) ~= 'string' then return false end
  prewarmWant = path
  prewarmNow()
  return true
end

-- Somebody else just ran be:reloadCollision() (the terrain commit): the sidewalks made so far collide.
function M.collisionRebuilt()
  -- that rebuild already made them solid: one held now leaves the physics at the next one
  local removed = noteNewColliders()
  lastReloadAt = now()
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  local p = veh and veh.getPosition and veh:getPosition()
  if p then lastReloadX, lastReloadY = p.x, p.y end
  collisionDirty, deletesDirty, sinceReload = removed > 0, false, stopwatch.new()
end

-- Every road removed now (setting off, mod unload).
function M.clear()
  for key in pairs(tiles) do dropTile(key) end
  for _, f in pairs(far) do
    if f.id then deleteQ[#deleteQ + 1] = f.id end
    slotDrop(f.slot)
  end
  far, farQ, farH = {}, {}, 1
  createQ, held, newColliders = {}, {}, {}
  for _, id in ipairs(deleteQ) do
    local o = scenetree.findObjectById(id)
    if o then
      if isCollider(o) then collisionDirty = true end
      o:delete()
    end
    slotObjGone(id)
  end
  deleteQ = {}
  walkQ, parked = {}, {}
  lastReloadX, lastReloadY = nil, nil
  if collisionDirty then be:reloadCollision(); collisionDirty = false end
end

-- The level ended: its objects are gone with it.
function M.reset()
  -- (the level took the objects: every slot goes)
  for _, t in pairs(tiles) do for _, sl in pairs(t.slots or {}) do slotsGone[#slotsGone + 1] = { sl[1], sl[2], sl[3] } end end
  for _, f in pairs(far) do if f.slot then slotsGone[#slotsGone + 1] = { f.slot[1], f.slot[2], f.slot[3] } end end
  for n, d in pairs(slotDropped) do slotsGone[#slotsGone + 1] = { n, d[1], d[2] } end
  objSlot, slotLive, slotDropped = {}, {}, {}
  tiles, createQ, deleteQ, walkQ, anchorId, loadedMaterials = {}, {}, {}, {}, nil, false
  far, farQ, farH = {}, {}, 1
  lastReloadX, lastReloadY = nil, nil
  collisionDirty, deletesDirty = false, false
  unitStats, unitLog = {}, {}
end

-- On / off (the settings page, through core.setRoadsEnabled, which asks the server for the tiles
-- again when switched back on). Returns true if it was off and is on now.
function M.setEnabled(on)
  local was = M.enabled
  M.enabled = on and true or false
  if not M.enabled then M.clear() end
  return M.enabled and not was
end

-- The state of the road tiles along a line (tools: how far ahead of a fast car its tiles are ready). From (x, y)
-- along the unit direction (hx, hy) to dist m, every 32 m: { d, key, arrived (ms ago or false), road (its road mesh
-- made), col ('none': no colliders, 'parked' / 'made' / 'solid': made before the last collision rebuild) }.
function M.ahead(x, y, hx, hy, dist)
  local out, seen, n, tnow = {}, {}, M.TILE_M, now()
  for d = 0, dist, 32 do
    local px, py = x + hx * d, y + hy * d
    local sq = math.floor(px / n) .. '_' .. math.floor(py / n)
    if not seen[sq] then
      seen[sq] = true
      local t = tiles[tileKey(anchorId, sq)]
      local e = { d = d, key = sq, arrived = t and math.floor(tnow - (t.at or tnow)) or false }
      if t then
        e.road = t.road ~= nil or (t.queued or 0) <= 0
        e.bld = t.bld ~= nil
        e.walk = t.walk ~= nil
        local want, made, solid = 0, 0, 0
        for what in pairs(t.colWant or {}) do
          want = want + 1
          if t[what] then
            made = made + 1
            if (t.colAt and t.colAt[what] or tnow) < lastReloadAt then solid = solid + 1 end
          end
        end
        e.col = want == 0 and 'none' or (solid == want and 'solid') or (made == want and 'made') or 'parked'
      end
      out[#out + 1] = e
    end
  end
  return out
end

function M.status()
  local nt, no, nw, nr, nd, nb = 0, 0, 0, 0, 0, 0
  for _, t in pairs(tiles) do
    nt = nt + 1; no = no + #t.objs
    if t.walk then nw = nw + 1 end
    if t.road then nr = nr + 1 end
    if t.deck then nd = (nd or 0) + 1 end
    if t.bld then nb = nb + 1 end
  end
  local nf = 0
  for _, f in pairs(far) do if f.id and not f.hidden then nf = nf + 1 end end
  return { enabled = M.enabled, tiles = nt, roads = no, queued = #createQ, deleting = #deleteQ, anchor = anchorId,
    walks = nw, roadMeshes = nr, decks = nd, buildings = nb, farBuildings = nf, walksQueued = #walkQ, solidAround = M.solidAround or 0,
    quietS = sinceChange() / 1000, error = lastError, units = unitStats, unitLog = unitLog }
end

local okSweep, swept = pcall(M.sweep)
if okSweep and swept > 0 then log('W', 'beamngEarth', 'roads: deleted ' .. swept .. ' road objects left by a previous Lua state') end

return M
