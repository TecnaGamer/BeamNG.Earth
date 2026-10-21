-- luajit mod/tests/test_farfield.lua  (far field, V1_2_DESIGN 2.3, 2.5, 2.8, 4.4, 5.4; V1_3_SPEC C-E)
-- Hidden preload, swap groups (no removal before the replacement is loaded), C2 crossover timing,
-- C1 off-screen swaps, crossovers at any distance in view, the transition cap (near in-view groups
-- wait for a slot), covered-L0 sink/hide and onBlockCommit, commit gating, governor budget (no
-- imports around a commit), rebase transform, prefetch, a randomized no-hole simulation against a
-- scripted server; then (v1.3) mesh near-visual mode and the per-tile imagery material lifecycle.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local farfield = require('beamngEarth/farfield')
farfield.SPARE_MAX = 0 -- these tests check the delete pipeline; the spare pool is test_farfield_spares.lua
local governor = require('beamngEarth/governor')
local geodesy = require('beamngEarth/geodesy')
local crossover = require('beamngEarth/crossover')
crossover.MAX_GROUPS = 24
crossover.INSTANT_M = 0 -- test_farfield tests C2 crossovers down to 126 m; INSTANT_M = 800 is verified in test_crossover.lua

-- fake wall clock: each spawn costs `spawnCost` ms, everything else is free
local now, spawnCost = 0, 1.5
local function clock() return now end
local realCreate = createObject
createObject = function(class) now = now + spawnCost; return realCreate(class) end

-- a governor that allows everything (logic tests), and a real one (budget tests)
local free = { clock = clock }
function free:try() return true end
function free:charge() end
function free:chargeMotion() end

local DT = 1 / 60
local sizeOf = {} -- shape -> tile size (footprints of the stub objects)
local function size(l) return 256 * 2 ^ l end
local function key(l, i, j) return l .. '_' .. i .. '_' .. j end
local function tile(a, l, i, j, opts)
  opts = opts or {}
  local shape = '/temp/beamngEarth/t/far_' .. a .. '_' .. key(l, i, j) .. (opts.dae and '.dae' or '.cdae')
  sizeOf[shape] = size(l)
  return { type = 'far_tile', anchorId = a, key = key(l, i, j), level = l, shape = shape,
    pos = { i * size(l), j * size(l), opts.z or 100 }, err = opts.err or 0.5, sink = opts.sink,
    state = opts.state or 'group' }
end
local function swap(a, id, add, remove, dIn, dOut)
  return { type = 'far_swap', anchorId = a, id = id, add = add, remove = remove or {}, dIn = dIn, dOut = dOut }
end
local function obj(k) local t = farfield._tile(k); return t and S.objects[t.id] end
local function nTS() return #S.liveOf('TSStatic') end
local function visible(o) return o ~= nil and not o.hidden end
local function step(gov, cam, n)
  for _ = 1, n or 1 do farfield.update(gov or free, DT, cam) end
end
local camNorth = { x = 0, y = 0, z = 200, fx = 0, fy = 1, fz = 0, fovDeg = 60 }
local camSouth = { x = 0, y = 0, z = 200, fx = 0, fy = -1, fz = 0, fovDeg = 60 }

------------------------------------------------------------------------------------------
-- first fill (one-sided group, nothing to cross over): instant
farfield.switchAnchor(1, false, { cx = 0, cy = 0 })
T.check(S.calls.loadJsonMaterialsFile == 1, 'static far-tile material loaded before the first tile')
farfield.onTile(tile(1, 1, 0, 8)) -- 512 m tile at (0, 4096), north of the camera
step()
local o = obj('1_0_8')
T.check(o == nil and farfield._pending('1_0_8').loaded, 'loaded, but not live before its swap')
local pend = S.objects[farfield._pending('1_0_8').id]
T.check(pend.hidden == true and pend.fields.collisionType == 'None', 'created hidden (P6), render-only')
T.check(pend.fields.isRenderShadowEnabled == 'false' and pend.fields.castShadows == '0', 'farfield shadows disabled (P8-1)')
T.check(pend.fields.shapeName:match('%.cdae$') ~= nil, '.cdae shape accepted')
farfield.onSwap(swap(1, 1, { '1_0_8' }))
step(free, camNorth)
o = obj('1_0_8')
T.check(visible(o) and o.pos.z == 100, 'first fill: shown at its true height in one frame')

------------------------------------------------------------------------------------------
-- C2: split 1_0_8 into its 4 children, in view
local kids = { key(0, 0, 16), key(0, 1, 16), key(0, 0, 17), key(0, 1, 17) }
farfield.onTile(tile(1, 0, 0, 16, { z = 101 }))
farfield.onTile(tile(1, 0, 1, 16, { z = 101 }))
local gov = governor.new({ clock = clock })
-- budget: no unit before the bucket holds a unit's cost (no "at least one per frame")
gov:beginFrame(true, 11.5)
step(gov, camNorth)
T.check(farfield._pending(kids[1]).loaded == false, 'governor: no import in the first frame (b = 0.05 ms)')
for _ = 1, 30 do gov:beginFrame(true, 11.5); step(gov, camNorth) end
T.check(farfield._pending(kids[1]).loaded and farfield._pending(kids[2]).loaded, 'imports run once the budget allows')
farfield.onSwap(swap(1, 2, kids, { '1_0_8' }, 1.5, 2.5))
step(free, camNorth)
T.check(visible(obj('1_0_8')) and obj(kids[1]) == nil, 'group waits for all its adds (2 of 4 not received)')
farfield.onTile(tile(1, 0, 0, 17, { z = 101 }))
farfield.onTile(tile(1, 0, 1, 17, { z = 101 }))
spawnCost = 1000 -- one import blows the frame: the governor stops after it
gov:beginFrame(true, 11.5)
step(gov, camNorth)
spawnCost = 1.5
T.check(farfield._pending(kids[4]) and not farfield._pending(kids[4]).loaded, 'one add still loading')
T.check(visible(obj('1_0_8')) and obj(kids[1]) == nil, 'no removal before every replacement is loaded')
local parent = obj('1_0_8')
step(free, camNorth) -- last add loads, then the group starts (same frame: loads come after starts, so next)
step(free, camNorth)
T.check(obj(kids[1]) ~= nil, 'group started once all adds were loaded')
local t0 = farfield._clock()
local k1 = obj(kids[1])
T.check(visible(k1) and math.abs(k1.pos.z - (101 - 1.5)) < 1e-6, 'adds unhidden at z - dIn (under the old surface)')
T.check(visible(parent) and parent.pos.z == 100, 'old tile unchanged at the start')
-- phase 1 half way: smootherstep(0.5) = 0.5
local function advanceTo(t)
  while farfield._clock() - t0 < t - 1e-9 do step(free, camNorth) end
end
advanceTo(0.125)
local tt = farfield._clock() - t0
T.near(k1.pos.z, 101 + crossover.addOffset(1.5, tt), 1e-6, 'phase 1: adds rise (smootherstep)')
T.check(parent.pos.z == 100, 'phase 1: old tile stays')
advanceTo(0.25)
T.near(k1.pos.z, 101, 1e-6, 'adds at true height after 0.25 s')
advanceTo(0.375)
tt = farfield._clock() - t0
T.near(parent.pos.z, 100 + crossover.removeOffset(2.5, tt), 1e-6, 'phase 2: old tile sinks towards -dOut')
T.check(parent.pos.z < 100 and visible(parent), 'phase 2: old tile still shown while sinking')
advanceTo(0.5)
step(free, camNorth)
T.check(obj('1_0_8') == nil and (S.objects[parent.id] == nil or parent.hidden), 'old tile gone after the crossover')
T.check(farfield.stats().transitions == 0 and farfield.stats().crossovers == 1, 'one crossover done')
step(free, camNorth, 2)
T.check(S.objects[parent.id] == nil, 'old tile deleted')

------------------------------------------------------------------------------------------
-- C1: the same merge back, but the camera looks away: instant swap in one frame
farfield.onTile(tile(1, 1, 0, 8, { z = 99 }))
step(free, camSouth)
farfield.onSwap(swap(1, 3, { '1_0_8' }, kids, 2.0, 1.0))
step(free, camSouth)
o = obj('1_0_8')
T.check(visible(o) and o.pos.z == 99, 'off-screen: new tile at true height at once')
for _, k in ipairs(kids) do T.check(obj(k) == nil, 'off-screen: old tiles gone in the same frame') end
local shownKids = 0
for _, ob in ipairs(S.liveOf('TSStatic')) do
  if not ob.hidden and sizeOf[ob.fields.shapeName] == 256 and ob.pos.y >= 4096 and ob.pos.y < 4608 then shownKids = shownKids + 1 end
end
T.check(shownKids == 0, 'replaced tiles hidden (or deleted) in the swap frame')
T.check(farfield.stats().instant >= 2, 'C1 counted')
-- deletes never share a frame with an import (unless under pressure): a textured import (~3 ms)
-- plus its material and a few deletes would push the frame over base + 4 ms
farfield.onTile(tile(1, 1, 3, 8, { state = 'prefetch' }))
step(free, camSouth)
local first = farfield._pending('1_3_8')
T.check(first.loaded, 'prefetch tile loaded')
farfield.onTile(tile(1, 1, 3, 8, { state = 'prefetch', z = 98 })) -- re-sent: the loaded one is doomed
step(free, camSouth)
T.check(farfield._pending('1_3_8').loaded and S.objects[first.id] ~= nil, 'no delete in the frame that imported')
step(free, camSouth)
T.check(S.objects[first.id] == nil, 'deleted in the next frame')
-- beyond 30 km in view: a crossover too (v1.3 D removed the "always instant beyond 30 km" rule)
farfield.onTile(tile(1, 1, 0, 80))       -- 41 km north
step(free, camNorth)
farfield.onSwap(swap(1, 4, { '1_0_80' }))
step(free, camNorth)
farfield.onTile(tile(1, 0, 0, 160)); farfield.onTile(tile(1, 0, 1, 160)); farfield.onTile(tile(1, 0, 0, 161)); farfield.onTile(tile(1, 0, 1, 161))
step(free, camNorth)
local far80 = obj('1_0_80')
farfield.onSwap(swap(1, 5, { '0_0_160', '0_1_160', '0_0_161', '0_1_161' }, { '1_0_80' }, 1, 1))
step(free, camNorth)
T.check(visible(far80) and far80.pos.z == 100 and math.abs(obj('0_0_160').pos.z - 99) < 0.1,
  '> 30 km in view: crossover (adds rise from z - dIn under the old tile)')
T.check(farfield.stats().transitions == 1, '> 30 km in view: counted as a running crossover')
step(free, camNorth, 40)
T.check(obj('1_0_80') == nil and visible(obj('0_0_160')) and obj('0_0_160').pos.z == 100, '> 30 km: crossover finished')
-- ... but far off-screen it is still instant (C1)
farfield.onTile(tile(1, 1, 0, 80))
step(free, camSouth)
farfield.onSwap(swap(1, 9, { '1_0_80' }, { '0_0_160', '0_1_160', '0_0_161', '0_1_161' }, 1, 1))
step(free, camSouth)
T.check(visible(obj('1_0_80')) and obj('1_0_80').pos.z == 100 and obj('0_0_160') == nil, '> 30 km off-screen: instant')

------------------------------------------------------------------------------------------
-- serialization: a merge that shares keys with a running split waits for it
farfield.onTile(tile(1, 2, 0, 3)) -- 1 km tile at (0, 3072)
step(free, camNorth)
farfield.onSwap(swap(1, 6, { '2_0_3' }))
step(free, camNorth)
local q = { key(1, 0, 6), key(1, 1, 6), key(1, 0, 7), key(1, 1, 7) }
for _, k in ipairs(q) do local l, i, j = k:match('(%d+)_(%d+)_(%d+)'); farfield.onTile(tile(1, tonumber(l), tonumber(i), tonumber(j))) end
step(free, camNorth)
farfield.onSwap(swap(1, 7, q, { '2_0_3' }, 1, 1))
step(free, camNorth)
T.check(farfield.stats().transitions == 1, 'split running')
farfield.onTile(tile(1, 2, 0, 3, { z = 98 })) -- merge back right away (server would not, hysteresis; still safe)
step(free, camNorth)
farfield.onSwap(swap(1, 8, { '2_0_3' }, q, 1, 1))
step(free, camNorth)
T.check(farfield.stats().groups == 1 and farfield._pending('2_0_3') ~= nil, 'merge waits while the split shares its keys')
for _ = 1, 40 do step(free, camNorth) end
T.check(farfield.stats().groups == 0 and visible(obj('2_0_3')) and obj('2_0_3').pos.z <= 98, 'merge ran after the split')
for _ = 1, 40 do step(free, camNorth) end
T.check(obj('2_0_3').pos.z == 98 and obj(q[1]) == nil, 'merge finished: parent back, children gone')

------------------------------------------------------------------------------------------
-- transition cap: 24 concurrent crossovers; the 25th (in view, 3 km) waits for a free slot instead of
-- popping, then crosses over too
local capKeys = {}
for n = 1, 25 do
  local i = n - 13
  farfield.onTile(tile(1, 0, i, 12))           -- a row of L0 tiles 3 km north
  capKeys[n] = key(0, i, 12)
end
step(free, camNorth)
local sid = 100
for n = 1, 25 do sid = sid + 1; farfield.onSwap(swap(1, sid, { capKeys[n] })) end
step(free, camNorth)
for n = 1, 25 do
  sid = sid + 1
  local l, i, j = 0, n - 13, 12
  farfield.onTile(tile(1, l, i, j, { z = 103 }))
end
step(free, camNorth)
for n = 1, 25 do sid = sid + 1; farfield.onSwap(swap(1, sid, { capKeys[n] }, { capKeys[n] }, 1, 1)) end
local pops, cross0 = farfield.stats().pops, farfield.stats().crossovers
step(free, camNorth)
T.check(farfield.stats().transitions == 24, 'at most 24 crossovers at once (' .. farfield.stats().transitions .. ')')
T.check(farfield.stats().pops == pops and farfield.stats().groups == 1, 'the 25th waits (no pop)')
T.check(farfield.stats().capWaits >= 1, 'cap wait counted')
local waitKey = capKeys[25]
T.check(farfield._pending(waitKey) ~= nil and farfield._pending(waitKey).id ~= nil
  and S.objects[farfield._pending(waitKey).id].hidden, 'waiting add stays loaded and hidden')
step(free, camNorth, 40) -- the first 24 end after 0.5 s: the 25th starts
T.check(farfield.stats().groups == 0 and farfield.stats().transitions == 1, 'the 25th started once a slot was free')
step(free, camNorth, 40)
T.check(farfield.stats().transitions == 0 and farfield.stats().groups == 0, 'all crossovers finished, none waiting')
T.check(farfield.stats().crossovers == cross0 + 25 and farfield.stats().pops == pops, 'all 25 crossed over, no pop')
for n = 1, 25 do T.check(obj(capKeys[n]).pos.z == 103, 're-sent key replaced in place') end
-- at the cap, a far in-view group (beyond NEAR_POP_M) still swaps instantly rather than waiting
for n = 1, 24 do farfield.onTile(tile(1, 0, n - 13, 12, { z = 104 })) end
farfield.onTile(tile(1, 3, 0, 4)) -- 2 km tile 8 km north
step(free, camNorth)
farfield.onSwap(swap(1, sid + 1, { key(3, 0, 4) }))
step(free, camNorth)
farfield.onTile(tile(1, 3, 0, 4, { z = 97 }))
step(free, camNorth)
for n = 1, 24 do sid = sid + 1; farfield.onSwap(swap(1, sid + 1, { capKeys[n] }, { capKeys[n] }, 1, 1)) end
sid = sid + 2
farfield.onSwap(swap(1, sid, { key(3, 0, 4) }, { key(3, 0, 4) }, 1, 1))
local inst0, pops0 = farfield.stats().instant, farfield.stats().pops
step(free, camNorth)
T.check(farfield.stats().transitions == 24 and farfield.stats().instant == inst0 + 1 and obj(key(3, 0, 4)).pos.z == 97,
  'at the cap, an in-view group beyond 5 km swaps instantly')
T.check(farfield.stats().pops == pops0 + 1, 'that in-view instant swap counts as a pop')
step(free, camNorth, 40)

------------------------------------------------------------------------------------------
-- covered L0 tiles under the block (block centred at (0,0): cover box [-1024, 1024]^2)
local edge, inner, outside, col = key(0, -4, -4), key(0, -1, -1), key(0, 4, 0), key(0, -3, 0)
farfield.onTile(tile(1, 0, -4, -4, { sink = 1.5, z = 50 }))
farfield.onTile(tile(1, 0, -1, -1, { sink = 1.0, z = 50 }))
farfield.onTile(tile(1, 0, 4, 0, { sink = 2.0, z = 50 }))   -- just east of the block
farfield.onTile(tile(1, 0, -3, 0, { sink = 0.5, z = 50 }))  -- second column: interior now
farfield.onTile(tile(1, 0, -4, 0, { z = 50 }))              -- edge ring, no sink from the server
step(free, camNorth)
farfield.onSwap(swap(1, 300, { edge, inner, outside, col, key(0, -4, 0) }))
step(free, camNorth)
T.check(visible(obj(edge)) and obj(edge).pos.z == 50 - 1.5, 'edge ring tile: shown, sunk by its sink')
T.check(obj(inner).hidden, 'interior tile: hidden')
T.check(visible(obj(outside)) and obj(outside).pos.z == 50, 'tile outside the block: true height')
T.check(obj(col).hidden, 'second-column tile: interior, hidden')
T.check(obj(key(0, -4, 0)).hidden, 'edge tile without sink: hidden rather than risk poking through')
T.check(farfield.stats().covered == 4, 'covered count')
-- commit gating: the move east uncovers the west column x in [-1024, -768): 8 cells, only 2 live
T.check(not farfield.uncoverReady(0, 0, 256, 0), 'gate closed: uncovered cells not all live')
for j = -4, 3 do
  if j ~= -4 and j ~= 0 then farfield.onTile(tile(1, 0, -4, j, { sink = 1, z = 50 })) end
end
step(free, camNorth)
T.check(not farfield.uncoverReady(0, 0, 256, 0), 'gate closed: loaded but not swapped in')
local ids = {}
for j = -4, 3 do if j ~= -4 and j ~= 0 then ids[#ids + 1] = key(0, -4, j) end end
farfield.onSwap(swap(1, 301, ids))
step(free, camNorth)
T.check(farfield.uncoverReady(0, 0, 256, 0), 'gate open: every uncovered cell has a live tile')
T.check(farfield.uncoverReady(0, 0, 0, 0), 'no move: nothing to uncover')
T.check(not farfield.uncoverReady(0, 0, 0, 256), 'move north uncovers the south row (not live)')
-- a coarse tile across the new block's edge (its L0 split has not run yet) keeps the gate closed
farfield.onTile(tile(1, 1, 1, 2, { z = 50 })) -- L1 [512, 1024] x [1024, 1536]
step(free, camNorth)
farfield.onSwap(swap(1, 302, { key(1, 1, 2) }))
step(free, camNorth)
T.check(farfield.uncoverReady(0, 0, 256, 0), 'gate open: the L1 tile lies outside the new block')
T.check(farfield.uncoverReady(0, 0, 0, 0), 'gate open: no move, the L1 tile only touches the block')
farfield.onBlockCommit(0, 0)
T.check(not farfield.uncoverReady(0, 256, 0, 256), 'gate closed: an L1 tile straddles the new block edge (nothing uncovered)')
farfield.onRemove({ type = 'far_remove', anchorId = 1, key = key(1, 1, 2) })
step(free, camNorth)
T.check(farfield._tile(key(1, 1, 2)) == nil, 'L1 tile removed again')
-- ... and so does one inside it (it would be hidden without a sink: a hole in the 2 m N/E strip)
farfield.onTile(tile(1, 1, 1, 6, { z = 50 })) -- L1 [512, 1024] x [3072, 3584]
step(free, camNorth)
farfield.onSwap(swap(1, 303, { key(1, 1, 6) }))
step(free, camNorth)
T.check(farfield.uncoverReady(0, 0, 0, 0), 'gate open: the L1 tile lies outside the block')
T.check(not farfield.uncoverReady(0, 2560, 0, 2560), 'gate closed: an L1 tile lies inside the new block')
farfield.onRemove({ type = 'far_remove', anchorId = 1, key = key(1, 1, 6) })
step(free, camNorth)

-- the commit frame, in a governor heavy slot: no import, covered/uncovered tiles switch atomically
local g2 = governor.new({ clock = clock })
for _ = 1, 100 do g2:beginFrame(false, 11.5) end
farfield.onTile(tile(1, 0, 30, 30)) -- streaming work waiting
local creates = S.calls.createObject_TSStatic
g2:beginFrame(farfield.hasWork(), 11.5)
T.check(g2:requestHeavy(), 'heavy slot granted (b == 0, calm)')
local changed = farfield.onBlockCommit(256, 0) -- block moves 256 m east: cover box [-768, 1280]
step(g2, camNorth)
T.check(S.calls.createObject_TSStatic == creates, 'no import in the commit frame')
T.check(changed >= 4, 'cover changes applied in the commit frame (' .. changed .. ')')
T.check(visible(obj(edge)) and obj(edge).pos.z == 50, 'newly uncovered edge tile: true height at once')
T.check(visible(obj(key(0, -4, 0))) and obj(key(0, -4, 0)).pos.z == 50, 'newly uncovered hidden tile: shown at once')
T.check(visible(obj(col)) and obj(col).pos.z == 50 - 0.5, 'interior -> edge ring: shown sunk at once')
T.check(obj(inner).hidden, 'still interior: stays hidden')
local out = obj(outside)
T.check(visible(out) and out.pos.z > 50 - 2.0 and out.pos.z <= 50, 'newly covered tile starts sinking (phase 2 only)')
for _ = 1, 2 do
  g2:beginFrame(farfield.hasWork(), 11.5)
  step(g2, camNorth)
  T.check(S.calls.createObject_TSStatic == creates, 'no import in the 2 frames after the commit')
end
for _ = 1, 30 do g2:beginFrame(farfield.hasWork(), 11.5); step(g2, camNorth) end
T.near(out.pos.z, 50 - 2.0, 1e-9, 'newly covered edge tile ends sunk by its sink after 0.3 s')
T.check(visible(out), 'edge ring tile stays shown (covers the 2 m N/E strip)')
-- move back west: the east tile is uncovered again, instantly
farfield.onBlockCommit(0, 0)
T.check(out.pos.z == 50 and visible(out), 'uncovered again: back at true height in the same frame')
-- a newly covered interior tile sinks, then hides
farfield.onBlockCommit(1024, 0) -- cover box [0, 2048]: tile (4,0) at [1024,1280] is interior now
step(free, camNorth)
T.check(visible(out) and out.pos.z < 50, 'covered as interior: sinks first')
for _ = 1, 30 do step(free, camNorth) end
T.check(out.hidden and math.abs(out.pos.z - (50 - 2.0)) < 1e-9, 'then hides')
farfield.onBlockCommit(0, 0)
step(free, camNorth)

------------------------------------------------------------------------------------------
-- prefetch: loads hidden only when nothing else waits; evicted by far_remove; used by a swap
farfield.onTile(tile(1, 0, 40, 40, { state = 'prefetch' }))
farfield.onTile(tile(1, 0, 41, 40))
spawnCost = 5
local g3 = governor.new({ clock = clock })
local order = {}
for _ = 1, 200 do
  g3:beginFrame(farfield.hasWork(), 11.5)
  step(g3, camNorth)
  for _, k in ipairs({ '0_40_40', '0_41_40' }) do
    local p = farfield._pending(k)
    if p and p.loaded and not order[k] then order[k] = true; order[#order + 1] = k end
  end
end
spawnCost = 1.5
T.check(order[1] == '0_41_40' and order[2] == '0_40_40', 'group tile first, prefetch after')
local pid = farfield._pending('0_40_40').id
T.check(S.objects[pid].hidden, 'prefetched tile stays hidden')
creates = S.calls.createObject_TSStatic
farfield.onSwap(swap(1, 400, { '0_40_40', '0_41_40' }))
step(free, camNorth)
T.check(visible(obj('0_40_40')) and obj('0_40_40').id == pid and S.calls.createObject_TSStatic == creates,
  'a swap reveals the prefetched tile without another import')
farfield.onTile(tile(1, 0, 42, 40, { state = 'prefetch' }))
step(free, camNorth)
local evict = farfield._pending('0_42_40').id
farfield.onRemove({ type = 'far_remove', anchorId = 1, key = '0_42_40' })
step(free, camNorth, 3)
T.check(farfield._pending('0_42_40') == nil and S.objects[evict] == nil, 'far_remove evicts a prefetched tile')

-- a re-sent tile supersedes the queued one; a missing shape fails without blocking its group
farfield.onTile(tile(1, 0, 50, 50, { z = 1 }))
farfield.onTile(tile(1, 0, 50, 50, { z = 2 }))
local realExists = FS.fileExists
FS.fileExists = function(_, p) return not p:match('0_51_50') end
farfield.onTile(tile(1, 0, 51, 50))
step(free, camNorth)
FS.fileExists = realExists
farfield.onSwap(swap(1, 401, { '0_50_50', '0_51_50' }))
step(free, camNorth)
T.check(obj('0_50_50').pos.z == 2, 'the newer far_tile of a key wins')
T.check(farfield.stats().failed == 1 and farfield._tile('0_51_50') ~= nil, 'failed add counted, group not stuck')
-- stale anchor messages are ignored
farfield.onTile(tile(7, 0, 60, 60)); farfield.onSwap(swap(7, 1, { '0_60_60' }))
step(free, camNorth)
T.check(farfield._pending('0_60_60') == nil and farfield._tile('0_60_60') == nil, 'other anchor ignored')

------------------------------------------------------------------------------------------
-- rebase: every tile gets R*T + t (exact doubles); old tiles kept until their far_remove (once the
-- adds received before it are shown) or the new far_done
farfield.onRemove({ type = 'far_remove', anchorId = 1, key = '0_30_30' }) -- the server dropped it (no swap came)
step(free, camNorth)
T.check(farfield._pending('0_30_30') == nil, 'far_remove of a queued tile drops it')
local a1 = geodesy.newAnchor(36.5785, -118.2923)
local lat, lon = a1:enuToGeodetic(10000, 0, 0)
local a2 = geodesy.newAnchor(lat, lon)
local xf = a1:transformTo(a2)
local rec = farfield._tile('1_0_8')
local ex, ey, ez = xf:apply(rec.x, rec.y, rec.z)
-- a crossover in flight is finished first
farfield.onTile(tile(1, 0, 44, 40, { z = 200 }))
step(free, camNorth)
farfield.onSwap(swap(1, 402, { '0_44_40' }, { '0_40_40' }, 3, 3))
step(free, camNorth)
T.check(farfield.stats().transitions == 1, 'crossover running before the rebase')
local liveIds = {}
for _, ob in ipairs(S.liveOf('TSStatic')) do if not ob.hidden then liveIds[#liveIds + 1] = ob.id end end
farfield.transform(xf)
farfield.switchAnchor(2, true, { cx = 256, cy = 0 })
T.check(farfield.stats().transitions == 0 and obj('0_44_40') == nil, 'running crossover finished at the rebase')
local o8 = S.objects[rec.id]
T.near(o8.pos.x, ex, 1e-6, 'tile x mapped'); T.near(o8.pos.y, ey, 1e-6, 'tile y mapped'); T.near(o8.pos.z, ez, 1e-6, 'tile z mapped')
local bx, by, bz, bw = xf:beamngQuat()
T.near(o8.rot.x, bx, 1e-15, 'tile rot = R'); T.near(o8.rot.w, bw, 1e-15, 'tile rot w')
local vx, vy, vz = geodesy.beamngRotate(o8.rot.x, o8.rot.y, o8.rot.z, o8.rot.w, 1000, 0, 0)
local ox, oy, oz = xf:inverse():apply(o8.pos.x, o8.pos.y, o8.pos.z)
local wx, wy, wz = xf:apply(ox + 1000, oy, oz)
T.near(o8.pos.x + vx, wx, 1e-6, 'vertex x'); T.near(o8.pos.y + vy, wy, 1e-6, 'vertex y'); T.near(o8.pos.z + vz, wz, 1e-6, 'vertex z')
local oldN = farfield.stats().old
T.check(oldN > 20 and farfield.stats().live == 0, 'old tiles kept (' .. oldN .. ')')
local held = farfield.heldAnchors()
T.check(#held == 1 and held[1] == 1, 'heldAnchors: the previous anchor while its tiles are kept')
-- new anchor: a one-sided group over old tiles rises (phase 1) instead of popping
farfield.onTile(tile(2, 1, 0, 8, { z = 100 }))
farfield.onRemove({ type = 'far_remove', anchorId = 1, key = '1_0_8' })
local gone = farfield._oldId(1, '1_0_8')
step(free, camNorth)
T.check(S.objects[gone] ~= nil and visible(S.objects[gone]), 'old tile stays: an add received before its far_remove is not shown yet')
farfield.onSwap(swap(2, 1, { '1_0_8' }))
step(free, camNorth)
local nn = obj('1_0_8')
T.check(visible(nn) and nn.pos.z < 100, 'one-sided group over old tiles rises from below')
T.check(S.objects[gone] == nil or S.objects[gone].hidden, 'old tile removed once the earlier add is shown')
for _ = 1, 20 do step(free, camNorth) end
T.check(nn.pos.z == 100, 'risen to true height')
farfield.onDone({ type = 'far_done', anchorId = 2, count = 1 })
for _ = 1, 5 do step(free, camNorth) end
T.check(farfield.stats().old == 0, 'far_done with everything shown: old tiles released')
for _ = 1, 60 do step(free, camNorth) end
local left = 0
for _, id in ipairs(liveIds) do if S.objects[id] then left = left + 1 end end
T.check(left == 0, 'all old tiles deleted eventually')
T.check(#farfield.heldAnchors() == 0, 'heldAnchors: empty once the old objects are deleted')

-- lossy: a rebase while adds are loading drops them; that anchor's far_removes are then ignored
farfield.onTile(tile(2, 0, 0, 30)); farfield.onTile(tile(2, 0, 1, 30))
step(free, camNorth)
farfield.onSwap(swap(2, 2, { '0_0_30' }))
step(free, camNorth)
farfield.onTile(tile(2, 0, 2, 30)) -- still queued at the switch
local a3 = geodesy.newAnchor(a2:enuToGeodetic(5000, 0, 0))
farfield.transform(a2:transformTo(a3))
farfield.switchAnchor(3, true, { cx = 0, cy = 0 })
local kept = farfield._oldId(2, '0_0_30')
farfield.onRemove({ type = 'far_remove', anchorId = 2, key = '0_0_30' })
step(free, camNorth, 3)
T.check(kept ~= nil and S.objects[kept] ~= nil, 'lossy anchor: its far_remove is ignored')
held = farfield.heldAnchors()
T.check(#held == 1 and held[1] == 2, 'heldAnchors: kept old tiles hold anchor 2')
farfield.onDone({ type = 'far_done', anchorId = 3, count = 0 })
step(free, camNorth, 40)
T.check(S.objects[kept] == nil and farfield.stats().old == 0, 'lossy anchor tiles deleted after far_done')

-- old tiles take their cover state from the new block at the switch
farfield.onTile(tile(3, 0, 10, 0, { sink = 1, z = 50 }))
step(free, camNorth)
farfield.onSwap(swap(3, 1, { '0_10_0' }))
step(free, camNorth)
local idA = obj('0_10_0').id
farfield.switchAnchor(4, true, { cx = 2560 + 128, cy = 0 }) -- identity "rebase", block now over it
T.check(S.objects[idA].hidden, 'old tile under the new block: hidden at the switch')

farfield.switchAnchor(5, false, { cx = 0, cy = 0 }) -- teleport: hide all, delete progressively
for _, ob in ipairs(S.liveOf('TSStatic')) do T.check(ob.hidden, 'teleport hides old tiles at once') end
step(free, nil, 200)
T.check(nTS() == 0, 'teleport: everything deleted')

-- far_removes processed a few per frame (budget): the FIFO must not lose its tail once its head
-- entries are cleared (a '#' on a table with leading nils can return 0 in LuaJIT)
do
  local keys = {}
  farfield.switchAnchor(6, false, { cx = 100000, cy = 0 })
  for i = 0, 11 do farfield.onTile(tile(6, 0, i, 70)); keys[#keys + 1] = key(0, i, 70) end
  step(free, camNorth)
  farfield.onSwap(swap(6, 1, keys))
  step(free, camNorth)
  farfield.switchAnchor(7, true, { cx = 100000, cy = 0 }) -- rebase (identity), block far away
  local ids = {}
  for _, k in ipairs(keys) do
    ids[#ids + 1] = farfield._oldId(6, k)
    farfield.onRemove({ type = 'far_remove', anchorId = 6, key = k })
  end
  local oneReveal = { clock = clock, n = 0 }
  function oneReveal:try(kind) if kind ~= 'reveal' then return true end; self.n = self.n + 1; return self.n == 1 end
  function oneReveal:charge() end
  function oneReveal:chargeMotion() end
  for _ = 1, 30 do oneReveal.n = 0; farfield.update(oneReveal, DT, camNorth) end
  local shownOld = 0
  for _, id in ipairs(ids) do if S.objects[id] and not S.objects[id].hidden then shownOld = shownOld + 1 end end
  T.check(shownOld == 0 and farfield.stats().old == 0, 'budgeted far_removes: every old tile removed (' .. shownOld .. ' left)')
  T.check(not farfield.hasWork() or farfield.stats().deleting > 0, 'removal queue drained')
  farfield.switchAnchor(8, false, { cx = 0, cy = 0 })
  step(free, nil, 200)
end

------------------------------------------------------------------------------------------
-- mesh mode after a rebase: a previous anchor's tile goes as soon as settled current tiles cover it
-- completely (no z-fighting second ground near the player for the whole reload); a partly covered
-- one stays; tiles still rising in a crossover do not count yet; terrain mode keeps the v1.2 rule
do
  local camAway = { x = 0, y = 5000, z = 200, fx = 0, fy = 1, fz = 0, fovDeg = 60 } -- tiles behind it
  local function oldShown(a, k)
    local id = farfield._oldId(a, k)
    return id ~= nil and visible(S.objects[id])
  end
  local function setup(a, mode)
    farfield.setNearVisual(mode)
    farfield.switchAnchor(a, false, { cx = 100000, cy = 0 })
    farfield.onTile(tile(a, -1, 0, 0)); farfield.onTile(tile(a, -1, 1, 0))
    step(free, camAway)
    farfield.onSwap(swap(a, 1, { '-1_0_0', '-1_1_0' }))
    step(free, camAway)
    farfield.switchAnchor(a + 1, true, { cx = 100000, cy = 0 }) -- identity rebase: both become old tiles
  end
  local quarter = { '-2_0_0', '-2_1_0', '-2_0_1', '-2_1_1' } -- the 64 m tiles of old -1_0_0
  setup(30, 'mesh')
  for _, k in ipairs(quarter) do
    local l, i, j = k:match('^(-?%d+)_(-?%d+)_(-?%d+)$')
    farfield.onTile(tile(31, tonumber(l), tonumber(i), tonumber(j)))
  end
  step(free, camAway)
  farfield.onSwap(swap(31, 1, { quarter[1], quarter[2] }))
  step(free, camAway) -- off-screen: instant
  T.check(oldShown(30, '-1_0_0') and farfield.stats().old == 2, 'mesh: half-covered old tile stays')
  farfield.onSwap(swap(31, 2, { quarter[3], quarter[4] }))
  step(free, camAway)
  T.check(farfield._oldId(30, '-1_0_0') == nil and farfield.stats().old == 1, 'mesh: covered old tile dropped at once')
  T.check(oldShown(30, '-1_1_0'), 'mesh: the uncovered old tile stays')
  farfield.onRemove({ type = 'far_remove', anchorId = 30, key = '-1_0_0' }) -- its far_remove comes later
  step(free, camAway, 3)
  T.check(farfield.stats().old == 1, 'the late far_remove of a dropped tile is harmless')
  -- in view: the adds rise first (crossover phase 1); the old tile goes only once they are settled
  farfield.onTile(tile(31, -1, 1, 0))
  step(free, camNorth)
  farfield.onSwap(swap(31, 3, { '-1_1_0' }))
  local camHere = { x = 64, y = -200, z = 150, fx = 0, fy = 1, fz = -0.3, fovDeg = 60 }
  step(free, camHere)
  T.check(farfield.stats().transitions == 1 and oldShown(30, '-1_1_0'), 'mesh: old tile kept while the add rises')
  for _ = 1, 30 do step(free, camHere) end
  T.check(farfield.stats().old == 0 and farfield.stats().oldCovered == 2, 'mesh: dropped once the rise ended')
  -- mesh mode imports the queued tile nearest the camera first (terrain mode: server order)
  local oneImport = { clock = clock, n = 0 }
  function oneImport:try(kind) if kind ~= 'cdae' then return true end; self.n = self.n + 1; return self.n == 1 end
  function oneImport:charge() end
  function oneImport:chargeMotion() end
  local function order(mode)
    farfield.setNearVisual(mode)
    farfield.switchAnchor(50, false, { cx = 100000, cy = 0 })
    for _, i in ipairs({ 40, 2, 9 }) do farfield.onTile(tile(50, -2, i, 0)) end -- server order
    local got, seen = {}, {}
    for _ = 1, 3 do
      oneImport.n = 0
      farfield.update(oneImport, DT, { x = 0, y = 0, z = 100, fx = 0, fy = 1, fz = 0, fovDeg = 60 })
      for _, i in ipairs({ 40, 2, 9 }) do
        local p = farfield._pending(key(-2, i, 0))
        if p.loaded and not seen[i] then seen[i] = true; got[#got + 1] = i end
      end
    end
    return table.concat(got, ',')
  end
  T.check(order('mesh') == '2,9,40', 'mesh: nearest queued tile imported first (' .. order('mesh') .. ')')
  T.check(order('terrain') == '40,2,9', 'terrain: server order (' .. order('terrain') .. ')')
  -- ... and what is on screen first: behind the camera a tile counts VIEW_PENALTY times as far
  farfield.setNearVisual('mesh')
  farfield.switchAnchor(51, false, { cx = 100000, cy = 0 })
  farfield.onTile(tile(51, -2, 0, -3))  -- 128 m behind (south)
  farfield.onTile(tile(51, -2, 0, 5))   -- 320 m ahead (north)
  oneImport.n = 0
  farfield.update(oneImport, DT, { x = 0, y = 0, z = 100, fx = 0, fy = 1, fz = 0, fovDeg = 60 })
  T.check(farfield._pending(key(-2, 0, 5)).loaded and not farfield._pending(key(-2, 0, -3)).loaded,
    'mesh: the tile ahead (on screen) imported before the nearer one behind')
  -- terrain mode: unchanged (old tiles wait for their far_remove or far_done)
  setup(40, 'terrain')
  for _, k in ipairs(quarter) do
    local l, i, j = k:match('^(-?%d+)_(-?%d+)_(-?%d+)$')
    farfield.onTile(tile(41, tonumber(l), tonumber(i), tonumber(j)))
  end
  step(free, camAway)
  farfield.onSwap(swap(41, 1, quarter))
  step(free, camAway)
  T.check(oldShown(40, '-1_0_0') and farfield.stats().old == 2, 'terrain: covered old tile kept (v1.2 rule)')
  farfield.switchAnchor(42, false, { cx = 100000, cy = 0 })
  step(free, nil, 200)
end

------------------------------------------------------------------------------------------
-- no-hole simulation: a scripted server splits/merges tiles and moves the block; at the end of
-- every frame each sample point is covered by the block's collidable ground or by a shown tile
do
  math.randomseed(2026)
  farfield.forget()
  for _, ob in ipairs(S.liveOf('TSStatic')) do S.objects[ob.id] = nil end
  local A = 10
  local R = 4096
  local gv = governor.new({ clock = clock })
  farfield.switchAnchor(A, false, { cx = 0, cy = 0 })
  local blk = { cx = 0, cy = 0 }
  -- server state: shown set (after swaps) and an outbox of groups to emit
  local shown, locked, outbox = {}, {}, {}
  local gid = 0
  local function parse(k) local l, i, j = k:match('^(%d+)_(%-?%d+)_(%-?%d+)$'); return tonumber(l), tonumber(i), tonumber(j) end
  local function bbox(k) local l, i, j = parse(k); local s = size(l); return i * s, j * s, (i + 1) * s, (j + 1) * s end
  local function overlaps(k, x0, y0, x1, y1) local a, b, c, d = bbox(k); return a < x1 and x0 < c and b < y1 and y0 < d end
  local function inBox(k, x0, y0, x1, y1) local a, b, c, d = bbox(k); return a >= x0 and b >= y0 and c <= x1 and d <= y1 end
  local function queueGroup(adds, removes)
    for _, k in ipairs(adds) do locked[k] = true; shown[k] = true end
    for _, k in ipairs(removes) do locked[k] = true; shown[k] = nil end
    outbox[#outbox + 1] = { adds = adds, removes = removes, sent = 0 }
  end
  -- initial partition: L2 over [-R, R]^2, L0 under the block
  local init = {}
  local function addInit(l, i, j)
    local k = key(l, i, j)
    if l > 0 and overlaps(k, -1024, -1024, 1024, 1024) then
      for di = 0, 1 do for dj = 0, 1 do addInit(l - 1, 2 * i + di, 2 * j + dj) end end
    else
      init[#init + 1] = k
    end
  end
  for i = -4, 3 do for j = -4, 3 do addInit(2, i, j) end end
  queueGroup(init, {})
  local function split(k)
    local l, i, j = parse(k)
    local kids_ = {}
    for di = 0, 1 do for dj = 0, 1 do kids_[#kids_ + 1] = key(l - 1, 2 * i + di, 2 * j + dj) end end
    queueGroup(kids_, { k })
  end
  local function forceL0(x0, y0, x1, y1) -- split everything overlapping the box down to L0
    local again = true
    while again do
      again = false
      for k in pairs(shown) do
        local l = parse(k)
        if l > 0 and not locked[k] and overlaps(k, x0, y0, x1, y1) then split(k); again = true; break end
      end
    end
  end
  local function serverFrame()
    -- send up to 3 tiles of the first groups, a swap once a group's adds are all sent
    local budget = 3
    local i = 1
    while i <= #outbox do
      local g = outbox[i]
      while budget > 0 and g.sent < #g.adds do
        g.sent = g.sent + 1
        budget = budget - 1
        local l, ti, tj = parse(g.adds[g.sent])
        farfield.onTile(tile(A, l, ti, tj, { sink = l == 0 and 0.8 or nil, err = 0.3 * (l + 1), z = 100 + l }))
      end
      if g.sent == #g.adds then
        gid = gid + 1
        farfield.onSwap(swap(A, gid, g.adds, g.removes, 0.5, 0.5))
        for _, k in ipairs(g.adds) do locked[k] = nil end
        for _, k in ipairs(g.removes) do locked[k] = nil end
        table.remove(outbox, i)
      else
        i = i + 1
      end
      if budget == 0 then break end
    end
  end
  local function randomOp()
    local keys = {}
    for k in pairs(shown) do if not locked[k] then keys[#keys + 1] = k end end
    table.sort(keys)
    if #keys == 0 then return end
    local k = keys[math.random(#keys)]
    local l, i, j = parse(k)
    local bx0, by0, bx1, by1 = blk.cx - 1024, blk.cy - 1024, blk.cx + 1024, blk.cy + 1024
    if l > 0 and math.random() < 0.5 then
      split(k)
    elseif l < 2 then -- merge with siblings, outside the forced L0 area
      local pi, pj = math.floor(i / 2), math.floor(j / 2)
      local p = key(l + 1, pi, pj)
      if overlaps(p, bx0, by0, bx1, by1) or (blk.next and overlaps(p, blk.next[1] - 1024, blk.next[2] - 1024, blk.next[1] + 1024, blk.next[2] + 1024)) then return end
      local sib = {}
      for di = 0, 1 do for dj = 0, 1 do
        local s = key(l, 2 * pi + di, 2 * pj + dj)
        if not shown[s] or locked[s] then return end
        sib[#sib + 1] = s
      end end
      queueGroup({ p }, sib)
    end
  end
  -- sample points (64 m offsets never lie on a tile edge) + the block's 2 m N/E strip
  local function covered(px, py, cov)
    return cov(px, py)
  end
  local holes, frames, crossFrames, commits, importsNearCommit = 0, 0, 0, 0, 0
  local straddles = 0
  local heavyAt = {}
  local importsAt = {}
  local cams = { camNorth, camSouth, { x = 0, y = 0, z = 200, fx = 1, fy = 0, fz = 0, fovDeg = 60 } }
  for f = 1, 2500 do
    frames = f
    now = now + 11.5
    local cam = cams[1 + math.floor(f / 300) % 3]
    if f % 4 == 0 then serverFrame() end
    if f % 23 == 0 then randomOp() end
    -- the block moves every ~500 frames: force L0 under the next block, commit once gated
    if f % 500 == 250 and not blk.next then
      local dx, dy = 0, 0
      if math.random() < 0.5 then dx = (math.random() < 0.5) and -256 or 256 else dy = (math.random() < 0.5) and -256 or 256 end
      blk.next = { blk.cx + dx, blk.cy + dy }
      forceL0(blk.next[1] - 1024, blk.next[2] - 1024, blk.next[1] + 1024, blk.next[2] + 1024)
    end
    -- like the server's selection on every pose: keep forcing L0 under the pending block (tiles
    -- split one level per group, so a split's children are split again once they are emitted)
    if blk.next and f % 4 == 0 then
      forceL0(blk.next[1] - 1024, blk.next[2] - 1024, blk.next[1] + 1024, blk.next[2] + 1024)
    end
    gv:beginFrame(farfield.hasWork(), 11.5)
    local before = S.calls.createObject_TSStatic or 0
    if blk.next and farfield.uncoverReady(blk.cx, blk.cy, blk.next[1], blk.next[2]) and gv:requestHeavy() then
      farfield.onBlockCommit(blk.next[1], blk.next[2])
      blk.cx, blk.cy, blk.next = blk.next[1], blk.next[2], nil
      commits = commits + 1
      heavyAt[f] = true
    end
    farfield.update(gv, DT, cam)
    importsAt[f] = (S.calls.createObject_TSStatic or 0) - before
    if farfield.stats().transitions > 0 then crossFrames = crossFrames + 1 end
    -- coverage
    local vis = {}
    for _, ob in pairs(S.objects) do
      if ob.class == 'TSStatic' and not ob.hidden then vis[#vis + 1] = ob end
    end
    -- no shown tile straddles the committed block's edge (it would poke through the terrain);
    -- tiles sinking out of a crossover are allowed (the commit does not touch running removes)
    local bx0, by0, bx1, by1 = blk.cx - 1024, blk.cy - 1024, blk.cx + 1024, blk.cy + 1024
    for _, ob in ipairs(vis) do
      local s = sizeOf[ob.fields.shapeName]
      local x0, y0 = ob.pos.x, ob.pos.y
      if x0 < bx1 and bx0 < x0 + s and y0 < by1 and by0 < y0 + s
        and (x0 < bx0 or y0 < by0 or x0 + s > bx1 or y0 + s > by1) and ob.pos.z >= 100 + math.log(s / 256) / math.log(2) then
        straddles = straddles + 1
      end
    end
    local function cov(px, py)
      if px >= blk.cx - 1024 and px < blk.cx + 1022 and py >= blk.cy - 1024 and py < blk.cy + 1022 then return true end
      for _, ob in ipairs(vis) do
        local s = sizeOf[ob.fields.shapeName]
        if px > ob.pos.x and px < ob.pos.x + s and py > ob.pos.y and py < ob.pos.y + s then return true end
      end
      return false
    end
    if f > 200 then -- after the first fill
      for px = -R + 64 + 128 * (f % 2), R - 64, 256 do
        for py = -R + 64, R - 64, 256 do
          if not covered(px, py, cov) then holes = holes + 1 end
        end
      end
      for t = -1024 + 64, 1022, 128 do
        if not cov(blk.cx + 1023, blk.cy + t) then holes = holes + 1 end -- east 2 m strip
        if not cov(blk.cx + t, blk.cy + 1023) then holes = holes + 1 end -- north 2 m strip
      end
    end
  end
  for f in pairs(heavyAt) do
    for k = f - 2, f + 2 do if (importsAt[k] or 0) > 0 then importsNearCommit = importsNearCommit + 1 end end
  end
  T.check(straddles == 0, 'no coarse tile shown at true height across the committed block edge (' .. straddles .. ')')
  T.check(holes == 0, 'no-hole invariant: every sample covered by the block or a shown tile, every frame (' .. holes .. ' misses)')
  T.check(commits >= 3, 'block commits happened (' .. commits .. ')')
  T.check(importsNearCommit == 0, 'no import within +-2 frames of a commit')
  T.check(crossFrames > 50, 'crossovers ran during the simulation (' .. crossFrames .. ' frames)')
  local st = farfield.stats()
  T.check(st.crossovers > 5 and st.instant > 5, 'both C2 and C1 swaps exercised (' .. st.crossovers .. '/' .. st.instant .. ')')
end

------------------------------------------------------------------------------------------
-- ground under the player (V1_4_SPEC 3.0 M0, the exactMiss metric): key lookup from the exact
-- level up; a miss when no tile, a coarser one, or the exact tile still crossing over (> 1 cm off);
-- a previous anchor's exact tile (looked up through the inverse rebase transform) counts as exact;
-- terrain mode counts nothing; no player, no update
do
  local camHere = { x = 64, y = -200, z = 150, fx = 0, fy = 1, fz = -0.3, fovDeg = 60 }
  local pl = { x = 10, y = 10, z = 100 }
  local function upd(n, cam, p)
    for _ = 1, n or 1 do farfield.update(free, DT, cam or camHere, p == nil and pl or p or nil) end
  end
  farfield.setNearVisual('mesh')
  farfield.switchAnchor(60, false, { cx = 100000, cy = 0 })
  local s0 = farfield.stats()
  upd()
  local s1 = farfield.stats()
  local lvl, isOld, miss = farfield.exactState()
  T.check(lvl == nil and not isOld and miss, 'no tile under the player: a miss')
  T.check(s1.exactFrames == s0.exactFrames + 1 and s1.exactMiss == s0.exactMiss + 1, 'miss counted')
  T.check(s1.underLevel == nil and s1.exactLevel == -2 and s1.waitingGroups == s1.groups, 'stats fields')
  farfield.onTile(tile(60, -1, 0, 0))
  upd()
  farfield.onSwap(swap(60, 1, { '-1_0_0' }))
  upd()
  lvl, isOld, miss = farfield.exactState()
  T.check(lvl == -1 and miss, 'an L-1 tile under the player is a miss (exact = L-2)')
  -- split into the four L-2 tiles, in view: a crossover; the exact tile counts once settled
  local quarter = { '-2_0_0', '-2_1_0', '-2_0_1', '-2_1_1' }
  for _, k in ipairs(quarter) do
    local l, i, j = k:match('^(-?%d+)_(-?%d+)_(-?%d+)$')
    farfield.onTile(tile(60, tonumber(l), tonumber(i), tonumber(j)))
  end
  upd()
  farfield.onSwap(swap(60, 2, quarter, { '-1_0_0' }, 2, 2))
  upd()
  T.check(farfield.stats().transitions == 1, 'crossover running')
  lvl, isOld, miss = farfield.exactState()
  T.check(lvl == -2 and miss, 'exact tile still rising: a miss')
  local m0 = farfield.stats().exactMiss
  upd(40)
  lvl, isOld, miss = farfield.exactState()
  T.check(lvl == -2 and not miss and farfield.stats().transitions == 0, 'settled exact tile: no miss')
  local s2 = farfield.stats()
  T.check(s2.exactMiss - m0 < 40 and s2.underLevel == -2 and not s2.underOld, 'misses stop once settled')
  -- a rebase 10 km east: the player's new-frame position maps back onto the old exact tile
  local b1 = geodesy.newAnchor(36.5785, -118.2923)
  local b2 = geodesy.newAnchor(b1:enuToGeodetic(10000, 300, 0))
  local xf1 = b1:transformTo(b2)
  farfield.transform(xf1)
  farfield.switchAnchor(61, true, { cx = 100000, cy = 0 })
  local nx, ny, nz = xf1:apply(pl.x, pl.y, pl.z)
  local pl2 = { x = nx, y = ny, z = nz }
  upd(1, camHere, pl2)
  lvl, isOld, miss = farfield.exactState()
  T.check(lvl == -2 and isOld and not miss, 'old anchor exact tile under the player counts as exact')
  -- a second rebase: two inverse transforms back to anchor 60's plane
  local b3 = geodesy.newAnchor(b2:enuToGeodetic(-9000, 2000, 0))
  local xf2 = b2:transformTo(b3)
  farfield.transform(xf2)
  farfield.switchAnchor(62, true, { cx = 100000, cy = 0 })
  local mx, my, mz = xf2:apply(nx, ny, nz)
  upd(1, camHere, { x = mx, y = my, z = mz })
  lvl, isOld, miss = farfield.exactState()
  T.check(lvl == -2 and isOld and not miss, 'two rebases back: still found')
  upd(1, camHere, { x = mx + 200, y = my, z = mz })
  lvl, isOld, miss = farfield.exactState()
  T.check(lvl == nil and miss, 'beside the old tiles: nothing under the player')
  -- exactLevelFor: the level whose tile is `cells` lattice squares wide (core.lua, from hello.cells)
  T.check(farfield.exactLevelFor(32) == -2 and farfield.exactLevelFor(64) == -1 and farfield.exactLevelFor(nil) == -2,
    'exact level: L-2 for 32 cells, L-1 for 64')
  -- setExactLevel (M1's 64-cell layout: L-1 is exact)
  T.check(farfield.setExactLevel(-1) == -1 and farfield.stats().exactLevel == -1, 'setExactLevel')
  farfield.switchAnchor(63, false, { cx = 100000, cy = 0 })
  farfield.onTile(tile(63, -1, 0, 0))
  upd()
  farfield.onSwap(swap(63, 1, { '-1_0_0' }))
  upd()
  lvl, isOld, miss = farfield.exactState()
  T.check(lvl == -1 and not isOld and not miss, '64-cell layout: an L-1 tile is exact')
  farfield.setExactLevel(nil)
  T.check(farfield.stats().exactLevel == farfield.EXACT_LEVEL, 'setExactLevel(nil): back to the default')
  -- terrain mode (the block is the visible ground) and no player: nothing counted
  local s3 = farfield.stats()
  farfield.setNearVisual('terrain')
  upd(3)
  farfield.update(free, DT, camHere)
  local s4 = farfield.stats()
  T.check(s4.exactFrames == s3.exactFrames and s4.exactMiss == s3.exactMiss, 'terrain mode / no player: not counted')
  T.check(s4.underLevel == -1, 'terrain mode still reports the level under the player')
  farfield.switchAnchor(64, false, { cx = 0, cy = 0 })
  step(free, nil, 100)
end

------------------------------------------------------------------------------------------
-- P10-B: governed reveal cap (REVEALS_PER_FRAME = 2, 1 in burst mode unless gov.urgent) and no
-- co-scheduling of reveal + import in the same governed frame
do
  farfield.setNearVisual('terrain')
  farfield.switchAnchor(70, false, { cx = 0, cy = 0 })
  -- Preload 6 tiles with free governor so their groups are ready to reveal
  for i = 1, 6 do
    farfield.onTile(tile(70, 0, i + 10, 10))
  end
  step(free, camSouth)
  for i = 1, 6 do
    farfield.onSwap(swap(70, 700 + i, { key(0, i + 10, 10) }))
  end
  -- Also queue a 7th tile that still needs importing
  farfield.onTile(tile(70, 0, 20, 10))

  local gv = governor.new({ clock = clock })
  for _ = 1, 80 do now = now + 10; gv:beginFrame(true, 10.0) end

  -- 1) Normal governed frame (burst=false): reveals at most REVEALS_PER_FRAME = 2 groups, no import in same frame
  now = now + 10
  gv:beginFrame(true, 10.0)
  gv.burst, gv.urgent = false, false
  local importsBefore = farfield.stats().imports
  farfield.update(gv, DT, camSouth)
  T.check(farfield.stats().groups == 4, 'normal governed frame reveals at most REVEALS_PER_FRAME = 2 swap groups (4 of 6 still waiting)')
  T.check(farfield.stats().imports == importsBefore, 'no tile import in a governed frame that revealed swap groups')

  -- 2) Burst mode (burst=true, urgent=false): reveals at most 1 group per frame (after 1 L-H-L cooldown frame)
  now = now + 10; gv:beginFrame(true, 10.0)
  now = now + 10; gv:beginFrame(true, 10.0)
  gv.burst, gv.urgent = true, false
  farfield.update(gv, DT, camSouth)
  T.check(farfield.stats().groups == 3, 'burst mode reveals at most 1 waiting swap group per frame (3 of 6 still waiting)')
  T.check(farfield.stats().imports == importsBefore, 'no tile import in a burst frame that revealed a swap group')

  -- 3) Urgent overrides burst (burst=true, urgent=true): reveals up to REVEALS_PER_FRAME = 2 groups
  now = now + 10; gv:beginFrame(true, 10.0)
  now = now + 10; gv:beginFrame(true, 10.0)
  gv.burst, gv.urgent = true, true
  farfield.update(gv, DT, camSouth)
  T.check(farfield.stats().groups == 1, 'gov.urgent overrides gov.burst, allowing REVEALS_PER_FRAME = 2 reveals (1 of 6 still waiting)')

  -- Drain remaining reveal and import over subsequent frames (no starvation)
  gv.burst, gv.urgent = false, false
  for _ = 1, 30 do
    now = now + 10
    gv:beginFrame(farfield.hasWork(), 10.0)
    farfield.update(gv, DT, camSouth)
  end
  T.check(farfield.stats().groups == 0 and farfield._pending(key(0, 20, 10)).loaded,
    'all waiting swap groups revealed and queued tile imported within 30 frames')
  farfield.switchAnchor(71, false, { cx = 0, cy = 0 })
  step(free, nil, 50)
end

T.done('test_farfield')
