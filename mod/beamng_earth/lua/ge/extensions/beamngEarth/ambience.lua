-- Forest wind and room tone (2026-10-26, the user: "Start with 1"), and the rivers' sound: the stock maps' ambience
-- (lua/ge/extensions/core/audioForest.lua: one wind emitter at the trees round the listener, louder and denser with
-- more and bigger trees, and a room tone everywhere) fed from the vegetation tiles' sound nodes (server
-- vegetation._ambience: a tile's trees and bushes in ~21 m squares, its ambience set by climate) instead of a
-- level's Forest: ours are many small ones made and dropped as the player moves, the stock module reads one at load.
local M = {}

local kdTree = nil       -- the stock kdtreepoint3d (required at the first cloud: not there outside the game)

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
    local o = (amb or ev) and Sim.upcast(createObject(ev and 'SFXEmitter' or 'SFXSpace'))
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
    if o then o:delete() end
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
M.RAIN_TRACK = 'amb_rain_medium'
M.RAIN_GAIN = 0.5       -- the loop's volume at full rain ...
M.RAIN_FADE_S = 2       -- ... reached (or left) over this long
local rainEm, rainVol, rainAt = nil, 0, nil
M.rainVol = 0

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

local function updateRain()
  local now = os.clockhp()
  local dt = rainAt and math.min(now - rainAt, 0.5) or 0
  rainAt = now
  local w = rawget(_G, 'beamngEarth_weather')
  local want = M.RAIN_GAIN * math.max(0, math.min(1, w and tonumber(w.rainNow) or 0))
  if want > 0 and inTunnel(listener.x, listener.y, listener.z) then want = 0 end
  local step = math.max(M.RAIN_GAIN, rainVol) * dt / M.RAIN_FADE_S   -- (the full range in RAIN_FADE_S)
  rainVol = rainVol < want and math.min(want, rainVol + step) or math.max(want, rainVol - step)
  M.rainVol = rainVol
  if rainEm and not simObjectExists(rainEm) then rainEm = nil end
  if rainVol > 0.001 and not rainEm then
    local e = Sim.upcast(createObject('SFXEmitter'))
    if e then
      e:setTransform(MatrixF(true))
      e:setField('track', 0, M.RAIN_TRACK)
      e.canSave = false
      e:registerObject('bngEarthRain')
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
  for key in pairs(spaces) do M.dropSpaces(key) end
  if rainEm and simObjectExists(rainEm) then rainEm:delete() end
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

function M.status()
  local n = 0
  for _ in pairs(tiles) do n = n + 1 end
  return { tiles = n, nodes = M.nodes, builds = M.builds, buildMs = M.buildMs, wind = current and current[1],
    roomtone = current and current[2], ribbons = M.ribbons,
    ribbonMs = M.ribbonMs, ribbonBuilds = M.ribbonBuilds, spacesMade = M.spacesMade, shoreRibbons = M.shoreRibbons,
    rain = M.rainVol }
end

return M
