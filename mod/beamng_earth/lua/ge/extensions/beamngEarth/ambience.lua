-- Forest wind and room tone (2026-10-26, the user: "Start with 1"), and the rivers' sound: the stock maps' ambience
-- (lua/ge/extensions/core/audioForest.lua: one wind emitter at the trees round the listener, louder and denser with
-- more and bigger trees, and a room tone everywhere) fed from the vegetation tiles' sound nodes (server
-- vegetation._ambience: a tile's trees and bushes in ~21 m squares, its ambience set by climate) instead of a
-- level's Forest: ours are many small ones made and dropped as the player moves, the stock module reads one at load.
local M = {}

local kdTree = nil       -- the stock kdtreepoint3d (required at the first cloud: not there outside the game)
local liveaudio = require('beamngEarth/liveaudio') -- the Weather page's Weather sounds: Live (the server plays them)

M.RADIUS_M = 600      -- the node cloud: the nodes this close to the listener (the stock emitter hears 150 m) ...
M.MOVE_M = 300        -- ... made again once the listener is this far from where it was made, or a tile near changed
M.GAP_S = 1           -- at most this often
M.VOTE_M = 1000       -- tiles this close choose the ambience set, by their sound items
M.SWITCH = 1.5        -- another set takes over once it has this many times the current one's items
M.PITCH_FULL = 20     -- items of a node at full density (pitch 1): the stock nodes count the items within 20 m (1257 m2)
                      -- and East Coast USA's 95th percentile is 55; ours count a 455 m2 square, 0.36 of that: ~20.
                      -- (Recorded 2026-10-26: East Coast USA's dense forest gives volume 0.3, pitch 0.6.)
M.GHOST_MAX_S = 120   -- a rebase's previous tiles stay at most this long (trees.lua GHOST_MAX_S)
local TILE = 256

-- [set][variant + 1] = { wind, room tone }: the server's sets (native ambience.rs: 0 temperate, 1 Mediterranean,
-- 2 desert, 3 tropical) as the stock maps' (East Coast USA's _01, West Coast USA's _02; Italy's; Utah's _01,
-- Johnson Valley's _02; Jungle Rock Island's)
local W, R = 'event:>Ambient>Wind>Wind_', 'event:>Ambient>Roomtones>Roomtone_'
M.SETS = {
  [0] = { { W .. 'ConiferousDeciduous_01', R .. 'ConiferousDeciduous_01' },
          { W .. 'ConiferousDeciduous_02', R .. 'ConiferousDeciduous_02' } },
  [1] = { { W .. 'Mediterranean_01', R .. 'Mediterranean_01' }, { W .. 'Mediterranean_02', R .. 'Mediterranean_02' } },
  [2] = { { W .. 'Desert_01', R .. 'Desert_01' }, { W .. 'Desert_02', R .. 'Desert_02' } },
  [3] = { { W .. 'Tropical', R .. 'Tropical' } },
}

local tiles = {}        -- "anchorId:key" -> { nodes (flat x, y, z, depth, size, count rows), set, weight, cx, cy, gx, gy, ghost }
local dirty = false
local builtAt = nil     -- { x, y } where the cloud was made
local lastBuild = -1e9
local current = nil     -- { wind, roomtone } playing
local roomtone = nil    -- its SFXEmitter (ours: it follows the listener; the stock one stays at the level's origin)
local listener = vec3()
local moveSpaces         -- (the tunnels' and bridges' sound spaces, below)
M.nodes, M.builds, M.buildMs = 0, 0, 0

-- Pure: the events of a tile's biome { set, variant } (nil for an unknown set).
function M.events(biome)
  local s = M.SETS[tonumber(biome and biome[1]) or -1]
  if not s then return nil end
  return s[(tonumber(biome[2]) or 0) + 1] or s[1]
end

-- A vegetation tile's ambience (core.lua, after trees.lua read its file: data.audio, data.biome).
function M.onTile(anchorId, key, data)
  if type(data) ~= 'table' or type(data.biome) ~= 'table' then return end
  local nodes = type(data.audio) == 'table' and data.audio or {}
  local w = 0
  for i = 6, #nodes, 6 do w = w + (nodes[i] or 0) end
  if type(data.geo) == 'table' then
    -- a geotile (trees.lua placed its nodes into the anchor's world): no anchor's, moved at a rebase, never a ghost
    local cx, cy = tonumber(data.geo.cx) or 0, tonumber(data.geo.cy) or 0
    tiles['g:' .. tostring(key)] = { geo = true, nodes = nodes, biome = data.biome, weight = w, cx = cx, cy = cy,
      gx = math.floor(cx / TILE), gy = math.floor(cy / TILE) }
    dirty = true
    return
  end
  local tx, ty = tostring(key):match('(%-?%d+)_(%-?%d+)$')
  tx, ty = tonumber(tx) or 0, tonumber(ty) or 0
  tiles[anchorId .. ':' .. tostring(key)] = { anchor = anchorId, nodes = nodes, biome = data.biome, weight = w,
    cx = tx * TILE + TILE / 2, cy = ty * TILE + TILE / 2, gx = tx, gy = ty }
  dirty = true
end

function M.onDrop(anchorId, key)
  local k = (tostring(key):match('^g%d+_') and 'g' or anchorId) .. ':' .. tostring(key)
  if tiles[k] then
    tiles[k] = nil
    dirty = true
  end
end

-- Origin rebase (core.lua finishRebase): the tiles' nodes move with the world (p_new = R*p + t) and stay as ghosts
-- until the new anchor's tiles cover them (as trees.lua's).
function M.transform(xf)
  for _, t in pairs(tiles) do
    local a = t.nodes
    for i = 1, #a, 6 do
      a[i], a[i + 1], a[i + 2] = xf:apply(a[i], a[i + 1], a[i + 2])
    end
    t.cx, t.cy = xf:apply(t.cx, t.cy, 0)
    if t.geo then
      t.gx, t.gy = math.floor(t.cx / TILE), math.floor(t.cy / TILE)
    else
      t.ghost = t.ghost or os.clockhp()
    end
  end
  dirty = true
  moveSpaces(xf)            -- (the road tiles' sound spaces: their tiles stay as ghosts the same way, roads.lua)
end

local function covered()
  local c = {}
  for _, t in pairs(tiles) do
    if not t.ghost then c[t.gx .. '_' .. t.gy] = true end
  end
  return c
end

local function heard(t, cov)
  return not (t.ghost and cov[math.floor(t.cx / TILE) .. '_' .. math.floor(t.cy / TILE)])
end

-- Pure: the ambience set the tiles near (x, y) choose: { set, variant } with the most sound items (every tile
-- counts a little: a place without trees still has its set), kept unless another has M.SWITCH times its items.
function M.vote(list, x, y, keep)
  local by = {}
  for _, t in ipairs(list) do
    if (t.cx - x) ^ 2 + (t.cy - y) ^ 2 <= M.VOTE_M ^ 2 then      -- (a disc of tile centres, 2026-10-28)
      local k = tostring(t.biome[1]) .. ':' .. tostring(t.biome[2] or 0)
      local e = by[k] or { biome = t.biome, w = 0 }
      e.w = e.w + t.weight + 0.001
      by[k] = e
    end
  end
  local best = nil
  for _, e in pairs(by) do
    if not best or e.w > best.w then best = e end
  end
  if not best then return keep end
  if keep then
    local k = by[tostring(keep[1]) .. ':' .. tostring(keep[2] or 0)]
    if k and best.w < k.w * M.SWITCH then return keep end
  end
  return best.biome
end

local function forest()
  return extensions and extensions.core_audioForest
end

-- The room tone emitter of event ev (none for nil): a new one for another event (an SFXEmitter given another track
-- kept the old one playing, and stop() no longer stopped it: recorded 2026-10-26).
local function setRoomtone(ev)
  if roomtone and simObjectExists(roomtone) then roomtone:delete() end
  roomtone = nil
  if not ev then return end
  local e = Sim.upcast(createObject('SFXEmitter'))
  if not e then return end
  e:setTransform(MatrixF(true))
  e:setField('track', 0, ev)
  e.canSave = false
  e:registerObject('bngEarthRoomtone_' .. Engine.generateUUID())
  e:play()
  roomtone = e
end

local biome = nil
local function build(x, y)
  local t0 = os.clockhp()
  local cov = covered()
  local list = {}
  for _, t in pairs(tiles) do
    if heard(t, cov) then list[#list + 1] = t end
  end
  local r = M.RADIUS_M
  local r2, reach = r * r, r + TILE * 0.71
  local X, Y, Z, D, S, C = {}, {}, {}, {}, {}, {}
  local n = 0
  for _, t in ipairs(list) do
    if math.abs(t.cx - x) <= reach and math.abs(t.cy - y) <= reach then
      local a = t.nodes
      for i = 1, #a, 6 do
        local dx, dy = a[i] - x, a[i + 1] - y
        if dx * dx + dy * dy <= r2 then
          n = n + 1
          X[n], Y[n], Z[n], D[n], S[n], C[n] = a[i], a[i + 1], a[i + 2], a[i + 3], a[i + 4], a[i + 5]
        end
      end
    end
  end
  biome = M.vote(list, x, y, biome)
  local ev = M.events(biome)
  local af = forest()
  if af then
    local kd = nil
    if n > 0 then
      kdTree = kdTree or require('kdtreepoint3d')
      kd = kdTree.new(n)
      for i = 1, n do kd:preLoad(i, X[i], Y[i], Z[i] + D[i]) end
      kd:build()
    end
    M.lastCloud = { nodeCount = n, nodeX = X, nodeY = Y, nodeZ = Z, nodeDepth = D, nodeAvgNormVol = S,
      nodeSourceCount = C, pitchDensityInv = 1 / M.PITCH_FULL, kdNodes = kd }
    af.setNodeCloud(M.lastCloud)
    if ev and (not current or current[1] ~= ev[1]) then
      af.setEnabled(false)                  -- (a new emitter for it: setRoomtone)
      af.setMainEvent(ev[1])
    end
    af.setEnabled(n > 0 and ev ~= nil)
  end
  if not current or not ev or current[2] ~= (ev and ev[2]) then setRoomtone(ev and ev[2]) end
  current = ev
  M.nodes, M.builds, M.buildMs = n, M.builds + 1, (os.clockhp() - t0) * 1000
end

-- Rivers: the stock river sound (lua/ge/extensions/core/audioRibbon.lua: of a level's "ribbons", strips along its
-- rivers, the closest two play the water's sound at the listener's nearest points, louder nearer) fed from the
-- server's rivers (sea.lua: centrelines with widths, server riverlines.py). A ribbon's nodes are bank pairs across
-- the flow at the water's surface, as wide as the water; depth and speed as every stock ribbon's (10 and 1: its
-- volume then hangs on the distance alone). (Wide calm rivers drawn as water tiles, lakes and the sea: none.)
M.RIVER_EVENT = 'event:>Ambient>Water>River'
-- Shores: the same ribbons along the sea's and the large lakes' shorelines (server seacarve.shore_plan, sea.lua), with
-- the bank's "Waves" event (in no stock level: East Coast USA's "Waves" ribbons have no events; recorded 2026-10-26 it
-- plays, quieter than the river at the same volume; the user: "it sounds like shoreline"), a strip SHORE_W wide.
M.SHORE_EVENT = 'event:>Ambient>Water>Waves'
M.SHORE_W = 20
M.RIVER_KINDS = { river = true, stream = true }   -- running water (canals are still: no rushing water there)
M.RIBBON_DEPTH, M.RIBBON_SPEED = 10, 1
M.RIBBON_WIDTH_MIN = 0.5

-- Pure: a river's nodes ({x, y, z, width, ...} downstream) as ribbon nodes: { x, y, z } bank pairs, left then right
-- of the flow, at the node's z (width: every node's, instead of its own).
function M.banks(nodes, width)
  local out, n = {}, #nodes
  for i = 1, n do
    local a, b, c = nodes[math.max(i - 1, 1)], nodes[math.min(i + 1, n)], nodes[i]
    local tx, ty = b[1] - a[1], b[2] - a[2]
    local l = math.sqrt(tx * tx + ty * ty)
    if l < 1e-6 then tx, ty, l = 1, 0, 1 end
    local h = math.max(width or tonumber(c[4]) or 0, M.RIBBON_WIDTH_MIN) / 2
    local nx, ny = -ty / l * h, tx / l * h
    out[#out + 1] = { c[1] + nx, c[2] + ny, c[3] }
    out[#out + 1] = { c[1] - nx, c[2] - ny, c[3] }
  end
  return out
end

M.RIVER_M = 700         -- ribbons for the rivers this close to the listener (the stock module plays those within 400 m) ...
M.RIVER_MOVE_M = 300    -- ... chosen again once the listener is this far from where they were chosen

local riverVersion, riverAnchor, riverSig, riverAt = nil, nil, nil, nil
local ribbonCache, boxes, cacheAnchor = {}, {}, nil     -- key -> ribbon / { x0, y0, x1, y1 } (the anchor's)
M.ribbons, M.ribbonMs, M.ribbonBuilds, M.ribbonsMade, M.shoreRibbons = 0, 0, 0, 0, 0

local function ribbonOf(rb, key, r)
  local nodes, depths = {}, {}
  for _, p in ipairs(M.banks(r.nodes, r.width)) do
    nodes[#nodes + 1] = vec3(p[1], p[2], p[3])
    depths[#depths + 1] = M.RIBBON_DEPTH
  end
  local ev = r.event or M.RIVER_EVENT
  local ribbon = { persistantId = key, name = key, isEnabled = true, isAmbient = false, isUpRibbon = false,
    isTopActive = false, isQuadAndVolume = false, speed = M.RIBBON_SPEED, nodes = nodes, depths = depths,
    emitters = { rb.createEmitterHost(ev), rb.createEmitterHost(ev), rb.createEmitterHost(ev),
                 rb.createEmitterHost(ev), rb.createEmitterHost(ev) } }
  rb.updateRibbonData(ribbon)
  M.ribbonsMade = M.ribbonsMade + 1
  return ribbon
end

local function setRibbons(rb, list)
  rb.setRibbons(list)
  rb.recomputeMap()
  rb.clearNearFarLists()
  -- no ribbons: the stock update stops at once, its emitters would keep their last volume
  if #list == 0 then rb.clearAllSFXEmitters() end
  M.ribbons = #list
end

-- The ribbons of the rivers near the listener (x, y), chosen again when sea.lua's rivers or the anchor changed or the
-- listener moved RIVER_MOVE_M; a river's ribbon is made once per anchor (its key names its nodes: server riverlines).
local function updateRivers(anchorId, sea, x, y)
  local rb = extensions and extensions.core_audioRibbon
  if not rb or not sea then return end
  local moved = not riverAt or math.max(math.abs(x - riverAt[1]), math.abs(y - riverAt[2])) > M.RIVER_MOVE_M
  if sea.riversVersion == riverVersion and anchorId == riverAnchor and not moved then return end
  local t0 = os.clockhp()
  riverVersion, riverAnchor, riverAt = sea.riversVersion, anchorId, { x, y }
  local got, a = sea.rivers()
  if a ~= anchorId then got = nil end
  -- the rivers and the shores, one set of ribbons (keys: the server's, R... and S...)
  local rivers = {}
  for k, rv in pairs(got or {}) do
    if M.RIVER_KINDS[rv.kind] then rivers[k] = rv end
  end
  local shores = a == anchorId and sea.shores and sea.shores() or {}
  for k, sh in pairs(shores) do
    rivers[k] = { kind = sh.kind, nodes = sh.nodes, width = M.SHORE_W, event = M.SHORE_EVENT }
  end
  if cacheAnchor ~= a then ribbonCache, boxes, cacheAnchor = {}, {}, a end   -- (a rebase moved their nodes)
  local keys, r = {}, M.RIVER_M
  for k, rv in pairs(rivers) do
    local b = boxes[k]
    if not b and type(rv.nodes) == 'table' and #rv.nodes >= 2 then
      b = { math.huge, math.huge, -math.huge, -math.huge }
      for _, n in ipairs(rv.nodes) do
        b[1], b[2], b[3], b[4] = math.min(b[1], n[1]), math.min(b[2], n[2]), math.max(b[3], n[1]), math.max(b[4], n[2])
      end
      boxes[k] = b
    end
    if b and x > b[1] - r and x < b[3] + r and y > b[2] - r and y < b[4] + r then keys[#keys + 1] = k end
  end
  for k in pairs(boxes) do if not rivers[k] then boxes[k], ribbonCache[k] = nil, nil end end
  table.sort(keys)
  local sig = tostring(a) .. '|' .. table.concat(keys, '|')
  if sig == riverSig then return end
  riverSig = sig
  local list = {}
  for _, k in ipairs(keys) do
    ribbonCache[k] = ribbonCache[k] or ribbonOf(rb, k, rivers[k])
    list[#list + 1] = ribbonCache[k]
  end
  M.shoreRibbons = 0
  for _, k in ipairs(keys) do if shores[k] then M.shoreRibbons = M.shoreRibbons + 1 end end
  setRibbons(rb, list)
  M.ribbonMs, M.ribbonBuilds = (os.clockhp() - t0) * 1000, M.ribbonBuilds + 1
end

-- Tunnels and bridges: the stock maps' sound spaces (SFXSpace boxes with an SFXAmbience, applied while the listener is
-- inside: West Coast USA's Level_Tunnel_Closed in its tunnels, Level_Under_Bridge_A under its bridge) along a road
-- tile's tunnel tubes and under its bridge decks (server roads.sound_boxes rows [kind, x, y, z, length, width, height,
-- yaw]; a box's scale is its full size, its length along the yaw). Recorded 2026-10-26 (Steam Deck, a horn beep under a
-- Denver deck): plain Level_Under_Bridge does nothing (the tail 45 dB down, as without), _A 25 dB down. No city bed:
-- Amb_WC_City's event plays nothing (silent even on an emitter of its own; WC_Warehouse plays). Low arches and short
-- tubes get Level_Tunnel_Open (the outside heard ~5 dB down; Closed cuts it off), the parking structures driven into
-- Level_Parking_House (~6 dB down, a reverb's tail).
M.SPACES = { [0] = 'Level_Tunnel_Closed', [1] = 'Level_Under_Bridge_A', [2] = 'Level_Tunnel_Open',
  [3] = 'Level_Parking_House' }
M.ROOFED = { [0] = true, [2] = true, [3] = true }   -- the spaces with a roof (no rain heard)
-- A closed tunnel's mouths (kind 4, a point 10 m inside): the stock portal snapshot event there (West Coast USA's
-- highway tunnels'), which fades the outside out over ~25 m before the portal instead of at once (recorded 2026-10-26:
-- cut off within ~10 m of it, 7 dB down at 20 m, untouched from 40 m).
M.PORTAL = 4
M.PORTAL_EVENT = 'event:>Snapshot Events>Snapshot_WC_Tunnel_Highway_In_01'
-- The point kinds: an event played there (the portal's; refineries' grid points, server industry.py: West Coast USA's
-- refinery tower sounds, "fans and monitors" to the user's ear, 2026-10-26)
local WC = 'event:>Ambient>Maps>West Coast>'
M.POINTS = { [4] = M.PORTAL_EVENT, [5] = WC .. 'Refinery_Tower_Engines', [6] = WC .. 'Refinery_Tower_Large',
  [7] = WC .. 'Refinery_Tower_Small', [8] = WC .. 'Power Plant_2' }
-- a point kind's volume (else 1): the power plant's, quieter (the user, 2026-10-26: "just quieter")
M.POINT_VOLUME = { [8] = 0.35 }
local spaces = {}       -- road tile key -> { { id, row }, ... }
M.spacesMade = 0
-- SFXSpaces are never deleted: parked out of reach and used again (2026-11-01: a teleport dropped a road tile's tunnel
-- spaces and the game crashed in the sound world's next update, a virtual call on a freed space: the engine's SFX
-- world still listed it). The point emitters are deleted as before.
local parked = {}       -- ambience name -> { space id, ... }
M.PARK_Z = -100000
M.spacesParked = 0

local function placeSpace(o, r)
  if M.POINTS[r[1]] then o:setPosition(vec3(r[2], r[3], r[4])); return end
  local m = MatrixF(true)
  m:setFromEuler(vec3(0, 0, -r[8]))         -- (clockwise: trees.lua)
  m:setPosition(vec3(r[2], r[3], r[4]))
  o:setTransform(m)
  o:setScale(vec3(r[5], r[6], r[7]))
end

-- A road tile's sound spaces (roads.lua onTile: data.sound, its flat rows).
function M.onSpaces(key, flat)
  M.dropSpaces(key)
  local list = {}
  for i = 1, #flat - 7, 8 do
    local r = { flat[i], flat[i + 1], flat[i + 2], flat[i + 3], flat[i + 4], flat[i + 5], flat[i + 6], flat[i + 7] }
    local amb = M.SPACES[r[1]]
    local ev = M.POINTS[r[1]]
    local o
    local pool = not ev and amb and parked[amb]
    while pool and #pool > 0 and not o do          -- a parked space of this ambience
      o = scenetree.findObjectById(table.remove(pool))
      if o then M.spacesParked = M.spacesParked - 1 end
    end
    if o then
      placeSpace(o, r)
      list[#list + 1] = { id = o:getId(), row = r }
    else
      o = (amb or ev) and Sim.upcast(createObject(ev and 'SFXEmitter' or 'SFXSpace'))
      if o then
        if ev then
          o:setTransform(MatrixF(true))
          o:setField('track', 0, ev)
        else
          o:setField('soundAmbience', 0, amb)
        end
        placeSpace(o, r)
        o.canSave = false
        o:registerObject('bngEarthSpace_' .. (M.spacesMade + 1))
        if ev then
          o:play()
          if M.POINT_VOLUME[r[1]] then o:setVolumePitchCT(M.POINT_VOLUME[r[1]], 1, 0, 0) end
        end
        list[#list + 1] = { id = o:getId(), row = r }
        M.spacesMade = M.spacesMade + 1
      end
    end
  end
  spaces[key] = list
end

-- The tile goes (roads.lua dropTile).
-- A road geotile's spaces placed again (roads.lua M.replace: the rows mapped into the new anchor's world): moved where
-- the count is the same, made again else.
function M.placeSpaces(key, flat)
  local list = spaces[key]
  if not list or type(flat) ~= 'table' or #list ~= math.floor(#flat / 8) then return M.onSpaces(key, flat) end
  for k, s in ipairs(list) do
    local i = (k - 1) * 8 + 1
    s.row = { flat[i], flat[i + 1], flat[i + 2], flat[i + 3], flat[i + 4], flat[i + 5], flat[i + 6], flat[i + 7] }
    local o = scenetree.findObjectById(s.id)
    if o then placeSpace(o, s.row) end
  end
end

function M.dropSpaces(key)
  for _, s in ipairs(spaces[key] or {}) do
    local o = scenetree.findObjectById(s.id)
    local amb = M.SPACES[s.row[1]]
    if o and amb and not M.POINTS[s.row[1]] then        -- a space: parked (never deleted, above)
      o:setPosition(vec3(0, 0, M.PARK_Z))
      o:setScale(vec3(1, 1, 1))
      parked[amb] = parked[amb] or {}
      table.insert(parked[amb], s.id)
      M.spacesParked = M.spacesParked + 1
    elseif o then
      o:delete()
    end
  end
  spaces[key] = nil
end

moveSpaces = function(xf)
  for _, list in pairs(spaces) do
    for _, s in ipairs(list) do
      local r = s.row
      local x, y, z = xf:apply(r[2], r[3], r[4])
      local ax, ay = xf:apply(r[2] + math.cos(r[8]), r[3] + math.sin(r[8]), r[4])
      r[2], r[3], r[4], r[8] = x, y, z, math.atan2(ay - y, ax - x)
      local o = scenetree.findObjectById(s.id)
      if o then placeSpace(o, r) end
    end
  end
end

-- Rain: no stock level ties a sound to its rain; the stock rain loop (SFXProfile amb_rain_medium,
-- art/sound/environment/amb_rain_medium.ogg; it plays in the "Other" sound group, its volume set by setVolumePitchCT)
-- at the listener while it rains (weather.lua's rain share, none above the cloud base), louder with more rain, fading
-- in and out, quiet inside a tunnel's space. At 1 the loop is as loud as an idling engine (-35 dBFS on the Steam Deck,
-- its forest -63; recorded 2026-10-26).
-- Weather phase B: the rain in layers (generated by native/weathersynth, levels/beamng_earth/art/sound/weather/
-- SOURCES.md): light, medium and heavy rain crossfaded by the rate (weather.rainMmHNow, in log steps between the rates
-- the loops were rendered at: RAIN_LAYERS centres), the loops stepped in loudness themselves (light -30, medium -24,
-- heavy -17 LUFS) under the one volume above (RAIN_GAIN x the rain share: ~7.5 dB more from light to heavy; ~20 dB in
-- all); under a roof (weather's shelter fade: a deck, a parking structure, a roof over the camera) they give way to a
-- muffled copy (low-passed), quieter still in a tunnel; in the driver's camera the rain drums on the car's roof
-- (RAIN_ROOF: three roof loops crossfaded the same way) and the outside is damped (RAIN_CABIN). The stock loop stays the
-- fallback when the files are missing.
M.RAIN_DIR = '/levels/beamng_earth/art/sound/weather/'
M.RAIN_LAYERS = { { 'light', 'bne_rain_light.ogg', 0.7 }, { 'medium', 'bne_rain_medium.ogg', 5 },
  { 'heavy', 'bne_rain_heavy.ogg', 35 } }
M.RAIN_ROOF_LAYERS = { light = 'bne_rain_roof_light.ogg', medium = 'bne_rain_roof.ogg', heavy = 'bne_rain_roof_heavy.ogg' }
-- (the Generated set since method B, 2026-11-26: the roof heard inside the car, and the open shelter's own loop)
M.RAIN_CABIN_LAYERS = { light = 'bne_rain_roof_cabin_light.ogg', medium = 'bne_rain_roof_cabin.ogg',
  heavy = 'bne_rain_roof_cabin_heavy.ogg' }
M.RAIN_MUFFLED = 'bne_rain_muffled.ogg'
M.RAIN_MUFFLED_OPEN = 'bne_rain_muffled_open.ogg'
M.RAIN_UNDER_ROOF = 0.7  -- the muffled layer's share of the rain's volume under a roof ...
M.RAIN_IN_TUNNEL = 0.15  -- ... and in a tunnel
M.RAIN_CABIN = 0.35      -- the outside layers in the driver's camera
M.RAIN_ROOF = 0.9        -- the roof drumming there
-- The roof (the user, 2026-11-26: "good when close to the car"): inside the car (RAIN_CABIN_CAMS) the roof loops
-- through the cabin filter (roofcab_*: bne_rain_rec_roof_cabin_*); outside, the plain roof loops near the car: full in
-- the hood / bumper cameras (RAIN_NEAR_CAMS), in the others by the camera's distance to the car, ROOF_ORBIT at
-- ROOF_NEAR_M or closer, none from ROOF_FAR_M ("lightly in orbit").
M.RAIN_CABIN_CAMS = { driver = true }
M.RAIN_NEAR_CAMS = { onboard = true }
-- (BeamNG 0.40 names the seat cameras 'occupant.<seat>' and the hood / bumper ones 'onboard.hood' ...: core/camera.lua;
-- the user, 2026-11-26: "the rain sound doesn't change when the camera is inside the cabin")
function M.camKind(n)
  if type(n) ~= 'string' then return nil end
  if M.RAIN_CABIN_CAMS[n] or n:find('^occupant%.') or n == 'onboard.driver' then return 'cabin' end
  if M.RAIN_NEAR_CAMS[n] or n:find('^onboard%.') then return 'near' end
  return nil
end
M.ROOF_ORBIT = 0.5
M.ROOF_NEAR_M, M.ROOF_FAR_M = 4, 12
M.RAIN_TRACK = 'amb_rain_medium'
-- The thunder-over-rain balance (the user, 2026-11-26, on "thunder sounds can be increased in volume by alot": "Try
-- ducking or simply turning the default rain volume down"). One name picks the option (listening mixes:
-- ~/bnge-tests/weather/thunder_mix/<name>.ogg):
-- - current: the rain at RAIN_GAIN 0.5, only strikes (within storm.STRIKE_M) duck it;
-- - duck: close strikes duck it too (CLOSE_DUCK_DB: -6 dB at 300 m to -3 dB at 1500 m, the strike duck's envelope);
-- - rainlow: the rain 3 dB down (0.35; the muffled, roof and cabin layers with it: they all scale with RAIN_GAIN);
-- - both: the close duck and the rain 2 dB down (0.4).
M.MIX_OPTIONS = {
  current = { rainGain = 0.5, closeDuck = false },
  duck = { rainGain = 0.5, closeDuck = true },
  rainlow = { rainGain = 0.35, closeDuck = false },
  both = { rainGain = 0.4, closeDuck = true },
}
M.MIX = 'rainlow'      -- (the user, 2026-11-26: "Let's just lower the rain sound")
M.CLOSE_DUCK_DB = { -6, -3 }   -- (dB at storm.STRIKE_M, at storm.CLOSE_M; linear in distance between)
M.RAIN_GAIN = M.MIX_OPTIONS[M.MIX].rainGain   -- the loop's volume at full rain ...
M.CLOSE_DUCK = M.MIX_OPTIONS[M.MIX].closeDuck
M.RAIN_FADE_S = 2       -- ... reached (or left) over this long
local rainEm, rainVol, rainAt = nil, 0, nil
local layerEm, layerVol = {}, {}      -- name -> the emitter / its volume now
local layersOk = nil                  -- the files are there (checked once)
M.rainVol = 0
M.rainLayers = layerVol

-- The Weather page's Rain sound: 'generated' (weathersynth's method B, a physical model drop by drop: the bne_rain_*.ogg
-- above, 30 s loops; native/weathersynth/src/rainb.rs, SOURCES.md) or 'recorded'
-- (CC0 field recordings, screened, cut and crossfaded by weathersynth's texture renderer: bne_rain_rec_*.ogg, the same
-- loudness per file; credits in CREDITS.md "Weather sounds"). A set whose files are missing falls back to generated.
M.RAIN_SET = 'generated'
M.REC_PREFIX = 'bne_rain_rec_'
-- The Weather page's sound sets, one row per family (the hook for more: a family's set picks its files here, and
-- liveaudio.LIVE_RAIN / LIVE_WIND say whether the live synth plays that set itself; the settings page's entry and
-- settings.lua's apply function call the setter). Both sets of a family play at the same loudness.
--   rain: 'generated' bne_rain_*.ogg (weathersynth's method B; its live engine, rainb_live.rs, is
--         liveaudio.LIVE_RAIN.generated) / 'recorded' bne_rain_rec_*.ogg (CC0 recordings mixed by texmix.rs)
--   wind: 'generated' bne_wind_gen_*.ogg (windsynth.rs) / 'recorded' bne_wind_*.ogg (CC0 recordings); files in both
--         weather sound modes (the live synth plays no wind: liveaudio.LIVE_WIND)
-- The default of each is thunder's (settings.lua weatherThunderSound): 'generated'.
M.WIND_SET = 'generated'
M.WIND_GEN_PREFIX = 'bne_wind_gen_'
local recOk = nil                     -- the recorded files are there (checked once)
function M.setRainSet(v)
  local s = (v == 'recorded') and 'recorded' or 'generated'
  liveaudio.setRainSet(s)
  if s == M.RAIN_SET then return end
  M.RAIN_SET = s
  -- the playing layers go; the next frame makes them again from the other files, at the volume they had
  for name, e in pairs(layerEm) do if simObjectExists(e) then e:delete() end; layerEm[name] = nil end
end
function M.setWindSet(v)
  local s = (v == 'recorded') and 'recorded' or 'generated'
  if s == M.WIND_SET then return end
  M.WIND_SET = s
  -- the wind layers playing go; the next frame makes them again from the other files, at the volume they had
  for name, e in pairs(layerEm) do
    if name:match('^wind_') then
      if simObjectExists(e) then e:delete() end
      layerEm[name] = nil
    end
  end
end

local function recorded()
  if M.RAIN_SET ~= 'recorded' then return false end
  if recOk == nil then recOk = FS and FS.fileExists and FS:fileExists(M.RAIN_DIR .. M.REC_PREFIX .. 'medium.ogg') or false end
  return recOk
end

-- Pure: equal-power weights (name -> 0..1, their squares summing to 1) for v over the layers L ({ name, file, centre }):
-- each layer a triangle in log10(v) (log) or v between its neighbours' centres, the ends held, and its square root the
-- gain. The loops are uncorrelated recordings, so equal power crossfades them without the 3 dB dip of linear weights.
function M.layerWeights(v, L, log)
  local w = {}
  for _, l in ipairs(L) do w[l[1]] = 0 end
  local f = log and function(u) return math.log10(math.max(u, 1e-3)) end or function(u) return u end
  local x = f(tonumber(v) or 0)
  if x <= f(L[1][3]) then w[L[1][1]] = 1 return w end
  if x >= f(L[#L][3]) then w[L[#L][1]] = 1 return w end
  for i = 1, #L - 1 do
    local a, b = f(L[i][3]), f(L[i + 1][3])
    if x <= b then
      local t = (x - a) / (b - a)
      w[L[i][1]], w[L[i + 1][1]] = math.sqrt(1 - t), math.sqrt(t)
      return w
    end
  end
  return w
end

-- Pure: the rain layers' weights for a rate (mm/h), equal power (M.layerWeights in log10 rate).
function M.rainWeights(mmh)
  return M.layerWeights(mmh, M.RAIN_LAYERS, true)
end

-- (the linear weights before 2026-11-26, kept for the record: each layer a triangle summing to 1)
function M.rainWeightsLinear(mmh)
  local L = M.RAIN_LAYERS
  local w = {}
  for _, l in ipairs(L) do w[l[1]] = 0 end
  local x = math.log10(math.max(tonumber(mmh) or 0, 1e-3))
  if x <= math.log10(L[1][3]) then w[L[1][1]] = 1 return w end
  if x >= math.log10(L[#L][3]) then w[L[#L][1]] = 1 return w end
  for i = 1, #L - 1 do
    local a, b = math.log10(L[i][3]), math.log10(L[i + 1][3])
    if x <= b then
      local t = (x - a) / (b - a)
      w[L[i][1]], w[L[i + 1][1]] = 1 - t, t
      return w
    end
  end
  return w
end

-- Pure: every layer's volume (name -> volume; 'muffled' and 'roof_light' / 'roof_medium' / 'roof_heavy' too) for the
-- rain's overall volume v, its rate, the
-- shelter fade (1 open .. 0 under a roof), in a tunnel, in the driver's camera.
-- roofNear: the plain roof's share outside (M.roofNear); open: under an open shelter (a bridge, an overpass: muffled B,
-- 'muffled_open'), else an enclosed one or a tunnel (muffled A, 'muffled'). The roof inside is 'roofcab_*'.
function M.rainMix(v, mmh, shelter, tunnel, cabin, roofNear, open)
  local out = {}
  local w = M.rainWeights(mmh)
  shelter = math.max(0, math.min(1, tonumber(shelter) or 1))
  if tunnel then shelter = 0 end
  local outside = v * shelter * (cabin and M.RAIN_CABIN or 1)
  for _, l in ipairs(M.RAIN_LAYERS) do out[l[1]] = outside * w[l[1]] end
  local under = v * (1 - shelter) * (tunnel and M.RAIN_IN_TUNNEL or M.RAIN_UNDER_ROOF)
  local b = (open and not tunnel) and true or false
  out.muffled = b and 0 or under
  out.muffled_open = b and under or 0
  local near = cabin and 0 or math.max(0, math.min(1, tonumber(roofNear) or 0))
  for _, l in ipairs(M.RAIN_LAYERS) do
    out['roof_' .. l[1]] = v * shelter * M.RAIN_ROOF * w[l[1]] * near
    out['roofcab_' .. l[1]] = cabin and v * shelter * M.RAIN_ROOF * w[l[1]] or 0
  end
  return out
end

-- Pure: the plain roof's share outside for a camera mode and its distance d (m) to the car.
function M.roofNearFor(camName, d)
  if M.camKind(camName) == 'cabin' then return 0 end
  if M.camKind(camName) == 'near' then return 1 end
  d = tonumber(d)
  if not d then return 0 end
  return M.ROOF_ORBIT * math.max(0, math.min(1, (M.ROOF_FAR_M - d) / (M.ROOF_FAR_M - M.ROOF_NEAR_M)))
end

-- Pure: is a shelter kind (weather.lua shelterWhy) an open one (muffled B)? Bridges' under-deck boxes and a roof with
-- open sides; tunnels, arches, parking structures and a roof with walls both sides are enclosed (muffled A).
M.OPEN_SHELTERS = { bridge = true, roof = true }
function M.openShelter(kind) return M.OPEN_SHELTERS[kind] == true end

local function layerFile(name)
  local strength = name:match('^wind_(%a+)$')
  if strength and M.WIND_SET == 'generated' then return M.WIND_GEN_PREFIX .. strength .. '.ogg' end
  if M.EXTRA_FILES[name] then return M.EXTRA_FILES[name] end
  local rec = recorded()
  if name == 'muffled' then return rec and (M.REC_PREFIX .. 'muffled.ogg') or M.RAIN_MUFFLED end
  if name == 'muffled_open' then return rec and (M.REC_PREFIX .. 'muffled_open.ogg') or M.RAIN_MUFFLED_OPEN end
  local roof = name:match('^roof_(%a+)$')
  if roof then
    return rec and (M.REC_PREFIX .. (roof == 'medium' and 'roof' or ('roof_' .. roof)) .. '.ogg') or M.RAIN_ROOF_LAYERS[roof]
  end
  local cab = name:match('^roofcab_(%a+)$')
  if cab then
    return rec and (M.REC_PREFIX .. (cab == 'medium' and 'roof_cabin' or ('roof_cabin_' .. cab)) .. '.ogg') or M.RAIN_CABIN_LAYERS[cab]
  end
  for _, l in ipairs(M.RAIN_LAYERS) do if l[1] == name then return rec and (M.REC_PREFIX .. name .. '.ogg') or l[2] end end
end

-- The file-based layers' channel: an SFXEmitter given a fileName and no description plays on no slider but Master
-- (in-game probe, 2026-11-25); these put it on the Environment slider like the stock weather (the FMOD-track
-- emitters keep their bank's buses: useTrackDescriptionOnly). Set before registerObject.
M.LAYER_DESC = 'AudioEnvironment'
M.LAYER_GROUP = 'AudioChannelEnvironment'

local function setLayer(name, vol)
  local e = layerEm[name]
  if e and not simObjectExists(e) then e = nil; layerEm[name] = nil end
  layerVol[name] = vol
  if vol > 0.001 and not e then
    e = Sim.upcast(createObject('SFXEmitter'))
    if not e then return end
    e:setTransform(MatrixF(true))
    e:setField('fileName', 0, M.RAIN_DIR .. layerFile(name))
    e:setField('isLooping', 0, '1')
    e:setField('is3D', 0, '0')
    e:setField('isStreaming', 0, '1')
    e:setField('volume', 0, '0')
    e:setField('description', 0, M.LAYER_DESC)
    e:setField('sourceGroup', 0, M.LAYER_GROUP)
    e.canSave = false
    e:registerObject('')
    e:play()
    layerEm[name] = e
  end
  if not e then return end
  if vol <= 0.001 then
    e:delete()
    layerEm[name] = nil
  else
    e:setPosition(listener)
    e:setVolumePitchCT(vol, 1, 0, 0)
  end
end

-- The rain bed ducked (storm.lua: under a strike's thunder): to `to` of its volume for `seconds`, the last second easing
-- back. Nothing runs for it otherwise.
local duckUntil, duckLen, duckTo = 0, 0, 1
function M.duck(seconds, to)
  duckLen, duckTo = math.max(0.1, tonumber(seconds) or 3), math.max(0, math.min(1, tonumber(to) or 0.35))
  duckUntil = os.clockhp() + duckLen
end
-- Pure-ish: the old timed duck's factor at time now (os.clockhp).
local function legacyDuck(now)
  local left = duckUntil - now
  if left <= 0 then return 1 end
  if left >= 1 then return duckTo end
  return duckTo + (1 - duckTo) * (1 - left)
end

-- The scheduled duck under a strike (~/bnge-tests/weather/thunder_research/CLOSE_STRIKE_AND_LOUDNESS.md, "Delivery";
-- the thunder never gets playback gain above 1.0, the bed makes room instead). Each strike is an event
-- { flash, t0 (the crack's arrival), depth dB } in os.clockhp:
-- - from the flash: a pre-dip of PREDIP_DB;
-- - a DUCK_RAMP_S ramp ending at t0, down to the depth (-10 dB at 60 m to -5 dB at 280 m);
-- - held DUCK_HOLD_S, then released over DUCK_RELEASE_S (linear in dB).
-- The rain bed follows it, and the music channel follows the same envelope down to MUSIC_DB. Its volume is the
-- player's own setting x the duck, set only while a duck runs and restored on every exit: the end, clear/reset (the
-- level, the connection) and unload.
M.DUCK_RAMP_S, M.DUCK_HOLD_S, M.DUCK_RELEASE_S = 0.015, 0.45, 2.0
M.PREDIP_DB = -2.5
M.MUSIC_DB = -5
M.MUSIC_CHANNEL = 'AudioChannelMusic'
local ducks = {}
local musicDucked = false

-- Pure: the depth (dB, negative) for a strike d m away: -10 at 60 m, -5 at 280 m, linear between, clamped.
function M.duckDepthDb(d)
  d = tonumber(d) or 280
  local t = math.max(0, math.min(1, (d - 60) / (280 - 60)))
  return -10 + 5 * t
end

-- Pure: the duck's envelope 0..1 (1: the full depth) of one event at time now; and whether the event is over.
function M.duckEnvelope(ev, now)
  local depth = ev.depth
  local pre = math.min(0, M.PREDIP_DB) / depth                  -- (the pre-dip as a share of the depth)
  local r0 = ev.t0 - M.DUCK_RAMP_S
  if now < ev.flash then return 0, false end
  if now < r0 then return pre, false end
  if now < ev.t0 then return pre + (1 - pre) * (now - r0) / M.DUCK_RAMP_S, false end
  local h = ev.t0 + M.DUCK_HOLD_S
  if now < h then return 1, false end
  local e = 1 - (now - h) / M.DUCK_RELEASE_S
  if e <= 0 then return 0, true end
  return e, false
end

-- Pure: the depth for a close strike (storm's 'close', STRIKE_M .. CLOSE_M) d m away: CLOSE_DUCK_DB's -6 dB at 300 m to
-- -3 dB at 1500 m, linear, clamped.
function M.closeDuckDepthDb(d)
  d = tonumber(d) or 1500
  local t = math.max(0, math.min(1, (d - 300) / (1500 - 300)))
  return M.CLOSE_DUCK_DB[1] + (M.CLOSE_DUCK_DB[2] - M.CLOSE_DUCK_DB[1]) * t
end

-- Pure: does a thunder of kind duck the rain (strikes always; closes with the mix option's CLOSE_DUCK)?
function M.ducksFor(kind)
  return kind == 'strike' or (kind == 'close' and M.CLOSE_DUCK == true)
end

-- storm.lua: a strike's duck (the clock now is its flash; t0 when its crack arrives; d m away; kind storm's,
-- 'strike' when not given).
function M.scheduleDuck(flash, t0, d, kind)
  local depth = kind == 'close' and M.closeDuckDepthDb(d) or M.duckDepthDb(d)
  ducks[#ducks + 1] = { flash = tonumber(flash) or os.clockhp(), t0 = tonumber(t0) or os.clockhp(), depth = depth }
end

-- The rain bed's factor at time now: the deepest duck running (and the old timed duck, M.duck).
function M.duckFactor(now)
  local f = legacyDuck(now)
  for _, ev in ipairs(ducks) do
    local e = M.duckEnvelope(ev, now)
    if e > 0 then f = math.min(f, 10 ^ (ev.depth * e / 20)) end
  end
  return f
end

local function music(v, temporary)
  local au = Engine and Engine.Audio
  if au and au.setChannelVolume and au.getChannelVolume then
    pcall(function()
      local base = au.getChannelVolume(M.MUSIC_CHANNEL, false)        -- (the player's setting, not the ducked value)
      au.setChannelVolume(M.MUSIC_CHANNEL, (tonumber(base) or 1) * v, temporary)
    end)
  end
end

-- The music back to the player's own setting (every exit path).
function M.restoreMusic()
  if musicDucked then
    music(1, true)
    musicDucked = false
  end
end

-- Every frame (M.update, before the dry return of the rain): the events that ended dropped, the music channel on the
-- deepest envelope. Nothing when no duck is scheduled.
local function updateDucks(now)
  if not next(ducks) then return end
  local keep, env = {}, 0
  for _, ev in ipairs(ducks) do
    local e, over = M.duckEnvelope(ev, now)
    if not over then keep[#keep + 1] = ev end
    env = math.max(env, e)
  end
  ducks = keep
  if env > 0 then
    music(10 ^ (M.MUSIC_DB * env / 20), true)
    musicDucked = true
  else
    M.restoreMusic()
  end
end
M._updateDucks = updateDucks

local function camName()
  local cam = rawget(_G, 'core_camera')
  if not (cam and cam.getActiveCamName) then return nil end
  local ok, n = pcall(cam.getActiveCamName)
  return ok and n or nil
end

local function cabinCam()
  local n = camName()
  return M.camKind(n) == 'cabin'
end

-- The plain roof's share outside now (M.roofNearFor: the camera's mode and its distance to the player's car).
local function roofNear()
  local n = camName()
  if not n or M.camKind(n) == 'cabin' then return 0 end
  if M.camKind(n) == 'near' then return 1 end
  local cam = rawget(_G, 'core_camera')
  local p = cam and cam.getPosition and cam.getPosition()
  local veh = rawget(_G, 'be') and be.getPlayerVehicle and be:getPlayerVehicle(0)
  local okv, vp = pcall(function() return veh and veh:getPosition() end)
  if not (p and okv and vp) then return 0 end
  local dx, dy, dz = p.x - vp.x, p.y - vp.y, p.z - vp.z
  return M.roofNearFor(n, math.sqrt(dx * dx + dy * dy + dz * dz))
end

-- Under an open shelter now (muffled B)? weather.lua's shelter kind, kept while the shelter fades back out.
local lastShelterKind = nil
local function openShelterNow(w)
  local k = w and w.shelterWhy
  if k then lastShelterKind = k end
  return M.openShelter(lastShelterKind)
end

-- Is (x, y, z) inside one of the road tiles' roofed spaces (tunnels, arches, parking structures: M.ROOFED)?
local function inTunnel(x, y, z)
  for _, list in pairs(spaces) do
    for _, sp in ipairs(list) do
      local r = sp.row
      if M.ROOFED[r[1]] then
        local dx, dy = x - r[2], y - r[3]
        local c, s = math.cos(r[8]), math.sin(r[8])
        if math.abs(dx * c + dy * s) <= r[5] / 2 and math.abs(-dx * s + dy * c) <= r[6] / 2
            and math.abs(z - r[4]) <= r[7] / 2 then
          return true
        end
      end
    end
  end
  return false
end
M.inTunnel = inTunnel

-- Shelter from rain and snow (weather.lua's precipitation owner): a roofed space (M.ROOFED) or a bridge's under-deck box
-- (kind 1) holding (x, y, z): its space's name ('tunnel', 'bridge', ...), or nil in the open.
M.SHELTER_NAMES = { [0] = 'tunnel', [1] = 'bridge', [2] = 'arch', [3] = 'parking' }
function M.shelterAt(x, y, z)
  for _, list in pairs(spaces) do
    for _, sp in ipairs(list) do
      local r = sp.row
      local name = M.SHELTER_NAMES[r[1]]
      if name then
        local dx, dy = x - r[2], y - r[3]
        local c, s = math.cos(r[8]), math.sin(r[8])
        if math.abs(dx * c + dy * s) <= r[5] / 2 and math.abs(-dx * s + dy * c) <= r[6] / 2
            and math.abs(z - r[4]) <= r[7] / 2 then
          return name
        end
      end
    end
  end
  return nil
end

-- The tunnels' lights at night (nightlights.lua's pool): along every tunnel box (kinds 0 and 2),
-- every TUNNEL_LIGHT_M, TUNNEL_LIGHT_DROP_M under its roof.
M.TUNNEL_LIGHT_M = 20
M.TUNNEL_LIGHT_DROP_M = 0.8
function M.tunnelLights()
  local out = {}
  for _, list in pairs(spaces) do
    for _, sp in ipairs(list) do
      local r = sp.row
      if r[1] == 0 or r[1] == 2 then
        local c, s = math.cos(r[8]), math.sin(r[8])
        local n = math.max(1, math.floor(r[5] / M.TUNNEL_LIGHT_M + 0.5))
        local z = r[4] + r[7] / 2 - M.TUNNEL_LIGHT_DROP_M
        for k = 0, n - 1 do
          local t = (k + 0.5) / n * r[5] - r[5] / 2
          out[#out + 1] = { r[2] + c * t, r[3] + s * t, z }
        end
      end
    end
  end
  return out
end

-- Wind by strength, drips after rain, hail (the user, 2026-11-26: approved with the recorded rain): CC0 recordings
-- mixed by weathersynth (texmix.rs) into seamless loops, on the Environment channel like the rain, in both weather
-- sound modes (the live synth does not play them). Each one's emitter exists only while it is heard; calm, dry and
-- no hail: nothing but a few comparisons a frame.
-- wind: three loops by the weather's wind (weather.windMsNow, m/s at the ground), equal power between the centres,
-- fading in from WIND_FROM_MS to WIND_FULL_MS; damped in the car (RAIN_CABIN) and under a roof, none in a tunnel.
M.WIND_LAYERS = { { 'wind_light', 'bne_wind_light.ogg', 3 }, { 'wind_moderate', 'bne_wind_moderate.ogg', 8 },
  { 'wind_strong', 'bne_wind_strong.ogg', 15 } }
M.WIND_FROM_MS, M.WIND_FULL_MS = 1, 3
M.WIND_GAIN = 0.5
M.WIND_UNDER_ROOF = 0.4
-- drips: after the rain stops, trees and eaves keep dripping for DRIPS_S, ever sparser (the user, 2026-11-26: "too
-- fast"): three loops of single drops (bne_drips_1 / 2 / 3: 0.8, 0.3, 0.1 drops a second, each drop as loud) crossfaded
-- by the time since it stopped (DRIPS_LAYERS centres, a share of DRIPS_S), the last fading out over DRIPS_TAIL of it;
-- as loud as the rain was (its share, at most 1), quieter in the car and in a tunnel
M.DRIPS_S = 600
M.DRIPS_GAIN = 0.5
M.DRIPS_LAYERS = { { 'drips_1', 'bne_drips_1.ogg', 0.0 }, { 'drips_2', 'bne_drips_2.ogg', 0.35 },
  { 'drips_3', 'bne_drips_3.ogg', 0.7 } }
M.DRIPS_TAIL = 0.2
-- hail: weather.hailNow (the hail's share, 0..1), louder on a roof over the head (the car's, a shelter's)
-- (the hail file is levelled by its peak, about -26 LUFS: 1.0 plays it as loud as the old -20 LUFS wash at 0.5)
M.HAIL_GAIN = 1.0
M.EXTRA_FADE_S = 2
M.EXTRA_FILES = { wind_light = 'bne_wind_light.ogg', wind_moderate = 'bne_wind_moderate.ogg',
  wind_strong = 'bne_wind_strong.ogg', drips_1 = 'bne_drips_1.ogg', drips_2 = 'bne_drips_2.ogg',
  drips_3 = 'bne_drips_3.ogg', hail = 'bne_hail.ogg' }
local extraVol = {}                   -- name -> volume now (faded)
local extraOk = nil                   -- the files are there (checked once)
local wet, dryAt = 0, nil             -- the rain's share while it fell, and when it stopped
M.extraLayers = extraVol

-- Pure: the extra layers' volumes for the wind (m/s), the drips' level (0..1), the hail's share, the shelter fade
-- (1 open .. 0 under a roof), in a tunnel, in the driver's camera.
function M.extraMix(windMs, drips, hail, shelter, tunnel, cabin)
  local out = {}
  shelter = math.max(0, math.min(1, tonumber(shelter) or 1))
  local on = math.max(0, math.min(1, ((tonumber(windMs) or 0) - M.WIND_FROM_MS) / (M.WIND_FULL_MS - M.WIND_FROM_MS)))
  on = on * on * (3 - 2 * on)
  local damp = (tunnel and 0 or 1) * (cabin and M.RAIN_CABIN or 1) * (M.WIND_UNDER_ROOF + (1 - M.WIND_UNDER_ROOF) * shelter)
  local w = M.layerWeights(windMs, M.WIND_LAYERS, false)
  for _, l in ipairs(M.WIND_LAYERS) do out[l[1]] = M.WIND_GAIN * on * damp * w[l[1]] end
  -- drips: { level 0..1, age 0..1 of DRIPS_S } (or a bare level: age 0)
  local dl, age = drips, 0
  if type(drips) == 'table' then dl, age = drips[1], drips[2] end
  local dv = M.DRIPS_GAIN * math.max(0, math.min(1, tonumber(dl) or 0)) * (tunnel and M.RAIN_IN_TUNNEL or 1) * (cabin and M.RAIN_CABIN or 1)
  local dw = M.layerWeights(math.max(0, math.min(1, tonumber(age) or 0)), M.DRIPS_LAYERS, false)
  for _, l in ipairs(M.DRIPS_LAYERS) do out[l[1]] = dv * dw[l[1]] end
  out.hail = M.HAIL_GAIN * math.max(0, math.min(1, tonumber(hail) or 0)) * (tunnel and M.RAIN_IN_TUNNEL or 1)
  return out
end

-- Pure: the drips dryS seconds after a rain whose share was `share`: { level, age } (age = dryS / DRIPS_S, the
-- layers' crossfade; the level the share until the last DRIPS_TAIL of DRIPS_S, then down to 0).
function M.dripsLevel(share, dryS)
  if not dryS then return { 0, 0 } end
  local age = math.max(0, dryS / M.DRIPS_S)
  local lvl = math.max(0, math.min(1, share or 0)) * math.max(0, math.min(1, (1 - age) / M.DRIPS_TAIL))
  return { lvl, math.min(1, age) }
end

local function updateExtras(now, dt, w)
  local windMs = w and tonumber(w.windMsNow) or 0
  local hail = w and tonumber(w.hailNow) or 0
  local rain = w and tonumber(w.rainNow) or 0
  if rain > 0.02 then wet, dryAt = math.max(wet, math.min(1, rain)), nil
  elseif wet > 0 and not dryAt then dryAt = now end
  local dripping = dryAt ~= nil and now - dryAt < M.DRIPS_S
  if not dripping and dryAt then wet, dryAt = 0, nil end
  if windMs < M.WIND_FROM_MS and hail <= 0 and not dripping and not next(extraVol) then return end   -- (calm, dry: nothing)
  if extraOk == nil then extraOk = FS and FS.fileExists and FS:fileExists(M.RAIN_DIR .. M.EXTRA_FILES.wind_light) or false end
  if not extraOk then return end
  local mix = M.extraMix(windMs, dripping and M.dripsLevel(wet, now - dryAt) or { 0, 0 }, hail,
    w and tonumber(w.shelter) or 1, inTunnel(listener.x, listener.y, listener.z), cabinCam())
  -- (each layer moves at most its full gain in EXTRA_FADE_S: a gust's change is followed, a start or stop fades)
  local step = 0.5 * dt / M.EXTRA_FADE_S
  for name, want in pairs(mix) do
    local v = extraVol[name] or 0
    v = v < want and math.min(want, v + step) or math.max(want, v - step)
    if v <= 0.001 and want <= 0 then v = 0 end
    setLayer(name, v)
    extraVol[name] = v > 0 and v or nil
  end
end

local function updateRain()
  local now = os.clockhp()
  local dt = rainAt and math.min(now - rainAt, 0.5) or 0
  rainAt = now
  local w = rawget(_G, 'beamngEarth_weather')
  updateExtras(now, dt, w)
  local want = M.RAIN_GAIN * math.max(0, math.min(1, w and tonumber(w.rainNow) or 0))
  -- live weather sounds: the synth's message, its context (the tunnel walk, the camera, the volumes) built only when
  -- one is due (every liveaudio.SEND_S, or a thunder waiting); the mod's own rain fades out while the server says it
  -- plays (liveaudio.ownFactor) and back in when it stops answering. Pre-recorded, said and faded: nothing at all.
  if liveaudio.active() then
    if liveaudio.due(now) then
      liveaudio.tick(now, liveaudio.mode() == 'live' and { rainMmH = w and tonumber(w.rainMmHNow) or 0,
        windMs = w and tonumber(w.windMsNow) or 0, shelter = w and tonumber(w.shelter) or 1,
        rainVol = want, rainSet = M.RAIN_SET,
        tunnel = inTunnel(listener.x, listener.y, listener.z), cabin = cabinCam(), duck = M.duckFactor(now),
        roofNear = roofNear(), enclosed = not openShelterNow(w) } or nil)
    end
    want = want * liveaudio.ownFactor(now)
  end
  if want <= 0 and rainVol <= 0 and not rainEm and not next(layerEm) then return end    -- (dry: nothing)
  if layersOk == nil then
    layersOk = FS and FS.fileExists and FS:fileExists(M.RAIN_DIR .. M.RAIN_LAYERS[1][2]) or false
  end
  local tunnel = want > 0 and inTunnel(listener.x, listener.y, listener.z)
  if tunnel and not layersOk then want = 0 end
  local step = math.max(M.RAIN_GAIN, rainVol) * dt / M.RAIN_FADE_S   -- (the full range in RAIN_FADE_S)
  rainVol = rainVol < want and math.min(want, rainVol + step) or math.max(want, rainVol - step)
  M.rainVol = rainVol
  if layersOk then
    local mix = M.rainMix(rainVol * M.duckFactor(now), w and tonumber(w.rainMmHNow) or 0, w and tonumber(w.shelter) or 1, tunnel,
      cabinCam(), roofNear(), openShelterNow(w))
    for name, v in pairs(mix) do setLayer(name, v) end
    return
  end
  if rainEm and not simObjectExists(rainEm) then rainEm = nil end
  if rainVol > 0.001 and not rainEm then
    local e = Sim.upcast(createObject('SFXEmitter'))
    if e then
      e:setTransform(MatrixF(true))
      e:setField('track', 0, M.RAIN_TRACK)
      e.canSave = false
      -- Weather finds the level's Precipitation by name when crossing the cloud base.
      e:registerObject('bngEarthRainAudio')
      e:play()
      rainEm = e
    end
  end
  if rainEm then
    if rainVol <= 0.001 and want <= 0 then
      rainEm:delete()
      rainEm = nil
    else
      rainEm:setPosition(listener)
      rainEm:setVolumePitchCT(rainVol, 1, 0, 0)
    end
  end
end

-- Every frame (core.lua): the cloud made again when the listener has moved or the tiles near it changed; the room
-- tone follows the listener. Another anchor's tiles go (a teleport), but a rebase's ghosts. sea: sea.lua (the rivers).
function M.update(anchorId, sea)
  updateDucks(os.clockhp())
  listener.x, listener.y, listener.z = SFXSystem.getPositionXYZ()
  updateRivers(anchorId, sea, listener.x, listener.y)
  updateRain()
  if not next(tiles) and not current then return end
  local now = os.clockhp()
  for k, t in pairs(tiles) do
    if (t.ghost and now - t.ghost > M.GHOST_MAX_S) or (not t.ghost and not t.geo and t.anchor ~= anchorId) then
      tiles[k] = nil
      dirty = true
    end
  end
  if roomtone then
    if simObjectExists(roomtone) then roomtone:setPosition(listener) else roomtone, current = nil, nil; dirty = true end
  end
  if now - lastBuild < M.GAP_S then return end
  local moved = not builtAt or math.max(math.abs(listener.x - builtAt.x), math.abs(listener.y - builtAt.y)) > M.MOVE_M
  if not (moved or dirty) then return end
  dirty, lastBuild = false, now
  builtAt = { x = listener.x, y = listener.y }
  build(listener.x, listener.y)
end

-- The server's tiles go (disconnect): the ambience with them.
function M.clear()
  ducks = {}
  M.restoreMusic()
  for key in pairs(spaces) do M.dropSpaces(key) end
  if rainEm and simObjectExists(rainEm) then rainEm:delete() end
  for name, e in pairs(layerEm) do if simObjectExists(e) then e:delete() end; layerVol[name] = 0 end
  layerEm = {}
  for name in pairs(extraVol) do extraVol[name] = nil end
  wet, dryAt = 0, nil
  rainEm, rainVol, rainAt = nil, 0, nil
  tiles, dirty, builtAt, biome, current = {}, false, nil, nil, nil
  setRoomtone(nil)
  local af = forest()
  if af then af.setEnabled(false) end
  local rb = extensions and extensions.core_audioRibbon
  if rb and riverSig then setRibbons(rb, {}) end
  riverVersion, riverAnchor, riverSig, riverAt = nil, nil, nil, nil
  ribbonCache, boxes, cacheAnchor = {}, {}, nil
end

-- The level goes.
function M.reset()
  M.clear()
end

M._setLayersOk = function(v) layersOk = v end   -- (tests: nil = look again, false = the stock loop)

function M.status()
  local n = 0
  for _ in pairs(tiles) do n = n + 1 end
  return { tiles = n, nodes = M.nodes, builds = M.builds, buildMs = M.buildMs, wind = current and current[1],
    roomtone = current and current[2], ribbons = M.ribbons,
    ribbonMs = M.ribbonMs, ribbonBuilds = M.ribbonBuilds, spacesMade = M.spacesMade, shoreRibbons = M.shoreRibbons,
    rain = M.rainVol }
end

return M
