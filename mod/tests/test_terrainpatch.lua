-- luajit mod/tests/test_terrainpatch.lua  (ground under other vehicles' collision islands, terrainpatch.lua): the boxes
-- snapped as the server's terrainpatch.snap_box, the islands that need a patch (not the active car's, not well inside the
-- terrain block), and update asking the server for them (at most MAX_INFLIGHT at a time) and letting them go.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
-- roads.lua's collision rebuilds (counted: a cell collides from the rebuild after it was made, groundReady)
local rebuilds = 0
package.loaded['beamngEarth/roads'] = { requestCollision = function() end, reloads = function() return rebuilds end }
local tp = require('beamngEarth/terrainpatch')
local cf = require('beamngEarth/collisionfoci')

-- on (patches come in small pieces; ENABLED false turns them off and asks for nothing)
T.check(tp.ENABLED == true, 'patches on by default')
do
  local sent = {}
  tp.ENABLED = false
  tp.update(5, 1, { { x = 5000, y = 0, r = 320 } }, { x = 0, y = 0 }, { -512, -512, 510, 510 },
    function(m) sent[#sent + 1] = m end)
  T.check(#sent == 0, 'off: no patch asked of the server')
  tp.ENABLED = true
end

-- snap: out to the 128 m grid, clipped to MAX_SIDE_M round the centre (as the server)
local x0, y0, x1, y1 = tp.snap(10, -70, 650, 570)
T.check(x0 == 0 and y0 == -128 and x1 == 768 and y1 == 640, 'snapped out: ' .. table.concat({ x0, y0, x1, y1 }, ' '))
x0, y0, x1, y1 = tp.snap(0, 0, 4000, 100)
T.check(x1 - x0 <= tp.MAX_SIDE_M + 2 * tp.SNAP_M and x1 - x0 >= tp.MAX_SIDE_M, 'clipped to the max side (then snapped out)')

-- wanted: the active car's island never; an island well inside the block never; a far one yes
local block = { -512, -512, 512, 512 }
local foci = cf.compute({ { id = 1, x = 0, y = 0, kind = 'player' }, { id = 2, x = 2000, y = 0, kind = 'player' },
  { id = 3, x = 200, y = 100, kind = 'traffic' } }, 1, 0, 0)
local isl = cf.islands(foci, { x = 0, y = 0, r = cf.PLAYER_R, active = true })
local w = tp.wanted(isl, block)
local keys = {}
for k in pairs(w) do keys[#keys + 1] = k end
T.check(#keys == 1, 'one patch (the far car): ' .. table.concat(keys, ','))
local box = w[keys[1]]
T.check(box[1] <= 2000 - tp.PATCH_R and box[3] >= 2000 + tp.PATCH_R and box[3] - box[1] <= 2 * tp.PATCH_R + 2 * tp.SNAP_M,
  'the patch covers PATCH_R round the far car (not its whole island)')
T.check(not keys[1]:find('-', 1, true), 'keys carry no minus sign (the server\'s key pattern)')
-- a lone traffic car well inside the block: none
local inside = cf.islands(cf.compute({ { id = 3, x = 200, y = 100, kind = 'traffic' } }, nil), nil)
T.check(next(tp.wanted(inside, block)) == nil, 'well inside the block: no patch')
-- no block (the ground not in yet): every island off the active car's
T.check(next(tp.wanted(isl, nil)) ~= nil, 'no block: the far car still gets one')

-- update: asks the server, at most MAX_INFLIGHT at a time; a reply's error retries later; unwanted ones go
local sent = {}
local send = function(m) sent[#sent + 1] = m end
local many = {}
for i = 1, 5 do many[#many + 1] = { id = 10 + i, x = i * 3000, y = 0, kind = 'player' } end
many[#many + 1] = { id = 1, x = 0, y = 0, kind = 'player' }
local f = cf.compute(many, 1, 0, 0)
tp.update(10, 7, f, { x = 0, y = 0 }, block, send)
T.check(#sent == tp.MAX_INFLIGHT, 'at most MAX_INFLIGHT asked at once: ' .. #sent)
T.check(sent[1].type == 'terrain_patch' and sent[1].anchorId == 7 and sent[1].x1 > sent[1].x0, 'the request\'s fields')
tp.onPatch({ anchorId = 7, key = sent[1].key, error = 'offline' })
tp.update(1.5, 7, f, { x = 0, y = 0 }, block, send)
T.check(#sent == tp.MAX_INFLIGHT + 1, 'one slot freed by the error: one more asked: ' .. #sent)
local errored = tp.state()[sent[1].key]
T.check(errored and not errored.pending and errored.retryAt, 'the failed one waits before asking again')
-- a reply's cells: queued, made PIECES_PER_FRAME a frame, one collision-only TSStatic each at its centre; a second
-- patch over a shared cell adds no object
local pk, pk2 = sent[2].key, sent[3] and sent[3].key
local parts = {}
for i = 1, 5 do parts[i] = { path = '/temp/patch_7_' .. i .. '_0.cdae', x = i * 64 - 32, y = 2032 } end
tp.onPatch({ anchorId = 7, key = pk, tris = 2560, parts = parts })
local _, cellsNow, queueNow = tp.state()
T.check(#queueNow == 5, 'cells queued, none made in the message handler: ' .. #queueNow)
tp.update(0.01, 7, f, { x = 0, y = 0 }, block, send)
_, cellsNow, queueNow = tp.state()
T.check(#queueNow == 5 - tp.PIECES_PER_FRAME, 'PIECES_PER_FRAME made a frame')
for _ = 1, 3 do tp.update(0.01, 7, f, { x = 0, y = 0 }, block, send) end
_, cellsNow, queueNow = tp.state()
local c1 = cellsNow['/temp/patch_7_1_0.cdae']
local o1 = c1 and c1.id and scenetree.findObjectById(c1.id)
T.check(#queueNow == 0 and o1 and o1.fields.shapeName == '/temp/patch_7_1_0.cdae' and o1.fields.collisionType == 'Collision Mesh'
  and o1.pos.x == 32, 'every cell made, at its centre')
-- groundReady: on the block (MARGIN_M in), or under a cell a collision rebuild came after
T.check(tp.groundReady(0, 0), 'on the block: ground')
tp.update(0.01, 7, f, { x = 0, y = 0 }, nil, send)          -- (no block: only the cells)
T.check(not tp.groundReady(32, 2032), 'a cell made, no rebuild since: not solid yet')
rebuilds = rebuilds + 1
T.check(tp.groundReady(32, 2032) and tp.groundReady(32 + tp.PIECE_M / 2 - 1, 2032), 'after a rebuild: solid under the cell')
T.check(not tp.groundReady(5000, 5000), 'no cell, off the block: no ground')
tp.update(0.01, 7, f, { x = 0, y = 0 }, block, send)
local objs0 = S.calls.createObject_TSStatic or 0
if pk2 then
  tp.onPatch({ anchorId = 7, key = pk2, tris = 1024, parts = { parts[5], { path = '/temp/patch_7_6_0.cdae', x = 352, y = 2032 } } })
  for _ = 1, 3 do tp.update(0.01, 7, f, { x = 0, y = 0 }, block, send) end
  T.check((S.calls.createObject_TSStatic or 0) - objs0 == 1 and cellsNow['/temp/patch_7_5_0.cdae'].refs == 2,
    'a second patch: the shared cell reused, only the new one made')
end
local pieceIds = { c1.id }
-- all far cars gone: their patches go after KEEP_S
tp.update(1.5, 7, {}, { x = 0, y = 0 }, block, send)
T.check(next((tp.state())) ~= nil, 'kept a while')
tp.update(tp.KEEP_S + 2, 7, {}, { x = 0, y = 0 }, block, send)
T.check(next((tp.state())) == nil, 'let go after KEEP_S')
local _, cellsLeft = tp.state()
T.check(#pieceIds == 1 and not scenetree.findObjectById(pieceIds[1]) and next(cellsLeft) == nil,
  'let go: every cell deleted')
-- an anchor change clears everything
tp.update(10, 7, f, { x = 0, y = 0 }, block, send)
tp.update(10, 8, {}, { x = 0, y = 0 }, block, send)
T.check(next((tp.state())) == nil, 'anchor change: cleared')

-- cells the block covers are not made; the block moving over a made cell deletes it, moving off makes it again
do
  tp.clear()
  local sent2 = {}
  local function send2(m) sent2[#sent2 + 1] = m end
  local far = cf.compute({ { id = 1, x = 0, y = 0, kind = 'player' }, { id = 2, x = 3000, y = 0, kind = 'player' } }, 1, 0, 0)
  local blk = { -512, -512, 512, 512 }
  tp.update(10, 9, far, { x = 0, y = 0 }, blk, send2)
  local key = sent2[1] and sent2[1].key
  tp.onPatch({ anchorId = 9, key = key, tris = 512, parts = { { path = '/temp/p9_in.cdae', x = 64, y = 64 },
    { path = '/temp/p9_out.cdae', x = 3000, y = 64 } } })
  for _ = 1, 3 do tp.update(0.01, 9, far, { x = 0, y = 0 }, blk, send2) end
  local _, cs = tp.state()
  T.check(cs['/temp/p9_out.cdae'].id and not cs['/temp/p9_in.cdae'].id, 'a cell under the block not made, one off it made')
  local moved = { 2500, -512, 3524, 512 }       -- the block over the far cell
  tp.update(1.5, 9, far, { x = 3000, y = 0 }, moved, send2)
  T.check(not cs['/temp/p9_out.cdae'].id, 'the block moved over a made cell: deleted')
  tp.update(1.5, 9, far, { x = 0, y = 0 }, blk, send2)
  for _ = 1, 3 do tp.update(0.01, 9, far, { x = 0, y = 0 }, blk, send2) end
  T.check(cs['/temp/p9_out.cdae'].id ~= nil and not cs['/temp/p9_in.cdae'].id, 'the block moved off: made again')
  tp.clear()
end

T.done('test_terrainpatch')
