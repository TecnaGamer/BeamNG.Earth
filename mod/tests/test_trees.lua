-- luajit mod/tests/test_trees.lua  (trees.lua: vegetation tiles become Forest items, thinned by distance)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local items = {}                 -- every live item of every Forest
local forests = 0
local realCreate = createObject
createObject = function(class)
  local o = realCreate(class)
  if class == 'Forest' then
    -- a Forest of its own items (trees.lua: one per tile), deleted with them
    local mine = {}
    local fdata = {}
    function fdata:createNewItem(d, m, sc)
      if CREATE_HOOK then CREATE_HOOK() end      -- (the time budget's tests: a fake clock's tick)
      -- (the engine copies the transform into the item: trees.lua passes the same matrix for every item)
      local it = { d = d, m = { p = m.p, e = m.e }, sc = sc }; items[it] = true; mine[it] = true; return it
    end
    function fdata:removeItem(it) items[it] = nil; mine[it] = nil end
    function o:getData() return fdata end
    local del = o.delete
    function o:delete() for it in pairs(mine) do items[it] = nil end; forests = forests - 1; if del then del(self) end end
    forests = forests + 1
  end
  return o
end
worldEditorCppApi = { createObject = function(class) return createObject(class) end }
Sim = { getDataBlockSet = function() return { addObject = function() end } end }
loadJsonMaterialsFile = function() end
local matrices = 0
MatrixF = function()        -- (copies what it is given, as the engine's matrix does)
  matrices = matrices + 1
  return { setFromEuler = function(self, e) self.e = vec3(e) end, setPosition = function(self, p) self.p = vec3(p) end }
end
local files = {}
readFile = function(p) return files[p] end
local decoded = setmetatable({}, { __mode = 'k' })    -- every decoded file's rows (weak: what the module still holds)
jsonDecode = function(s)
  local d = assert(load('return ' .. s))()
  if type(d) == 'table' and type(d.items) == 'table' then
    for _, r in ipairs(d.items) do decoded[r] = true end
  end
  return d
end
local trees = require('beamngEarth/trees')
-- The detail keep-up defaults (2026-11-13: PROP_LEAD_S 4, NEAR_URGENT_MS 8, POOL_SPARE_DRIVE 2, NEW_SHAPES_PER_FRAME 2)
-- checked once here; the tests below exercise the base behaviour with them off and turn each on where they test it.
T.check(trees.PROP_LEAD_S == 4 and trees.NEAR_URGENT_MS == 8 and trees.POOL_SPARE_DRIVE == 2
  and trees.NEW_SHAPES_PER_FRAME == 2, 'detail keep-up defaults on')
trees.PROP_LEAD_S, trees.NEAR_URGENT_MS, trees.POOL_SPARE_DRIVE, trees.NEW_SHAPES_PER_FRAME = 0, 0, 0, 1
trees.EVERY_N = 1               -- (every update works here; the batching is tested below)
-- (no time budget, no spare Forests: both tested at the end)
local FRAME_MS, POOL_SPARE = trees.FRAME_MS, trees.POOL_SPARE
trees.FRAME_MS, trees.POOL_SPARE = math.huge, 0
-- the running item count (trees.status().items, M.COUNT_ITEMS) against a full count each time a check counts the
-- items (checked at the file's end)
local countChecks, countMiss = 0, {}
local function count()
  local n = 0; for _ in pairs(items) do n = n + 1 end
  countChecks = countChecks + 1
  local run, full = trees.status().items, trees.countItems()
  if run ~= full and #countMiss < 5 then countMiss[#countMiss + 1] = 'check ' .. countChecks .. ': ' .. run .. ' vs ' .. full end
  return n
end

-- the variant: a height inside its range, scaled to it
local shape, sc = trees.pick('conifer', 20.0, 0.3)
T.check(shape:find('douglasfir_large') ~= nil and sc > 0.9 and sc < 1.1, 'a 20 m conifer: a large fir near its size')
shape, sc = trees.pick('rock', 0.6, 0.1)
T.check(shape:find('rock') ~= nil and math.abs(sc - 0.6 / select(2, unpack({ shape, 0.6 })) * 1) < 2, 'a rock')
shape, sc = trees.pick('pole_concrete_recessed', 8.5, 0)
T.check(shape:find('/regional_poles/bne_pole_concrete_recessed', 1, true) ~= nil and sc == 1,
        'regional pole: its own model at the authored height, not a fallback tree')

shape, sc = trees.pick('streetlight_high_mast', 18.03, 0)
T.check(shape == '/levels/driver_training/art/shapes/buildings/s_light_pole_high_mast' and sc == 1,
        'mapped high-mast lamp: ETK stock asset at its authored scale')
shape, sc = trees.pick('streetlight_straight_mast', 12.25, 0)
T.check(shape == '/levels/driver_training/art/shapes/buildings/s_light_pole' and sc == 1,
        'straight-mast lamp: Driver Experience stock pole at authored scale')
shape, sc = trees.pick('streetlight_double_arm', 13.76, 0)
T.check(shape == '/art/shapes/objects/pole_light_double' and sc == 1,
        'double-arm lamp: West Coast stock pole at authored scale')
T.check(trees.NO_COLLIDE.high_mast_light == true, 'high-mast light markers do not collide')

-- a tile of 100 items with ranks 0 .. 0.99 at the player: all made; far away: a quarter
local rows = {}
for i = 0, 99 do rows[#rows + 1] = string.format('{0, %d, 10, 5, 0, 1, 12, %.2f}', i, i / 100) end
files['/t/v.json'] = '{ groups = {}, items = { ' .. table.concat(rows, ', ') .. ' } }'
trees.onTile({ anchorId = 1, key = '0_0', path = '/t/v.json', n = 100 }, 1)
trees.update(1, { x = 50, y = 50 })
T.check(count() == 100 and trees.status().items == 100, 'near: every item')
do -- an item where its row puts it: position from x, y, z, the heading from yaw (negated: setFromEuler turns clockwise)
  local ok = 0
  for it in pairs(items) do
    if it.m.p.y == 10 and it.m.p.z == 5 and it.m.p.x >= 0 and it.m.p.x <= 99 and it.m.e.z == 0 then ok = ok + 1 end
  end
  T.check(ok == 100, 'every item at its row\'s position and heading')
  T.check(matrices == 1, 'one matrix for every item made (PR #41: no MatrixF garbage an item)')
end
-- the tile keeps its items as one flat array (trees.flatten), not the decoded rows: a table per item was most of a
-- forest's Lua heap, walked by every garbage collection (GitHub #66)
collectgarbage('collect')
T.check(next(decoded) == nil, 'the decoded rows are not kept')
trees.update(1, { x = 50, y = 1900 })
T.check(count() == 20, 'far: the fifth with the lowest ranks, the same trees')
trees.update(1, { x = 50, y = 1100 })
T.check(count() == 35, 'beyond 700 m: 35 %')
trees.update(1, { x = 50, y = 600 })
T.check(count() == 60, 'middle distance: 60 %')

-- only near tiles collide: their items are re-made of solid types when the tile comes near, and of non-solid ones
-- once it is beyond SOLID_M + SOLID_HYST_M
local function solidCount()
  local n = 0
  for it in pairs(items) do if it.d and it.d.fields and it.d.fields.collidable == '1' then n = n + 1 end end
  return n
end
T.check(solidCount() == 0, 'far: nothing collides')
trees.update(1, { x = 50, y = 50 })
T.check(count() == 100 and solidCount() == 100, 'near: every item collides')
trees.update(1, { x = 50, y = 256 + trees.SOLID_M + 50 })
T.check(count() > 0 and solidCount() == count(), 'just beyond SOLID_M: still solid (hysteresis)')
trees.update(1, { x = 50, y = 256 + trees.SOLID_M + trees.SOLID_HYST_M + 20 })
T.check(count() > 0 and solidCount() == 0, 'beyond the hysteresis: re-made, not solid')
do
  local m, r = trees.made, trees.removed
  trees.update(1, { x = 50, y = 256 + trees.SOLID_M + trees.SOLID_HYST_M + 20 })
  T.check(trees.made == m and trees.removed == r, 'a settled tile of non-solid items: nothing re-made')
end
-- the camera away from the car (core.viewAway): what is shown follows the camera, what collides the car (the user,
-- 2026-10-20: "All collision ... should be following the car while visual stuff should follow the camera")
trees.update(1, { x = 50, y = 1900 }, { x = 50, y = 50 })
T.check(count() == 100 and solidCount() == 0, 'the camera at the tile, the car far: every item shown, none collides')
trees.update(1, { x = 50, y = 50 }, { x = 50, y = 1900 })
T.check(count() == 100 and solidCount() == 100, 'the car at the tile, the camera far: every item, all collide')
trees.update(1, { x = 50, y = 50 })

trees.onDrop({ anchorId = 1, key = '0_0' })
trees.update(1, { x = 50, y = 50 })
T.check(count() == 0 and trees.status().tiles == 0 and forests == 1, 'dropped: its items removed, its Forest kept')
T.check(trees.forestName(3) == 'bngEarthForest_3', 'pool Forests are numbered')
T.check(trees.typeName('/a/b-c', true) == 'bngEarthTree__a_b_c' and trees.typeName('/a/b-c') == 'bngEarthTree__a_b_c_n',
  'an item type per shape and solidity')
do  -- every item of a tile's Forest uses that Forest's slot of item types
  local names = {}
  for it in pairs(items) do names[it.d] = true end
  T.check(next(names) == nil, 'no items left before the slot check')
end

trees.onTile({ anchorId = 1, key = '0_0', path = '/t/v.json', n = 100 }, 1)
trees.update(1, { x = 50, y = 50 })
T.check(count() == 100 and forests == 1, 'the next tile reuses the empty Forest')
trees.update(2, { x = 50, y = 50 })
T.check(count() == 0, 'another anchor: removed')
trees.onTile({ anchorId = 2, key = '0_0', path = '/t/v.json', n = 100 }, 2)
trees.update(2, { x = 50, y = 50 })
trees.clear()
T.check(count() == 0 and trees.status().tiles == 0, 'clear')

-- a frame touches shapes whose items in the level add up to SHAPE_ITEMS_PER_FRAME at most (2026-10-15: a change
-- costs the engine ~0.5 us per item of each shape touched): two tiles of two different shapes, the budget below the
-- first shape's items: the second waits a frame
local savedV = trees.SHAPE_VARIANTS
trees.SHAPE_VARIANTS = 1        -- one spelling per shape here (variants: below)
local function tile(key, height, n)
  local r = {}
  for i = 0, n - 1 do r[#r + 1] = string.format('{0, %d, 10, 5, 0, 1, %d, %.3f}', i, height, i / n / 2) end
  files['/t/' .. key] = '{ groups = {}, items = { ' .. table.concat(r, ', ') .. ' } }'
  trees.onTile({ anchorId = 3, key = key, path = '/t/' .. key, n = n }, 3)
end
tile('0_0', 4, 50)              -- a small deciduous shape
trees.update(3, { x = 50, y = 50 })
T.check(count() == 50, 'the first tile: made')
local saved = trees.SHAPE_ITEMS_PER_FRAME
trees.SHAPE_ITEMS_PER_FRAME = 10
tile('1_0', 4, 20)              -- the same shape (50 in the level: over the budget, but the frame's first shape)
tile('0_1', 25, 20)             -- a tall one: another shape
trees.update(3, { x = 50, y = 50 })
T.check(count() == 70, "one frame: the first shape's items only")
trees.update(3, { x = 50, y = 50 })
T.check(count() == 90, 'the next frame: the other shape')
trees.SHAPE_ITEMS_PER_FRAME = saved

-- a dropped tile drains over frames when its shapes are over the budget, and is not re-filled meanwhile
trees.SHAPE_ITEMS_PER_FRAME = 0
trees.onDrop({ anchorId = 3, key = '0_0' })
trees.onDrop({ anchorId = 3, key = '0_1' })
trees.update(3, { x = 50, y = 50 })
T.check(count() < 90 and count() > 20, 'a frame removes one shape: ' .. count())
for _ = 1, 20 do trees.update(3, { x = 50, y = 50 }) end
T.check(count() == 20 and trees.status().tiles == 1, 'drained over frames, the kept tile untouched')
trees.SHAPE_ITEMS_PER_FRAME = saved
trees.clear()

-- new shapes (never made this session) come one a frame while driving (a frame budget under LOADING_FRAME_MS): their
-- first items load the shape in the engine's tick (75-255 ms frames, Windows 2026-11-03); any number while loading
local savedF = trees.FORESTS_PER_FRAME
trees.FORESTS_PER_FRAME = 99   -- (new Forests have a limit of their own: not this test's)
trees.reset()
tile('0_0', 4, 10)
tile('0_1', 25, 10)
trees.update(3, { x = 50, y = 50 }, nil, 4.0)
T.check(count() == 10, 'driving: one new shape a frame (' .. count() .. ')')
trees.update(3, { x = 50, y = 50 }, nil, 4.0)
T.check(count() > 10 and count() < 20, 'driving: the next new shape the frame after (the tall tile has two)')
trees.update(3, { x = 50, y = 50 }, nil, 4.0)
T.check(count() == 20, 'driving: its third shape the frame after that')
tile('1_0', 4, 10)
tile('1_1', 25, 10)
trees.update(3, { x = 50, y = 50 }, nil, 4.0)
T.check(count() == 40, 'shapes made before: not new, both in one frame (' .. count() .. ')')
trees.clear()
trees.reset()
tile('0_0', 4, 10)
tile('0_1', 25, 10)
trees.update(3, { x = 50, y = 50 }, nil, trees.LOADING_FRAME_MS)
T.check(count() == 20, 'loading, no fill said (the old rule): every new shape at once (' .. count() .. ')')
trees.clear()
trees.reset()
tile('0_0', 4, 10)
tile('0_1', 25, 10)
trees.update(3, { x = 50, y = 50 }, nil, trees.LOADING_FRAME_MS, true)
T.check(count() == 20, 'filling (a teleport): every new shape at once (' .. count() .. ')')
-- standing still is the governor's loading mode too (the bigger Lua budget), but not a fill (PR #1128): a parked player
-- sees each first load's frame (104-356 ms hitches while slow, 2026-11-10), so new shapes still come one a frame...
local filled = {}
for it in pairs(items) do filled[#filled + 1] = string.format('%.0f,%.0f,%.2f', it.m.p.x, it.m.p.y, it.sc) end
table.sort(filled)
trees.clear()
trees.reset()
tile('0_0', 4, 10)
tile('0_1', 25, 10)
trees.update(3, { x = 50, y = 50 }, nil, trees.LOADING_FRAME_MS, false)
T.check(count() == 10, 'standing: one new shape a frame (' .. count() .. ')')
local passes = 1
while count() < 20 and passes < 10 do trees.update(3, { x = 50, y = 50 }, nil, trees.LOADING_FRAME_MS, false); passes = passes + 1 end
T.check(count() == 20 and passes <= 3, '... split over ' .. passes .. ' passes')
-- ... and the same items stand afterwards as a fill makes at once (order-independent)
local spread = {}
for it in pairs(items) do spread[#spread + 1] = string.format('%.0f,%.0f,%.2f', it.m.p.x, it.m.p.y, it.sc) end
table.sort(spread)
T.check(#filled == #spread and table.concat(filled, ' ') == table.concat(spread, ' '),
  'the same items, filled at once or spread over passes')
trees.clear()
-- the loading cover (fill) warms the shapes a tile's rows will need, shown or not yet (a far tile's bushes): one item
-- each, far under the car; a pass while driving after it finds them loaded (any number in one frame)
trees.reset()
files['/t/warm'] = '{ items = { {2, 5, 10, 5, 0, 1, 1.5, 0.0}, {2, 9, 10, 5, 0, 1, 3.3, 0.0} } }'
trees.onTile({ anchorId = 3, key = '8_0', path = '/t/warm', n = 2 }, 3)
local w0 = trees.warmed
trees.update(3, { x = 50, y = 50, z = 7 }, nil, trees.LOADING_FRAME_MS, true)
T.check(trees.warmed - w0 == 2 and count() == 2, 'the cover: a far tile\'s two bush shapes warmed (' .. (trees.warmed - w0) .. ')')
local deep = true
for it in pairs(items) do if it.m.p.z > 7 - trees.WARM_DEPTH_M + 1 then deep = false end end
T.check(deep, '... under the car')
trees.update(3, { x = 50, y = 50, z = 7 }, nil, trees.LOADING_FRAME_MS, true)
T.check(trees.warmed - w0 == 2, '... once')
trees.update(3, { x = 2048 + 50, y = 50 }, nil, 4.0)
T.check(count() == 4, 'driving there: both bushes in one frame, their shapes warmed (' .. count() .. ')')
-- the session's shapes saved, and warmed at the next level start (catalogue shapes only, each once)
do
  local savedWrite, savedEnc = writeFile, jsonEncode
  writeFile = function(p, text) files[p] = text; return true end
  jsonEncode = function(t)
    local q = {}
    for _, sh in ipairs(t.shapes) do q[#q + 1] = string.format('%q', sh) end
    return '{ v = 1, shapes = { ' .. table.concat(q, ', ') .. ', "/levels/x/evil" } }'
  end
  T.check(trees.saveShapes(true) and files[trees.SHAPES_FILE] ~= nil, 'the shapes saved')
  T.check(not trees.saveShapes(true), '... not again while the set is the same')
  trees.clear()
  trees.reset()
  for it in pairs(items) do items[it] = nil end
  local w1 = trees.warmed
  local n = trees.prewarmSaved()
  T.check(n == 2 and trees.warmed - w1 == 2 and count() == 2, 'the next level: its two shapes warmed, not the stranger (' .. n .. ')')
  T.check(trees.prewarmSaved() == 0, '... once')
  writeFile, jsonEncode = savedWrite, savedEnc
end
trees.clear()
trees.reset()
for it in pairs(items) do items[it] = nil end      -- (the warm Forest's: the level takes them)
trees.FORESTS_PER_FRAME = savedF
trees.SHAPE_VARIANTS = savedV

-- shape path variants: the same file spelled differently (the engine counts them as shapes of their own)
T.check(trees.variant('/a/b/c', 0) == '/a/b/c' and trees.variant('/a/b/c', 2) == '/a/b/././c', 'variant spellings')
do
  local seen, n = {}, 0
  for _, k in ipairs({ '1:0_0', '1:1_0', '1:0_1', '1:-1_0', '1:0_-1', '1:p3_4' }) do
    local v = trees.tileVariant(k)
    T.check(v >= 0 and v < trees.SHAPE_VARIANTS, 'a variant in range: ' .. k)
    if not seen[v] then seen[v] = true; n = n + 1 end
  end
  T.check(trees.tileVariant('1:0_0') ~= trees.tileVariant('1:1_0') and trees.tileVariant('1:0_0') ~= trees.tileVariant('1:0_1'),
    'neighbouring tiles: different variants')
end

-- a rare prop's group keeps one spelling in every tile (each spelling is a first load: 40-130 ms on Windows); trees
-- keep the tile's variant
do
  T.check(trees.tileVariant('5:1_0') > 0, 'a tile with a variant')
  files['/t/dv'] = '{ groups = { "door", "deciduous" }, items = { {0, 5, 10, 5, 0, 1, 2.2, 0.0}, {1, 6, 10, 5, 0, 1, 12, 0.0} } }'
  trees.onTile({ anchorId = 5, key = '1_0', path = '/t/dv', n = 2 }, 5)
  trees.update(5, { x = 300, y = 50 })
  local door, tree = nil, nil
  for it in pairs(items) do
    local name = tostring(it.d and (it.d.shapeFile or it.d.name or it.d))
    if name:find('door') then door = name elseif name:find('tree') then tree = name end
  end
  T.check(door ~= nil and not door:find('___', 1, true), 'a door: its one spelling (' .. tostring(door) .. ')')
  T.check(tree ~= nil and not tree:find('___', 1, true), 'a tree of a small population: its first spelling ('
    .. tostring(tree) .. ')')
  trees.clear()
  trees.reset()
  -- ... the tile's variant once the shape has SPLIT_ITEMS items a spelling before it (0: always)
  local saved = trees.SPLIT_ITEMS
  trees.SPLIT_ITEMS = 0
  files['/t/dv2'] = '{ groups = { "door", "deciduous" }, items = { {1, 6, 10, 5, 0, 1, 12, 0.0} } }'
  trees.onTile({ anchorId = 5, key = '1_0', path = '/t/dv2', n = 1 }, 5)
  trees.update(5, { x = 300, y = 50 })
  tree = nil
  for it in pairs(items) do
    local name = tostring(it.d and (it.d.shapeFile or it.d.name or it.d))
    if name:find('tree') then tree = name end
  end
  T.check(tree ~= nil and tree:find('___', 1, true) ~= nil, 'a tree: the tile\'s variant (' .. tostring(tree) .. ')')
  trees.SPLIT_ITEMS = saved
  trees.clear()
  trees.reset()
end

-- bushes only near the player
files['/t/b'] = '{ items = { {2, 5, 10, 5, 0, 1, 1.5, 0.0}, {0, 6, 10, 5, 0, 1, 12, 0.0} } }'
trees.onTile({ anchorId = 4, key = '0_0', path = '/t/b', n = 2 }, 4)
trees.update(4, { x = 50, y = 50 })
T.check(count() == 2, 'near: the bush and the tree')
trees.update(4, { x = 50, y = 256 + trees.GROUP_MAX_M.shrub + 100 })
T.check(count() == 1, 'beyond the bushes: the tree only')
trees.clear()

-- batching: work every EVERY_N-th update with N times the items
trees.clear()
trees.EVERY_N = 3
files['/t/e'] = '{ groups = {}, items = { {0, 1, 10, 5, 0, 1, 12, 0.0} } }'
trees.onTile({ anchorId = 9, key = '0_0', path = '/t/e', n = 1 }, 9)
local seen = {}
for i = 1, 3 do trees.update(9, { x = 50, y = 50 }); seen[i] = count() end
T.check(seen[1] == 0 and seen[2] == 0 and seen[3] == 1, 'the third update makes the item')
trees.EVERY_N = 1
trees.clear()

-- origin rebase: a tile's Forest takes the rebase's transform and its items stay until the new anchor's tiles over it
-- are filled (2026-10-17: every tree went at a rebase and came back over ~25 s)
local shift = { apply = function(_, x, y, z) return x, y - 20, z end, beamngQuat = function() return 0, 0, 0, 1 end }
local geo = { quatMul = function(ax, ay, az, aw) return ax, ay, az, aw end }
trees.onTile({ anchorId = 5, key = '0_0', path = '/t/v.json', n = 100 }, 5)
trees.update(5, { x = 50, y = 50 })
T.check(count() == 100, 'shown')
trees.transform(shift, geo)
local moved = 0
for _, o in pairs(S.objects) do if o.class == 'Forest' and o.pos and o.pos.y == -20 then moved = moved + 1 end end
T.check(moved == 1, 'its Forest moved by the rebase')
trees.update(6, { x = 50, y = 30 })
T.check(count() == 100, 'kept under the new anchor')
trees.onTile({ anchorId = 6, key = '0_0', path = '/t/v.json', n = 100 }, 6)
trees.update(6, { x = 50, y = 30 })
T.check(count() == 200, 'half covered (its square overlaps 0_-1 too): the ghost stays')
trees.onTile({ anchorId = 6, key = '0_-1', path = '/t/v.json', n = 100 }, 6)
trees.update(6, { x = 50, y = 30 }); trees.update(6, { x = 50, y = 30 }); trees.update(6, { x = 50, y = 30 })
T.check(count() == 200 and trees.status().tiles == 2, 'covered: the ghost goes')
local reused
trees.clear()
trees.onTile({ anchorId = 7, key = '0_0', path = '/t/v.json', n = 100 }, 7)
trees.update(7, { x = 50, y = 50 })
for _, o in pairs(S.objects) do if o.class == 'Forest' and o.pos and o.pos.y == 0 and o.rot then reused = true end end
T.check(reused, 'a pooled Forest is reset to no transform when reused')
trees.clear()
-- a dense forest's tile is held to M.TILE_CAP items (Bodø, GitHub #66: 1.66 M items in the level): its cap-th rank
local function ranked(n, top)
  local rows = {}
  for i = 1, n do rows[i] = { 0, 0, 0, 0, 0, 1, 10, (n - i) / n * top } end    -- (any order)
  return { f = trees.flatten(rows), s = trees.STRIDE, n = n }
end
T.check(trees.tileThreshold(0, ranked(500, 1)) == 1.0, 'a sparse tile near the player: all of it')
T.check(math.abs(trees.tileThreshold(0, ranked(20000, 1)) - 6000 / 20000) < 1e-12, 'a dense one: its 6000 lowest ranks')
T.check(math.abs(trees.tileThreshold(0, ranked(8000, 0.3)) - 6000 / 8000 * 0.3) < 1e-12,
  'the server sent the lowest 30 % only: still its 6000 lowest')
T.check(trees.tileThreshold(1000, ranked(800, 1)) == 0.35, 'under the cap: the ring as before')
T.check(math.abs(trees.tileThreshold(2000, ranked(10000, 1)) - 400 / 10000) < 1e-12, 'far: 400')
T.check(trees.tileThreshold(0, { f = {}, s = trees.STRIDE, n = 0 }) == 1.0, 'no rows')

-- rows packed flat: STRIDE numbers a row in the server's order, the readers' defaults, a broken row never shown
do
  -- (an FFI double array where there is FFI: read through these, not # or table.concat)
  local function span(f, i, j)
    local out = {}
    for k = i, j do out[#out + 1] = string.format('%g', f[k]) end
    return table.concat(out, ',')
  end
  local f, n = trees.flatten({ { 3, 1.5, 2.5, 3.5, 90, 0.8, 14, 0.25 }, { 1, 4, 5, 6 }, 'x', { 0, nil, 1, 1 } })
  T.check(trees.STRIDE == 8 and n == 4, 'flatten: 8 numbers a row, the row count')
  T.check(span(f, 1, 8) == '3,1.5,2.5,3.5,90,0.8,14,0.25', 'flatten: a row\'s fields in order')
  T.check(span(f, 9, 16) == '1,4,5,6,0,1,10,0', 'flatten: missing yaw, scale, height, rank as read before')
  T.check(f[24] == 2 and f[32] == 2, 'flatten: a row that is no table or has no position is never shown')
  T.check(select(2, trees.flatten(nil)) == 0, 'flatten: no rows')
  T.check(trees.FFI == (type(f) == 'cdata'), 'flatten: FFI doubles where there is FFI (the collector does not walk them)')
  local p, pn = trees.packFlat({ 1, 2, 3, 4, 5, 6, 7, 8, 9, 'x' }, 8)
  T.check(pn == 1 and p[1] == 1 and p[8] == 8, 'packFlat: whole rows only')
end

-- a server that knows the mod reads flat tiles (hello.vegFlat) sends { groups, stride, flat }: kept as decoded (no copy,
-- no table per item), the same items as the rows of the same tile; a wider stride (a later server's extra fields) reads
-- the same; a broken one is an error, nothing shown
do
  local function made()
    local out = {}
    for it in pairs(items) do out[#out + 1] = string.format('%s %.2f %.2f %.2f', it.d.fields.shapeFile, it.m.p.x, it.m.p.y, it.m.p.z) end
    table.sort(out)
    return table.concat(out, ';')
  end
  local rowsText, flatVals, wide = {}, {}, {}
  for i = 0, 49 do
    local r = { i % 4, i * 3.5, 20 + i, 5, i * 7, 1, 4 + i % 9, i / 50 }
    rowsText[#rowsText + 1] = '{' .. table.concat(r, ', ') .. '}'
    for _, v in ipairs(r) do flatVals[#flatVals + 1] = v; wide[#wide + 1] = v end
    wide[#wide + 1] = 99                       -- (a ninth field this mod does not know)
  end
  files['/t/rows'] = '{ groups = {}, items = { ' .. table.concat(rowsText, ', ') .. ' } }'
  files['/t/flat'] = '{ groups = {}, stride = 8, flat = { ' .. table.concat(flatVals, ', ') .. ' } }'
  files['/t/wide'] = '{ groups = {}, stride = 9, flat = { ' .. table.concat(wide, ', ') .. ' } }'
  trees.clear()
  trees.onTile({ anchorId = 8, key = '0_0', path = '/t/rows', n = 50 }, 8)
  trees.update(8, { x = 50, y = 50 })
  local fromRows = made()
  trees.clear()
  local sent = { groups = {}, stride = 8, flat = assert(load('return { ' .. table.concat(flatVals, ', ') .. ' }'))() }
  trees.onTile({ anchorId = 8, key = '0_0', path = '/t/none', n = 50, data = sent }, 8)
  trees.update(8, { x = 50, y = 50 })
  local kept = false
  for i = 1, 20 do
    local name, v = debug.getupvalue(trees.status, i)
    if name == 'tiles' then kept = v['8:0_0'] and v['8:0_0'].n == 50 and v['8:0_0'].f[1] == sent.flat[1] end
  end
  T.check(count() == 50 and made() == fromRows, 'flat: the same items as the rows')
  T.check(kept, 'flat: the decoded array\'s rows are the tile\'s (packed as the collector does not walk them)')
  trees.clear()
  trees.onTile({ anchorId = 8, key = '0_0', path = '/t/wide', n = 50 }, 8)
  trees.update(8, { x = 50, y = 50 })
  T.check(made() == fromRows, 'flat, stride 9: the same items')
  trees.clear()
  local errs = trees.errors
  files['/t/bad'] = '{ groups = {}, stride = 4, flat = { 0, 1, 2, 3 } }'
  trees.onTile({ anchorId = 8, key = '0_0', path = '/t/bad', n = 1 }, 8)
  trees.update(8, { x = 50, y = 50 })
  T.check(count() == 0 and trees.errors == errs + 1, 'flat with a stride under 8: an error, nothing shown')
  trees.clear()
end

-- a group the catalog has no shapes for (a newer server's sign, say): left out and logged, never a tree
trees.clear()
files['/t/u'] = '{ groups = { "bench", "nl_unknown_sign" }, items = { {0, 1, 10, 0, 0, 1, 1, 0.0}, {1, 2, 10, 0, 0, 1, 3, 0.0} } }'
trees.onTile({ anchorId = 4, key = 'u', path = '/t/u', n = 2 }, 4)
trees.update(4, { x = 2, y = 10 })
T.check(count() == 1, 'unknown group: only the bench is made')
T.check(trees.unknownGroups.nl_unknown_sign == true, 'unknown group: noted')
trees.clear()

-- the items a run made: shape, position, scale (the same trees in the same places?)
local function placed()
  local out = {}
  for it in pairs(items) do
    out[#out + 1] = string.format('%s %.2f %.2f %.2f %.3f %s', it.d.fields.shapeFile, it.m.p.x, it.m.p.y, it.m.p.z, it.sc,
      it.d.fields.collidable)
  end
  table.sort(out)
  return table.concat(out, ';')
end

-- a tree frame's Lua time (FRAME_MS; a fake clock, 1 ms an item made): the frame stops once it is spent, the next goes
-- on from the row it stopped at (not the tile's first), and in the end the same items in the same places as without it
do
  trees.onTile({ anchorId = 10, key = '0_0', path = '/t/v.json', n = 100 }, 10)
  trees.update(10, { x = 50, y = 50 })
  local unbudgeted = placed()
  trees.clear()
  local now, clock = 0, trees.clock
  CREATE_HOOK = function() now = now + 1 end
  trees.clock = function() local t0 = now; return function() return now - t0 end end
  trees.FRAME_MS = 10
  trees.onTile({ anchorId = 10, key = '0_0', path = '/t/v.json', n = 100 }, 10)
  trees.update(10, { x = 50, y = 50 })
  T.check(count() == 11, 'a frame stops once its time is spent: ' .. count())
  trees.update(10, { x = 50, y = 50 })
  T.check(count() == 22, 'the next frame goes on: ' .. count())
  T.check(trees.lastRows == 100 - 11, 'from the row it stopped at: ' .. tostring(trees.lastRows))
  for _ = 1, 10 do trees.update(10, { x = 50, y = 50 }) end
  T.check(count() == 100 and placed() == unbudgeted, 'in the end the same items in the same places')
  for _ = 1, 10 do trees.update(10, { x = 50, y = 1900 }) end
  T.check(count() == 20 and solidCount() == 0, 'far: thinned to the far ring, nothing solid')
  -- near again: its 20 items re-made solid, as many as the caller's time for the frame allows (frameMs)
  trees.update(10, { x = 50, y = 50 }, nil, 3)
  T.check(count() == 20 and solidCount() == 4, "the caller's time for the frame (frameMs): " .. solidCount())
  CREATE_HOOK, trees.clock, trees.FRAME_MS = nil, clock, math.huge
  trees.clear()
end

-- frameMs 0 (core: the governor's heavy frame or its quiet frames): no tree work, and the batch waits for a frame with
-- time, not the next EVERY_N-th
do
  trees.EVERY_N = 3
  trees.onTile({ anchorId = 11, key = '0_0', path = '/t/v.json', n = 100 }, 11)
  trees.update(11, { x = 50, y = 50 }, nil, 0)
  trees.update(11, { x = 50, y = 50 }, nil, 0)
  trees.update(11, { x = 50, y = 50 })
  trees.update(11, { x = 50, y = 50 })
  T.check(count() == 0, 'heavy frames do not count towards the batch')
  trees.update(11, { x = 50, y = 50 }, nil, 0)
  T.check(count() == 0, 'none in a heavy frame')
  trees.update(11, { x = 50, y = 50 })
  T.check(count() == 100, 'the batch in the next frame with time')
  trees.EVERY_N = 1
  trees.clear()
end

-- a new Forest costs ~9 ms: one a frame at most (two new tiles, no Forest in the pool: the second waits a frame), and
-- spares made in frames that change nothing, POOL_SPARE at most, taken by the next new tiles
do
  trees.reset()                 -- (an empty pool, as a new level)
  local f0 = forests
  trees.onTile({ anchorId = 12, key = '0_0', path = '/t/v.json', n = 100 }, 12)
  trees.onTile({ anchorId = 12, key = '1_0', path = '/t/v.json', n = 100 }, 12)
  trees.update(12, { x = 256, y = 128 })
  T.check(forests == f0 + 1 and count() == 100, 'one new Forest a frame: the second tile waits')
  trees.update(12, { x = 256, y = 128 })
  T.check(forests == f0 + 2 and count() == 200, 'the next frame: the second tile')
  trees.POOL_SPARE = 2
  trees.update(12, { x = 256, y = 128 })
  T.check(forests == f0 + 3 and count() == 200, 'a frame that changed nothing makes a spare Forest')
  trees.update(12, { x = 256, y = 128 })
  trees.update(12, { x = 256, y = 128 })
  T.check(forests == f0 + 4, 'POOL_SPARE spares at most')
  trees.onTile({ anchorId = 12, key = '0_1', path = '/t/v.json', n = 100 }, 12)
  trees.onTile({ anchorId = 12, key = '1_1', path = '/t/v.json', n = 100 }, 12)
  trees.update(12, { x = 256, y = 128 })
  T.check(forests == f0 + 4 and count() == 400, 'the next new tiles take the spares: no Forest made in their frame')
  trees.POOL_SPARE = 0
  trees.clear()
end
-- spares only while calm (the governor's loading budget: a teleport, the level's start, the car standing), none while
-- driving (a ~9 ms Forest every 3 s of a town drive made the game's time run unevenly, 2026-10-29)
T.check(trees.spareWanted(FRAME_MS) == 0, 'driving (the real frame budget): no spare Forests made')
T.check(trees.spareWanted(trees.LOADING_FRAME_MS) == trees.POOL_SPARE_CALM, 'calm: a bigger pool made ready')
T.check(trees.spareWanted(nil) == trees.POOL_SPARE, 'no governor: POOL_SPARE')

-- the row cap (ROWS_PER_FRAME) leaves a tile's refresh (navgraph's signal heads: M.suppress) for a later frame; it
-- dropped it, and the tile kept its static signal under the switchable one
do
  files['/t/sig'] = '{ groups = { "signal" }, items = { {0, 10, 10, 0, 0, 1, 9, 0.0} } }'
  trees.onTile({ anchorId = 13, key = '0_0', path = '/t/sig', n = 1 }, 13)
  trees.onTile({ anchorId = 13, key = '1_0', path = '/t/sig', n = 1 }, 13)
  trees.update(13, { x = 256, y = 128 })
  T.check(count() == 2, 'a signal in each tile')
  local cap = trees.ROWS_PER_FRAME
  trees.ROWS_PER_FRAME = 0
  trees.suppress = function(g) return g == 'signal' end
  trees.refresh()
  trees.update(13, { x = 256, y = 128 })
  T.check(count() == 1, 'the row cap: one tile this frame')
  trees.update(13, { x = 256, y = 128 })
  T.check(count() == 0, 'the other tile in the next')
  trees.suppress, trees.ROWS_PER_FRAME = nil, cap
  trees.clear()
end
-- a road geotile's static signal (its own frame) hidden by a switchable head where its placement puts it in the
-- anchor's world (both stood side by side: the user's report 21, Fells Point 2026-11-12)
do
  local gd, gt = require('beamngEarth/geodesy'), require('beamngEarth/geotile')
  local LAT, LON = 39.2834, -76.5934
  local x, y = gt.tileOf(LAT, LON)
  local tf = gt.frameOf({ key = gt.key(x, y) })
  local a = gd.newAnchor(LAT, LON + 300 / (111320 * math.cos(math.rad(LAT))))
  trees.onGeoRows('psig', gt.key(x, y), { { 0, 10, 10, 2, 0, 1, 9, 0.0 } }, { 'signal' }, nil, tf, 0, a)
  local pl = gt.placement(tf, a, 0, 0, 0)
  local hx, hy = gt.apply(pl, 10, 10, 2)
  T.check(math.abs(hx - 10) > 100, 'the anchor 300 m away: placed far from its frame coordinates')
  trees.update(15, { x = hx, y = hy })
  T.check(count() == 1, 'the static signal made')
  trees.suppress = function(g, sx, sy) return g == 'signal' and math.abs(sx - hx) < 4 and math.abs(sy - hy) < 4 end
  trees.refresh('signal')
  for _ = 1, 3 do trees.update(15, { x = hx, y = hy }) end
  T.check(count() == 0, 'hidden where the head stands in the world')
  trees.suppress = nil
  trees.clear()
end
-- a group turned off (settings: Rocks, GitHub #5): its items go, back when on again
do
  files['/t/rocks'] = '{ groups = { "rock", "deciduous" }, items = { {0, 10, 10, 0, 0, 1, 2, 0.0}, {1, 20, 10, 0, 0, 1, 12, 0.0} } }'
  trees.onTile({ anchorId = 14, key = '0_0', path = '/t/rocks', n = 2 }, 14)
  trees.update(14, { x = 10, y = 10 })
  T.check(count() == 2, 'a rock and a tree')
  trees.setGroupEnabled({ 'rock' }, false)
  trees.update(14, { x = 10, y = 10 })
  T.check(count() == 1, 'rocks off: the rock gone')
  trees.setGroupEnabled({ 'rock' }, true)
  trees.update(14, { x = 10, y = 10 })
  T.check(count() == 2, 'rocks on again: back')
  trees.clear()
end
do -- GitHub #415: the static boom's copy has its lamps' material renamed (unlit), the rest of the shape as it was
  local dae = '<material id="instanced_trafficlight-material" name="instanced_trafficlight"><x name="concrete_plain"/>'
  local out = trees.staticSignalDae(dae)
  T.check(out and not out:find('instanced_trafficlight', 1, true), 'no instanced lamp material left')
  T.check(out and out:find(trees.STATIC_SIGNAL_MAT .. '-material', 1, true) and out:find('concrete_plain', 1, true),
    'renamed, the others kept')
  T.check(trees.staticSignalDae('<COLLADA/>') == nil and trees.staticSignalDae(nil) == nil, 'not the boom: none')
  local m = trees.staticSignalMaterial()[trees.STATIC_SIGNAL_MAT]
  T.check(m and m.mapTo == trees.STATIC_SIGNAL_MAT and not m.Stages[1].instanceEmissive and not m.Stages[1].emissive,
    'its material does not glow')
end
do -- Left-kerb static signals use the unlit reflected asset, at the stock model's scale.
  local savedWrite = writeFile
  writeFile = function(p, text) files[p] = text end
  files['/t/signal_left'] = '{ groups = { "signal_left" }, items = { {0, 10, 10, 0, 0, 1, 9.4, 0} } }'
  trees.onTile({ anchorId = 14, key = '0_0', path = '/t/signal_left', n = 1 }, 14)
  trees.update(14, { x = 10, y = 10 })
  local it = next(items)
  T.check(count() == 1 and it.d.fields.shapeFile == trees.STATIC_SIGNAL_LEFT .. '.dae', 'unlit reflected forest signal')
  T.check(it and math.abs(it.sc - 1) < 1e-9 and trees.ONE_SPELLING.signal_left, 'stock scale, one cached shape spelling')
  trees.clear()
  writeFile = savedWrite
end
do -- the median barriers' groups (server guardrails.GROUPS): shapes at scale 1, never thinned like trees
  for _, g in ipairs({ 'jersey', 'jersey_end', 'cable' }) do
    local shape, sc = trees.pick(g, g == 'cable' and 1.0 or 0.88, 0.3)
    T.check(trees.CATALOG[g] and shape == trees.CATALOG[g][1][1] and math.abs(sc - 1) < 1e-9, g .. ': its model at 1:1')
    T.check(trees.isBarrier(g), g .. ': a barrier')
  end
  T.check(trees.isBarrier('guardrail_back_end') and not trees.isBarrier('deciduous') and not trees.isBarrier(nil),
    'guard rails are barriers, trees not')
end
do -- Germany's junction signs (server designs.GROUPS): each mesh at its true size, never thinned, one spelling
  for _, g in ipairs({ 'de_z205', 'de_z206_g1', 'de_z306_p4_g3', 'de_z307' }) do
    local c = trees.CATALOG[g] and trees.CATALOG[g][1] or {}
    T.check(c[1] == '/levels/beamng_earth/art/shapes/beamngEarth/bne_' .. g, g .. ': its own mesh')
    local shape, sc = trees.pick(g, c[2] or 1, 0.3)
    T.check(c[1] ~= nil and shape == c[1] and math.abs(sc - 1) < 1e-9, g .. ': at 1:1')
    T.check(trees.isBarrier(g) and trees.GROUP_MAX_M[g] == 300 and trees.ONE_SPELLING[g],
      g .. ': kept, near only, one spelling')
  end
end
do -- stock world props (server worldprops.py): the catalog height is the mesh, so that height is 1:1
  local props = {
    { 'celltower', 28.93 }, { 'watertower', 26.16 }, { 'barn', 8.41 }, { 'container', 2.59 }, { 'billboard', 11.95 },
  }
  for _, it in ipairs(props) do
    local shape, sc = trees.pick(it[1], it[2], 0)
    T.check(trees.CATALOG[it[1]] and shape == trees.CATALOG[it[1]][1][1] and math.abs(sc - 1) < 1e-6,
      it[1] .. ': its model at 1:1')
    T.check(trees.GROUP_MAX_M[it[1]] and trees.GROUP_MAX_M[it[1]] > 0, it[1] .. ': a draw distance')
  end
end
trees.FRAME_MS, trees.POOL_SPARE = FRAME_MS, POOL_SPARE
-- a pass with nothing to do is skipped (the game speed pulsing, 2026-10-28): until a tile comes or goes, the car moves
-- RECHECK_M, or RECHECK_S goes by
do
  trees.reset()
  trees.POOL_SPARE = 0
  trees.onTile({ anchorId = 13, key = '0_0', path = '/t/v.json', n = 100 }, 13)
  for _ = 1, 4 do trees.update(13, { x = 128, y = 128 }) end
  local s0 = trees.skipped or 0
  trees.update(13, { x = 128, y = 128 })
  trees.update(13, { x = 129, y = 128 })
  T.check((trees.skipped or 0) == s0 + 2, 'nothing changed and the car stood: the passes are skipped')
  trees.update(13, { x = 128 + trees.RECHECK_M + 1, y = 128 })
  T.check((trees.skipped or 0) == s0 + 2, 'the car moved RECHECK_M: a pass')
  trees.onTile({ anchorId = 13, key = '1_0', path = '/t/v.json', n = 100 }, 13)
  trees.update(13, { x = 128 + trees.RECHECK_M + 1, y = 128 })
  T.check((trees.skipped or 0) == s0 + 2 and count() == 200, 'a new tile: a pass at once')
  trees.update(13, { x = 128 + trees.RECHECK_M + 1, y = 128 })
  T.check((trees.skipped or 0) == s0 + 3, 'then skipped again')
end
do -- roadside delineators stay traversable, even in the solid near-car pool
  trees.clear()
  files['/t/balises'] = '{ groups = { "fr_j1", "fr_j3", "fr_j6" }, items = { {0, 20, 20, 0, 0, 1, 1.3, 0}, {1, 30, 20, 0, 0, 1, 1.3, 0}, {2, 40, 20, 0, 0, 1, 1.05, 0} } }'
  trees.onTile({ anchorId = 14, key = '0_0', path = '/t/balises', n = 3 }, 14)
  for _ = 1, 4 do trees.update(14, { x = 30, y = 20 }) end
  T.check(count() == 3 and solidCount() == 0, 'J1 J3 J6: visible but never solid near the car')
  trees.update(14, { x = 30, y = 650 })
  trees.update(14, { x = 30, y = 20 })
  T.check(count() == 3 and solidCount() == 0, 'J1 J3 J6: a revisit never enables collisions')
end
do -- country-specific delineators use the same non-solid pools on revisit
  trees.clear()
  files['/t/eubalises'] = '{ groups = { "eu_marker_de", "eu_marker_pl", "eu_marker_gb" }, items = { {0, 20, 20, 0, 0, 1, 1.15, 0}, {1, 30, 20, 0, 0, 1, 1.15, 0}, {2, 40, 20, 0, 0, 1, 1.15, 0} } }'
  trees.onTile({ anchorId = 15, key = '0_0', path = '/t/eubalises', n = 3 }, 15)
  for _ = 1, 4 do trees.update(15, { x = 30, y = 20 }) end
  T.check(count() == 3 and solidCount() == 0, 'European delineators are visible and never collidable')
  trees.update(15, { x = 30, y = 650 })
  trees.update(15, { x = 30, y = 20 })
  T.check(count() == 3 and solidCount() == 0, 'European delineator revisit remains non-solid')
  T.check(trees.CATALOG.stop_arret[1][1]:match('stop_arret$') ~= nil, 'Quebec uses the accented ARRET asset')
  T.check(trees.CATALOG.stop_dur[1][1]:match('stop_dur$') ~= nil, 'Turkey uses the DUR asset')
end
-- geotiles (server --geo-tiles on): a tile in its own frame is made where its placement puts it in the anchor's
-- world; a rebase moves its Forest at once, then it is made again at the new placement (far too: the engine draws
-- far impostors without the Forest's transform); it is no ghost and needs nothing from the server again
do
  local gd = require('beamngEarth/geodesy')
  local gt = require('beamngEarth/geotile')
  trees.clear()
  trees.reset()
  for k in pairs(items) do items[k] = nil end
  trees.POOL_SPARE, trees.RECHECK_S = 0, -1
  local LAT, LON = 39.2834, -76.5934
  local A = gd.newAnchor(LAT - 0.02, LON - 0.1)        -- the world before: ~9 km away
  local gx, gy = gt.tileOf(LAT, LON)
  local key = gt.key(gx, gy)
  local cl, cn = gt.centre(17, gx, gy)
  local tf = gd.newAnchor(cl, cn)
  local grows = {}
  for i = 0, 49 do grows[#grows + 1] = string.format('{0, %d, %d, %d, 30, 1, 12, %.2f}', i * 4 - 100, i * 3 - 75,
    40 + i, i / 100) end
  files['/t/g.json'] = '{ groups = {}, items = { ' .. table.concat(grows, ', ') .. ' } }'
  trees.onTile({ anchorId = 21, key = key, path = '/t/g.json', n = 50, frame = { cl, cn }, pivot = 60 }, 21, A)
  local px, py = A:geodeticToWorld(cl, cn, 0)
  trees.update(21, { x = px + 1000, y = py })
  T.check(count() == 35, 'a geotile 1 km off: its lower ranks made (not colliding)')
  local function off(pl)            -- the worst distance of an item from its row placed by pl
    local worst = 0
    for it in pairs(items) do
      local best = math.huge
      for k = 0, 49 do
        local x, y, z = gt.apply(pl, k * 4 - 100, k * 3 - 75, 40 + k)
        best = math.min(best, math.abs(x - it.m.p.x) + math.abs(y - it.m.p.y) + math.abs(z - it.m.p.z))
      end
      worst = math.max(worst, best)
    end
    return worst
  end
  local w = off(gt.placement(tf, A, 0, 0, 60))
  T.check(w < 1e-6, 'each item at its row placed into the anchor\'s world: ' .. w)
  local made = trees.made
  -- a rebase: the new anchor at the tile
  local B = gd.newAnchor(cl, cn)
  local xf = A:worldTransformTo(B, px, py, 60)
  trees.transform(xf, gd, B)
  T.check(trees.status().tiles == 1 and count() == 35, 'kept across the rebase, no ghost')
  T.check(trees.geoMoved == 1, 'placed again')
  -- its Forest takes the move at once (drawn in place near); far as well as near its items are made again at the
  -- new placement and the Forest goes back to the origin (the engine draws far items' impostors without the Forest's
  -- transform: whole tiles floated 767 m off, 2026-11-01)
  local f
  for _, o in pairs(S.objects) do if o.class == 'Forest' and o.pos and math.abs(o.pos.x) > 1 then f = o end end
  T.check(f ~= nil, 'its Forest took the move')
  for _ = 1, 4 do trees.update(21, { x = 1000, y = 0 }) end
  T.check(trees.geoFlushed == 1 and trees.made == made + 35 and count() == 35,
    'far: made again (once) at the new placement: ' .. tostring(trees.geoFlushed) .. ' ' .. (trees.made - made))
  f = nil
  for _, o in pairs(S.objects) do if o.class == 'Forest' and o.pos and math.abs(o.pos.x) > 1 then f = o end end
  T.check(f == nil, 'its Forest back at the origin')
  -- the player at it: it collides, its higher ranks made too, not made again
  for _ = 1, 4 do trees.update(21, { x = 0, y = 0 }) end
  T.check(trees.geoFlushed == 1 and count() == 50, 'near: its higher ranks added, no second flush')
  w = off(gt.placement(tf, B, 0, 0, 60))
  T.check(w < 1e-6, 'at the new placement: ' .. w)
  -- its drop by key, whatever the anchor
  trees.onDrop({ anchorId = 99, key = key })
  for _ = 1, 3 do trees.update(21, { x = 0, y = 0 }) end
  T.check(count() == 0 and trees.status().tiles == 0, 'dropped')
  trees.RECHECK_S = 1.0
end

-- a suppress rule's refresh re-runs only the tiles holding that group (navgraph: 'signal'), not every tile
do
  trees.onRows(31, 'sigA', { { 0, 1, 1, 0, 0, 1, 5, 0.1 }, { 1, 3, 3, 0, 0, 1, 5, 0.1 } }, { 'deciduous', 'signal' })
  trees.onRows(31, 'treeB', { { 0, 5, 5, 0, 0, 1, 5, 0.1 } }, { 'deciduous', 'signal' })
  for _ = 1, 30 do trees.update(31, { x = 0, y = 0 }) end
  local tl = {}
  for i = 1, 200 do local n, v = debug.getupvalue(trees.update, i) if not n then break end if n == 'tiles' then tl = v end end
  local a, b
  for k, t in pairs(tl) do
    if k:find('sigA', 1, true) then a = t elseif k:find('treeB', 1, true) then b = t end
  end
  T.check(a and b and not a.pending and not b.pending, 'refresh: both tiles done first')
  T.check(trees.tileHasGroup(a, 'signal') and not trees.tileHasGroup(b, 'signal'), 'refresh: which tile holds signals')
  trees.refresh('signal')
  T.check(a.pending == true and not b.pending, 'refresh: only the tile with signals again')
  trees.refresh()
  T.check(b.pending == true, 'refresh: no group: every tile')
  trees.onDrop({ anchorId = 31, key = 'sigA' })
  trees.onDrop({ anchorId = 31, key = 'treeB' })
  for _ = 1, 30 do trees.update(31, { x = 0, y = 0 }) end
end

-- the pass order's sort keys: nearer first, the index only between equal quarter metres, the index recoverable
do
  local ks = { trees.orderKey(300.2, 1), trees.orderKey(12.0, 2), trees.orderKey(12.3, 3), trees.orderKey(0, 4),
    trees.orderKey(12.0, 5) }
  table.sort(ks)
  local at = {}
  for i, k in ipairs(ks) do at[i] = k % 2 ^ 20 end
  T.check(table.concat(at, ',') == '4,2,5,3,1', 'pass order: ' .. table.concat(at, ','))
  T.check(trees.orderKey(1e9, 7) % 2 ^ 20 == 7 and trees.orderKey(1e9, 7) < 2 ^ 53, 'pass order: far tiles capped')
end
do -- far clumps (server vegetation.CLUMP_OF, GitHub #1074): the clump beyond CLUMP_M, the trees it stands for within it
  local function shapes()
    local out = {}
    for it in pairs(items) do
      local sf = it.d.fields.shapeFile
      local k = sf:find('group_low', 1, true) and 'clump' or sf:find('douglasfir', 1, true) and 'fir' or 'other'
      out[k] = (out[k] or 0) + 1
    end
    return out
  end
  for k in pairs(items) do items[k] = nil end
  -- three firs of a square (scale < 0), its clump, and a lone fir
  trees.onRows(41, '0_0', { { 0, 10, 10, 0, 0, -1, 20, 0.1 }, { 0, 14, 10, 0, 0, -1, 21, 0.2 },
    { 0, 12, 14, 0, 0, -1, 22, 0.3 }, { 1, 12, 11, 0, 0, 1, 21, 0.1 }, { 0, 100, 100, 0, 0, 1, 20, 0.05 } },
    { 'conifer', 'conifer_clump' })
  for _ = 1, 10 do trees.update(41, { x = 0, y = 0 }) end
  local s = shapes()
  T.check(s.fir == 4 and not s.clump, 'near: the square\'s firs and the lone one, no clump')
  for _ = 1, 10 do trees.update(41, { x = 0, y = 256 + trees.CLUMP_M + 100 }) end
  s = shapes()
  T.check(s.fir == 1 and s.clump == 1, 'far: the clump and the lone fir, not the square\'s firs')
  T.check(trees.clumpDistance('normal', 'normal', 200) == 200 and trees.clumpDistance('lowest', 'far', 200) == 130,
    'clump distance by density and distance')
  -- the far cap counts the clumps and the single trees, not the trees the clumps stand for
  local rows = {}
  for i = 1, 3000 do rows[#rows + 1] = { 0, i % 200, 50 + i % 7, 0, 0, -1, 20, 0.001 } end     -- (low ranks first)
  for i = 1, 300 do rows[#rows + 1] = { 1, i % 200, 60, 0, 0, 1, 20, 0.1 } end
  for k in pairs(items) do items[k] = nil end
  trees.onRows(41, '0_0', rows, { 'conifer', 'conifer_clump' })
  for _ = 1, 20 do trees.update(41, { x = 0, y = 256 + 900 }) end      -- (the 700-1500 m ring: 900 a tile at most)
  s = shapes()
  T.check(s.clump == 300 and not s.fir, 'far cap: every clump (the 3000 trees they stand for not counted)')
  trees.onDrop({ anchorId = 41, key = '0_0' })
  for _ = 1, 20 do trees.update(41, { x = 0, y = 0 }) end
end

do -- round bales in their season only, by hemisphere (server vegetation.bale_items)
  trees.applySeason(8)
  T.check(not trees.groupOff.straw_n and trees.groupOff.straw_s and not trees.groupOff.silage_n
    and trees.groupOff.silage_s, 'August: the north\'s bales')
  trees.applySeason(1)
  T.check(trees.groupOff.straw_n and not trees.groupOff.straw_s and trees.groupOff.silage_n
    and not trees.groupOff.silage_s, 'January: the south\'s bales')
  T.check(trees.CATALOG.straw_s[1][1] == trees.CATALOG.straw_n[1][1] and trees.GROUP_MAX_M.straw_n == 400,
    'one straw bale shape, near only')
  trees.applySeason(11)
  T.check(trees.groupOff.straw_n and trees.groupOff.straw_s and trees.groupOff.silage_n and not trees.groupOff.silage_s,
    'November: the south\'s silage only')
end

do -- a pin flag (server golf.py): the mesh height, so scale 1 is 1:1, and it is not a barrier
  local shape, sc = trees.pick('golfflag', 2.05, 0.3)
  T.check(trees.CATALOG.golfflag and shape == trees.CATALOG.golfflag[1][1] and math.abs(sc - 1) < 1e-9,
    'golfflag: its model at 1:1')
  T.check(not trees.isBarrier('golfflag'), 'a pin flag is not a barrier')
end

T.check(countChecks > 50 and #countMiss == 0, 'the running item count matches a full count at each of '
  .. countChecks .. ' checks' .. (#countMiss > 0 and (' (' .. table.concat(countMiss, '; ') .. ')') or ''))
do -- the switch: COUNT_ITEMS false counts them again (the old way)
  trees.COUNT_ITEMS = false
  T.check(trees.status().items == trees.countItems(), 'COUNT_ITEMS off: counted')
  trees.COUNT_ITEMS = true
end

do -- the keep-up recorder's view (keepup.lua): rows that should stand near a spot and are not made, and the hooks
  T.check(#trees.keepupMissing(50, 10, 20) == 0, 'keepupMissing: nothing missing round a made tile')
  local made, tiles_ = {}, {}
  trees.onKeepupItem = function(t, i, g, x, y) made[#made + 1] = { t.id, i, g, x, y } end
  trees.onKeepupTile = function(key) tiles_[#tiles_ + 1] = key end
  local r2 = {}
  for i = 0, 9 do r2[#r2 + 1] = string.format('{0, %d, 300, 5, 0, 1, 12, %.2f}', i, i / 10) end
  files['/t/k.json'] = '{ groups = {}, items = { ' .. table.concat(r2, ', ') .. ' } }'
  trees.onTile({ anchorId = 1, key = '0_1', path = '/t/k.json', n = 10 }, 1)
  local miss = trees.keepupMissing(5, 300, 20)
  T.check(#miss == 10 and miss[1].key == '1:0_1' and miss[1].group == '?' and tiles_[1] == '1:0_1',
    'keepupMissing: a tile not made yet near the spot: its rows (' .. #miss .. ' ' .. tostring(miss[1] and miss[1].key)
    .. ' ' .. tostring(miss[1] and miss[1].group) .. ' ' .. tostring(tiles_[1]) .. ')')
  T.check(#trees.keepupMissing(5, 300, 3) == 7, 'keepupMissing: only the rows within r')
  trees.update(1, { x = 5, y = 300 })
  T.check(#trees.keepupMissing(5, 300, 20) == 0 and #made == 10 and made[1][1] == '1:0_1' and made[1][5] == 300,
    'keepup hooks: each item made reported with its tile, row and spot')
  trees.onKeepupItem, trees.onKeepupTile = nil, nil
  trees.onDrop({ anchorId = 1, key = '0_1' })
  trees.update(1, { x = 50, y = 50 })
end
do -- PROP_LEAD_S: at speed the furniture tiles ahead count nearer for the groups' caps (a door's 120 m)
  trees.onRows(7, 'p1_0', { { 0, 300, 128, 0, 0, 1, 2.2, 0.0 } }, { 'door' })
  local function doors() local n = 0; for it in pairs(items) do if it.m.p.x == 300 and it.m.p.y == 128 then n = n + 1 end end; return n end
  local fast = { x = 100, y = 128, vx = 30, vy = 0, speed = 30 }
  trees.update(7, fast)
  T.check(doors() == 0, 'PROP_LEAD_S off: a door 156 m out (its tile) is not shown')
  trees.PROP_LEAD_S = 4
  trees.update(7, { x = 110, y = 128, vx = 30, vy = 0, speed = 30 })
  T.check(doors() == 1, 'PROP_LEAD_S 4 at 30 m/s: shown (120 m nearer)')
  trees.update(7, { x = 120, y = 128, vx = -30, vy = 0, speed = 30 })
  T.check(doors() == 0, '... not for a tile behind the car')
  trees.PROP_LEAD_S = 0
  trees.dropKey('7:p1_0')
  trees.update(7, { x = 130, y = 128 })
end
T.check(trees.spareWanted(trees.FRAME_MS) == 0 and trees.spareWanted(trees.LOADING_FRAME_MS) >= trees.POOL_SPARE,
  'no spare Forests while driving by default (POOL_SPARE_DRIVE 0)')
T.done('test_trees')
