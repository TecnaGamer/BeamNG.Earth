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
    function fdata:createNewItem(d, m, sc) local it = { d = d, m = m, sc = sc }; items[it] = true; mine[it] = true; return it end
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
MatrixF = function() return { setFromEuler = function() end, setPosition = function(self, p) self.p = p end } end
local files = {}
readFile = function(p) return files[p] end
jsonDecode = function(s) return assert(load('return ' .. s))() end
local trees = require('beamngEarth/trees')
trees.EVERY_N = 1               -- (every update works here; the batching is tested below)
local function count() local n = 0; for _ in pairs(items) do n = n + 1 end; return n end

-- the variant: a height inside its range, scaled to it
local shape, sc = trees.pick('conifer', 20.0, 0.3)
T.check(shape:find('douglasfir_large') ~= nil and sc > 0.9 and sc < 1.1, 'a 20 m conifer: a large fir near its size')
shape, sc = trees.pick('rock', 0.6, 0.1)
T.check(shape:find('rock') ~= nil and math.abs(sc - 0.6 / select(2, unpack({ shape, 0.6 })) * 1) < 2, 'a rock')

-- a tile of 100 items with ranks 0 .. 0.99 at the player: all made; far away: a quarter
local rows = {}
for i = 0, 99 do rows[#rows + 1] = string.format('{0, %d, 10, 5, 0, 1, 12, %.2f}', i, i / 100) end
files['/t/v.json'] = '{ groups = {}, items = { ' .. table.concat(rows, ', ') .. ' } }'
trees.onTile({ anchorId = 1, key = '0_0', path = '/t/v.json', n = 100 }, 1)
trees.update(1, { x = 50, y = 50 })
T.check(count() == 100 and trees.status().items == 100, 'near: every item')
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
trees.update(1, { x = 50, y = 50 })

trees.onDrop({ anchorId = 1, key = '0_0' })
trees.update(1, { x = 50, y = 50 })
T.check(count() == 0 and trees.status().tiles == 0 and forests == 1, 'dropped: its items removed, its Forest kept')
for _, o in pairs(S.objects) do
  if o.class == 'Forest' then T.check(o.hidden == true, 'released empty Forest is hidden in pool') end
end
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
for _, o in pairs(S.objects) do
  if o.class == 'Forest' then T.check(o.hidden == false, 'reused Forest is unhidden') end
end
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

-- R2: per-type shadowNullDetailSize (solid datablock = 35, non-solid datablock = 150)
do
  local sawSolid35, sawNonSolid150 = false, false
  for _, o in pairs(S.objects) do
    if o.class == 'TSForestItemData' and o.fields then
      if o.fields.collidable == '1' and o.fields.shadowNullDetailSize == '35' then
        sawSolid35 = true
      elseif o.fields.collidable == '0' and o.fields.shadowNullDetailSize == '150' then
        sawNonSolid150 = true
      end
    end
  end
  T.check(sawSolid35, 'solid TSForestItemData datablock gets shadowNullDetailSize 35')
  T.check(sawNonSolid150, 'non-solid TSForestItemData datablock gets shadowNullDetailSize 150')
end

-- P10-B / R1 / R5: distinct-Forest mutation cap, burst=true/urgent=false -> 1, urgent=true -> 2,
-- near-solid tile (dCar <= SOLID_M) bypasses the cap, and shadowctl.noteBurst() called once per mutated frame
do
  local okSc, scMod = pcall(require, 'beamngEarth/shadowctl')
  if not okSc or type(scMod) ~= 'table' then
    scMod = {}
    package.loaded['beamngEarth/shadowctl'] = scMod
  end
  local origNoteBurst = scMod.noteBurst
  local burstCalls = 0
  scMod.noteBurst = function(...)
    burstCalls = burstCalls + 1
    if origNoteBurst then return origNoteBurst(...) end
  end

  local function queueFarTiles(aid)
    -- Place 4 tiles around player at (128, 128) with dCar = sqrt(128^2 + 128^2) ≈ 181 m (> SOLID_M = 90, <= 220 m)
    for _, k in ipairs({ '1_1', '1_-1', '-1_1', '-1_-1' }) do
      local r = {}
      for i = 0, 4 do r[#r + 1] = string.format('{0, %d, 10, 5, 0, 1, 12, %.3f}', i, i / 20) end
      files['/t/f_' .. k] = '{ groups = {}, items = { ' .. table.concat(r, ', ') .. ' } }'
      trees.onTile({ anchorId = aid, key = k, path = '/t/f_' .. k, n = 5 }, aid)
    end
  end

  -- 1) burst=true, urgent=false -> at most 1 distinct Forest mutated per update (for non-solid tiles)
  queueFarTiles(11)
  local govStub = {
    burst = true,
    urgent = false,
    try = function() return true end,
    charge = function() end,
  }
  burstCalls = 0
  trees.update(11, { x = 128, y = 128 }, nil, govStub)
  T.check(trees.lastForestsTouched == 1,
    'gov stub burst=true, urgent=false mutates at most 1 distinct Forest (' .. tostring(trees.lastForestsTouched) .. ')')
  T.check(burstCalls == 1, 'noteBurst called once on frame with mutations (' .. burstCalls .. ')')

  -- 2) urgent=true (even with burst=true) -> up to 2 distinct Forests mutated per update
  govStub.urgent = true
  burstCalls = 0
  trees.update(11, { x = 128, y = 128 }, nil, govStub)
  T.check(trees.lastForestsTouched == 2,
    'gov stub burst=true, urgent=true mutates up to 2 distinct Forests (' .. tostring(trees.lastForestsTouched) .. ')')
  T.check(burstCalls == 1, 'noteBurst called once per frame even when 2 Forests are mutated (' .. burstCalls .. ')')

  -- Finish remaining far tiles and verify noteBurst is NOT called when no Forest is mutated
  trees.update(11, { x = 128, y = 128 }, nil, govStub)
  burstCalls = 0
  trees.update(11, { x = 128, y = 128 }, nil, govStub)
  T.check(trees.lastForestsTouched == 0 and burstCalls == 0,
    'noteBurst not called at all on update without Forest mutations (' .. burstCalls .. ')')
  trees.clear()

  -- 3) near-solid tile (dCar <= SOLID_M) bypasses the burst=1 cap
  -- Queue two near-solid tiles at 0_0 (cx=128, cy=128) and 1_0 (cx=384, cy=128) with player at x=256, y=128 (dCar=0 for both)
  for ti = 0, 1 do
    local k = ti .. '_0'
    local r = {}
    for i = 0, 4 do r[#r + 1] = string.format('{0, %d, 10, 5, 0, 1, 12, %.3f}', i, i / 20) end
    files['/t/ns_' .. k] = '{ groups = {}, items = { ' .. table.concat(r, ', ') .. ' } }'
    trees.onTile({ anchorId = 12, key = k, path = '/t/ns_' .. k, n = 5 }, 12)
  end
  govStub.burst = true
  govStub.urgent = false
  trees.update(12, { x = 256, y = 128 }, nil, govStub)
  T.check(trees.lastForestsTouched == 2 and count() == 10,
    'near-solid tiles (dCar <= SOLID_M) bypass the burst=1 Forest cap (' .. tostring(trees.lastForestsTouched) .. ')')
  trees.clear()

  scMod.noteBurst = origNoteBurst
end
T.done('test_trees')
