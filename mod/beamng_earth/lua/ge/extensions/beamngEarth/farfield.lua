-- Far field (protocol 4, docs/V1_3_SPEC.md B-E over docs/V1_2_DESIGN.md 2.3, 2.5, 2.7, 5.4, 6, 12):
-- render-only TSStatic tiles from server-written .cdae (or .dae) files, kept as a dynamic-LOD
-- quadtree by the server.
--
-- Messages (core.lua routes them here):
--   far_tile {anchorId, key, level, shape, pos = SW corner, err, sink?, state = 'group'|'prefetch',
--             material?, texture?, detail?, tex?}
--     A tile to load. It is created HIDDEN (probe P6: a hidden TSStatic pays its whole import at
--     registerObject, and unhiding later costs nothing), within the governor's budget. 'prefetch'
--     tiles load only when nothing else waits.
--     With `material` (protocol 4, satellite imagery) the tile's own Material (mapTo = material,
--     colorMap = texture, detail map on UV1 if `detail`) is created first, as its own governor unit,
--     and deleted with the last tile using it (satmat.lua). Without it (protocol 3 tiles,
--     `--imagery off`) the tile uses the static far-tile material. `tex` (FORMAT 4, docs/V1_4_SPEC.md
--     3.1 M1) is the texture's edge in px: the governor unit kind cdaeTex128/256/512 (governor.texKind).
--   far_swap {anchorId, id, add = [keys], remove = [keys], dIn?, dOut?}
--     One LOD change. It runs once every add is loaded, never earlier ("no removal before the
--     replacement is loaded", so never a hole), and groups that share a key run in arrival order.
--     It runs as a C2 vertical crossover (crossover.lua) whenever any of it is in view, at any
--     distance (v1.3 D: no "beyond 30 km instant" rule), else as an instant swap (C1). At the
--     transition cap a group in view within NEAR_POP_M waits for a free slot instead of popping.
--     dIn >= max(new - old), dOut >= max(old - new) are the server's measured amplitudes; without
--     them (or for a one-sided group) the bound 1.2 * (err_new + err_old) + 0.2 m is used.
--   far_remove {anchorId, key}  a prefetch eviction, or a previous anchor's tile after a rebase:
--     applied once every tile received before it is shown (v1.1 rule).
--   far_done {anchorId, count}  the desired set is sent; after a rebase, the previous anchor's tiles
--     go once everything received is shown.
--   far_retex {anchorId, key, texture}  (--colormatch / --deshadow) the server rendered the texture of a
--     tile this module shows or holds again, under a new file name: the tile's material switches to it
--     (satmat requestRetex / retexNext: colorMap + flush + reload) as a governor unit 'retex', at most
--     RETEX_PER_FRAME per frame. A key that is neither shown nor loading under the current anchor (gone
--     meanwhile, or a previous anchor's) is ignored; a tile whose material is not created yet just
--     takes the new file when it is. Nothing is deleted here: the server deletes the old file with the
--     tile's other files.
-- Keys are opaque. A tile's footprint is [pos.x, pos.x + 256*2^level] x [pos.y, ...] (levels below
-- 0 are the finer near tiles of mesh mode: L-1 = 128 m, L-2 = 64 m).
--
-- Near visual (server hello.nearVisual, setNearVisual):
--   'terrain' (v1.2): the TerrainBlock is the visible near ground. The server keeps the 8 x 8 L0
--     tiles under the committed block loaded, and this module decides their state locally from the
--     block's [cx-1024, cx+1024]^2 ("covered", design 2.3):
--       edge ring (touching the block boundary): lowered by the server's `sink` (>= max(tile -
--         terrain) + 0.3 m), so it is under the terrain everywhere; it acts as an under-floor and
--         covers the 2 m N/E strip past the block's collidable width (lattice.lua);
--       interior: hidden.
--     onBlockCommit runs in the commit frame (the governor's heavy slot): newly uncovered tiles come
--     back at their true height at once; newly covered ones sink over 0.3 s (then hide if interior).
--   'detail' (2026-10-12): the mesh layout ('mesh' below) with the TerrainBlock drawn as the near ground
--     (aerial photo base colour, terrain.lua): tiles under it are covered as in 'terrain' mode, against
--     the 512² block (the server forces L-1 under it, so tile edges meet its 128 m-grid edges). The dev
--     tools' raw view (setRawView) shows the tiles instead, as in 'mesh' mode, without a server round trip.
--   'mesh' (v1.3 B): the TerrainBlock is physics-only (render disabled, core.lua/terrain.lua) and the
--     tiles are the whole visible ground: nothing is ever covered, sunk or hidden for the block, and
--     terrain-follow commits are not gated on far tiles (uncoverReady is always true). Queued tiles
--     import nearest the camera first, and after a rebase a previous anchor's tile goes as soon as
--     settled current tiles cover it (peekImport, dropCoveredOld).
--
-- After an origin rebase every tile is moved into the new frame (R*T + t, exact doubles kept here,
-- never read back from float32 scene positions) and kept while the server swaps it out. If a
-- previous anchor's add was dropped (still loading at the switch, or arriving late), the far_removes
-- of that anchor are ignored instead (the dropped tile may be what covered their area) and its
-- tiles wait for far_done.
--
-- Ground under the player (docs/V1_4_SPEC.md 3.0 M0, the `exactMiss` metric of 3.4): every update
-- given the player's position finds the shown tile under it by key lookup (no loop over tiles) and
-- counts the frame as an exact miss when, in mesh mode, that tile is not the exact level (the
-- physics lattice: L-2 with 32-cell tiles; M1's 64 cells make it L-1: core.lua sets it from
-- hello.cells through setExactLevel(exactLevelFor(cells))), is in a
-- crossover more than 1 cm off its height, or there is none. A previous anchor's exact tile counts
-- as exact. stats() reports exactMiss / exactFrames / underLevel; exactState() is for the per-frame
-- driver (tools/drive_driver.lua).
--
-- Server sessions (docs/ROADMAP.md "Robustness" 3): a restarted server numbers its anchors from 1
-- again and writes into a new session folder. Every instance remembers its session (switchAnchor's
-- `session`, the server's hello.sessionId), previous anchors' tiles are keyed by session and anchor,
-- so an old session's anchor 1 never meets the new one's, and heldAnchors() names only the current
-- session's anchors. heldSessions() lists the session folders whose files objects here still use
-- (shapes, and textures of the imagery materials): sent in hello and every pose, the server keeps
-- those folders until they are released.
--
-- Dev tools (dev.lua, docs/DEVTOOLS.md): setWatcher(w) makes this module call w.onAdd(inst) when a
-- tile's object is created, w.onRemove(id) when an object is deleted and w.onReset() when every
-- footprint moved (rebase) or the field was forgotten; eachTile / queuedTiles / counters are
-- read-only views; setFrozen(true) holds swap groups (inspection). Off by default: no watcher, not
-- frozen, so none of this does anything.

local M = {}

local geodesy = require('beamngEarth/geodesy')
local crossover = require('beamngEarth/crossover')
local lattice = require('beamngEarth/lattice')
local satmat = require('beamngEarth/satmat')
local governor = require('beamngEarth/governor')
local shadowctl = require('beamngEarth/shadowctl')

local floor, min, max, abs, huge, ln = math.floor, math.min, math.max, math.abs, math.huge, math.log
local sqrt, deg, rad, atan, tan = math.sqrt, math.deg, math.rad, math.atan, math.tan

M.MATERIAL = 'bngEarthFar_v3' -- the material name inside the server's shapes (design 6.2)
M.MATERIALS_FILE = '/levels/beamng_earth/art/shapes/beamngEarth/main.materials.json'
M.EDGE_BAND = 128       -- a covered tile nearer than this to the block boundary is in the edge ring
M.HIDE_DEPTH = 2.0      -- covered tile without a server `sink`: how far it sinks before it hides
M.DELETE_PRESSURE = 64  -- deletes wait for the import queue unless this many objects are doomed
M.DELETES_WHILE_QUEUED = 4 -- deletes allowed in a frame while imports are still queued
M.MIN_DELETES_PER_FRAME = 4 -- guaranteed minimum doomed tile deletions per frame even when importing
M.DELETE_BUDGET_MS = 0.5 -- ms; per-frame time cap for guaranteed doomed tile deletions
-- Lazy unloading (the user, 2026-10-12: "maybe a more lazy unloading system would be better"): a tile
-- removed from the current anchor is kept hidden as a spare for SPARE_S, at most SPARE_MAX of them, and
-- revived when the server sends the same tile (key and shape file) again instead of importing it again.
-- An out-and-back flight imported 3-4x as many tiles per second on the way back as on the way out (the
-- server answered from its cache; the import is the mod's cost), at ~13 fps less. 0: delete at once.
M.VIEW_PENALTY = 3      -- import order: outside the camera's view cone a tile counts this many times as far
M.VIEW_MARGIN_DEG = 15  -- the cone: half the horizontal field of view plus this ...
M.VIEW_MAX_DEG = 80     -- ... at most this far off the camera's heading
M.SPARE_MAX = 64
M.SPARE_S = 30
M.MAX_DT = 0.1          -- s; longest step of the transition clock per frame
M.EXACT_LEVEL = -2      -- the exact (physics lattice) level of the 32-cell mesh layout (server MESH_MIN_LEVEL)
M.MAX_LEVEL = 9         -- coarsest level (server farfield.MAX_LEVEL); the horizon mode's L10 tiles (M1h)
                        -- are never under the player (they split within 328 km), so shownAt stops here
M.EXACT_OFF = 0.01      -- m; an exact tile further than this off its height (crossover) is a miss
M.RETEX_PER_FRAME = 2   -- far_retex material switches per frame at most (each also a governor unit)
M.REVEALS_PER_FRAME = 2 -- swap groups revealed per frame at most during governed streaming (1 in burst mode)

local pending = {}      -- key -> unclaimed instance received but not yet claimed by a far_swap (current anchor)
local waitingInsts = {} -- inst -> inst for every unstarted instance (unclaimed in pending, or claimed by a waiting group)
local live = {}         -- key -> shown instance (current anchor), covered or not
local old = {}          -- 'anchorId/key' -> shown instance of a previous anchor
local oldCount = 0
local mainQ, mH, mT = {}, 1, 0 -- instances to import, FIFO (server order)
local preQ, pH, pT = {}, 1, 0  -- prefetch instances to import when mainQ is empty
local nQueued = 0       -- instances still to import
local groups = {}       -- far_swap groups in arrival order: { id, add, remove, state = 'wait'|'run'|'done', ... }
local activeGroups = 0  -- running crossovers
-- far_remove records { inst, seq, oldKey }, FIFO [rH, rT]. The tail is kept explicitly: once the
-- head entries are set to nil, #removals may return 0 (a border before them), not the tail.
local removals, rH, rT = {}, 1, 0
local doomed = {}       -- object ids to delete
local matOf = {}        -- doomed object id -> name of the per-tile material it holds (released at delete)
local doomedAnchor = {} -- doomed object id -> anchorId of its shape file (heldAnchors)
local doomedSession = {} -- doomed object id -> session of its instance (heldAnchors) ...
local doomedFolder = {}  -- ... and the session folder of its shape file (heldSessions)
-- Pooled shape files (server/matslots.py, 2026-10-16): the engine's frame cost grows with every distinct shape
-- file it ever loaded, so the server sends a fixed set of paths (<session>/slot_<n>.cdae) and puts each new tile's
-- shape there. A path must not be loaded while another object still holds it (the engine's cached shape would
-- be shared, or the file watcher would turn that object into the new tile): a spare or a doomed object holding
-- it is deleted first.
-- The server re-uses a slot once the mod reports the object (or never-spawned instance) of its latest far_tile with
-- it gone: far_tile.slotSerial, pose.slotsGone [[slot, serial], ...] (core.lua), hello.slotsHeld on a reconnect.
local slotHolder = {}  -- pooled shape path -> object id that loaded it
local holderPath = {}  -- object id -> its pooled shape path
local objSlot = {}     -- object id -> { n = slot, serial, session, key } of the message it shows
local slotsGone = {}   -- { n, serial, session } to report (takeSlotsGone)
local spares = {}       -- key .. '|' .. shape -> { id, mat, anchor, session, folder, t } (hidden objects)
local spareQ, sH, sT = {}, 1, 0 -- spare keys, oldest first (entries already revived are skipped)
local nSpares = 0
local switching = false -- inside switchAnchor: the old anchor's tiles are not parked
local moving = {}       -- instance -> true while its z offset changes over time
local lossy = {}        -- anchorId -> true: one of its adds was dropped, defer its removes until covered/done
local block = nil       -- committed terrain block { cx, cy, x0, y0, x1, y1 } (cover box)
local anchorId = nil    -- current (committed) anchor
local session = nil     -- server session of the current anchor (switchAnchor; nil = not told)
local doneReceived = false
local doneSeq = nil     -- seqN when far_done arrived for the current anchor
local seqN = 0          -- arrival counter of far_tile messages
local serial = 0        -- object name counter
local clock = 0         -- s, transition clock (advanced by update)
local materialChecked = false
local nearVisual = 'terrain' -- 'terrain' | 'mesh' | 'detail' (server hello; survives forget(): a server setting)
local rawView = false        -- detail mode, dev tools: the tiles are the ground (the block hidden by core)

-- The tiles use the mesh-mode layout and rules (import order, old-anchor drops, the exact-ground metric).
local function meshLayout() return nearVisual ~= 'terrain' end
-- The TerrainBlock is the visible near ground: tiles under it are covered.
local function blockShown() return nearVisual == 'terrain' or (nearVisual == 'detail' and not rawView) end
-- Level of the tiles the server forces under the block: L0 (terrain mode, 256 m grid) or L-1 (detail).
local function forcedLevel() return nearVisual == 'detail' and -1 or 0 end
local counters = { imports = 0, failed = 0, crossovers = 0, instant = 0, pops = 0, textured = 0, materials = 0,
  capWaits = 0, oldCovered = 0, exactFrames = 0, exactMiss = 0, retex = 0, retexStale = 0, retexFailed = 0,
  parked = 0, revived = 0 }
local exactLevel = M.EXACT_LEVEL
local under = { level = nil, old = false, miss = false } -- the tile under the player, last update
-- Previous anchors whose tiles may still be shown, newest first: { anchor, invs = inverse transforms
-- from the current frame back to that anchor's plane, applied in order } (the player lookup).
local oldFrames = {}
local lastFrame = { units = 0, moving = 0 }
local watcher = nil     -- dev tools observer (setWatcher), nil = none
local frozen = false    -- dev tools: swap groups wait (setFrozen)

-- A previous anchor's tile: session and anchor qualify the key (a restarted server reuses ids).
local function oldKey(s, a, key) return tostring(s) .. ':' .. tostring(a) .. '/' .. key end

-- The session folder of a VFS path written by the server ('/temp/beamngEarth/<session>/...'), or nil.
function M.folderOf(path)
  return type(path) == 'string' and path:match('^/temp/beamngEarth/([^/]+)/') or nil
end

-- folderOf memoised for heldSessions, which runs on every pose (2 Hz) over every material's texture: ~11k
-- string matches cost ~8 ms a pose at 100 mph in Manhattan (2026-10-18); a lookup per path instead. Cleared when it
-- grows past FOLDER_MEMO_MAX paths (a long session's dropped tiles).
local FOLDER_MEMO_MAX = 60000
local folderMemo, folderMemoN = {}, 0
local function folderOfMemo(path)
  local f = folderMemo[path]
  if f == nil then
    f = M.folderOf(path) or false
    if type(path) == 'string' then
      if folderMemoN >= FOLDER_MEMO_MAX then folderMemo, folderMemoN = {}, 0 end
      folderMemo[path], folderMemoN = f, folderMemoN + 1
    end
  end
  return f or nil
end

local function deleteById(id)
  local o = scenetree.findObjectById(id)
  if o then o:delete() end
  local p = holderPath[id]
  if p then
    holderPath[id] = nil
    if slotHolder[p] == id then slotHolder[p] = nil end
  end
  local os_ = objSlot[id]
  if os_ then
    objSlot[id] = nil
    slotsGone[#slotsGone + 1] = { os_.n, os_.serial, os_.session }
  end
  if watcher then watcher.onRemove(id) end
end

-- Is the shape path a pooled one (re-used by later tiles)? Its slot number.
function M.pooledShape(path) return type(path) == 'string' and path:find('/slot_%d+[_%d]*%.cdae$') ~= nil end
local function slotOf(path) return tonumber(type(path) == 'string' and path:match('/slot_(%d+)[_%d]*%.cdae$') or nil) end

-- An instance that never got an object is gone: its far_tile's slot is reported as such.
local function instGone(inst)
  local n = inst.slotSerial and slotOf(inst.shape)
  if n then slotsGone[#slotsGone + 1] = { n, inst.slotSerial, inst.session } end
  inst.slotSerial = nil
end

-- The [slot, serial] pairs gone since the last call, for the server session `sess` (others are dropped: another
-- server's slots).
function M.takeSlotsGone(sess)
  if #slotsGone == 0 then return nil end
  local out = {}
  for _, g in ipairs(slotsGone) do
    if g[3] ~= nil and g[3] == sess then out[#out + 1] = { g[1], g[2] } end
  end
  slotsGone = {}
  return #out > 0 and out or nil
end

-- Every [slot, serial] the mod still holds for server session `sess` (hello after a reconnect): objects, and
-- instances not spawned yet.
function M.slotsHeld(sess)
  local out = {}
  for _, o in pairs(objSlot) do
    if o.session == sess then out[#out + 1] = { o.n, o.serial } end
  end
  for _, set in ipairs({ waitingInsts, live, old }) do
    for _, inst in pairs(set) do
      local n = not inst.id and inst.slotSerial and slotOf(inst.shape)
      if n and inst.session == sess then out[#out + 1] = { n, inst.slotSerial } end
    end
  end
  return out
end

local function getObj(inst) return inst.id and scenetree.findObjectById(inst.id) end
local park -- (inst) -> true if kept as a spare (defined with the spares below)
local switchAnchor -- M.switchAnchor's body (it runs with parking off)

-- TSStatic visibility: setHidden as in levels/smallgrid/mainLevel.lua:27; the field otherwise.
local function setHidden(o, h)
  if o.setHidden then o:setHidden(h) else o.hidden = h end
end

-- Static far-tile material (design 6.2). Our level loads every *materials.json under its folder at
-- level load (lua/ge/server/server.lua:199-213), so this only matters in other levels (testing).
-- It must exist before the first tile: a Material with mapTo = the shape's material name overrides
-- the shape's own (probe P2).
local function ensureMaterial()
  if materialChecked then return end
  materialChecked = true
  if scenetree.findObject(M.MATERIAL) then return end
  if FS:fileExists(M.MATERIALS_FILE) then
    log('I', 'beamngEarth', 'loading far-tile material from ' .. M.MATERIALS_FILE)
    loadJsonMaterialsFile(M.MATERIALS_FILE)
  else
    log('W', 'beamngEarth', 'far-tile material file missing: ' .. M.MATERIALS_FILE)
  end
end

------------------------------------------------------------------------------------------
-- instances

local function newInst(msg, seq)
  local level = tonumber(msg.level) or 0
  local size = lattice.L0 * 2 ^ level
  local x, y, z = tonumber(msg.pos[1]) or 0, tonumber(msg.pos[2]) or 0, tonumber(msg.pos[3]) or 0
  return {
    key = tostring(msg.key), level = level, shape = msg.shape, seq = seq, anchor = msg.anchorId,
    x = x, y = y, z = z, qx = 0, qy = 0, qz = 0, qw = 1, rotated = false,
    x0 = x, y0 = y, x1 = x + size, y1 = y + size, -- footprint (AABB in the current frame)
    err = tonumber(msg.err) or crossover.defaultErr(level),
    sink = tonumber(msg.sink),
    prefetch = msg.state == 'prefetch',
    -- per-tile imagery material (protocol 4); nil = the static far material (protocol 3 tile)
    material = type(msg.material) == 'string' and msg.material ~= '' and msg.material or nil,
    texture = type(msg.texture) == 'string' and msg.texture or nil,
    detail = msg.detail == true,
    tex = tonumber(msg.tex), -- texture px (FORMAT 4); nil from a FORMAT 3 server
    slotSerial = tonumber(msg.slotSerial), -- a pooled shape file's send serial (server/matslots.py)
    mat = nil, matFailed = false, -- mat: the material name this instance holds a reference to
    id = nil, queued = true, loaded = false, failed = false, shown = false, doomed = false,
    cover = 'none', coverOff = 0, coverHide = false, coverAnim = nil, -- cover state and its target
    grp = nil,                                                       -- { g, role = 'add'|'remove' }
    off = 0, hidden = true,                                          -- what the object has now
  }
end

-- Governor unit kind of an import: a .cdae with its own imagery texture costs more (texture load,
-- ~3 ms in-game) than one on the shared static material (~1.9 ms); with FORMAT 4 the texture size
-- picks the kind (cdaeTex128/256/512, governor.texKind).
local function kindOf(inst)
  if not inst.shape:lower():match('%.cdae$') then return 'dae' end
  return inst.material and governor.texKind(inst.tex) or 'cdae'
end

-- Drop the instance's material reference (an object that never spawned, or failed to).
local function releaseMat(inst)
  if inst.mat then
    satmat.unref(inst.mat)
    inst.mat = nil
  end
end

local function writePos(o, inst, off)
  if inst.rotated then
    o:setPosRot(inst.x, inst.y, inst.z + off, inst.qx, inst.qy, inst.qz, inst.qw)
  else
    o:setPosition(vec3(inst.x, inst.y, inst.z + off))
  end
end

-- Revive a spare of this tile (same key and shape file, same anchor and session): no import.
local function revive(inst)
  local k = inst.key .. '|' .. inst.shape
  local sp = spares[k]
  if not sp or sp.anchor ~= inst.anchor or sp.session ~= inst.session then return false end
  local o = scenetree.findObjectById(sp.id)
  spares[k], nSpares = nil, nSpares - 1
  if not o then return false end
  shadowctl.excludeObject(o, 'far')
  if sp.mat then satmat.unref(sp.mat) end      -- the new instance holds its own reference
  inst.id, inst.loaded, inst.off, inst.hidden = sp.id, true, 0, true
  if objSlot[sp.id] and inst.slotSerial then objSlot[sp.id].serial = inst.slotSerial end -- the newer send's
  inst.slotSerial = nil
  writePos(o, inst, 0)
  counters.revived = counters.revived + 1
  if watcher then watcher.onAdd(inst) end
  return true
end

-- A set's instances in arrival order (seq): deletions in a deterministic order, not pairs()' hash order.
local function bySeq(set)
  local t = {}
  for _, inst in pairs(set) do t[#t + 1] = inst end
  table.sort(t, function(a, b) return a.seq < b.seq end)
  return t
end

local freeSlot -- (path, id): the object still holding a pooled shape path let go of it (defined with the spares)

local function spawn(inst)
  if revive(inst) then return end
  ensureMaterial()
  local pooled = M.pooledShape(inst.shape)
  if pooled and slotHolder[inst.shape] then freeSlot(inst.shape, slotHolder[inst.shape], inst.key) end
  if not inst.shapeValidated and not FS:fileExists(inst.shape) then
    log('W', 'beamngEarth', 'far tile shape missing: ' .. inst.shape)
    inst.failed = true
    return
  end
  inst.shapeValidated = true
  -- TSStatic setup as in probes/p5_tsstatic.lua / util/procTrack.lua:1278-1291. collisionType 'None'
  -- keeps it out of the static collision rebuild. Created hidden (P6), placed at its true height.
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, inst.shape)
  o:setField('collisionType', 0, 'None')
  o:setField('decalType', 0, 'None')
  shadowctl.excludeObject(o, 'far')
  o.canSave = false
  o.hidden = true
  o:setPosition(vec3(inst.x, inst.y, inst.z))
  serial = serial + 1
  o:registerObject('bngEarthFar_' .. serial .. '_' .. (inst.key:gsub('[^%w_]', '_')))
  scenetree.MissionGroup:addObject(o)
  inst.id, inst.loaded, inst.off, inst.hidden = o:getID(), true, 0, true
  if pooled then
    slotHolder[inst.shape], holderPath[inst.id] = inst.id, inst.shape
    if inst.slotSerial then
      objSlot[inst.id] = { n = slotOf(inst.shape), serial = inst.slotSerial, session = inst.session, key = inst.key }
      inst.slotSerial = nil -- the object reports it now
    end
  end
  counters.imports = counters.imports + 1
  if watcher then watcher.onAdd(inst) end
end

-- Cover state of a footprint against the committed block: 'none', 'edge' or 'interior'. In mesh
-- mode the block is invisible, so nothing is covered.
local function coverOf(inst)
  local b = block
  if not b or not blockShown() then return 'none' end
  local e = 0.01
  if inst.x0 < b.x0 - e or inst.y0 < b.y0 - e or inst.x1 > b.x1 + e or inst.y1 > b.y1 + e then return 'none' end
  local d = min(inst.x0 - b.x0, inst.y0 - b.y0, b.x1 - inst.x1, b.y1 - inst.y1)
  return d < M.EDGE_BAND and 'edge' or 'interior'
end

-- Target z offset and hidden flag for a cover state.
local function coverTarget(inst, cover)
  if cover == 'none' then return 0, false end
  if inst.sink then return -inst.sink, cover == 'interior' end
  return -M.HIDE_DEPTH, true -- no sink from the server: hide rather than risk poking through
end

-- Current cover offset (ends a finished cover animation).
local function coverOffNow(inst)
  local a = inst.coverAnim
  if not a then return inst.coverOff end
  local t = clock - a.t0
  if t >= a.dur then
    inst.coverAnim = nil
    return inst.coverOff
  end
  return crossover.ramp(a.from, inst.coverOff, t, a.dur)
end

local function groupOffNow(inst)
  local r = inst.grp
  if not r then return 0 end
  local t = clock - r.g.t0
  if r.role == 'add' then return crossover.addOffset(r.g.dIn, t) end
  return crossover.removeOffset(r.g.dOut, t)
end

-- Bring the object in line with the instance state: z offset, then visibility.
local function refresh(inst)
  if inst.doomed then return end
  local o = getObj(inst)
  if not o then return end
  local off = coverOffNow(inst) + groupOffNow(inst)
  local hide = (not inst.shown) or (inst.coverHide and not inst.coverAnim)
  if abs(off - inst.off) > 1e-4 then
    writePos(o, inst, off)
    inst.off = off
  end
  if hide ~= inst.hidden then
    setHidden(o, hide)
    inst.hidden = hide
  end
end

-- Take an instance out of the field; its object goes through the delete queue.
-- A spare goes to the delete queue (evicted, or every spare at a rebase / anchor switch).
local function dropSpare(k)
  local sp = spares[k]
  if not sp then return end
  spares[k], nSpares = nil, nSpares - 1
  doomed[#doomed + 1] = sp.id
  doomedAnchor[sp.id], doomedSession[sp.id], doomedFolder[sp.id] = sp.anchor, sp.session or false, sp.folder or false
  if sp.mat then matOf[sp.id] = sp.mat end
end

-- The object holding a pooled shape path that a new tile is about to load: a spare is dropped and a doomed
-- object deleted now (the delete queue skips it later and releases its material then); one still shown (the
-- server re-used the path too early) is left, with a warning.
freeSlot = function(path, id, key)
  local os_ = objSlot[id]
  if os_ and os_.key == key and not doomedAnchor[id] then
    local spare = false
    for _, sp in pairs(spares) do if sp.id == id then spare = true; break end end
    if not spare then return end -- the same tile sent again while its object is shown: the same file
  end
  for k, sp in pairs(spares) do
    if sp.id == id then
      dropSpare(k)
      break
    end
  end
  if doomedAnchor[id] ~= nil then
    deleteById(id)
    counters.slotFreed = (counters.slotFreed or 0) + 1
  elseif scenetree.findObjectById(id) then
    counters.slotBusy = (counters.slotBusy or 0) + 1
    log('W', 'beamngEarth', 'far tile shape ' .. path .. ' loaded again while another tile shows it')
    holderPath[id], slotHolder[path] = nil, nil
  else
    holderPath[id], slotHolder[path] = nil, nil
  end
end

local function dropAllSpares()
  for k in pairs(spares) do dropSpare(k) end
  spareQ, sH, sT = {}, 1, 0
end

-- Keep a removed tile of the current anchor as a hidden spare (lazy unloading). Returns true if kept.
park = function(inst)
  if switching or M.SPARE_MAX <= 0 or not inst.loaded or inst.failed or inst.anchor ~= anchorId
    or inst.session ~= session then
    return false
  end
  local o = getObj(inst)
  if not o then return false end
  if not inst.hidden then setHidden(o, true); inst.hidden = true end
  local k = inst.key .. '|' .. inst.shape
  if spares[k] then dropSpare(k) end           -- an older copy of the same tile
  sT = sT + 1
  spares[k] = { id = inst.id, mat = inst.mat, anchor = inst.anchor, session = inst.session,
    folder = M.folderOf(inst.shape), t = clock, q = sT }
  inst.mat = nil
  nSpares = nSpares + 1
  spareQ[sT] = k
  while nSpares > M.SPARE_MAX and sH <= sT do
    local q = sH; local ok = spareQ[q]; spareQ[q] = nil; sH = q + 1
    if spares[ok] and spares[ok].q == q then dropSpare(ok) end
  end
  counters.parked = counters.parked + 1
  if watcher then watcher.onRemove(inst.id) end
  return true
end

local function doom(inst, hideNow)
  if inst.doomed then return end
  if hideNow and inst.id and not inst.hidden then
    local o = getObj(inst)
    if o then setHidden(o, true) end
    inst.hidden = true
  end
  inst.doomed, inst.grp, inst.coverAnim = true, nil, nil
  waitingInsts[inst] = nil
  moving[inst] = nil
  if inst.queued then inst.queued = false; nQueued = nQueued - 1 end
  if inst.id and park(inst) then return end
  if inst.id then
    doomed[#doomed + 1] = inst.id
    doomedAnchor[inst.id] = inst.anchor
    doomedSession[inst.id] = inst.session or false
    doomedFolder[inst.id] = M.folderOf(inst.shape) or false
    if inst.mat then matOf[inst.id], inst.mat = inst.mat, nil end -- released once the object is deleted
  else
    releaseMat(inst)
    instGone(inst)
  end
end

-- Re-evaluate the cover state of a shown instance. animate: a tile that was visible and becomes
-- covered sinks over COVER_SINK_S (design 2.5 "terrain<->tile transitions use phase 2 only");
-- every other change is instant (a newly uncovered tile must be there in the commit frame).
local function applyCover(inst, animate)
  local c = coverOf(inst)
  if c == inst.cover then return false end
  local fromOff, wasVisible = coverOffNow(inst), (inst.cover == 'none' and not inst.hidden)
  inst.cover = c
  inst.coverOff, inst.coverHide = coverTarget(inst, c)
  if animate and wasVisible and c ~= 'none' then
    inst.coverAnim = { from = fromOff, t0 = clock, dur = crossover.COVER_SINK_S }
    moving[inst] = true
  else
    inst.coverAnim = nil
  end
  refresh(inst)
  return true
end

------------------------------------------------------------------------------------------
-- swap groups

local function minPendingSeq()
  local m = huge
  for _, inst in pairs(waitingInsts) do
    if not inst.prefetch and inst.seq < m then m = inst.seq end
  end
  return m
end

-- Every add loaded (or failed). An add never received makes the group wait (no hole); only once
-- the server said far_done and nothing is loading does such a group run without it (server error).
local function groupReady(g)
  for _, key in ipairs(g.add) do
    local a = (g.insts and g.insts[key]) or pending[key]
    if a then
      if not a.loaded and not a.failed then return false end
    elseif (not g.needs or g.needs[key]) and not live[key] and not (doneReceived and nQueued == 0) then
      return false
    end
  end
  return true
end

-- In view (camera cone + margin, at any distance) and the nearest distance, over instances.
local function inView(list, cam, delta)
  if not cam then return true, 0 end
  local any, nearest = false, huge
  for _, inst in ipairs(list) do
    local cx, cy, cz, r = crossover.tileSphere(inst.x0, inst.y0, inst.x1, inst.y1, inst.z, delta)
    local vis, d = crossover.sphereInView(cam, cx, cy, cz, r)
    if d < nearest then nearest = d end
    if vis then any = true end
  end
  return any, nearest
end

-- Mesh mode, after a rebase: the previous anchor's tiles stay until the server far_removes them,
-- and a far_remove waits until every tile received before it is shown - often the whole reload
-- (~10 s). Near the player that is a second ground z-fighting with the new one (the old L-1 / L-2
-- surfaces are not the new physics lattice). So a previous anchor's tile that settled
-- current-anchor tiles (shown at their true height, not failed, not in a rise crossover) cover
-- completely goes at once. Safe: the current anchor's covered area never shrinks (every swap group
-- replaces its own footprint). Called with the tiles that just settled.
local function dropCoveredOld(adds)
  if oldCount == 0 or not meshLayout() or #adds == 0 then return end
  local drop = {}
  for k, o in pairs(old) do
    local hit = false
    for _, a in ipairs(adds) do
      if a.x0 < o.x1 and o.x0 < a.x1 and a.y0 < o.y1 and o.y0 < a.y1 then hit = true; break end
    end
    if hit and not o.doomed and o.grp == nil then
      local area = 0
      for _, l in pairs(live) do
        if l.shown and not l.failed and (l.grp == nil or (l.grp.g and l.grp.g.mode == 'cross')) then
          local w = min(l.x1, o.x1) - max(l.x0, o.x0)
          local h = min(l.y1, o.y1) - max(l.y0, o.y0)
          if w > 0 and h > 0 then area = area + w * h end
        end
      end
      if area >= (o.x1 - o.x0) * (o.y1 - o.y0) * (1 - 1e-9) then drop[#drop + 1] = { k = k, o = o } end
    end
  end
  -- in arrival order (deterministic: the delete queue order does not depend on pairs())
  table.sort(drop, function(a, b) return a.o.seq < b.o.seq end)
  for _, d in ipairs(drop) do
    old[d.k] = nil
    oldCount = oldCount - 1
    doom(d.o, true)
    counters.oldCovered = counters.oldCovered + 1
  end
end

local function finishGroup(g)
  for _, a in ipairs(g.adds) do
    if a.grp and a.grp.g == g then
      a.grp = nil
      refresh(a)
      if not a.coverAnim then moving[a] = nil end
    end
  end
  for _, r in ipairs(g.rems) do doom(r, true) end
  if g.state == 'run' then activeGroups = activeGroups - 1 end
  g.state = 'done'
  dropCoveredOld(g.adds)
end

-- Start a ready group. Returns false (nothing changed) when it must wait: it would cross over in
-- view within NEAR_POP_M while MAX_GROUPS crossovers already run (v1.3 D: the cap stays, but a near
-- in-view swap waits a few frames for a free slot instead of popping).
local function startGroup(g, cam, player)
  -- 1. plan, without changing any state
  -- what goes: the live instances of the removed keys, and any live instance an add replaces
  local rems, seen = {}, {}
  for _, key in ipairs(g.remove) do
    local r = live[key]
    if r and not seen[r] then rems[#rems + 1], seen[r] = r, true end
  end
  local adds, unknown = {}, nil
  for _, key in ipairs(g.add) do
    local a = (g.insts and g.insts[key]) or pending[key]
    if a then
      local cur = live[key]
      if cur and not seen[cur] then rems[#rems + 1], seen[cur] = cur, true end
      adds[#adds + 1] = a
    elseif not live[key] then
      unknown = key
    end
  end
  -- adds take their cover state at once; covered (or hidden) removes go at once (nothing to see)
  local visAdds, visRems, errN, errO = {}, {}, 0, 0
  for _, a in ipairs(adds) do
    if coverOf(a) == 'none' and not a.failed then
      visAdds[#visAdds + 1] = a
      errN = max(errN, a.err)
    end
  end
  for _, r in ipairs(rems) do
    if r.cover == 'none' and not r.hidden and r.grp == nil then
      visRems[#visRems + 1] = r
      errO = max(errO, r.err)
    end
  end
  local mode, dIn, dOut = 'instant', 0, 0
  if #visAdds > 0 or #visRems > 0 then
    local bound = crossover.delta(errN, errO)
    if #visAdds > 0 and #visRems > 0 and g.msgIn and g.msgOut then
      dIn, dOut = g.msgIn, g.msgOut
    else
      dIn, dOut = bound, bound
    end
    local all = {}
    for _, a in ipairs(visAdds) do all[#all + 1] = a end
    for _, r in ipairs(visRems) do all[#all + 1] = r end
    local vis, nearest = inView(all, cam, max(dIn, dOut))
    local spd = 0.0
    if player then
      local vx, vy = tonumber(player.vx) or 0.0, tonumber(player.vy) or 0.0
      spd = tonumber(player.speed) or sqrt(vx * vx + vy * vy)
    end
    local alpha = min(1.0, max(0.0, (spd - 25.0) / 65.0))
    local highSpeedBypass = (spd >= 50.0) or (alpha >= 0.4)
    if #visRems == 0 and oldCount == 0 then
      mode = 'instant' -- nothing visible to cross over with (first fill)
    elseif highSpeedBypass and #visAdds > 0 then
      -- Speed-Gated C1 Instant Swaps: bypass moving crossover list and execute an instant atomic C1 swap
      mode = 'instant'
    elseif not vis then
      mode = 'instant' -- C1: off-screen
    elseif nearest <= (crossover.INSTANT_M or 350) then
      -- Within 350m of player: road driveability is critical. Instant swap eliminates coarse LOD overhang over road.
      mode = 'instant'
    elseif activeGroups >= crossover.MAX_GROUPS then
      if nearest <= crossover.NEAR_POP_M then
        counters.capWaits = counters.capWaits + 1
        return false -- wait for a free slot (a crossover ends within 0.5 s)
      else
        mode = 'instant' -- over the cap and far: C1, a visible pop (stats; v1.3 D: every in-view swap should cross over)
        counters.pops = counters.pops + 1
      end
    else
      mode = (#visRems == 0) and 'rise' or 'cross' -- rise: over a previous anchor's tiles
    end
  end

  -- 2. apply
  if unknown then
    counters.unknownSwapKeys = (counters.unknownSwapKeys or 0) + 1
    log('W', 'beamngEarth', 'far_swap ' .. tostring(g.id) .. ': unknown tile ' .. unknown)
  end
  for _, a in ipairs(adds) do
    waitingInsts[a] = nil
    if g.insts then g.insts[a.key] = nil end
    if pending[a.key] == a then pending[a.key] = nil end
    if a.queued then a.queued = false; nQueued = nQueued - 1 end -- failed before its import
    live[a.key] = a
    a.shown = true
    a.cover = coverOf(a)
    a.coverOff, a.coverHide = coverTarget(a, a.cover)
  end
  for _, r in ipairs(rems) do
    if live[r.key] == r then live[r.key] = nil end
  end
  local isVis = {}
  for _, r in ipairs(visRems) do isVis[r] = true end
  for _, r in ipairs(rems) do
    if not isVis[r] then doom(r, true) end
  end
  g.adds, g.rems, g.dIn, g.dOut = visAdds, visRems, dIn, dOut
  if mode == 'instant' then
    for _, a in ipairs(adds) do refresh(a) end
    for _, r in ipairs(visRems) do doom(r, true) end
    g.state = 'done'
    counters.instant = counters.instant + 1
    dropCoveredOld(visAdds)
    return true
  end
  g.t0, g.dur, g.mode, g.state = clock, crossover.duration(mode == 'cross'), mode, 'run'
  for _, a in ipairs(visAdds) do a.grp = { g = g, role = 'add' }; moving[a] = true end
  for _, r in ipairs(visRems) do r.grp = { g = g, role = 'remove' }; moving[r] = true end
  for _, a in ipairs(adds) do refresh(a) end -- unhidden at z - Delta: under the old surface
  activeGroups = activeGroups + 1
  counters.crossovers = counters.crossovers + 1
  return true
end

-- End every running transition at once (before a rebase transform or an anchor switch).
local function finishAll()
  for _, g in ipairs(groups) do
    if g.state == 'run' then finishGroup(g) end
  end
  for inst in pairs(moving) do
    inst.coverAnim = nil
    refresh(inst)
  end
  moving = {}
end

------------------------------------------------------------------------------------------
-- messages

function M.onTile(msg)
  if msg.anchorId ~= anchorId then -- late tile of a previous anchor (sent before our commit reached the server)
    if msg.anchorId ~= nil then lossy[msg.anchorId] = true end
    local n = tonumber(msg.slotSerial) and slotOf(msg.shape)
    if n then slotsGone[#slotsGone + 1] = { n, tonumber(msg.slotSerial), session } end -- never held
    return
  end
  if type(msg.pos) ~= 'table' or type(msg.shape) ~= 'string' or msg.key == nil then
    log('W', 'beamngEarth', 'bad far_tile ignored: ' .. tostring(msg.key))
    return
  end
  local key = tostring(msg.key)
  seqN = seqN + 1
  local inst = newInst(msg, seqN)
  inst.session = session
  if inst.material then counters.textured = counters.textured + 1 end
  waitingInsts[inst] = inst
  local claimed = false
  if not inst.prefetch then
    for _, g in ipairs(groups) do
      if g.state == 'wait' and g.needs and g.needs[key] and not g.insts[key] then
        g.insts[key], g.needs[key] = inst, nil
        claimed = true
        break
      end
    end
  end
  if not claimed then
    local prev = pending[key]
    if prev then doom(prev, true) end -- re-sent before it was claimed by a swap: the newer one wins
    pending[key] = inst
  end
  nQueued = nQueued + 1
  if inst.prefetch then
    pT = pT + 1; preQ[pT] = inst
  else
    mT = mT + 1; mainQ[mT] = inst
  end
end

local function filterKeys(list, drop)
  local out = {}
  for _, k in ipairs(list) do
    if not drop[k] then out[#out + 1] = k end
  end
  return out
end

function M.onSwap(msg)
  if msg.anchorId ~= anchorId then return end
  local g = { id = msg.id, add = {}, remove = {}, insts = {}, needs = {}, seq = seqN, state = 'wait',
    msgIn = tonumber(msg.dIn), msgOut = tonumber(msg.dOut) }
  for _, k in ipairs(type(msg.add) == 'table' and msg.add or {}) do
    k = tostring(k)
    g.add[#g.add + 1] = k
    local p = pending[k]
    if p then
      pending[k] = nil
      g.insts[k] = p
      if p.prefetch then -- a prefetched tile is needed now: import it with the group's tiles
        p.prefetch = false
        if p.queued then mT = mT + 1; mainQ[mT] = p end
      end
    else
      g.needs[k] = true
    end
  end
  for _, k in ipairs(type(msg.remove) == 'table' and msg.remove or {}) do g.remove[#g.remove + 1] = tostring(k) end

  -- Algebraic swap cancellation against waiting groups (gOld.state == 'wait')
  for i = #groups, 1, -1 do
    local gOld = groups[i]
    if gOld.state == 'wait' and #g.remove > 0 then
      local newRem = {}
      for _, k in ipairs(g.remove) do newRem[k] = true end
      local cancelAdd = {}
      local anyCancel = false
      for _, k in ipairs(gOld.add) do
        if newRem[k] and live[k] == nil then
          cancelAdd[k] = true
          anyCancel = true
        end
      end
      if anyCancel then
        for k in pairs(cancelAdd) do
          local inst = gOld.insts and gOld.insts[k]
          if inst then
            gOld.insts[k] = nil
            doom(inst, true)
          end
          if gOld.needs then gOld.needs[k] = nil end
        end
        gOld.add = filterKeys(gOld.add, cancelAdd)
        g.remove = filterKeys(g.remove, cancelAdd)

        local newAdd = {}
        for _, k in ipairs(g.add) do newAdd[k] = true end
        local cancelRem = {}
        for _, p in ipairs(gOld.remove) do
          if newAdd[p] then cancelRem[p] = true end
        end
        if next(cancelRem) then
          for p in pairs(cancelRem) do
            local instP = g.insts[p]
            if instP then
              g.insts[p] = nil
              local lv = live[p]
              if lv and lv.id and instP.slotSerial and objSlot[lv.id] and objSlot[lv.id].n == slotOf(instP.shape) then
                objSlot[lv.id].serial = instP.slotSerial
                instP.slotSerial = nil
              end
              doom(instP, true)
            end
            if g.needs then g.needs[p] = nil end
          end
          gOld.remove = filterKeys(gOld.remove, cancelRem)
          g.add = filterKeys(g.add, cancelRem)
        end

        -- Merge any remaining keys of gOld into g so no partial group runs with a missing quadrant
        local seenAdd = {}
        for _, k in ipairs(g.add) do seenAdd[k] = true end
        for _, k in ipairs(gOld.add) do
          if not seenAdd[k] then
            g.add[#g.add + 1] = k
            seenAdd[k] = true
          end
          if gOld.insts and gOld.insts[k] then
            g.insts[k] = gOld.insts[k]
            gOld.insts[k] = nil
          elseif gOld.needs and gOld.needs[k] then
            g.needs[k] = true
            gOld.needs[k] = nil
          end
        end
        local seenRem = {}
        for _, k in ipairs(g.remove) do seenRem[k] = true end
        for _, k in ipairs(gOld.remove) do
          if not seenRem[k] then
            g.remove[#g.remove + 1] = k
            seenRem[k] = true
          end
        end
        if gOld.seq and gOld.seq < g.seq then g.seq = gOld.seq end
        table.remove(groups, i)
      end
    end
  end

  if #g.add > 0 or #g.remove > 0 then
    groups[#groups + 1] = g
  end
end

function M.onRemove(msg)
  local key = tostring(msg.key)
  if msg.anchorId ~= anchorId then
    -- a previous anchor's tile, swapped out now that its area is covered in the new frame
    local ok = oldKey(session, msg.anchorId, key)
    local inst = old[ok]
    if inst and not inst.removing and not lossy[msg.anchorId] then
      inst.removing = true
      rT = rT + 1; removals[rT] = { inst = inst, seq = seqN, oldKey = ok }
    end
    return
  end
  local p = pending[key]
  if p then -- prefetch eviction (or an add withdrawn before it was shown)
    pending[key] = nil
    doom(p, true)
    return
  end
  for _, g in ipairs(groups) do
    if g.state == 'wait' then
      if g.insts and g.insts[key] then
        local inst = g.insts[key]
        g.insts[key] = nil
        doom(inst, true)
        return
      end
      if g.needs and g.needs[key] then
        g.needs[key] = nil
      end
    end
  end
  local l = live[key]
  if l and not l.removing then
    l.removing = true
    rT = rT + 1; removals[rT] = { inst = l, seq = seqN }
  end
end

function M.onDone(msg)
  if msg.anchorId == anchorId then
    if not doneReceived then
      doneReceived = true
      doneSeq = seqN
    end
  end
end

-- far_retex: point the tile's material at its re-rendered texture (time-sliced in update). The shown
-- instance and one still loading for the same key share the material (same name); both records take
-- the new file, so a material created later uses it too.
function M.onRetex(msg)
  local key, tex = msg.key ~= nil and tostring(msg.key), msg.texture
  if msg.anchorId ~= anchorId or not key or type(tex) ~= 'string' or tex == '' then
    counters.retexStale = counters.retexStale + 1
    return
  end
  local hit = false
  local function retexInst(inst)
    if inst and not inst.doomed and inst.material then
      if not satmat.isNewer(tex, inst.texture) then inst.texture = tex end -- never back to an older revision
      satmat.requestRetex(inst.material, tex)
      hit = true
    end
  end
  retexInst(live[key])
  retexInst(pending[key])
  for _, inst in pairs(waitingInsts) do
    if inst.key == key and inst ~= pending[key] then retexInst(inst) end
  end
  if not hit then counters.retexStale = counters.retexStale + 1 end
end

------------------------------------------------------------------------------------------
-- terrain block

local function setBlock(cx, cy)
  local x0, y0, x1, y1 = lattice.coverBox(cx, cy)
  block = { cx = cx, cy = cy, x0 = x0, y0 = y0, x1 = x1, y1 = y1 }
end

-- Near visual from the server's hello: 'mesh' (tiles are the whole visible ground, the block is
-- physics-only) or 'terrain' (v1.2). A change re-applies every shown tile's cover state at once
-- (mesh: covered tiles come back at their true height).
function M.setNearVisual(mode)
  mode = (mode == 'mesh' or mode == 'detail') and mode or 'terrain'
  if mode == nearVisual then return end
  nearVisual = mode
  for _, inst in pairs(live) do applyCover(inst, false) end
  for _, inst in pairs(old) do applyCover(inst, false) end
end

function M.nearVisual() return nearVisual end

-- Detail mode, dev tools (raw / generated before-after captures): on = the tiles are the ground again
-- (nothing covered; core hides the block), off = the block. Re-applies every tile's cover at once.
function M.setRawView(on)
  on = on and true or false
  if on == rawView then return rawView end
  rawView = on
  for _, inst in pairs(live) do applyCover(inst, false) end
  for _, inst in pairs(old) do applyCover(inst, false) end
  return rawView
end

function M.rawView() return rawView end

-- The terrain block moved (terrain-follow commit frame, after importMaps + setPosition). Covers and
-- uncovers far tiles; about 48 setPosition/setHidden calls for a 256 m move. Returns the changes.
function M.onBlockCommit(cx, cy)
  setBlock(cx, cy)
  if not blockShown() then return 0 end
  local n = 0
  for _, inst in pairs(live) do if applyCover(inst, true) then n = n + 1 end end
  for _, inst in pairs(old) do if applyCover(inst, true) then n = n + 1 end end
  return n
end

-- Is every 256 m cell that a move of the block from (ocx, ocy) to (ncx, ncy) uncovers shown by a
-- loaded tile? (commit gating, design 2.3; the server streams these first.) Also false while a
-- coarser-than-L0 tile overlaps the new block: the server forces L0 under the pending block, and
-- until that split has run here, a coarse tile across the block's edge would stay shown at its
-- true height over the new terrain (coverOf only covers tiles fully inside the block), and one
-- inside it would be hidden (it has no `sink`), leaving the 2 m N/E strip uncovered.
function M.uncoverReady(ocx, ocy, ncx, ncy)
  if not blockShown() then return true end -- the block is invisible: a move uncovers nothing
  local ox0, oy0, ox1, oy1 = lattice.coverBox(ocx, ocy)
  local nx0, ny0, nx1, ny1 = lattice.coverBox(ncx, ncy)
  local fl = forcedLevel()
  local L = lattice.L0 * 2 ^ fl
  local e = 0.01
  for _, inst in pairs(live) do
    if not inst.failed and inst.x0 < nx1 - e and nx0 + e < inst.x1 and inst.y0 < ny1 - e and ny0 + e < inst.y1
      and (inst.level > fl or inst.x0 < nx0 - e or inst.y0 < ny0 - e or inst.x1 > nx1 + e or inst.y1 > ny1 + e) then
      return false
    end
  end
  local idx = {}
  for _, inst in pairs(live) do
    if inst.level == fl and not inst.failed then idx[floor(inst.x0 / L + 0.5) .. ',' .. floor(inst.y0 / L + 0.5)] = true end
  end
  local function anyCovers(px, py, set)
    for _, inst in pairs(set) do
      if not inst.failed and not inst.doomed and px > inst.x0 and px < inst.x1 and py > inst.y0 and py < inst.y1 then
        return true
      end
    end
    return false
  end
  for gx = ox0, ox1 - 1, L do
    for gy = oy0, oy1 - 1, L do
      local px, py = gx + L / 2, gy + L / 2
      if not (px > nx0 and px < nx1 and py > ny0 and py < ny1)
        and not idx[floor(gx / L + 0.5) .. ',' .. floor(gy / L + 0.5)]
        and not anyCovers(px, py, live) and not anyCovers(px, py, old) then
        return false
      end
    end
  end
  return true
end

------------------------------------------------------------------------------------------
-- anchors

local function setTransform(inst)
  local o = getObj(inst)
  if o then o:setPosRot(inst.x, inst.y, inst.z + inst.off, inst.qx, inst.qy, inst.qz, inst.qw) end
end

-- Origin rebase: map every tile into the new frame, T_new = R*T_old + t (xf from
-- geodesy Anchor:transformTo). TSStatic:setPosRot(x,y,z, qx,qy,qz,qw) takes a BeamNG quaternion
-- (editor/tech/roadArchitect/staticMesh.lua:151, tech/techCore.lua handleTeleportScenarioObject).
-- The tile's own rotation is applied first, then R: q_new = q_old * Rq (BeamNG order).
-- Running transitions end first. Call before switchAnchor(..., true).
function M.transform(xf)
  finishAll()
  dropAllSpares() -- their positions are the old frame's
  if xf.inverse then
    local inv = xf:inverse()
    for _, f in ipairs(oldFrames) do table.insert(f.invs, 1, inv) end
    table.insert(oldFrames, 1, { anchor = anchorId, session = session, invs = { inv } })
    oldFrames[5] = nil -- a tile older than four rebases is long gone
  end
  local bx, by, bz, bw = xf:beamngQuat()
  local function move(inst)
    if inst.doomed then return end
    local s = inst.x1 - inst.x0
    -- footprint: AABB of the moved corners (slightly larger than the turned square)
    local ax, ay = xf:apply(inst.x0, inst.y0, inst.z)
    local bx_, by_ = xf:apply(inst.x0 + s, inst.y0, inst.z)
    local cx, cy = xf:apply(inst.x0, inst.y0 + s, inst.z)
    local dx, dy = xf:apply(inst.x0 + s, inst.y0 + s, inst.z)
    inst.x0, inst.y0 = min(ax, bx_, cx, dx), min(ay, by_, cy, dy)
    inst.x1, inst.y1 = max(ax, bx_, cx, dx), max(ay, by_, cy, dy)
    inst.x, inst.y, inst.z = xf:apply(inst.x, inst.y, inst.z)
    inst.qx, inst.qy, inst.qz, inst.qw = geodesy.quatMul(inst.qx, inst.qy, inst.qz, inst.qw, bx, by, bz, bw)
    inst.rotated = true
    setTransform(inst)
  end
  for _, inst in pairs(live) do move(inst) end
  for _, inst in pairs(old) do move(inst) end
  if watcher then watcher.onReset() end
end

-- New committed anchor. keepOld=true (rebase): current tiles stay until the server removes them or
-- the new far_done. keepOld=false (teleport): everything is hidden at once and deleted progressively.
-- blk: the new anchor's terrain block {cx, cy}; the kept tiles' cover state follows it at once.
-- newSession: the server session the new anchor belongs to (hello.sessionId; nil = unchanged). A
-- resync with a restarted server keeps the old session's tiles like a rebase; its far_removes never
-- come (the new server does not know them), so they go when covered (mesh) or at the new far_done.
function M.switchAnchor(newAnchorId, keepOld, blk, newSession)
  switching = true
  local ok, err = pcall(switchAnchor, newAnchorId, keepOld, blk, newSession)
  switching = false
  dropAllSpares()
  if not ok then error(err, 0) end
end

switchAnchor = function(newAnchorId, keepOld, blk, newSession)
  finishAll()
  dropAllSpares()
  ensureMaterial()
  local keep = {}
  if keepOld then
    local due = minPendingSeq()
    -- removals whose adds are all shown stay valid in the new frame
    for i = rH, rT do
      local r = removals[i]
      if r and r.inst and not r.inst.doomed and r.seq < due then
        keep[#keep + 1] = { inst = r.inst, seq = 0, oldKey = r.oldKey or oldKey(session, anchorId, r.inst.key) }
      end
    end
    -- cap retained old-anchor history to at most two rebases back so old tiles never accumulate unboundedly
    if #oldFrames > 2 and oldCount > 0 then
      local f1, f2 = oldFrames[1], oldFrames[2]
      for ok, inst in pairs(old) do
        local keep1 = f1 and inst.anchor == f1.anchor and inst.session == f1.session
        local keep2 = f2 and inst.anchor == f2.anchor and inst.session == f2.session
        if not keep1 and not keep2 then
          doom(inst, false)
          old[ok] = nil
          oldCount = oldCount - 1
        end
      end
      for i = 3, #oldFrames do oldFrames[i] = nil end
    end
    local dropped = false
    for _, inst in ipairs(bySeq(waitingInsts)) do
      if not inst.prefetch then dropped = true end
      doom(inst, true)
    end
    if dropped and anchorId ~= nil then lossy[anchorId] = true end
    for key, inst in pairs(live) do
      old[oldKey(session, anchorId, key)] = inst
      oldCount = oldCount + 1
    end
  else
    for _, set in ipairs({ live, old, waitingInsts }) do
      for _, inst in ipairs(bySeq(set)) do doom(inst, true) end
    end
    old, oldCount, lossy, oldFrames = {}, 0, {}, {}
  end
  pending, waitingInsts, live = {}, {}, {}
  mainQ, mH, mT, preQ, pH, pT, nQueued = {}, 1, 0, {}, 1, 0, 0
  groups, activeGroups, moving = {}, 0, {}
  removals, rH, rT = keep, 1, #keep
  anchorId = newAnchorId
  if newSession ~= nil and newSession ~= session then
    lossy = {} -- anchor ids of the previous session mean nothing to the new server
    session = newSession
  end
  doneReceived, doneSeq = false, nil
  if blk then
    setBlock(blk.cx, blk.cy)
    for _, inst in pairs(old) do applyCover(inst, false) end
  end
end

------------------------------------------------------------------------------------------
-- per frame

-- The next tile to import: group tiles in the server's order, then prefetch tiles. Mesh mode
-- (cam given): the queued group tile nearest the camera instead. The near ground IS these tiles
-- there, and after a rebase (or a teleport) hundreds of tiles queue up faster than the budget
-- imports them (~0.7 textured tiles per frame): in server order the ground the car drives onto
-- would wait behind distant tiles for seconds (the old anchor's or a coarser tile showing
-- meanwhile). Terrain mode keeps the server's order (v1.2).
local function peekImport(cam, player)
  while mH <= mT do
    local inst = mainQ[mH]
    if waitingInsts[inst] and not inst.doomed and inst.queued then break end
    mainQ[mH] = nil
    mH = mH + 1
  end
  if mH <= mT then
    local px, py, lx, ly
    if player then
      px, py = tonumber(player.x), tonumber(player.y)
      local vx, vy = tonumber(player.vx) or 0, tonumber(player.vy) or 0
      local spd = tonumber(player.speed) or sqrt(vx * vx + vy * vy)
      if px and py and spd > 15 then
        lx, ly = px + vx * 3.0, py + vy * 3.0
      end
    end
    if not meshLayout() or (not cam and not (px and py)) then return mainQ[mH], true end
    -- Sibling completion boost: if a tile belongs to a waiting group that covers the player or lookahead
    -- point, or already has >= 1 sibling loaded, boost its priority so all siblings import back-to-back.
    local boostInst = {}
    for _, g in ipairs(groups) do
      if g.state == 'wait' and g.insts then
        local boost = false
        local gx0, gy0, gx1, gy1 = huge, huge, -huge, -huge
        for _, sib in pairs(g.insts) do
          if sib.loaded then boost = true end
          if sib.x0 < gx0 then gx0 = sib.x0 end
          if sib.y0 < gy0 then gy0 = sib.y0 end
          if sib.x1 > gx1 then gx1 = sib.x1 end
          if sib.y1 > gy1 then gy1 = sib.y1 end
        end
        if not boost and px and py and px >= gx0 and px <= gx1 and py >= gy0 and py <= gy1 then
          boost = true
        end
        if not boost and lx and ly and lx >= gx0 and lx <= gx1 and ly >= gy0 and ly <= gy1 then
          boost = true
        end
        if boost then
          for _, sib in pairs(g.insts) do
            if sib.queued and not sib.doomed then boostInst[sib] = true end
          end
        end
      end
    end
    -- nearest the camera / player / velocity-lookahead first; outside its view cone a tile counts
    -- VIEW_PENALTY times as far (squared here), except L-1 / L0 tiles within 450m.
    local cosmax, fx, fy
    if cam then
      fx, fy = cam.fx or 0, cam.fy or 0
      local fn = sqrt(fx * fx + fy * fy)
      if fn > 0.2 then
        fx, fy = fx / fn, fy / fn
        local half = deg(atan(tan(rad(cam.fovDeg or 60) / 2) * 16 / 9)) + M.VIEW_MARGIN_DEG
        cosmax = math.cos(rad(min(half, M.VIEW_MAX_DEG)))
      end
    end
    local pen2 = M.VIEW_PENALTY * M.VIEW_PENALTY
    local best, bd = nil, huge
    for i = mH, mT do
      local inst = mainQ[i]
      if waitingInsts[inst] and not inst.doomed and inst.queued then
        local d = huge
        if cam and cam.x and cam.y then
          local dx = max(inst.x0 - cam.x, 0, cam.x - inst.x1)
          local dy = max(inst.y0 - cam.y, 0, cam.y - inst.y1)
          d = dx * dx + dy * dy
        end
        if px and py then
          local pdx = max(inst.x0 - px, 0, px - inst.x1)
          local pdy = max(inst.y0 - py, 0, py - inst.y1)
          local pd = pdx * pdx + pdy * pdy
          if pd < d then d = pd end
        end
        if lx and ly then
          local ldx = max(inst.x0 - lx, 0, lx - inst.x1)
          local ldy = max(inst.y0 - ly, 0, ly - inst.y1)
          local ld = ldx * ldx + ldy * ldy
          if ld < d then d = ld end
        end
        local rawD = d
        if inst.level and inst.level <= -1 and rawD <= 160000 then -- L-1 within 400m
          d = d * 0.04
        elseif inst.level and inst.level <= 0 and rawD <= 250000 then -- L0 within 500m
          d = d * 0.25
        end
        if boostInst[inst] then
          d = d * 0.05
        end
        if cosmax and d > 0 then
          local cx, cy = (inst.x0 + inst.x1) / 2 - cam.x, (inst.y0 + inst.y1) / 2 - cam.y
          local r2 = cx * cx + cy * cy
          -- NEVER apply the 9x view-cone penalty to L-1 or L0 tiles within 450m (r <= 450)
          local nearFine = inst.level and inst.level <= 0 and (r2 <= 202500 or rawD <= 202500)
          if not nearFine then
            local r = sqrt(r2)
            if r > 350.0 and r > inst.x1 - inst.x0 and (cx * fx + cy * fy) / r < cosmax then d = d * pen2 end
          end
        end
        if d < bd then best, bd = inst, d end
      end
    end
    return best, true
  end
  while pH <= pT do
    local inst = preQ[pH]
    if waitingInsts[inst] and not inst.doomed and inst.queued and inst.prefetch then return inst, false end
    preQ[pH] = nil
    pH = pH + 1
  end
end

local function hasWaitingGroups()
  for _, g in ipairs(groups) do if g.state == 'wait' then return true end end
  return false
end

-- Streaming work waiting (for the governor's budget ramp).
function M.hasWork()
  return nQueued > 0 or #doomed > 0 or rH <= rT or hasWaitingGroups() or satmat.hasDeletes() or satmat.hasRetex()
end

local function pendingMainCount()
  local n = 0
  for _, inst in pairs(waitingInsts) do if not inst.prefetch then n = n + 1 end end
  return n
end

local function pendingBefore(seq)
  for _, inst in pairs(waitingInsts) do
    if not inst.prefetch and inst.seq <= seq then return true end
  end
  for _, g in ipairs(groups) do
    if g.seq <= seq then return true end
  end
  return false
end

-- The shown tile containing (x, y) in `set` (live, or old with anchor a: keys 'a/L_i_j'), finest
-- level first, from the exact level up: at most MAX_LEVEL - exactLevel + 1 table lookups.
local function shownAt(set, sess, a, x, y)
  for l = exactLevel, M.MAX_LEVEL do
    local s = lattice.L0 * 2 ^ l
    local k = l .. '_' .. floor(x / s) .. '_' .. floor(y / s)
    local inst = set[a ~= nil and oldKey(sess, a, k) or k]
    if inst and inst.shown and not inst.failed and not inst.doomed then return inst end
  end
end

-- The ground under the player this frame (after this frame's reveals): see the module header.
local function updateUnder(player)
  local px, py, pz = tonumber(player.x), tonumber(player.y), tonumber(player.z) or 0
  if not px or not py or anchorId == nil then return end
  local inst, isOld = shownAt(live, nil, nil, px, py), false
  if not inst then
    for _, f in ipairs(oldFrames) do
      local x, y, z = px, py, pz
      for _, inv in ipairs(f.invs) do x, y, z = inv:apply(x, y, z) end
      inst = shownAt(old, f.session, f.anchor, x, y)
      if inst then isOld = true; break end
    end
  end
  local miss = false
  if meshLayout() and not blockShown() then -- else the TerrainBlock itself is the visible near ground
    miss = not inst or inst.level ~= exactLevel or abs(inst.off) > M.EXACT_OFF
    counters.exactFrames = counters.exactFrames + 1
    if miss then counters.exactMiss = counters.exactMiss + 1 end
  end
  under.level, under.old, under.miss = inst and inst.level, isOld, miss
end

-- gov: governor instance (governor.lua); dt: s since the last frame (wall); cam: nil or
-- {x,y,z,fx,fy,fz,fovDeg} for the off-screen test; player: nil or {x,y,z} (the exactMiss metric).
function M.update(gov, dt, cam, player)
  clock = clock + min(max(dt or 0, 0), M.MAX_DT)
  local now = gov.clock
  local units = 0

  -- 1. motion: finish crossovers that are over, then move what moves (time based, always runs)
  local t0 = now()
  local nMoving = 0
  for _, g in ipairs(groups) do
    if g.state == 'run' and (clock - g.t0 >= g.dur or (player and ((tonumber(player.speed) or sqrt((tonumber(player.vx) or 0)^2 + (tonumber(player.vy) or 0)^2)) >= 50.0 or min(1.0, max(0.0, ((tonumber(player.speed) or sqrt((tonumber(player.vx) or 0)^2 + (tonumber(player.vy) or 0)^2)) - 25.0) / 65.0)) >= 0.4))) then finishGroup(g) end
  end
  for inst in pairs(moving) do
    refresh(inst)
    nMoving = nMoving + 1
    if not inst.grp and not inst.coverAnim then moving[inst] = nil end
  end
  gov:chargeMotion(now() - t0)

  -- 2. start swap groups whose adds are all loaded, in order; groups sharing a key wait for the
  --    earlier one to finish
  local busy = {}
  for _, g in ipairs(groups) do
    if g.state == 'run' then
      for _, k in ipairs(g.add) do busy[k] = true end
      for _, k in ipairs(g.remove) do busy[k] = true end
    end
  end
  local stop = false
  local nRevealed = 0
  local maxReveals = (gov.frame == nil or gov.loading) and huge or (((gov.burst and not gov.urgent) and 1) or M.REVEALS_PER_FRAME or 2)
  for _, g in ipairs(groups) do
    if g.state == 'wait' then
      local blocked = stop or frozen or (nRevealed >= maxReveals)
      if not blocked then
        for _, k in ipairs(g.add) do if busy[k] then blocked = true; break end end
      end
      if not blocked then
        for _, k in ipairs(g.remove) do if busy[k] then blocked = true; break end end
      end
      if not blocked and groupReady(g) then
        if gov:try('reveal') then
          local t1 = now()
          if startGroup(g, cam, player) then
            gov:charge('reveal', now() - t1)
            units = units + 1
            nRevealed = nRevealed + 1
            if nRevealed >= maxReveals then stop = true end
          end -- else: waits for a crossover slot; its keys stay busy, other groups go on
        else
          stop = true -- out of budget: keep the order, try again next frame
        end
      end
      if g.state ~= 'done' then
        for _, k in ipairs(g.add) do busy[k] = true end
        for _, k in ipairs(g.remove) do busy[k] = true end
      end
    end
  end
  local kept = {}
  for _, g in ipairs(groups) do if g.state ~= 'done' then kept[#kept + 1] = g end end
  groups = kept

  -- 3. far_removes whose earlier adds are all shown
  if rH <= rT then
    local due = minPendingSeq()
    while rH <= rT and removals[rH].seq < due do
      local r = removals[rH]
      if not r.inst.doomed then
        if not gov:try('reveal') then break end
        local t1 = now()
        if r.oldKey then
          if old[r.oldKey] == r.inst then old[r.oldKey] = nil; oldCount = oldCount - 1 end
        elseif live[r.inst.key] == r.inst then
          live[r.inst.key] = nil
        end
        doom(r.inst, true)
        gov:charge('reveal', now() - t1)
        units = units + 1
      end
      removals[rH] = nil
      rH = rH + 1
    end
    if rH > rT then removals, rH, rT = {}, 1, 0 end
  end

  -- 4. imports (hidden), group tiles first, prefetch only when nothing else waits. A tile with its
  --    own imagery material gets the Material first, as a separate unit (satmat.lua); a re-sent key
  --    or a sibling sharing the name re-uses it for free.
  --    Yield imports on frames that already revealed a swap group (unless loading or free test gov) so
  --    unhide + .cdae resource init never stack in the same frame's RenderShadows pass.
  local imported = false
  local allowImports = (gov.frame == nil or gov.loading or nRevealed == 0)
  while allowImports do
    local inst = peekImport(cam, player)
    if not inst then break end
    if inst.material and not inst.mat and not inst.matFailed then
      if not satmat.exists(inst.material) then
        if not gov:try('material') then break end
        local t1 = now()
        local ok, err = pcall(satmat.create, inst.material, inst.texture, inst.detail)
        if not ok then log('E', 'beamngEarth', 'far tile material failed: ' .. tostring(err)) end
        gov:charge('material', now() - t1)
        units = units + 1
        if satmat.exists(inst.material) then counters.materials = counters.materials + 1 end
      end
      if satmat.ref(inst.material, inst.texture) then
        inst.mat = inst.material
      else
        inst.matFailed = true -- spawn anyway: better a tile without imagery than a hole
      end
    else
      local kind = kindOf(inst)
      if not gov:try(kind) then break end
      local t1 = now()
      local ok, err = pcall(spawn, inst)
      if not ok then
        inst.failed = true
        log('E', 'beamngEarth', 'far tile spawn failed: ' .. tostring(err))
      end
      if inst.failed then
        counters.failed = counters.failed + 1
        releaseMat(inst)
      end
      inst.queued = false
      nQueued = nQueued - 1
      inst.importMs = now() - t1 -- shown by the dev tools' mini-map
      gov:charge(kind, inst.importMs)
      units = units + 1
      imported = true
    end
  end

  -- 4b. far_retex: material texture switches, a few per frame within the budget (never thrown out of
  --     the frame: a failing switch is counted and dropped). Yield when an import or reveal already ran.
  local nRetex = 0
  while (not imported and nRevealed == 0) and nRetex < M.RETEX_PER_FRAME and satmat.hasRetex() and gov:try('retex') do
    local t1 = now()
    local ok, res = pcall(satmat.retexNext)
    if not ok then
      counters.retexFailed = counters.retexFailed + 1
      if counters.retexFailed <= 3 then log('E', 'beamngEarth', 'far tile retexture failed: ' .. tostring(res)) end
    elseif res then
      counters.retex = counters.retex + 1
    end
    gov:charge('retex', now() - t1)
    units = units + 1
    nRetex = nRetex + 1
  end

  -- 5. after a rebase: once far_done came and everything received is shown, the previous anchor's
  --    tiles go (progressively, through the delete queue)
  if doneReceived and oldCount > 0 and #groups == 0 and pendingMainCount() == 0 then
    for _, inst in pairs(old) do doom(inst, false) end
    old, oldCount, oldFrames, lossy = {}, 0, {}, {}
  end

  -- 6. deletes; then the materials no object uses any more (never before their last object).
  --    Not in a frame that imported a tile (unless under pressure): a textured import (~3 ms) plus
  --    its material and a few deletes would make a frame more than 4 ms long.
  -- spares past SPARE_S, or beyond SPARE_MAX, join the delete queue
  while sH <= sT do
    local q = sH
    local k = spareQ[q]
    local sp = spares[k]
    if sp and sp.q == q and nSpares <= M.SPARE_MAX and clock - sp.t < M.SPARE_S then break end
    spareQ[q] = nil; sH = q + 1
    if sp and sp.q == q then dropSpare(k) end
  end
  local pressure = #doomed > M.DELETE_PRESSURE
  if (nQueued == 0 and not imported) or pressure or (player ~= nil and not imported) then
    local maxDel = (player == nil or (nQueued == 0 and not imported)) and huge
      or (pressure and (M.DELETES_WHILE_QUEUED * 2) or M.DELETES_WHILE_QUEUED)
    local nDel = 0
    while #doomed > 0 and nDel < maxDel and gov:try('delete') do
      local t1 = now()
      local id = table.remove(doomed)
      deleteById(id)
      doomedAnchor[id], doomedSession[id], doomedFolder[id] = nil, nil, nil
      local mat = matOf[id]
      if mat then
        matOf[id] = nil
        satmat.unref(mat)
      end
      gov:charge('delete', now() - t1)
      units = units + 1
      nDel = nDel + 1
    end
    while satmat.hasDeletes() and (nQueued == 0 or nDel < maxDel) and gov:try('matDelete') do
      local t1 = now()
      satmat.deleteNext()
      gov:charge('matDelete', now() - t1)
      units = units + 1
      nDel = nDel + 1
    end
  end
  lastFrame.units, lastFrame.moving = units, nMoving
  if player then updateUnder(player) end
end

-- The tile level under the player at the last update (nil: none), whether it is a previous
-- anchor's tile, and whether that frame was an exact miss (mesh mode).
function M.exactState() return under.level, under.old, under.miss end

-- The exact (physics lattice) level of a tile grid: the level whose tile is `cells` lattice squares
-- wide (32 cells: L-2 = 64 m; 64 cells: L-1 = 128 m).
function M.exactLevelFor(cells)
  cells = tonumber(cells) or 32
  return floor(ln(cells * lattice.SQUARE / lattice.L0) / ln(2) + 0.5)
end

-- The exact (physics lattice) level: -2 for the v1.3.1 32-cell layout (default), -1 for 64 cells (M1).
function M.setExactLevel(level)
  exactLevel = tonumber(level) or M.EXACT_LEVEL
  return exactLevel
end

-- Delete everything right away (extension unload / level end).
function M.clear()
  finishAll() -- tiles sinking in a crossover are in no set but the delete queue afterwards
  for _, set in ipairs({ live, old, waitingInsts }) do
    for _, inst in pairs(set) do if inst.id then deleteById(inst.id) end end
  end
  for _, id in ipairs(doomed) do deleteById(id) end
  for _, sp in pairs(spares) do deleteById(sp.id) end
  M.forget() -- deletes the per-tile materials, now that no tile uses them
end

-- Forget object ids without touching the scene (the level already deleted them). Per-tile
-- materials are not level objects: they are deleted here (no tile uses them any more).
function M.forget()
  for id in pairs(objSlot) do -- their objects went with the level
    local o = objSlot[id]
    slotsGone[#slotsGone + 1] = { o.n, o.serial, o.session }
  end
  for _, set in ipairs({ waitingInsts, live, old }) do
    for _, inst in pairs(set) do if not inst.id then instGone(inst) end end
  end
  objSlot = {}
  pending, waitingInsts, live, old, oldCount, lossy, doomed, moving = {}, {}, {}, {}, 0, {}, {}, {}
  oldFrames, under.level, under.old, under.miss = {}, nil, false, false
  mainQ, mH, mT, preQ, pH, pT, nQueued = {}, 1, 0, {}, 1, 0, 0
  groups, activeGroups, removals, rH, rT = {}, 0, {}, 1, 0
  anchorId, doneReceived, doneSeq, block, session = nil, false, nil, nil, nil
  matOf, doomedAnchor, doomedSession, doomedFolder = {}, {}, {}, {}
  spares, spareQ, sH, sT, nSpares = {}, {}, 1, 0, 0
  slotHolder, holderPath = {}, {}
  satmat.clearAll()
  materialChecked = false
  if watcher then watcher.onReset() end
end

-- Previous anchors whose tile objects still exist here (shown as old tiles, or waiting in the
-- delete queue), sorted. Sent in every pose: the server keeps those anchors' shape and texture
-- files until they are gone. A file deleted under a live TSStatic makes the engine's file watcher
-- reload it ("Could not open '...far_<a>_....cdae'", seen in-game after a rebase whose reload
-- outlasted the server's fixed 30 s prune delay).
-- Only the current session's anchors: another session's numbers would name the new server's anchors.
function M.heldAnchors()
  local set = {}
  for _, inst in pairs(old) do
    if inst.anchor ~= nil and inst.anchor ~= anchorId and inst.session == session then set[inst.anchor] = true end
  end
  for id, a in pairs(doomedAnchor) do
    if a ~= anchorId and (doomedSession[id] or nil) == session then set[a] = true end
  end
  local out = {}
  for a in pairs(set) do out[#out + 1] = a end
  table.sort(out)
  return out
end

-- Session folders whose files objects here still use, sorted: the shapes of every tile object
-- (shown, kept, loaded hidden or waiting for deletion) and the textures of the imagery materials
-- (satmat, until each is deleted). Sent in hello and every pose (docs/ROADMAP.md "Robustness" 3): a
-- restarted server keeps these folders, and deletes one once no pose names it any more.
function M.heldSessions()
  local set = {}
  for _, tbl in ipairs({ waitingInsts, live, old }) do
    for _, inst in pairs(tbl) do
      local f = inst.id and inst.shape and folderOfMemo(inst.shape)
      if f then set[f] = true end
    end
  end
  for _, f in pairs(doomedFolder) do if f then set[f] = true end end
  for _, sp in pairs(spares) do if sp.folder then set[sp.folder] = true end end
  satmat.eachTexture(function(tex)
    local f = tex and folderOfMemo(tex)
    if f then set[f] = true end
  end)
  local out = {}
  for f in pairs(set) do out[#out + 1] = f end
  table.sort(out)
  return out
end

-- The server session of the current anchor (switchAnchor's newSession), or nil.
function M.session() return session end

function M.stats()
  local n, covered, hiddenLoaded = 0, 0, 0
  for _, inst in pairs(live) do
    n = n + 1
    if inst.cover ~= 'none' then covered = covered + 1 end
  end
  for _, inst in pairs(waitingInsts) do if inst.loaded then hiddenLoaded = hiddenLoaded + 1 end end
  local waiting = 0
  for _, g in ipairs(groups) do if g.state == 'wait' then waiting = waiting + 1 end end
  return { live = n, covered = covered, preloaded = hiddenLoaded, queued = nQueued, groups = waiting,
    transitions = activeGroups, old = oldCount, deleting = #doomed, done = doneReceived, spares = nSpares,
    imports = counters.imports, failed = counters.failed, crossovers = counters.crossovers,
    instant = counters.instant, pops = counters.pops, capWaits = counters.capWaits, frameUnits = lastFrame.units,
    moving = lastFrame.moving, materials = satmat.count(), materialsCreated = counters.materials,
    textured = counters.textured, oldCovered = counters.oldCovered, nearVisual = nearVisual, rawView = rawView,
    waitingGroups = waiting, exactLevel = exactLevel, underLevel = under.level, underOld = under.old,
    exactMiss = counters.exactMiss, exactFrames = counters.exactFrames, retex = counters.retex,
    retexStale = counters.retexStale, retexFailed = counters.retexFailed }
end

------------------------------------------------------------------------------------------
-- dev tools (dev.lua): see the module header

function M.setWatcher(w) watcher = w end
function M.setFrozen(on) frozen = on and true or false; return frozen end
function M.isFrozen() return frozen end
function M.anchorId() return anchorId end
function M.counters() return counters end -- read-only

-- Every tile instance with an object (pending, live and previous anchors'; not the delete queue).
function M.eachTile(fn)
  for _, set in ipairs({ waitingInsts, live, old }) do
    for _, inst in pairs(set) do if inst.id and not inst.doomed then fn(inst) end end
  end
end

-- The instances still to import, in queue order: group tiles, then prefetch tiles (peekImport
-- picks among the group tiles, nearest the camera first in mesh mode).
function M.queuedTiles()
  local main, pre = {}, {}
  for i = mH, mT do
    local inst = mainQ[i]
    if inst and waitingInsts[inst] and not inst.doomed and inst.queued and not inst.prefetch then main[#main + 1] = inst end
  end
  for i = pH, pT do
    local inst = preQ[i]
    if inst and waitingInsts[inst] and not inst.doomed and inst.queued and inst.prefetch then pre[#pre + 1] = inst end
  end
  return main, pre
end

-- for tests
function M._tile(key) return live[key] end
function M._pending(key)
  if pending[key] then return pending[key] end
  for _, g in ipairs(groups) do
    if g.state == 'wait' and g.insts and g.insts[key] then return g.insts[key] end
  end
end
function M._oldId(a, key, s) local inst = old[oldKey(s or session, a, key)]; return inst and inst.id end
function M._old(a, key, s) return old[oldKey(s or session, a, key)] end
function M._clock() return clock end

return M
