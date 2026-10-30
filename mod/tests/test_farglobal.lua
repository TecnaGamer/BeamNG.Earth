-- luajit mod/tests/test_farglobal.lua  (global far tiles, geotiles phase 3b: server farfield.select_global)
-- A global tile ('m<z>_<x>_<y>', far_tile.frame / pos {0, 0, pivot} / poly) is placed from its own frame into the
-- anchor's world (geotile.placement), survives a rebase and a teleport in `live` placed again into the new anchor
-- (one transform each, no `old`, no re-import), goes to `old` when the server session changes, never names an anchor
-- in heldAnchors, and is removed by a far_remove of the current anchor; a piece's footprint is its poly's AABB.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local farfield = require('beamngEarth/farfield')
farfield.SPARE_MAX = 0
local geodesy = require('beamngEarth/geodesy')
local geotile = require('beamngEarth/geotile')

local free = { clock = function() return 0 end }
function free:try() return true end
function free:charge() end
function free:chargeMotion() end
local function step(n) for _ = 1, n or 3 do farfield.update(free, 1 / 60, nil) end end

local LAT, LON = 35.46, -118.77
local a1 = geodesy.newAnchor(LAT, LON - 15000 / (111320 * math.cos(math.rad(LAT))))
local a2 = geodesy.newAnchor(LAT, LON)
local a3 = geodesy.newAnchor(LAT + 0.05, LON + 0.02)

-- a z12 tile ~30 km north-east of the place
local z = 12
local gx, gy = geotile.tileOf(LAT + 0.25, LON + 0.2, z)
local gkey = 'm' .. z .. '_' .. gx .. '_' .. gy
local cl, cn = geotile.centre(z, gx, gy)
local PZ = 812
local function gmsg(a, aid)
  local q = geotile.quad(a, z, gx, gy)
  return { type = 'far_tile', anchorId = aid, key = gkey, level = 5, shape = '/temp/beamngEarth/t/slot_1.cdae',
    pos = { 0, 0, PZ }, err = 1.5, frame = { cl, cn }, state = 'group',
    poly = { q[1][1], q[1][2], q[2][1], q[2][2], q[3][1], q[3][2], q[4][1], q[4][2] } }
end
local function placed(inst, a)
  local pl = geotile.placement(geodesy.newAnchor(cl, cn), a, 0, 0, PZ)
  local x, y, zz = geotile.apply(pl, 0, 0, PZ)
  return math.abs(inst.x - x) < 1e-6 and math.abs(inst.y - y) < 1e-6 and math.abs(inst.z - zz) < 1e-6
end

farfield.setNearVisual('mesh')
farfield.switchAnchor(1, false, { cx = 0, cy = 0 }, 'sA', a1)
farfield.onTile(gmsg(a1, 1))
farfield.onSwap({ type = 'far_swap', anchorId = 1, id = 1, add = { gkey }, remove = {} })
step()
local g = farfield._tile(gkey)
T.check(g ~= nil and g.shown and g.global ~= nil, 'the global tile is shown')
T.check(g.anchor == farfield.GLOBAL_AID, 'its instance belongs to no anchor')
T.check(placed(g, a1), 'placed from its frame into anchor 1 (geotile.placement at its pivot)')
local o = S.objects[g.id]
T.check(o ~= nil and o.rot ~= nil, 'spawned with its placement rotation (setPosRot)')
local q1 = geotile.quad(a1, z, gx, gy)
T.check(math.abs(g.x0 - math.min(q1[1][1], q1[4][1])) < 1e-6, 'footprint: its quad in anchor 1')
local imports = farfield.counters().imports

-- the rebase: transform (placed exactly into the new anchor) and switchAnchor keep it live
farfield.transform(a1:transformTo(a2), a2)
farfield.switchAnchor(2, true, { cx = 0, cy = 0 }, 'sA', a2)
step()
local g2 = farfield._tile(gkey)
T.check(g2 == g and g2.shown and not g2.doomed, 'the same instance stays live across the rebase')
T.check(farfield._old(1, gkey) == nil, 'not a previous anchor tile')
T.check(placed(g2, a2), 'placed exactly into anchor 2 (not moved with the anchors\' rigid transform)')
T.check(farfield.counters().imports == imports, 'no import again')
local held = farfield.heldAnchors()
T.check(#held == 0, 'heldAnchors names no anchor for it')
local q2 = geotile.quad(a2, z, gx, gy)
T.check(math.abs(g2.y1 - math.max(q2[3][2], q2[4][2])) < 1e-6, 'footprint: its quad in anchor 2')

-- a teleport keeps it too (the server keeps it shown), placed into the new anchor
farfield.switchAnchor(3, false, { cx = 0, cy = 0 }, 'sA', a3)
step()
T.check(farfield._tile(gkey) == g and placed(g, a3), 'kept and placed into anchor 3 at a teleport')

-- a far_remove of the current anchor takes it
farfield.onRemove({ type = 'far_remove', anchorId = 3, key = gkey })
step(5)
T.check(farfield._tile(gkey) == nil, 'removed by the current anchor\'s far_remove')

-- another server session: a previous anchor's tile
farfield.onTile(gmsg(a3, 3))
farfield.onSwap({ type = 'far_swap', anchorId = 3, id = 2, add = { gkey }, remove = {} })
step()
T.check(farfield._tile(gkey) ~= nil, 'shown again')
farfield.transform(a3:transformTo(a2), a2)
farfield.switchAnchor(1, true, { cx = 0, cy = 0 }, 'sB', a2)
T.check(farfield._tile(gkey) == nil and farfield._old(3, gkey, 'sA') ~= nil, 'a new server session: an old tile')

-- a global tile still loading at a rebase (sent, not swapped in) stays, placed into the new anchor; its swap comes
-- under the new anchor (the server keeps it sent): not received or imported again
farfield.switchAnchor(6, false, { cx = 0, cy = 0 }, 'sB', a1)
farfield.onTile(gmsg(a1, 6))
step()
local pend = farfield._pending(gkey)
T.check(pend ~= nil and pend.loaded and not pend.shown, 'loaded, waiting for its swap')
local imp = farfield.counters().imports
farfield.transform(a1:transformTo(a2), a2)
farfield.switchAnchor(7, true, { cx = 0, cy = 0 }, 'sB', a2)
T.check(farfield._pending(gkey) == pend and not pend.doomed, 'still loading after the rebase')
T.check(placed(pend, a2), 'placed into the new anchor')
farfield.onSwap({ type = 'far_swap', anchorId = 7, id = 9, add = { gkey }, remove = {} })
step()
T.check(farfield._tile(gkey) == pend and pend.shown, 'its swap under the new anchor shows it')
T.check(farfield.counters().imports == imp, 'imported once')
farfield.onRemove({ type = 'far_remove', anchorId = 7, key = gkey })
step(5)

-- a piece: its footprint is its polygon's AABB
farfield.switchAnchor(5, false, { cx = 0, cy = 0 }, 'sB', a2)
farfield.onTile({ type = 'far_tile', anchorId = 5, key = '2_3_4_c14_100_200', level = 2,
  shape = '/temp/beamngEarth/t/slot_2.cdae', pos = { 3072, 4096, 50 }, err = 0.5, state = 'group',
  poly = { 3072, 4096, 3500.5, 4096, 3480.25, 5120, 3072, 5120 } })
local p = farfield._pending('2_3_4_c14_100_200')
T.check(p and p.x1 == 3500.5 and p.y1 == 5120 and not p.global, 'a piece: footprint from its poly, an anchor tile')

T.done('farglobal')
