-- The cars' own navigation screens (BeamNavigator pages driven by the game's bngNavigator, navigator-class.js) while
-- BeamNG.Earth is active: each page stays as the car made it (its radio bar, tabs, clock, arrow and zoom; the user,
-- 2026-10-29: "all the screens should be as they normally would be, just with the correct map or satellite view",
-- "mess with the stock navigation as little as possible"), and only its map is given our roads:
--   "map" (the dashboardMap setting's default): the server's road graph around the car (navgraph.lua's ~1.1 km window)
--     in the stock nodes format, drawn by the page's own road renderer, again whenever a new graph comes. The stock
--     request (ui_uinavi.requestVehicleDashboardMap) sends the game map's roads once, when the screen starts, and the
--     stock page draws once: our graph moves on every ~400 m;
--   "satellite": the same, with the companion's satellite tiles as the map's terrain images under the roads (each Web
--     Mercator tile placed by its corners in the world frame).
-- Nothing of the page is replaced: one small function goes with every update (__beNav, PAGE_JS) and redraws through
-- the page's own map layer (applyMapSize, renderMap), sizing the map itself. The stock path cannot redraw: a second
-- setData redraws only every other time once the map has terrain tiles, and its sizing falls back to a box around
-- world (0, 0) whenever the content lies east or south of the origin (most of a drive between rebases)
-- (docs/research: the 2026-10-29 navigator study, navigator-class.js 171-191, 516-588).
-- The car's position, heading and speed stay the vehicle's own updateData (beamNavigator.lua, 15 fps).
-- Only the player's car's BeamNavigator screens (initmap nil) get our map; gauge screens with a map of their own
-- (genericGauges "initMap") and other cars keep the stock data.
local M = {}

M.STYLES = { map = true, satellite = true }
M.LAT_LIMIT = 85.0511287798066
M.CTX_INTERVAL_S = 0.25
M.TILE_INTERVAL_S = 0.5
M.PRUNE_S = 2.0
M.ACK_S = 3.0              -- a screen that has not said it drew an update by then is sent it again ...
M.ACK_TRIES = 3            -- ... this many times at most
M.WINDOW_M = 1100          -- server navgraph.WINDOW_M: the graph's half size around its (cx, cy)
M.NODES_YIELD = 400        -- the graph's conversion pauses for a frame after this many roads (no hitch per graph)
M.RES_PX = 1024            -- the stock map canvas (bngNavigator's resolution option; no stock page changes it)
M.SAT_RESEND_S = 1.5       -- satellite: a redraw with more tiles at most this often (each is the whole map)
M.SAT_MAX_TILES = 121      -- (the server takes 128 tiles a request)

local style = 'map'
local enabled = true
local active = false
local screens = {}             -- vehicle id + material tag -> { vehicleId, tag, initmap, sentKey, ackKey, sentAt, tries }
local navExtension, stockRequest, wrappedRequest

local function extension(name)
  local all = rawget(_G, 'extensions')
  return type(all) == 'table' and all[name] or rawget(_G, name)
end

local function settingsExtension()
  return extension('beamngEarth_settings')
end

local function coreContext()
  local core = extension('beamngEarth_core')
  if not core or type(core.dashboardContext) ~= 'function' then return nil end
  local ok, ctx = pcall(core.dashboardContext)
  return ok and type(ctx) == 'table' and ctx or nil
end

-- The dashboardMap setting, read again every CTX_INTERVAL_S (onUpdate): this extension may load before the settings
-- file is read, or after settings applied it (settings.lua's apply then finds no dashboard to tell), and a style
-- read once kept the default: the satellite came only after switching to the default and back (the user, 2026-10-29).
local function readSetting()
  local settings = settingsExtension()
  if settings and type(settings.get) == 'function' then
    local value = settings.get('dashboardMap')
    if M.STYLES[value] and M.setStyle then M.setStyle(value) end
  end
end

local function playerVehicleId()
  local beObj = rawget(_G, 'be')
  if not beObj or type(beObj.getPlayerVehicleID) ~= 'function' then return nil end
  local ok, id = pcall(beObj.getPlayerVehicleID, beObj, 0)
  return ok and tonumber(id) or nil
end

local function vehicleFor(id)
  if not id then return nil end
  local tree = rawget(_G, 'scenetree')
  if not tree or type(tree.findObjectById) ~= 'function' then return nil end
  local ok, obj = pcall(tree.findObjectById, id)
  return ok and obj or nil
end

local function screenKey(id, tag)
  return tostring(id) .. '\0' .. tag
end

local function shouldDisplay(ctx)
  return enabled and ctx ~= nil and ctx.active == true
end

-- Ours to draw: a BeamNavigator screen (stock pages call map.setData) of the player's car.
local function ours(rec)
  return (rec.initmap == nil or rec.initmap == 'map.setData') and rec.vehicleId == playerVehicleId()
end

------------------------------------------------------------------------------------------
-- The page side: redraws the stock map from our data through the map layer's own methods. Sent with every update (a
-- page that loaded again has lost it). Its sizing is setMapDimensions' (navigator-class.js 516-588) without the
-- fallback for a negative centre (valid in every formula that uses it) and without a border (the frame is the area);
-- it waits (5 s at most) for terrain images still loading (their load handlers draw into the same canvas); it leaves
-- the startup flags so a late stock setData either does nothing or redraws whole; it says it drew (d.ack). Only the
-- newest update draws: each one from Lua takes a sequence number, and a waiting older one gives up (else it could draw
-- over a newer one that came while it waited). Assigned with every update: a newer mod's version replaces an older.
-- The page keeps the last update (__beLast: PROBE_JS asks for its key) and draws it again after a stock setData (one
-- that came after ours redrew the stock map over it). Satellite images are loaded first (5 s at most), so the stock
-- renderer draws them from the cache and the picture does not blink to bare grid on every redraw. d.credit: the
-- imagery's credit line (a small strip at the bottom; none when empty).
M.PAGE_JS = [[window.__beNav=function(d){
var nav=window.map,m=nav&&nav.layers&&nav.layers.map;
if(!m||!m.dimensions||!m.state||typeof m.applyMapSize!=="function"||typeof m.renderMap!=="function")return;
if(!d.__s){d.__s=window.__beSeq=(window.__beSeq||0)+1;}else if(d.__s!==window.__beSeq)return;
window.__beLast=d;
if(!nav.__beSetData&&typeof nav.setData==="function"){var sd=nav.setData;nav.__beSetData=sd;nav.setData=function(x){sd.call(nav,x);var l=window.__beLast;if(l){l.__s=0;l.t0=0;window.__beNav(l);}};}
clearTimeout(window.__beT);
var N=d.nodes||{},T=Array.isArray(d.terrainTiles)?d.terrainTiles:[];
if(!d.__pre){var f=T.filter(function(t){return t&&t.file;});
if(f.length){if(!d.__img){d.__img=f.map(function(t){var i=new Image();i.src=(t.file.charAt(0)==="/"?"":"/")+t.file;return i;});d.__p0=Date.now();}
if(d.__img.some(function(i){return !i.complete;})&&Date.now()-d.__p0<5000){window.__beT=setTimeout(function(){window.__beNav(d)},100);return;}}
d.__pre=1;}
if(m.tilesLoading>0&&Date.now()-(d.t0||(d.t0=Date.now()))<5000){window.__beT=setTimeout(function(){window.__beNav(d)},200);return;}
var c=document.getElementById("__beCredit");
if(d.credit){if(!c){c=document.createElement("div");c.id="__beCredit";c.style.cssText="position:absolute;left:0;right:0;bottom:0;padding:0 3px;font:7px/9px sans-serif;color:#fff;background:rgba(0,0,0,.55);z-index:96;pointer-events:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis";document.body.appendChild(c);}c.textContent=d.credit;}else if(c){c.parentNode.removeChild(c);}
for(var k in N){var l=N[k].links||{};for(var j in l)if(!N[j])delete l[j];}
var s=m.dimensions.scale||1,x0=1/0,x1=-1/0,y0=1/0,y1=-1/0;
function a(x,y){x/=s;y/=s;if(!isFinite(x)||!isFinite(y))return;if(x<x0)x0=x;if(x>x1)x1=x;if(y<y0)y0=y;if(y>y1)y1=y;}
T.forEach(function(t){if(t&&t.offset&&t.size){a(t.offset[0],-t.offset[1]);a(t.offset[0]+t.size[0],-t.offset[1]+t.size[1]);}});
for(var n in N)a(N[n].pos[0],-N[n].pos[1]);
if(!isFinite(x0)||!isFinite(y0)||x1<=x0||y1<=y0)return;
var D=m.dimensions,w=x1-x0,h=y1-y0,big=Math.max(w,h),r=big>m.resolution?m.resolution/big:1;
D.resScale=r;D.centerX=Math.round(-x0*r);D.centerY=Math.round(-y0*r);D.width=Math.round(w*r);D.height=Math.round(h*r);D.offset=0;D.ver++;
m.applyMapSize();
try{m.renderMap({nodes:N,terrainTiles:T});}catch(e){console.error("BeamNG.Earth navigator",e);m.render(true);}
nav.startup=false;nav.startupFallback=true;
m.setLocation(-m.state.x,m.state.y);if(typeof nav.update==="function")nav.update();
if(d.ack&&typeof beamng!=="undefined"&&beamng.sendEngineLua)beamng.sendEngineLua(d.ack);
};]]

------------------------------------------------------------------------------------------
-- The roads

-- Pure: a road's drivability for the screen: the stock renderer draws 0.9-1 white, 0.1-0.9 khaki, the rest brown
-- (navigator-class.js 686-693), its own levels' paved roads at 1; the server's town streets are 0.8 and its service
-- roads 0.4 (navgraph.py DRIVE), so main and town roads go to 1, service roads and living streets to 0.5, tracks
-- to 0 (the renderer rounds to tenths first: 0.05 became 0.1 and drew khaki over the brown). Only on the screens: the
-- AI graph keeps its values.
function M.screenDrivability(d)
  d = tonumber(d) or 1
  if d >= 0.75 then return 1 end
  if d >= 0.3 then return 0.5 end
  return 0
end

local function finite(v) return type(v) == 'number' and v == v and v > -1e7 and v < 1e7 end
local function r1(v) return math.floor(v * 10 + 0.5) / 10 end

-- Pure: the stock page's nodes ({id = {pos = {x, y}, radius, links = {[id] = {drivability}}}}) of a server graph, as
-- JSON text (string ids "n1"...: numbered keys would encode as an array; each road once, from one end; only nodes
-- that are there; positions to 0.1 m), the nodes' box {x0, y0, x1, y1} (nil: none) and the road count.
-- yieldEvery: a coroutine.yield after that many roads (in a coroutine).
function M.stockNodes(g, yieldEvery)
  local ids, order, links, seen = {}, {}, {}, {}
  local x0, y0, x1, y1 = math.huge, math.huge, -math.huge, -math.huge
  local function id(name, p)
    local s = ids[name]
    if not s then
      s = 'n' .. (#order + 1)
      ids[name] = s
      order[#order + 1] = name
      links[s] = {}
      x0, y0, x1, y1 = math.min(x0, p[1]), math.min(y0, p[2]), math.max(x1, p[1]), math.max(y1, p[2])
    end
    return s
  end
  local roads = 0
  for i, e in ipairs(type(g) == 'table' and type(g.edges) == 'table' and g.edges or {}) do
    local na, nb = e[1], e[2]
    local a, b = g.nodes[na], g.nodes[nb]
    if na ~= nb and type(a) == 'table' and type(b) == 'table' and finite(a[1]) and finite(a[2])
        and finite(b[1]) and finite(b[2]) then
      local ia, ib = id(na, a), id(nb, b)
      if not seen[ia .. '|' .. ib] and not seen[ib .. '|' .. ia] then
        seen[ia .. '|' .. ib] = true
        local l = links[ia]
        l[#l + 1] = '"' .. ib .. '":{"drivability":' .. M.screenDrivability(e[7]) .. '}'
        roads = roads + 1
      end
    end
    if yieldEvery and i % yieldEvery == 0 then coroutine.yield() end
  end
  local out = {}
  for k, name in ipairs(order) do
    local p, s = g.nodes[name], 'n' .. k
    local radius = tonumber(p[4])
    radius = (radius and radius == radius) and math.max(0.5, math.min(20, radius)) or 2
    out[k] = '"' .. s .. '":{"pos":[' .. r1(p[1]) .. ',' .. r1(p[2]) .. '],"radius":' .. r1(radius)
      .. ',"links":{' .. table.concat(links[s], ',') .. '}}'
    if yieldEvery and k % yieldEvery == 0 then coroutine.yield() end
  end
  return '{' .. table.concat(out, ',') .. '}', (#order > 0) and { x0, y0, x1, y1 } or nil, roads
end

-- Pure: the file-less terrain tile that is the map's area (the page sizes the map to its terrain tiles and nodes): the
-- server's window around (cx, cy), with the nodes' box. offset = {west, north}, size = {east-west, north-south}.
function M.frameTile(cx, cy, box, half)
  half = half or M.WINDOW_M
  local w, s, e, n = math.huge, math.huge, -math.huge, -math.huge
  if finite(cx) and finite(cy) then w, s, e, n = cx - half, cy - half, cx + half, cy + half end
  if box then w, s, e, n = math.min(w, box[1]), math.min(s, box[2]), math.max(e, box[3]), math.max(n, box[4]) end
  if not (finite(w) and finite(n)) or e <= w or n <= s then return nil end
  return { offset = { r1(w), r1(n) }, size = { r1(e - w), r1(n - s) } }
end

------------------------------------------------------------------------------------------
-- Satellite

local function project(lat, lon, zoom)
  if type(lat) ~= 'number' or type(lon) ~= 'number' or type(zoom) ~= 'number' then return nil end
  if lat ~= lat or lon ~= lon or zoom ~= math.floor(zoom) or zoom < 0 or zoom > 18 then return nil end
  lat = math.max(-M.LAT_LIMIT, math.min(M.LAT_LIMIT, lat))
  lon = (lon + 180) % 360 - 180
  local n = 2 ^ zoom
  local r = math.rad(lat)
  local x = (lon + 180) / 360 * n
  local y = (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n
  return x, math.max(0, math.min(n - 1e-9, y)), n
end
M.project = project

local C_EQ = 40075016.686

-- A tile's north-west corner (x may be unwrapped), degrees.
local function tileNW(z, x, y)
  local n = 2 ^ z
  return math.deg(2 * math.atan(math.exp(math.pi * (1 - 2 * y / n))) - math.pi / 2), x / n * 360 - 180
end
M.tileNW = tileNW

-- Pure: the tiles' zoom for an area span (m) at a latitude: tiles of ~250-650 m (finer toward the poles, where the
-- grid turns faster against the frame's north away from the anchor), at most SAT_MAX_TILES of them.
function M.satZoom(lat, span)
  local phi = math.rad(math.min(84, math.abs(lat)))
  local target = math.max(250, math.min(650, 562 / math.max(1e-3, math.tan(phi))))
  local z = math.floor(math.log(C_EQ * math.cos(phi) / target) / math.log(2) + 0.5)
  z = math.max(10, math.min(17, z))
  while z > 10 and (math.ceil(span / (C_EQ * math.cos(phi) / 2 ^ z)) + 1) ^ 2 > M.SAT_MAX_TILES do z = z - 1 end
  return z
end

-- Pure: the satellite tiles over a map area for anchor a (geodesy Anchor: worldToGeodetic / geodeticToWorld), the
-- most wanted (nearest the car) first: { z, frame, tiles = {{z, x, tileX, y, entry = {offset, size}}}, requests }.
-- Each tile is the rectangle of its four corners in the world frame (shared corners: no gaps); its edges and the frame's
-- are on a lattice of p m with a frame of exactly RES_PX * p, so the page's scale is 1/p and every tile lands on whole
-- canvas pixels (no hairline seams). nil without an anchor or past the Mercator limits.
function M.satLayout(a, area, carX, carY)
  if not a or not area or type(a.worldToGeodetic) ~= 'function' then return nil end
  local w, n = area.offset[1], area.offset[2]
  local e, s = w + area.size[1], n - area.size[2]
  local clat, clon = a:worldToGeodetic((w + e) / 2, (s + n) / 2, 0)
  if not finite(clat) or math.abs(clat) > 85 then return nil end
  local z = M.satZoom(clat, math.max(e - w, n - s))
  local N = 2 ^ z
  local cfx = project(clat, clon, z)
  local tx0, ty0, tx1, ty1 = math.huge, math.huge, -math.huge, -math.huge
  for _, c in ipairs({ { w, s }, { e, s }, { w, n }, { e, n } }) do     -- all four: the grid is turned
    local lat, lon = a:worldToGeodetic(c[1], c[2], 0)
    local fx, fy = project(lat, lon, z)
    if not fx then return nil end
    fx = fx + N * math.floor((cfx - fx) / N + 0.5)                      -- unwrapped across the antimeridian
    tx0, tx1 = math.min(tx0, math.floor(fx)), math.max(tx1, math.floor(fx))
    ty0, ty1 = math.min(ty0, math.floor(fy)), math.max(ty1, math.floor(fy))
  end
  ty0, ty1 = math.max(0, ty0), math.min(N - 1, ty1)
  local corners = {}
  local function corner(i, j)
    local k = (i - tx0) * 4096 + (j - ty0)
    local c = corners[k]
    if not c then
      local la, lo = tileNW(z, i, j)
      local x, y = a:geodeticToWorld(la, lo, 0)
      c = { x, y }
      corners[k] = c
    end
    return c
  end
  -- one x per grid column line and one y per grid row line (the means of their corners): the rectangles then tile the
  -- plane, neighbours sharing whole edges both ways (per-tile corner means left 1-3 px holes or overlaps where four
  -- tiles meet, far from the anchor at high latitude: the grid turns against the frame's north)
  local colX, rowY = {}, {}
  for i = tx0, tx1 + 1 do
    local sum = 0
    for j = ty0, ty1 + 1 do sum = sum + corner(i, j)[1] end
    colX[i] = sum / (ty1 - ty0 + 2)
  end
  for j = ty0, ty1 + 1 do
    local sum = 0
    for i = tx0, tx1 + 1 do sum = sum + corner(i, j)[2] end
    rowY[j] = sum / (tx1 - tx0 + 2)
  end
  local tiles = {}
  local U = { w, s, e, n }
  for ty = ty0, ty1 do
    for tx = tx0, tx1 do
      local r = { w = colX[tx], e = colX[tx + 1], n = rowY[ty], s = rowY[ty + 1] }
      U = { math.min(U[1], r.w), math.min(U[2], r.s), math.max(U[3], r.e), math.max(U[4], r.n) }
      tiles[#tiles + 1] = { z = z, x = tx, tileX = tx % N, y = ty, r = r }
    end
  end
  local p = math.max(U[3] - U[1], U[4] - U[2]) / (M.RES_PX - 2)
  local function snap(v) return math.floor(v / p + 0.5) * p end
  local west, north = math.floor(U[1] / p) * p, math.ceil(U[4] / p) * p
  local frame = { offset = { west, north }, size = { M.RES_PX * p, M.RES_PX * p } }
  carX, carY = carX or (w + e) / 2, carY or (s + n) / 2
  local function d2(t) return ((t.r.w + t.r.e) / 2 - carX) ^ 2 + ((t.r.n + t.r.s) / 2 - carY) ^ 2 end
  table.sort(tiles, function(A, B) return d2(A) < d2(B) end)
  local requests = {}
  for i, t in ipairs(tiles) do
    local tw, te, tn, ts = snap(t.r.w), snap(t.r.e), snap(t.r.n), snap(t.r.s)
    t.entry = { offset = { tw, tn }, size = { te - tw, tn - ts } }
    requests[i] = { z, t.tileX, t.y, 'sat' }
  end
  return { z = z, p = p, frame = frame, tiles = tiles, requests = requests }
end

------------------------------------------------------------------------------------------
-- What the screens show now: content = { key, nodes (JSON text), tiles (terrainTiles), credit }

local job = nil                -- { key, co }: a graph's conversion in progress
local built = nil              -- { key, nodes, box, cx, cy, anchor }: the last converted graph (or a moved one)
local lastReal = nil           -- { g, key, anchorId, anchorObj }: the last server graph converted, for a rebase
local content = nil            -- what the player's screens should show (nil: leave the stock map)
local sat = nil                -- the satellite tiles of the content's area (satTiles)
local lastTilesAt = -math.huge
local frameOnly = nil          -- { anchor, key }: after a new anchor with no roads to show, the grid around the car

local function carPosition()
  local veh = vehicleFor(playerVehicleId())
  if not veh then return nil end
  local ok, p = pcall(function() return veh:getPosition() end)
  if ok and p and finite(p.x) and finite(p.y) then return p.x, p.y end
  return nil
end

-- Pure: graph g (anchor a's frame) in anchor b's (geodesy Anchor:transformTo, rigid): a rebase's new frame is ~15 km
-- along, the roads around the car the same roads; nil if either anchor is missing.
function M.movedGraph(g, a, b)
  if not (g and a and b and type(a.transformTo) == 'function') then return nil end
  local xf = a:transformTo(b)
  local nodes = {}
  for name, p in pairs(g.nodes) do
    if type(p) == 'table' and finite(p[1]) and finite(p[2]) then
      local x, y, z = xf:apply(p[1], p[2], tonumber(p[3]) or 0)
      nodes[name] = { x, y, z, p[4] }
    end
  end
  local cx, cy
  if finite(g.cx) and finite(g.cy) then cx, cy = xf:apply(g.cx, g.cy, 0) end
  return { nodes = nodes, edges = g.edges, cx = cx, cy = cy }
end

-- The roads for the current anchor: its server graph, else (a rebase: the new anchor's graph comes after the road
-- tiles around the car, up to the server's NAV_WAIT_S) the last graph moved into the new frame. Converted over a few
-- frames (NODES_YIELD) into `built`.
local function updateGraph(ctx)
  local okN, nav = pcall(require, 'beamngEarth/navgraph')
  local g = okN and type(nav) == 'table' and nav.graph and nav.graph() or nil
  local key, src, real
  if g and g.anchor ~= nil and g.anchor == ctx.anchorId then
    key, src, real = tostring(g.anchor) .. ':' .. tostring(g.serial), g, true
  elseif lastReal and ctx.anchorId ~= nil and lastReal.anchorId ~= ctx.anchorId and ctx.anchor then
    key = tostring(ctx.anchorId) .. ':moved:' .. lastReal.key
  else
    return
  end
  if built and built.key == key then return end
  if not job or job.key ~= key then
    local from, to = lastReal, ctx.anchor
    job = { key = key, real = real, g = g, co = coroutine.create(function()
      local graph = src or M.movedGraph(from.g, from.anchorObj, to)
      if not graph then return nil end
      local text, box = M.stockNodes(graph, M.NODES_YIELD)
      return text, box, graph.cx, graph.cy
    end) }
  end
  local ok, text, box, cx, cy = coroutine.resume(job.co)
  if not ok then
    log('W', 'beamngEarth', 'dashboard: road graph for the car screens: ' .. tostring(text))
    job = nil
    built = { key = key, nodes = '{}', anchor = ctx.anchorId }
  elseif coroutine.status(job.co) == 'dead' then
    if job.real then lastReal = { g = job.g, key = key, anchorId = ctx.anchorId, anchorObj = ctx.anchor } end
    job = nil
    built = { key = key, nodes = text or '{}', box = box, anchor = ctx.anchorId, cx = cx, cy = cy }
  end
end

-- The satellite tiles over `area` (base: what it belongs to, part of the update's key): asked for (settings.mapTiles:
-- only those not there yet) every TILE_INTERVAL_S until all are drawn, a redraw with the new ones at most every
-- SAT_RESEND_S (at once when the last comes). Returns the terrain tiles to draw (nil: none yet) and the key part,
-- unique per layout (a screen that kept an earlier layout's update is sent the new one).
local satGen = 0
local function satTiles(ctx, area, base, now)
  if not sat or sat.key ~= base then
    local x, y = carPosition()
    satGen = satGen + 1
    sat = { key = base, gen = satGen, layout = M.satLayout(ctx.anchor, area, x, y), ready = {}, readyN = -1,
      renders = 0, sentAt = -math.huge, tiles = nil }
    lastTilesAt = -math.huge          -- (asked at once: no roads-only update in between)
  end
  local L = sat.layout
  if not L then return nil end
  local part = '/sat' .. sat.gen .. '.'
  if sat.readyN == #L.tiles then return sat.tiles, part .. sat.renders end     -- all drawn: no more asking
  local settings = settingsExtension()
  local mapOpen = settings and type(settings.isMapOpen) == 'function' and settings.isMapOpen()
  if settings and type(settings.mapTiles) == 'function' and not mapOpen and now - lastTilesAt >= M.TILE_INTERVAL_S then
    lastTilesAt = now
    local missing = {}
    for i, t in ipairs(L.tiles) do
      if not sat.ready[i] then missing[#missing + 1] = L.requests[i] end
    end
    local readySet = {}
    for _, k in ipairs(settings.mapTiles(missing) or {}) do readySet[k] = true end
    local list = { L.frame }
    for i, t in ipairs(L.tiles) do
      if not sat.ready[i] and readySet[string.format('sat/%d/%d/%d', t.z, t.tileX, t.y)] then
        sat.ready[i] = true
        t.entry.file = type(settings.mapTilePath) == 'function' and settings.mapTilePath('sat', t.z, t.tileX, t.y)
          or string.format('/temp/beamngEarth/maptiles/%d/%d/%d.jpg', t.z, t.tileX, t.y)
      end
      if sat.ready[i] then list[#list + 1] = t.entry end
    end
    local n = #list - 1
    if sat.readyN < 0 or (n > sat.readyN and (n == #L.tiles or now - sat.sentAt >= M.SAT_RESEND_S)) then
      sat.readyN, sat.sentAt, sat.renders, sat.tiles = n, now, sat.renders + 1, list
    end
  end
  return sat.tiles, part .. sat.renders
end

-- The on-screen credits setting (creditsVisible, on by default): the satellite's credit line follows it.
local function creditsShown()
  local settings = settingsExtension()
  return not (settings and type(settings.get) == 'function' and settings.get('creditsVisible') == false)
end

local function imageryCredits(ctx)
  local ok, credits = pcall(require, 'beamngEarth/credits')
  if ok and type(credits) == 'table' and type(credits.serverText) == 'function' then
    local text = credits.serverText()
    if type(text) == 'string' and text ~= '' then return text end
  end
  return type(ctx.attribution) == 'string' and ctx.attribution or ''
end

-- The satellite with no roads to draw (the server's --traffic off or --roads off, or before an anchor's first graph):
-- the area around the car, moved on in CAR_CELL_M steps.
M.CAR_CELL_M = 800

local function satelliteContent(ctx, base, area, nodes, now)
  local tiles, part = satTiles(ctx, area, base, now)
  if not tiles then return nil end
  return { key = base .. part, nodes = nodes, tiles = tiles, credit = creditsShown() and imageryCredits(ctx) or '' }
end

local function updateContent(ctx, now)
  updateGraph(ctx)
  local x, y = carPosition()
  if built and built.anchor == ctx.anchorId then
    local area = M.frameTile(built.cx, built.cy, built.box)
    -- (a moved graph that does not reach the car is no use: a teleport, not a rebase)
    local reaches = area and (not x or (x >= area.offset[1] and x <= area.offset[1] + area.size[1]
      and y <= area.offset[2] and y >= area.offset[2] - area.size[2]))
    if area and reaches then
      frameOnly = nil
      if style == 'satellite' then
        local c = satelliteContent(ctx, built.key, area, built.nodes, now)
        if c then content = c return end
      end
      content = { key = built.key .. '/map', nodes = built.nodes, tiles = { area } }
      return
    end
  end
  if style == 'satellite' and x and ctx.anchorId ~= nil then
    local cell = M.CAR_CELL_M
    local cx, cy = (math.floor(x / cell) + 0.5) * cell, (math.floor(y / cell) + 0.5) * cell
    local base = tostring(ctx.anchorId) .. ':car' .. cx .. ',' .. cy
    local c = satelliteContent(ctx, base, M.frameTile(cx, cy, nil), '{}', now)
    if c then content = c return end
  end
  -- a new anchor with no roads for it: the drawn roads are in the old frame; the grid around the car
  if (content or frameOnly) and ctx.anchorId ~= nil and (not frameOnly or frameOnly.anchor ~= ctx.anchorId) then
    local area = x and M.frameTile(x, y, nil)
    if area then
      frameOnly = { anchor = ctx.anchorId, key = tostring(ctx.anchorId) .. ':none' }
      content = { key = frameOnly.key, nodes = '{}', tiles = { area } }
    end
  end
end

------------------------------------------------------------------------------------------
-- Sending

local function luaCall(rec, fn, key)
  return 'if beamngEarth_dashboard then beamngEarth_dashboard.' .. fn .. '(' .. rec.vehicleId .. ','
    .. string.format('%q', rec.tag) .. ',' .. string.format('%q', key) .. ') end'
end

local function pageCall(rec, c)
  local target = vehicleFor(rec.vehicleId)
  if not target or type(target.queueJSUITexture) ~= 'function' then return false end
  target:queueJSUITexture(rec.tag, M.PAGE_JS .. '__beNav({"key":' .. jsonEncode(c.key) .. ',"ack":'
    .. jsonEncode(luaCall(rec, 'navDrawn', c.key)) .. ',"credit":' .. jsonEncode(c.credit or '')
    .. ',"terrainTiles":' .. jsonEncode(c.tiles) .. ',"nodes":' .. c.nodes .. '})')
  return true
end

-- Every PROBE_S a drawn screen is asked whether it still has our update (a car or page reloaded with no request we
-- saw, or a stock setData our page could not draw over): if not it is sent again, LOST_TRIES times per update at most.
M.PROBE_S = 5.0
M.LOST_TRIES = 3

local function probe(rec, key)
  local target = vehicleFor(rec.vehicleId)
  if not target or type(target.queueJSUITexture) ~= 'function' then return end
  target:queueJSUITexture(rec.tag, 'if(!window.__beLast||window.__beLast.key!==' .. jsonEncode(key)
    .. '){if(typeof beamng!=="undefined"&&beamng.sendEngineLua)beamng.sendEngineLua('
    .. jsonEncode(luaCall(rec, 'navLost', key)) .. ')}')
end

local function sendScreens(now)
  if not content then return end
  for _, rec in pairs(screens) do
    if ours(rec) then
      if rec.sentKey ~= content.key then
        if pageCall(rec, content) then
          if rec.lostKey ~= content.key then rec.lostKey, rec.lost = nil, 0 end
          rec.sentKey, rec.sentAt, rec.tries, rec.probedAt = content.key, now, 1, now
        end
      elseif rec.ackKey ~= rec.sentKey then
        if now - (rec.sentAt or now) >= M.ACK_S and (rec.tries or 0) < M.ACK_TRIES then
          if pageCall(rec, content) then rec.sentAt, rec.tries = now, (rec.tries or 0) + 1 end
        end
      elseif now - (rec.probedAt or now) >= M.PROBE_S and (rec.lost or 0) < M.LOST_TRIES then
        rec.probedAt = now
        probe(rec, rec.sentKey)
      end
    end
  end
end

-- A page drew an update (PAGE_JS's ack).
function M.navDrawn(vehicleId, tag, key)
  local rec = type(tag) == 'string' and screens[screenKey(tonumber(vehicleId) or -1, tag)]
  if not rec then return false end
  if key == rec.sentKey then rec.ackKey = key end
  return true
end

-- A page does not have the update it was sent (the probe): sent again.
function M.navLost(vehicleId, tag, key)
  local rec = type(tag) == 'string' and screens[screenKey(tonumber(vehicleId) or -1, tag)]
  if not rec or key ~= rec.sentKey then return false end
  rec.lostKey, rec.lost = key, (rec.lostKey == key and rec.lost or 0) + 1
  rec.sentKey, rec.ackKey = nil, nil
  return true
end

------------------------------------------------------------------------------------------
-- The screens

local function registerScreen(tag, initmap, vehId)
  if type(tag) ~= 'string' or tag == '' then return nil end
  local id = tonumber(vehId) or playerVehicleId()
  if not id then return nil end
  local key = screenKey(id, tag)
  local rec = screens[key] or { vehicleId = id, tag = tag }
  screens[key] = rec
  rec.initmap = initmap
  rec.sentKey, rec.ackKey, rec.sentAt, rec.tries = nil, nil, nil, 0      -- a new page: sent the map again
  rec.lostKey, rec.lost = nil, 0
  return rec
end

-- The player's car's BeamNavigator screens from its jbeam controllers (core_vehicle_manager's vehicle data): a car
-- whose screen asked for its map before the hook was in (the first car of a session loads ui_uinavi with that very
-- request), or before this extension was reloaded, is found here. Registered without touching the page (it already
-- has the stock map: ours is sent after it).
local function discoverScreens(id)
  if not id then return end
  local mgr = rawget(_G, 'core_vehicle_manager')
  if not mgr or type(mgr.getVehicleData) ~= 'function' then return end
  local ok, vd = pcall(mgr.getVehicleData, id)
  local ctrl = ok and type(vd) == 'table' and type(vd.vdata) == 'table' and vd.vdata.controller
  if type(ctrl) ~= 'table' then return end
  for _, c in pairs(ctrl) do
    local file = type(c) == 'table' and c.fileName
    if type(file) == 'string' and (file == 'beamNavigator' or file:sub(-14) == '/beamNavigator') then
      local tag = type(c.screenMaterialName) == 'string' and c.screenMaterialName or '@screen_gps'  -- (its default)
      local key = screenKey(id, tag)
      if not screens[key] then screens[key] = { vehicleId = id, tag = tag, tries = 0 } end
    end
  end
end
M._discoverScreens = discoverScreens

-- Wraps the stock request once ui_uinavi is loaded (never loading it: a plain extensions.ui_uinavi would, and its
-- per-frame onGuiUpdate would then run in sessions and cars that never asked for it). It is kept under the name it
-- was first asked for: 'ui_uinavi' (BeamNavigator) or 'ui_uiNavi' (the gauges' navigationData, sbrGauges).
local function loadedNavigation()
  local all = rawget(_G, 'extensions')
  if type(all) == 'table' then
    local nav = rawget(all, 'ui_uinavi') or rawget(all, 'ui_uiNavi')
    if nav then return nav end
  end
  return rawget(_G, 'ui_uinavi') or rawget(_G, 'ui_uiNavi')
end

local function installNavigationHook()
  local nav = loadedNavigation()
  local request = nav and nav.requestVehicleDashboardMap
  if type(request) ~= 'function' then return end
  if nav == navExtension and request == wrappedRequest then return end
  navExtension, stockRequest = nav, request
  wrappedRequest = function(tag, initmap, vehId)
    -- the stock setData goes first, as always; ours follows it into the page (sendScreens)
    local ok, result = pcall(request, tag, initmap, vehId)
    registerScreen(tag, initmap, vehId)
    if not ok then log('W', 'beamngEarth', 'dashboard: the stock navigation map: ' .. tostring(result)) return nil end
    return result
  end
  nav.requestVehicleDashboardMap = wrappedRequest
end

local function prune()
  for key, rec in pairs(screens) do
    if not vehicleFor(rec.vehicleId) then screens[key] = nil end
  end
end

local function resetContent()
  job, built, content, sat, frameOnly, lastReal = nil, nil, nil, nil, nil, nil
  lastTilesAt = -math.huge
  for _, rec in pairs(screens) do rec.sentKey, rec.ackKey = nil, nil end
end

function M.setEnabled(on)
  enabled = on == true
  return enabled
end

-- The dashboardMap setting: 'map' or 'satellite'.
function M.setStyle(v)
  if not M.STYLES[v] or v == style then return style end
  style = v
  sat = nil
  return style
end
function M.style() return style end

local ctxAt, ctxCached, prunedAt = -math.huge, nil, -math.huge
function M.onUpdate()
  installNavigationHook()
  local now = os.clock()
  if now - prunedAt >= M.PRUNE_S then
    prunedAt = now
    prune()
    discoverScreens(playerVehicleId())
  end
  if not next(screens) then return end    -- (no navigation screen, as in most cars: nothing to do)
  if now - ctxAt >= M.CTX_INTERVAL_S then
    ctxAt, ctxCached = now, coreContext()
    readSetting()
  end
  local show = shouldDisplay(ctxCached)
  if show ~= active then
    active = show
    if not active then resetContent() end
  end
  if not active then return end
  updateContent(ctxCached, now)
  sendScreens(now)
end

function M.onClientStartMission()
  readSetting()
end

function M.onClientEndMission()
  active = false
  screens = {}
  resetContent()
end

function M.onVehicleSwitched(_, newId)
  discoverScreens(tonumber(newId))
end

function M.onExtensionLoaded()
  readSetting()
  installNavigationHook()
end

function M.onExtensionUnloaded()
  if navExtension and navExtension.requestVehicleDashboardMap == wrappedRequest then
    navExtension.requestVehicleDashboardMap = stockRequest
  end
end

M._screens = function() return screens end   -- (tests)
M._content = function() return content end

readSetting()
return M
