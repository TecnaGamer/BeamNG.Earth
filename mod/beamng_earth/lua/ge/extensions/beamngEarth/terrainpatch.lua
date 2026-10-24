-- Ground under other vehicles' collision islands off the terrain block (BeamMP support, the user, 2026-10-22: "As cars
-- spread apart they can have their own island of collision"). The engine collides with one TerrainBlock only, and the
-- block follows the active car, so a car far from it had no ground. For each island of collisionfoci.lua that is not
-- the active car's and does not lie well inside the block, the server builds a collision-only shape of the same ground
-- (server terrainpatch.py, 'terrain_patch'); it goes in as a TSStatic that draws nothing, and out KEEP_S after no
-- island wants it.
local M = {}

-- On (off for a while on 2026-10-23): a patch comes in PIECE_M pieces, one small collision mesh each. As one mesh of up
-- to ~200k triangles (a 4 m grid) the Windows game built it in time growing with about the square of its triangles:
-- in a Windows player's BeamMP session (9 players) every collision rebuild with patches in took 16-26 s; on a Windows
-- test PC one 51k-triangle patch 3.81 s, the same ground as 16 meshes 0.09 s (2026-10-24). Linux builds either fast.
M.ENABLED = true
M.MARGIN_M = 64         -- an island whose box lies this far inside the block needs no patch
M.SNAP_M = 128          -- as the server's terrainpatch.SNAP_M / MAX_SIDE_M: the keys match its boxes
M.PIECE_M = 128         -- as the server's terrainpatch.PIECE_M: each cell's side, at its centre
-- The ground under a far car reaches PATCH_R round it (not its whole island: the roads, buildings and trees reach
-- collisionfoci's 320 m, the ground needs only where the car gets before the next patch; the whole island's 704 m
-- box made a Steam Deck keep 977 cells as another player drove 4 km, 2026-10-24).
M.PATCH_R = 256
M.MAX_SIDE_M = 1280
M.KEEP_S = 10
M.MAX_INFLIGHT = 2
M.RETRY_S = 5
M.UPDATE_S = 1
-- A patch's pieces are world cells (the server names them by anchor and 64 m cell): one TSStatic per cell, shared by
-- every patch over it and kept until the last lets go, so a far car moving on adds a row of cells, not a new patch's
-- shape files (every distinct shape path a session loads costs frame time for good, teleport-degradation). The
-- cells are made PIECES_PER_FRAME a frame: all 121 of a 704 m patch in one frame held a Windows test PC 1.0 s
-- (2026-10-24, ~8 ms each, the shape's first load).
M.PIECES_PER_FRAME = 2
-- A cell the terrain block covers is not made (and goes when the block moves over it): the block is that ground
-- already, and the cell (an 8 m grid) stood a little above it in places, an invisible bump under the player's own car
-- and other cars standing on it above the visible ground (the user, 2026-10-24, driving back past two BeamMP players:
-- "it looked like they were floating ... the collision terrain was a little broken"). An island 130 m or more off
-- the block's centre already wants a patch (its 320 m box reaches past MARGIN_M), so patches overlapped the block
-- all the time.
M.COVER_MARGIN = 2

local patches = {}      -- key -> { box, cells (paths), pending, wantedAt, retryAt }
local cells = {}        -- path -> { path, x, y, refs, id (its TSStatic, once made) }
local queue = {}        -- cells to make, in order
local anchorId = nil
local lastBlock = nil   -- the terrain block's box {x0, y0, x1, y1} at the last update (groundReady)
local since = math.huge
local clock = 0

-- Pure: the island box snapped as the server does (terrainpatch.snap_box): clipped to MAX_SIDE_M round its centre,
-- out to SNAP_M. x0, y0, x1, y1
function M.snap(x0, y0, x1, y1)
  local cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
  local hw, hh = math.min((x1 - x0) / 2, M.MAX_SIDE_M / 2), math.min((y1 - y0) / 2, M.MAX_SIDE_M / 2)
  return math.floor((cx - hw) / M.SNAP_M) * M.SNAP_M, math.floor((cy - hh) / M.SNAP_M) * M.SNAP_M,
    math.ceil((cx + hw) / M.SNAP_M) * M.SNAP_M, math.ceil((cy + hh) / M.SNAP_M) * M.SNAP_M
end

-- Pure: key -> box of the patches wanted: islands (collisionfoci.islands with the active car as its first member,
-- marked active) that are not the active car's and not well inside the block box b {x0, y0, x1, y1} (nil: none).
function M.wanted(islands, b)
  local out = {}
  for _, isl in ipairs(islands) do
    local hasActive = false
    for _, m in ipairs(isl.members) do if m.active then hasActive = true end end
    local inside = b and isl.x0 >= b[1] + M.MARGIN_M and isl.y0 >= b[2] + M.MARGIN_M
      and isl.x1 <= b[3] - M.MARGIN_M and isl.y1 <= b[4] - M.MARGIN_M
    if not hasActive and not inside then
      local bx0, by0, bx1, by1 = math.huge, math.huge, -math.huge, -math.huge
      for _, m in ipairs(isl.members) do
        bx0, by0 = math.min(bx0, m.x - M.PATCH_R), math.min(by0, m.y - M.PATCH_R)
        bx1, by1 = math.max(bx1, m.x + M.PATCH_R), math.max(by1, m.y + M.PATCH_R)
      end
      if bx0 > bx1 then bx0, by0, bx1, by1 = isl.x0, isl.y0, isl.x1, isl.y1 end
      local x0, y0, x1, y1 = M.snap(bx0, by0, bx1, by1)
      local key = (string.format('%d_%d_%d_%d', x0, y0, x1, y1):gsub('-', 'm'))
      out[key] = { x0, y0, x1, y1 }
    end
  end
  return out
end

local function requestCollision()
  local ok, roads = pcall(require, 'beamngEarth/roads')
  if ok and roads.requestCollision then roads.requestCollision() end
end

-- Collision rebuilds so far (roads.reloads); nil: not counted (a cell is then taken as solid once made)
local function reloadCount()
  local ok, roads = pcall(require, 'beamngEarth/roads')
  return ok and roads.reloads and roads.reloads() or nil
end

local function makeCell(c)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, c.path)
  o:setField('collisionType', 0, 'Collision Mesh')
  o:setField('decalType', 0, 'None')
  o:setField('dynamic', 0, 'true')        -- (no redraw of the cached sun shadow map: farfield.lua spawn)
  o.canSave = false
  o:setPosition(vec3(c.x, c.y, 0))
  o:registerObject('')
  scenetree.MissionGroup:addObject(o)
  c.id = o:getID()
  c.reloadsAtMake = reloadCount()
end

-- A patch lets go of its cells: a cell no patch holds any more is deleted (or never made, if still queued).
local function deletePatch(p)
  local gone = false
  for _, path in ipairs(p.cells or {}) do
    local c = cells[path]
    if c then
      c.refs = c.refs - 1
      if c.refs <= 0 then
        if c.id then
          local o = scenetree.findObjectById(c.id)
          if o then o:delete() end
          gone = true
        end
        c.dropped = true
        cells[path] = nil
      end
    end
  end
  p.cells = nil
  if gone then requestCollision() end
end

-- Everything out (an anchor change: the old frame's patches are in the wrong place; the level's end).
function M.clear()
  for _, p in pairs(patches) do deletePatch(p) end
  patches, cells, queue = {}, {}, {}
end

-- The queued cells made, PIECES_PER_FRAME a frame; the collision rebuilt once the queue is empty.
-- Pure: does the block box b {x0, y0, x1, y1} cover cell c's whole square (COVER_MARGIN in)?
function M.covered(c, b)
  if not b then return false end
  local h, m = M.PIECE_M / 2, M.COVER_MARGIN
  return c.x - h >= b[1] + m and c.y - h >= b[2] + m and c.x + h <= b[3] - m and c.y + h <= b[4] - m
end

local function makeQueued()
  local made = 0
  while made < M.PIECES_PER_FRAME and #queue > 0 do
    local c = table.remove(queue, 1)
    c.queued = nil
    if not c.dropped and not c.id and not M.covered(c, lastBlock) then
      makeCell(c)
      made = made + 1
    end
  end
  if made > 0 and #queue == 0 then requestCollision() end
end

-- Each frame (core.lua): aid the active anchor, foci (collisionfoci.last), active { x, y } (the active car), block the
-- terrain block's box {x0, y0, x1, y1} or nil; send(msg) to the server.
function M.update(dt, aid, foci, active, block, send)
  lastBlock = block
  if not M.ENABLED then
    if next(patches) then M.clear() end
    return
  end
  clock = clock + (dt or 0)
  since = since + (dt or 0)
  if #queue > 0 then makeQueued() end
  if since < M.UPDATE_S then return end
  since = 0
  if aid ~= anchorId then M.clear(); anchorId = aid end
  if not aid then return end
  -- the block moved: cells it now covers go, cells it left are made again
  local gone = false
  for _, c in pairs(cells) do
    if M.covered(c, block) then
      if c.id then
        local o = scenetree.findObjectById(c.id)
        if o then o:delete() end
        c.id, c.reloadsAtMake, gone = nil, nil, true
      end
    elseif not c.id and not c.queued then
      c.queued = true
      queue[#queue + 1] = c
    end
  end
  if gone then requestCollision() end
  local cf = require('beamngEarth/collisionfoci')
  local act = active and { x = active.x, y = active.y, r = cf.PLAYER_R, active = true } or nil
  local want = M.wanted(cf.islands(foci or {}, act), block)
  local inflight = 0
  for _, p in pairs(patches) do if p.pending then inflight = inflight + 1 end end
  for key, box in pairs(want) do
    local p = patches[key]
    if not p then p = { box = box }; patches[key] = p end
    p.wantedAt = clock
    if not p.cells and not p.pending and inflight < M.MAX_INFLIGHT and clock >= (p.retryAt or 0) then
      p.pending = true
      inflight = inflight + 1
      send({ type = 'terrain_patch', anchorId = aid, key = key, x0 = box[1], y0 = box[2], x1 = box[3], y1 = box[4] })
    end
  end
  for key, p in pairs(patches) do
    if not want[key] and clock - (p.wantedAt or 0) > M.KEEP_S then
      deletePatch(p)
      patches[key] = nil
    end
  end
end

-- The server's 'terrain_patch': { anchorId, key, parts = { { path, x, y } }, tris } (its cells) or
-- { anchorId, key, error }. Cells already made for another patch are shared; new ones are queued (makeQueued).
function M.onPatch(msg)
  local p = patches[msg.key]
  if not p or msg.anchorId ~= anchorId then return end
  p.pending = false
  if type(msg.parts) ~= 'table' or #msg.parts == 0 then
    p.retryAt = clock + M.RETRY_S
    return
  end
  local old = p.cells and { cells = p.cells } or nil
  p.cells = {}
  for _, part in ipairs(msg.parts) do
    if type(part) == 'table' and type(part.path) == 'string' then
      local c = cells[part.path]
      if not c then
        c = { path = part.path, x = tonumber(part.x) or 0, y = tonumber(part.y) or 0, refs = 0 }
        cells[part.path] = c
        c.queued = true
        queue[#queue + 1] = c
      end
      c.refs = c.refs + 1
      p.cells[#p.cells + 1] = part.path
    end
  end
  if old then deletePatch(old) end
end

-- Is there solid ground at (x, y) (the active frame)? On the terrain block (MARGIN_M inside its edge), or under a cell
-- made before a collision rebuild (roads.reloads: a cell collides from the rebuild after it was made). A remote car
-- coming back from far away waits for it (mpsession.lua: switched on over no ground, it fell, the ground came in
-- around it and its physics blew up; a Windows test PC, 2026-10-24). With the patches off always true: no ground will
-- come, the car goes on as before.
function M.groundReady(x, y)
  if not M.ENABLED then return true end
  local b = lastBlock
  local m = M.COVER_MARGIN
  if b and x >= b[1] + m and y >= b[2] + m and x <= b[3] - m and y <= b[4] - m then
    return true
  end
  local h, n = M.PIECE_M / 2, reloadCount()
  for _, c in pairs(cells) do
    if c.id and math.abs(x - c.x) <= h and math.abs(y - c.y) <= h then
      return n == nil or c.reloadsAtMake == nil or n > c.reloadsAtMake
    end
  end
  return false
end

-- The terrain block's box {x0, y0, x1, y1} at the last update, or nil (mpsession: other players' cars float off it)
function M.block() return lastBlock end

-- (tests, the dev panel) key -> { box, cells, pending }; path -> cell; the queue
function M.state() return patches, cells, queue end

return M
