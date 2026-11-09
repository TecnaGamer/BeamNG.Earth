-- luajit mod/tests/test_minimaproads.lua  (the minimap's roads from the server's whole graph)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
log = log or function() end
table.clear = table.clear or function(t) for k in pairs(t) do t[k] = nil end end
local R = require('beamngEarth/minimaproads')

local g = { anchor = 1,
  nodes = { a = { 0, 0, 1, 4 }, b = { 50, 0, 1, 4 }, c = { 300, 0, 1, 3 }, far = { 1050, 900, 1, 3 } },
  edges = { { 'a', 'b', 0, 1, 1, 11.2, 0.8, 4 }, { 'b', 'c', 1, 1, 0, 8.9, 1, 3 }, { 'c', 'far', 0, 1, 1, 11.2, 0.1, 3 },
            { 'b', 'a', 0, 1, 1, 11.2, 0.8, 4 }, { 'a', 'gone', 0, 1, 1, 11.2, 1, 3 } } }
local idx = R.build(g, 128)
T.check(idx.count == 3, 'one link per pair of nodes, none to a missing node: ' .. idx.count)
T.check(R.class(0.95) == 1 and R.class(0.5) == 2 and R.class(0.1) == 3 and R.class(nil) == 1, 'stock colour classes')
-- (the road colour from 0.85: Germany's give-way steps, navgraph.DE_YIELD_STEP, take a tertiary's 0.9 to 0.88, still
-- a road; no drivability class lies in 0.85 .. 0.9)
T.check(R.class(1) == 1 and R.class(0.9) == 1 and R.class(0.88) == 1 and R.class(0.85) == 1 and R.class(0.84) == 2
  and R.class(0.8) == 2 and R.class(0.26) == 2 and R.class(0.25) == 3, 'the boundaries: 0.85 and 0.25')
-- (by cell: the links of the cells the square meets, each once; the canvas clips the rest)
local q = R.query(idx, 0, 0, 60)
T.check(#q == 2 and idx.links[q[1]] == 0 and idx.links[q[1] + 2] == 50, 'near the origin: a-b, and b-c in the same cell')
q = R.query(idx, 600, -300, 50)
T.check(#q == 0, 'cells of no link: none')
q = R.query(idx, 128, 0, 200)
local once = {}
for _, l in ipairs(q) do once[l] = (once[l] or 0) + 1 end
T.check(#q == 3 and once[1] == 1 and once[6] == 1 and once[11] == 1, 'every link once though it spans several cells')
q = R.query(idx, 1000, 850, 100)
T.check(#q == 1 and idx.links[q[1] + 4] == 3, 'c-far (a long link) found near its far end, lowest colour')
T.check(#R.query(idx, -5000, -5000, 100) == 0, 'nothing far away')
-- a tunnel's stretch (not in the AI's edges): drawn too
do
  local gt = { anchor = 1, nodes = g.nodes, edges = g.edges, lines = { { -400, 0, -300, 0, 1 } } }
  local it = R.build(gt, 128)
  local ql = R.query(it, -350, 0, 20)
  T.check(it.count == 4 and #ql == 1 and it.links[ql[1]] == -400 and it.links[ql[1] + 4] == 1, 'a tunnel line drawn')
end

-- built over frames: the old index drawn until the new one is done
do
  local big = { anchor = 1, nodes = {}, edges = {} }
  for i = 0, 5000 do big.nodes['n' .. i] = { i * 10, 0, 0, 3 } end
  for i = 0, 4999 do big.edges[#big.edges + 1] = { 'n' .. i, 'n' .. (i + 1), 0, 1, 1, 11, 1, 3 } end
  local drawn = {}
  _G.ui_apps_minimap_utils = {
    getStyleColorSet = function() return { d = { color = 'C', colorLow = 'L', colorLowest = 'W' } } end,
    getDefaultStyleColorSet = function() return 'd' end,
    colors = { roadBgTransparentBlack = 'B' },
    getWorldToMapTransform = function() return 1, 0, 0, 1, 0, 0 end }
  package.loaded['ui/apps/minimap/layers'] = { ROADS_FG = 1, ROADS_BG = 2 }
  local stockCalls = 0
  _G.ui_apps_minimap_roads = { drawRoads = function() stockCalls = stockCalls + 1 end }
  local td = { lineRoundEndWithBackground = function(_, x1, y1, x2, y2, w, wb, c) drawn[#drawn + 1] = { x1, c } end }
  T.check(R.hook(), 'hooked')
  local hooked = ui_apps_minimap_roads.drawRoads
  T.check(R.hook() and ui_apps_minimap_roads.drawRoads == hooked, 'hooked once')
  ui_apps_minimap_roads.drawRoads(nil, 100, td, {}, { x = 0, y = 0 }, 0.5, 1)
  T.check(stockCalls == 1 and #drawn == 0, 'no graph yet: the stock roads')
  R.update(g)
  ui_apps_minimap_roads.drawRoads(nil, 100, td, {}, { x = 0, y = 0 }, 0.5, 1)
  T.check(#drawn == 2 and drawn[1][2] == 'L' and drawn[2][2] == 'C' and stockCalls == 1,
  'the small graph: built in one frame, drawn with the stock colours')
  local frames = 0
  repeat
    R.update(big)
    frames = frames + 1
    drawn = {}
    ui_apps_minimap_roads.drawRoads(nil, 100, td, {}, { x = 0, y = 0 }, 0.5, 1)
  until R.status().building == false or frames > 20
  T.check(frames >= 3, 'the big graph over several frames: ' .. frames)
  T.check(#drawn > 5, 'and drawn once built: ' .. #drawn)
  -- a teleport (another anchor): the old roads not drawn while the new index builds
  local other = { anchor = 2, nodes = big.nodes, edges = big.edges }
  R.update(other)
  drawn = {}
  ui_apps_minimap_roads.drawRoads(nil, 100, td, {}, { x = 0, y = 0 }, 0.5, 1)
  T.check(#drawn == 0 and stockCalls == 2, 'another anchor: the stock roads until ours is built')
  -- the module loaded anew (a new table): hooked again
  _G.ui_apps_minimap_roads = { drawRoads = function() stockCalls = stockCalls + 1 end }
  T.check(R.hook() and ui_apps_minimap_roads.drawRoads ~= hooked, 'hooked again on the new module')
  -- a failing draw: the stock roads from then on
  R.update(g)
  td.lineRoundEndWithBackground = function() error('boom') end
  ui_apps_minimap_roads.drawRoads(nil, 100, td, {}, { x = 0, y = 0 }, 0.5, 1)
  R.update(g)
  local before = stockCalls
  ui_apps_minimap_roads.drawRoads(nil, 100, td, {}, { x = 0, y = 0 }, 0.5, 1)
  T.check(stockCalls == before + 1, 'after a failure: the stock roads')
  R.unhook()
  T.check(ui_apps_minimap_roads.drawRoads ~= hooked and R.status().hooked == false, 'unhooked')
end
T.done('minimaproads')
