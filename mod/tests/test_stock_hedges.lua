-- Whole mapped hedge tiles retain their continuous fallback during budgeted Forest work.
local dir = arg[0]:match('^(.*)[/\\]') or '.'
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local items = {}
local realCreate = createObject
createObject = function(class)
  local o = realCreate(class)
  if class == 'Forest' then
    local fd = {}
    function fd:createNewItem(d, m, sc)
      local it = { d = d, m = m, sc = sc, forest = o }; items[it] = true; return it
    end
    function fd:removeItem(it) items[it] = nil end
    function o:getData() return fd end
  end
  return o
end
worldEditorCppApi = { createObject = createObject }
Sim = { getDataBlockSet = function() return { addObject = function() end } end }
loadJsonMaterialsFile = function() end
MatrixF = function() return { setFromEuler = function(self, e) self.e = e end,
  setPosition = function(self, p) self.p = p end } end
local trees = require('beamngEarth/trees')
trees.EVERY_N, trees.PER_FRAME, trees.FRAME_MS, trees.POOL_SPARE = 1, 1, math.huge, 0
local player = { x = 30, y = 30 }
local function update(n, p) for i = 1, n do trees.update(1, p or player) end end
local function rows(n)
  local rr = {}
  for i = 1, n do rr[i] = { 0, 20 + i * 2, 30, 10, 0, 1, 1.8, 0 } end
  return rr
end
local visible = false
trees.onHedges(1, 'h0_0', rows(3), { 'mapped_hedge' }, function(on) visible = on end)
update(1)
T.check(not visible and trees.status().items == 1, 'partial tile retains fallback')
for it in pairs(items) do T.check(it.forest.hidden, 'partially built Forest stays hidden') end
update(3)
T.check(visible and trees.status().items == 3, 'whole tile swaps only after all items exist')
for it in pairs(items) do
  T.check(not it.forest.hidden, 'ready Forest is visible')
  T.check(it.d.fields.collidable == '0', 'hedge remains non-colliding')
  T.near(tonumber(it.d.fields.windScale), 0.2, 1e-6, 'authored wind scale')
  T.near(tonumber(it.d.fields.rigidity), 10.2, 1e-6, 'authored wind spring')
end
trees.TILE_CAP = { { 1e9, 1 } }
trees.RINGS = { { 1e9, 0.01 } }
trees.refresh()
update(2)
T.check(visible and trees.status().items == 3, 'rank thinning cannot punch holes in a hedge')
for _, distance in ipairs({150, 151, 450, 900, 1600}) do
  update(5, { x = distance + 256, y = 30 })
  T.check(visible and trees.status().items == 3, 'stock LODs retain a complete hedge at distance '..distance)
  for it in pairs(items) do T.check(not it.forest.hidden, 'no distance-driven backstop switch') end
end
update(5)
T.check(visible, 'returning near restores the whole tile')
trees.onHedges(1, 'h0_0', rows(1), { 'mapped_hedge' }, function(on) visible = on end)
T.check(not visible, 'replaced payload immediately returns to fallback')
update(9)
T.check(visible and trees.status().items == 1, 'replacement retires old items without leaking them')

-- The same model used by an ordinary group must not inherit the hedge wind profile.
local original = trees.CATALOG.shrub
trees.CATALOG.shrub = trees.CATALOG.mapped_hedge
trees.onRows(1, 'p0_0', rows(1), { 'shrub' })
update(3)
local ordinary = false
for it in pairs(items) do
  if it.d.fields.windScale == '0.4' then ordinary = true; T.check(not it.forest.hidden, 'pooled Forest visibility resets') end
end
T.check(ordinary, 'ordinary item type keeps its existing profile')
trees.CATALOG.shrub = original
trees.dropKey('1:h0_0')
T.check(not visible, 'drop restores fallback before deletion')
update(5)
trees.clear()
T.check(trees.status().items == 0, 'clear removes every instance')
local failed = true
FS = { fileExists = function() return false end }
trees.onHedges(1, 'h0_0', rows(3), { 'mapped_hedge' }, function(on) failed = on end)
update(5)
T.check(not failed and trees.status().items == 0, 'missing stock model leaves fallback intact')
FS = nil
trees.onHedges(1, 'h0_0', rows(257), { 'mapped_hedge' }, function(on) failed = on end)
update(2)
T.check(not failed and trees.status().items == 0, 'oversized payload cannot bypass tile budgets')

-- Actual road-tile integration, including a fallback mesh arriving after the Forest completes.
local roads = require('beamngEarth/roads')
local gov = { try = function() return true end, charge = function() end, chargeMotion = function() end }
local function roadUpdate() for i = 1, 10 do roads.update(gov, 1) end end
local data = { chunks = {}, hedgeStock = { shape = '/t/stock.cdae', pos = { 0, 0, 0 } },
  hedge = { shape = '/t/ends.cdae', pos = { 0, 0, 0 } }, hedges = { items = rows(3), groups = { 'mapped_hedge' } } }
roads.onTile({ anchorId = 1, key = '0_0', path = '/t/tile', data = data }, 1)
update(5)
for it in pairs(items) do T.check(it.forest.hidden, 'Forest waits for its fallback mesh before the first swap') end
roadUpdate()
update(2)
local function fallback()
  for _, o in ipairs(S.liveOf('TSStatic')) do if o.fields.shapeName == '/t/stock.cdae' then return o end end
end
T.check(fallback() and fallback().hidden, 'late fallback mesh respects the completed swap')
for _, o in ipairs(S.liveOf('TSStatic')) do
  T.check(o.fields.collisionType == 'None', 'both hedge representations stay render-only')
  if o.fields.shapeName == '/t/ends.cdae' then T.check(not o.hidden, 'unfitting ends stay visible') end
end
update(1, { x = 450, y = 30 })
T.check(fallback().hidden, 'road fallback stays hidden beyond the former distance cutoff')
update(5)
roads.onDrop({ anchorId = 1, key = '0_0' })
roadUpdate()
update(8)
T.check(#S.liveOf('TSStatic') == 0 and trees.status().items == 0, 'ordinary drop removes both representations')
roads.onTile({ anchorId = 1, key = '0_0', path = '/t/tile', data = data }, 1)
roadUpdate()
update(5)
roads.transform({ apply = function(_, x, y, z) return x, y - 20, z end,
  beamngQuat = function() return 0, 0, 0, 1 end }, { quatMul = function(ax, ay, az, aw) return ax, ay, az, aw end })
T.check(fallback().pos.y == -20, 'stock fallback follows the existing rebase lifecycle')
roads.onDrop({ anchorId = 1, key = '0_0' })
roadUpdate()
T.check(#S.liveOf('TSStatic') > 0, 'rebased ghosts survive drops until replacement coverage')
roads.clear()
update(8)
T.check(#S.liveOf('TSStatic') == 0 and trees.status().items == 0, 'clear removes ghost meshes and their foliage')
trees.clear()
-- Current geotiles: rows survive rebase and recover after a tree clear / same-content server restart.
local geodesy=require('beamngEarth/geodesy')
local gt=require('beamngEarth/geotile')
local gx,gy=gt.tileOf(39.2834,-76.5934)
local gkey=gt.key(gx,gy)
local lat,lon=gt.centre(17,gx,gy)
local anchor=geodesy.newAnchor(lat,lon)
roads.setGeo(true)
roads.setSession('hedge-geo')
FS={fileExists=function() return true end}
local geoData={v=1,key=gkey,frame={lat,lon},h='hedge-geo',
 hedgeStock={shape='/t/stock.cdae',pos={0,0,0},pivot=0,relief=2},
 hedge={shape='/t/ends.cdae',pos={0,0,0},pivot=0,relief=2},
 hedges={groups={'mapped_hedge'},items=rows(3)}}
readFile=function() return 'geo' end
jsonDecode=function() return geoData end
local msg={anchorId=1,key=gkey,path='/t/geo',frame={lat,lon},h='hedge-geo'}
local function geoUpdate(n,a,aid)
 for _=1,n do roads.update(gov,aid or 1,nil,nil,a or anchor);trees.update(aid or 1,player) end
end
T.check(roads.onTile(msg,1,anchor),'hedge geotile accepted')
geoUpdate(10)
T.check(trees.status().items==3 and fallback().hidden,'geotile stock replaces only the fitted backstop')
local moved=geodesy.newAnchor(lat,lon+.001)
roads.replace(moved,2)
trees.transform({apply=function(_,x,y,z) return x,y,z end,
 beamngQuat=function() return 0,0,0,1 end},geodesy,moved)
geoUpdate(10,moved,2)
T.check(trees.status().items==3,'geotile foliage remains complete after rebase')
trees.clear()
T.check(not fallback().hidden,'tree reset restores geotile backstop')
T.check(roads.onTile(msg,2,moved),'same content accepted after tree reset')
geoUpdate(10,moved,2)
T.check(trees.status().items==3 and fallback().hidden,'same-content restart restores stock hedges')
roads.onDrop({key=gkey})
geoUpdate(10,moved,2)
T.check(#S.liveOf('TSStatic')==0 and trees.status().items==0,'geotile drop removes fallback and foliage')
roads.clear();trees.clear()
T.done('test_stock_hedges')
