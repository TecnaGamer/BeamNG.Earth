-- The minimap's roads from the server's whole road graph (navgraph.lua's graph: WINDOW_M = 1100 m round a centre ahead
-- of the car), not the game's map (the user, 2026-11-04: "the onscreen minimap can struggle to keep up with the
-- player. It's road lines don't load fast enough").
--
-- The stock minimap (ui_apps_minimap_roads) indexes map.getMap()'s links once per map.reset; that map is our graph
-- clipped to the terrain block's ground box (~390 m round the block's centre: the only collidable ground, for the AI),
-- applied again only after the block moved RECLIP_M and a light frame. At speed the car drove to the box's edge before
-- the next one came, and the minimap (zoomed out up to 1.75x above 22 mph, stock) showed the roads ending ahead.
--
-- Here: the server's graph is indexed on a grid of CELL_M cells over several frames (BUILD_EDGES links a frame, the
-- old index drawn meanwhile: no frame without roads), and drawRoads is replaced by one drawing from it the way the
-- stock one does (the same line call, widths and colours). Without a graph of ours (a stock level, a graph not yet
-- here) the stock function draws.
local M = {}

M.CELL_M = 128
M.BUILD_EDGES = 1500        -- links indexed a frame (~0.3 ms)

local index = nil           -- the index drawn: { links = {x1, y1, x2, y2, class, ...}, cells = {key -> {lIdx...}}, graph }
local building = nil        -- coroutine building the next one
local buildingFor = nil     -- the graph it is for
local stamp, seen = 0, {}   -- per draw: a link in several cells drawn once
local orig, wrap, holder = nil, nil, nil
local broken = false        -- a draw failed: the stock roads for the rest of the session

local floor = math.floor
local function key(i, j) return i * 65536 + j end

-- Pure: the drivability class of a link (the stock colours: >= 0.85 the road colour, > 0.25 the low one, else the
-- lowest). The stock minimap's road colour starts at 0.9; here at 0.85, so Germany's give-way steps
-- (navgraph.DE_YIELD_STEP) keep a tertiary's 205 approach (0.9 -> 0.88) in the road colour. No drivability class or
-- step outside Germany lies in 0.85 .. 0.9 (navgraph.DRIVE, ENTRY_DRIVABILITY 0.9): the same colours there.
function M.class(d)
  d = d or 1
  return d >= 0.85 and 1 or (d > 0.25 and 2 or 3)
end

-- Pure (a coroutine when yieldEvery): the index of graph g ({nodes = {name -> {x, y, z, radius}}, edges =
-- {{a, b, oneWay, out, back, speed, drivability, radius}}}). A link is in every cell its box (ends +- radius) touches.
function M.build(g, cellM, yieldEvery)
  cellM = cellM or M.CELL_M
  local links, cells = {}, {}
  local done = {}
  local n = 0
  for c, e in ipairs(g.edges or {}) do
    local a, b = g.nodes[e[1]], g.nodes[e[2]]
    if a and b and e[1] ~= e[2] then
      local pk = e[1] < e[2] and (e[1] .. '|' .. e[2]) or (e[2] .. '|' .. e[1])
      if not done[pk] then
        done[pk] = true
        local l = #links + 1
        links[l], links[l + 1], links[l + 2], links[l + 3], links[l + 4] = a[1], a[2], b[1], b[2], M.class(e[7])
        local ra, rb = a[4] or 0, b[4] or 0
        local i0 = floor(math.min(a[1] - ra, b[1] - rb) / cellM)
        local i1 = floor(math.max(a[1] + ra, b[1] + rb) / cellM)
        local j0 = floor(math.min(a[2] - ra, b[2] - rb) / cellM)
        local j1 = floor(math.max(a[2] + ra, b[2] + rb) / cellM)
        for i = i0, i1 do
          for j = j0, j1 do
            local k = key(i, j)
            local list = cells[k]
            if not list then list = {}; cells[k] = list end
            list[#list + 1] = l
          end
        end
      end
    end
    if yieldEvery and c % yieldEvery == 0 then coroutine.yield() end
  end
  -- the tunnel stretches (navgraph.py "lines": not the AI's, drawn all the same)
  for c, ln in ipairs(g.lines or {}) do
    local l = #links + 1
    links[l], links[l + 1], links[l + 2], links[l + 3], links[l + 4] = ln[1], ln[2], ln[3], ln[4], M.class(ln[5])
    for i = floor(math.min(ln[1], ln[3]) / cellM), floor(math.max(ln[1], ln[3]) / cellM) do
      for j = floor(math.min(ln[2], ln[4]) / cellM), floor(math.max(ln[2], ln[4]) / cellM) do
        local k = key(i, j)
        local list = cells[k]
        if not list then list = {}; cells[k] = list end
        list[#list + 1] = l
      end
    end
    if yieldEvery and c % yieldEvery == 0 then coroutine.yield() end
  end
  n = #links / 5
  return { links = links, cells = cells, cellM = cellM, count = n, graph = g }
end

-- Pure: the links (their first index in idx.links) whose cells meet the square of half size r round (x, y), each once.
function M.query(idx, x, y, r, out)
  out = out or {}
  stamp = stamp + 1
  local c = idx.cellM
  for i = floor((x - r) / c), floor((x + r) / c) do
    for j = floor((y - r) / c), floor((y + r) / c) do
      local list = idx.cells[key(i, j)]
      if list then
        for t = 1, #list do
          local l = list[t]
          if seen[l] ~= stamp then
            seen[l] = stamp
            out[#out + 1] = l
          end
        end
      end
    end
  end
  return out
end

-- Every frame (core): the index for the current graph, a frame's share at a time. graph: navgraph.currentGraph().
function M.update(graph)
  if broken or not graph or not graph.edges then
    index, building, buildingFor = nil, nil, nil
    return
  end
  if index and index.graph == graph then building, buildingFor = nil, nil return end
  if buildingFor ~= graph then
    -- (another anchor's, a teleport or rebase: its roads elsewhere in this frame, not drawn while the new one builds)
    if index and index.graph.anchor ~= graph.anchor or graph.moved then index = nil end
    buildingFor = graph
    building = coroutine.create(function() return M.build(graph, M.CELL_M, M.BUILD_EDGES) end)
  end
  local ok, res = coroutine.resume(building)
  if not ok then
    log('E', 'beamngEarth', 'minimap roads: ' .. tostring(res))
    building, buildingFor = nil, nil
    return
  end
  if coroutine.status(building) == 'dead' then
    index, building, buildingFor = res, nil, nil
    seen, stamp = {}, 0
  end
end

local list = {}
-- ui_apps_minimap_roads.drawRoads's replacement (its arguments): the links within radius of camPos.
local function draw(p, radius, td, debugSettings, camPos, scale, dpi)
  local utils = rawget(_G, 'ui_apps_minimap_utils')
  if not (index and utils and td and camPos) then return orig(p, radius, td, debugSettings, camPos, scale, dpi) end
  local set = utils.getStyleColorSet()[utils.getDefaultStyleColorSet()]
  local colours = { set.color, set.colorLow, set.colorLowest }
  local bg = utils.colors.roadBgTransparentBlack
  local xx, xy, yx, yy, ox, oy = utils.getWorldToMapTransform()
  local layers = require('ui/apps/minimap/layers')
  local fg, lbg = layers.ROADS_FG, layers.ROADS_BG
  local line = td.lineRoundEndWithBackground
  dpi = dpi or 1
  local w = 2 / (scale + 1e-10) * dpi
  local wb = w + 2 * dpi
  local links = index.links
  table.clear(list)
  M.query(index, camPos.x, camPos.y, radius, list)
  for t = 1, #list do
    local l = list[t]
    line(td, links[l], links[l + 1], links[l + 2], links[l + 3], w, wb, colours[links[l + 4]], bg, xx, xy, yx, yy,
      ox, oy, fg, lbg)
  end
  if p then p:add('Roads') end
end

-- Every frame in our level (core): drawRoads replaced (again after the minimap's module was loaded anew).
function M.hook()
  local mr = rawget(_G, 'ui_apps_minimap_roads')
  if not mr or type(mr.drawRoads) ~= 'function' then return false end
  if mr.drawRoads == wrap and holder == mr then return true end
  if holder and holder ~= mr then orig, wrap = nil, nil end
  orig, holder = mr.drawRoads, mr
  wrap = function(...)
    local ok, err = pcall(draw, ...)
    if not ok then
      log('E', 'beamngEarth', 'minimap roads draw: ' .. tostring(err))
      index, building, buildingFor, broken = nil, nil, nil, true
    end
  end
  mr.drawRoads = wrap
  return true
end

function M.unhook()
  if holder and wrap and holder.drawRoads == wrap then holder.drawRoads = orig end
  orig, wrap, holder = nil, nil, nil
  index, building, buildingFor, broken = nil, nil, nil, false
end

function M.status()
  return { links = index and index.count or 0, building = building ~= nil, hooked = wrap ~= nil }
end

return M
