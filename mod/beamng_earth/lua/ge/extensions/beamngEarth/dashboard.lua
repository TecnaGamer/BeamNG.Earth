-- Satellite imagery on the vehicle's own BeamNavigator WebView while BeamNG.Earth is active, with the server's road
-- graph drawn over it as lines (the user, 2026-11-12, London: "Nav screeb blurry mess": the photo alone, 1 m a pixel
-- from 10 m Sentinel-2 imagery in most of the world, showed no road to follow).
-- BeamNavigator calls ui_uinavi.requestVehicleDashboardMap with the screen's actual material tag;
-- that is the supported registration point, so this works without per-vehicle JBeam mappings.
local M = {}

M.DASHBOARD_URI = 'local://local/vehicles/common/beamng_earth_dashboard.html'
M.ZOOM_CITY, M.ZOOM_ROAD, M.ZOOM_HIGHWAY = 17, 16, 15
M.ZOOM_FAST = 14
M.LAT_LIMIT = 85.0511287798066
M.VIEW_INTERVAL_S = 0.1
M.TILE_INTERVAL_S = 0.5
M.RESTORE_RELOAD_S = 0.5

-- The photo tiles round the car: every tile within VIEW_RADIUS_PX of it (the farthest screen corner of a 512 x 256 screen
-- with the car 62 % down is ~300 px away; the fixed 3 x 3 block left bare corners when the car was near a tile's edge)
M.VIEW_RADIUS_PX = 320
M.ROADS_YIELD = 300       -- the roads' conversion pauses for a frame after this many edges (no hitch per graph)

-- The page's updates are acknowledged (from iambobab's PR #472): the roads and the tiles each go with a key and a Lua
-- call the page makes once it drew them (navDrawn, built here: the page only echoes it). An update not acknowledged
-- after ACK_S is sent again, ACK_TRIES times at most; a page that never said it loaded (screenReady) after APPLY_S is
-- loaded again, APPLY_TRIES times at most. Every PROBE_S a drawn screen is asked whether it still has what it was sent
-- (a car's page reloaded with no request we saw, or replaced by the stock page): if not (navLost) it is sent again or
-- our page loaded again, LOST_TRIES times per graph at most.
M.ACK_S, M.ACK_TRIES = 3.0, 3
M.APPLY_S, M.APPLY_TRIES = 5.0, 3
M.PROBE_S, M.LOST_TRIES = 5.0, 3

local enabled = true
local active = false
local screens = {}             -- vehicle id + WebView/material tag -> stock registration and current page state
local navExtension, stockRequest, wrappedRequest
local lastViewAt, lastTilesAt = 0, 0
local lastTilesSignature = nil
local currentVehicle = nil
local tilesUpdate = nil        -- { key, json }: the newest tiles update (the screens' "tiles" channel)
local tilesSerial = 0

-- (diagnostics: the main thread's cost of the last graph's roads, ms; hptimer where the game has it)
M.stats = { roadsSlices = 0, roadsWorstMs = 0, roadsTotalMs = 0, roadsSendMs = 0, roadsBytes = 0, roadsSends = 0,
  resends = 0, probes = 0, lost = 0, reapplied = 0 }
local function timer()
  local ok, sw = pcall(require, 'beamngEarth/stopwatch')
  if ok and type(sw) == 'table' and sw.new then return sw.new() end
  local t0 = os.clock()
  return function() return (os.clock() - t0) * 1000 end
end

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

local function readSetting()
  local settings = settingsExtension()
  if settings and type(settings.get) == 'function' then
    local value = settings.get('dashboardSatellite')
    if value ~= nil then enabled = value == true end
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

local function forVehicle(id, fn)
  if id == nil then return end
  local prefix = tostring(id) .. '\0'
  for key, rec in pairs(screens) do
    if key:sub(1, #prefix) == prefix then fn(rec) end
  end
end

local function shouldDisplay(ctx)
  return enabled and ctx ~= nil and ctx.active == true
end

local function dashboardCall(rec, method, data)
  local target = vehicleFor(rec.vehicleId)
  if not target or type(target.queueJSUITexture) ~= 'function' then return false end
  local js = 'window.beamngEarthDashboard&&window.beamngEarthDashboard.' .. method .. '(' .. jsonEncode(data) .. ')'
  target:queueJSUITexture(rec.tag, js)
  return true
end

-- The Lua the page runs back (beamng.sendEngineLua): built here from the screen's own record, the page only echoes it.
local function luaCall(rec, fn, ...)
  local args = { tostring(rec.vehicleId), string.format('%q', rec.tag) }
  for _, v in ipairs({ ... }) do args[#args + 1] = type(v) == 'number' and tostring(v) or string.format('%q', tostring(v)) end
  return 'if beamngEarth_dashboard then beamngEarth_dashboard.' .. fn .. '(' .. table.concat(args, ',') .. ') end'
end

-- a screen's updates: per channel ("roads", "tiles") the key sent, the key the page said it drew, when and how often
local function resetChannels(rec)
  rec.ch = { roads = { tries = 0 }, tiles = { tries = 0 } }
end

local function restoreScreen(rec)
  if not rec.applied or rec.restoring then return end
  local target = vehicleFor(rec.vehicleId)
  if not target or type(target.queueJSUITexture) ~= 'function' then
    rec.applied, rec.restoring = false, false
    return
  end
  target:queueJSUITexture(rec.tag, 'window.beamngEarthDashboard&&window.beamngEarthDashboard.restore()')
  rec.applied, rec.restoring, rec.ready = false, true, false
  rec.restoreAt = os.clock() + M.RESTORE_RELOAD_S
end

local function processRestores(now)
  local restored = false
  for key, rec in pairs(screens) do
    if rec.restoring and now >= rec.restoreAt then
      local target = vehicleFor(rec.vehicleId)
      if target and rec.stockRequest then
        rec.stockRequest(rec.tag, rec.initmap, rec.vehicleId)
      end
      rec.restoring, rec.restoreAt = false, nil
      if not target then screens[key] = nil else restored = true end
    elseif not vehicleFor(rec.vehicleId) then
      screens[key] = nil
    end
  end
  return restored
end

local function applyScreen(rec)
  if rec.applied or rec.restoring then return end
  if rec.vehicleId ~= playerVehicleId() then return end
  local target = vehicleFor(rec.vehicleId)
  if not target or type(target.queueJSUITexture) ~= 'function' then return end
  local prefix = M.DASHBOARD_URI .. '?vehicle=' .. rec.vehicleId .. '&screen='
  target:queueJSUITexture(rec.tag, 'window.location.replace(' .. jsonEncode(prefix)
    .. '+encodeURIComponent(' .. jsonEncode(rec.tag) .. ')+"&return="+encodeURIComponent(window.location.href))')
  rec.applied, rec.ready, rec.appliedAt = true, false, os.clock()
  rec.applyTries = (rec.applyTries or 0) + 1
  resetChannels(rec)
end

local function activateScreens()
  if not active then return end
  local id = playerVehicleId()
  currentVehicle = id
  forVehicle(id, applyScreen)
end

local function restoreScreens(id)
  if id ~= nil then
    forVehicle(id, restoreScreen)
    return
  end
  for _, rec in pairs(screens) do restoreScreen(rec) end
end

local function registerScreen(tag, initmap, vehId, callback)
  if type(tag) ~= 'string' or tag == '' then return end
  local id = tonumber(vehId) or playerVehicleId()
  if not id then return end
  local key = screenKey(id, tag)
  local rec = screens[key]
  if not rec then
    rec = { vehicleId = id, tag = tag }
    screens[key] = rec
  end
  rec.initmap, rec.stockRequest = initmap, callback
  rec.requested = true                          -- (came through the hook: askScreens)
  rec.applyTries, rec.lost, rec.lostKey = 0, 0, nil
  -- a request comes from a navigator starting (a spawn, the car reloaded with its id kept: Ctrl+R): its screen shows
  -- the stock page again, ours to load anew (in-game 2026-11-12: after a reload the stock page stayed, the record
  -- still marked as ours)
  rec.restoring, rec.applied = false, false
  if active and enabled and id == playerVehicleId() then applyScreen(rec) end
end

local function installNavigationHook()
  local all = rawget(_G, 'extensions')
  local nav = type(all) == 'table' and all.ui_uinavi or nil
  local request = nav and nav.requestVehicleDashboardMap
  if type(request) ~= 'function' then return end
  if nav == navExtension and request == wrappedRequest then return end
  navExtension, stockRequest = nav, request
  wrappedRequest = function(tag, initmap, vehId)
    local result = request(tag, initmap, vehId)
    registerScreen(tag, initmap, vehId, request)
    return result
  end
  nav.requestVehicleDashboardMap = wrappedRequest
end

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

local function tileLayout(lat, lon, zoom)
  local x, y, n = project(lat, lon, zoom)
  if not x then return nil end
  local r = M.VIEW_RADIUS_PX / 256
  local list = {}
  for worldX = math.floor(x - r), math.floor(x + r) do
    for tileY = math.floor(y - r), math.floor(y + r) do
      if tileY >= 0 and tileY < n then
        local ddx = math.max(worldX - x, 0, x - worldX - 1)
        local ddy = math.max(tileY - y, 0, y - tileY - 1)
        if ddx * ddx + ddy * ddy <= r * r then
          list[#list + 1] = { x = worldX, y = tileY, tileX = worldX % n, d = ddx * ddx + ddy * ddy }
        end
      end
    end
  end
  table.sort(list, function(a, b) return a.d < b.d end)     -- the car's own tile first
  local requests, tiles = {}, {}
  for i, t in ipairs(list) do
    requests[i] = { zoom, t.tileX, t.y, 'sat' }
    tiles[i] = { x = t.x, y = t.y, tileX = t.tileX }
  end
  return { zoom = zoom, pixelX = x * 256, pixelY = y * 256, requests = requests, tiles = tiles }
end
M.tileLayout = tileLayout

local function zoomFor(speed)
  speed = tonumber(speed) or 0
  if speed >= 60 then return M.ZOOM_FAST end
  if speed >= 35 then return M.ZOOM_HIGHWAY end
  if speed >= 15 then return M.ZOOM_ROAD end
  return M.ZOOM_CITY
end
M.zoomFor = zoomFor

local function keyFor(z, x, y)
  return string.format('sat/%d/%d/%d', z, x, y)
end

------------------------------------------------------------------------------------------
-- The roads over the photo

-- Pure: a graph edge's drivability (server navgraph.py DRIVE: main roads 1, tertiary 0.9, town streets 0.8, living
-- streets 0.5, service 0.4, busways / tracks 0.2) as the page's road class: 3 main, 2 town street, 1 service, 0 track.
function M.roadClass(d)
  d = tonumber(d) or 1
  if d >= 0.85 then return 3 end
  if d >= 0.7 then return 2 end
  if d >= 0.3 then return 1 end
  return 0
end

local function finite(v) return type(v) == 'number' and v == v and v > -1e7 and v < 1e7 end

-- Pure: the page's setRoads argument (JSON text) for graph g ({ anchor, nodes = { name = {x, y, z, radius} },
-- edges = { {a, b, oneway, lo, li, speed, drivability, half width} } }): n = node x, y pairs in metres from (ox, oy)
-- (0.1 m), e = (node a, node b, class, half width in dm) per road, each road once (not its reverse), only nodes a road
-- uses. Also the road count. yieldEvery: a coroutine.yield after that many edges (in a coroutine).
-- The numbers go into two reused buffers turned to text by table.concat at every pause, the pieces joined at the
-- end (no string per node, road or pair key: the old one made ~50,000 small strings per city graph for the main
-- thread's collector, and its last slice formatted every number at once).
local PAIR = 2 ^ 24                -- (a node pair's number key: ia * PAIR + ib, exact for ids under 2^24)
function M.roadsText(g, key, yieldEvery)
  local nodes = type(g) == 'table' and type(g.nodes) == 'table' and g.nodes or {}
  local index, seen = {}, {}
  local nbuf, ebuf, nparts, eparts, nb, eb = {}, {}, {}, {}, 0, 0
  local count, ox, oy = 0, nil, nil
  local floor, concat = math.floor, table.concat
  local function id(name, p)
    local i = count
    index[name] = i
    count = count + 1
    if not ox then ox, oy = floor(p[1]), floor(p[2]) end
    nbuf[nb + 1], nbuf[nb + 2] = floor((p[1] - ox) * 10 + 0.5) / 10, floor((p[2] - oy) * 10 + 0.5) / 10
    nb = nb + 2
    return i
  end
  local function flush()
    if nb > 0 then nparts[#nparts + 1], nb = concat(nbuf, ',', 1, nb), 0 end
    if eb > 0 then eparts[#eparts + 1], eb = concat(ebuf, ',', 1, eb), 0 end
  end
  local roads = 0
  for k, edge in ipairs(type(g) == 'table' and type(g.edges) == 'table' and g.edges or {}) do
    local na, nb_ = edge[1], edge[2]
    local a, b = nodes[na], nodes[nb_]
    if na ~= nb_ and type(a) == 'table' and type(b) == 'table' and finite(a[1]) and finite(a[2]) and finite(b[1])
        and finite(b[2]) then
      local ia, ib = index[na], index[nb_]
      if not (ia and ib and seen[ib * PAIR + ia]) then        -- (its reverse already drawn: not again)
        ia, ib = ia or id(na, a), ib or id(nb_, b)
        seen[ia * PAIR + ib] = true
        local half = tonumber(edge[8]) or math.max(tonumber(a[4]) or 0, tonumber(b[4]) or 0)
        if not finite(half) or half <= 0 then half = 3 end
        ebuf[eb + 1], ebuf[eb + 2], ebuf[eb + 3], ebuf[eb + 4] = ia, ib, M.roadClass(edge[7]),
          floor(math.min(half, 40) * 10 + 0.5)
        eb = eb + 4
        roads = roads + 1
      end
    end
    if yieldEvery and k % yieldEvery == 0 then
      flush()
      coroutine.yield()
    end
  end
  flush()
  local anchor = type(g) == 'table' and g.anchor
  return '{"key":' .. string.format('%q', tostring(key or '')) .. ',"anchor":'
    .. (type(anchor) == 'number' and tostring(anchor) or string.format('%q', tostring(anchor)))
    .. ',"ox":' .. (ox or 0) .. ',"oy":' .. (oy or 0) .. ',"n":[' .. concat(nparts, ',') .. '],"e":['
    .. concat(eparts, ',') .. ']}', roads
end

local roadsJob = nil          -- { key, co }: a graph's conversion in progress
local roadsBuilt = nil        -- { key, text, roads }: the last graph converted

-- The server's road graph (navgraph.lua's, moved with a rebase) as the page's roads, converted over a few frames.
local function updateRoads()
  local ok, nav = pcall(require, 'beamngEarth/navgraph')
  local g = ok and type(nav) == 'table' and type(nav.graph) == 'function' and nav.graph() or nil
  if type(g) ~= 'table' then return end
  local key = tostring(g.anchor) .. ':' .. tostring(g.serial) .. (g.moved and ':moved' or '') .. ':' .. tostring(g.nodes)
  if roadsBuilt and roadsBuilt.key == key then return end
  if not roadsJob or roadsJob.key ~= key then
    roadsJob = { key = key, slices = 0, ms = 0, worst = 0,
      co = coroutine.create(function() return M.roadsText(g, key, M.ROADS_YIELD) end) }
  end
  local elapsed = timer()
  local okR, text, roads = coroutine.resume(roadsJob.co)
  local ms = elapsed()
  roadsJob.slices, roadsJob.ms, roadsJob.worst = roadsJob.slices + 1, roadsJob.ms + ms, math.max(roadsJob.worst, ms)
  if not okR then
    if log then log('W', 'beamngEarth.dashboard', 'roads for the car screens: ' .. tostring(text)) end
    roadsJob, roadsBuilt = nil, { key = key, text = nil }
  elseif coroutine.status(roadsJob.co) == 'dead' then
    local st = M.stats
    st.roadsSlices, st.roadsTotalMs, st.roadsWorstMs, st.roadsBytes = roadsJob.slices, roadsJob.ms, roadsJob.worst, #text
    roadsJob, roadsBuilt = nil, { key = key, text = text, roads = roads }
  end
end

-- Pure: metres a photo pixel at a zoom and latitude (Web Mercator, 256 px tiles).
function M.metresPerPixel(lat, zoom)
  return 156543.03392804097 * math.cos(math.rad(lat)) / 2 ^ zoom
end

local function imageryCredits(ctx)
  local ok, credits = pcall(require, 'beamngEarth/credits')
  if ok and credits and credits.serverText then
    local text = credits.serverText()
    if type(text) == 'string' and text ~= '' then return text end
  end
  return type(ctx.attribution) == 'string' and ctx.attribution or ''
end

local function tileFiles(layout, ready, settings)
  local readySet = {}
  for _, key in ipairs(type(ready) == 'table' and ready or {}) do readySet[key] = true end
  local out, parts = {}, {}
  for _, tile in ipairs(layout.tiles) do
    local key = keyFor(layout.zoom, tile.tileX, tile.y)
    -- (only a stand-in there, settings.mapTiles' "satp": shown until the real tile's own path comes, a new src)
    local layer = readySet[key] and 'sat' or readySet['satp/' .. key:sub(5)] and 'satp' or nil
    if layer then
      local path = settings.mapTilePath and settings.mapTilePath(layer, layout.zoom, tile.tileX, tile.y)
        or string.format('/temp/beamngEarth/maptiles/%s%d/%d/%d.jpg', layer == 'satp' and 'prov/' or '', layout.zoom,
          tile.tileX, tile.y)
      out[#out + 1] = { x = tile.x, y = tile.y, src = path }
      parts[#parts + 1] = key .. '@' .. tostring(tile.x) .. (layer == 'satp' and 'p' or '')
    end
  end
  return out, table.concat(parts, '|')
end

local function updateTiles(ctx, layout, now)
  if not ctx.connected or not ctx.mapPort then return nil end
  if now - lastTilesAt < M.TILE_INTERVAL_S then return nil end
  local settings = settingsExtension()
  if not settings or type(settings.mapTiles) ~= 'function' then return nil end
  if type(settings.isMapOpen) == 'function' and settings.isMapOpen() then return nil end
  lastTilesAt = now
  local ready = settings.mapTiles(layout.requests)
  local files, signature = tileFiles(layout, ready, settings)
  if signature ~= lastTilesSignature then
    lastTilesSignature = signature
    tilesSerial = tilesSerial + 1
    local key = 't' .. tilesSerial
    tilesUpdate = { key = key, json = jsonEncode({ key = key, zoom = layout.zoom, tiles = files }) }
  end
end

local function sendView(ctx, layout, status, now)
  if now - lastViewAt < M.VIEW_INTERVAL_S then return end
  lastViewAt = now
  local player = ctx.player
  local view
  if layout and player then
    view = { pixelX = layout.pixelX, pixelY = layout.pixelY, zoom = layout.zoom,
      heading = tonumber(player.heading) or 0, attribution = imageryCredits(ctx), status = status or '',
      -- the roads' frame: the car in the world frame, true north there, the photo's metres a pixel
      x = finite(player.x) and player.x or nil, y = finite(player.y) and player.y or nil,
      north = finite(player.north) and player.north or 0, anchor = ctx.anchorId,
      mpp = M.metresPerPixel(player.lat, layout.zoom) }
  else
    view = { status = status or '', attribution = imageryCredits(ctx) }
  end
  for _, rec in pairs(screens) do
    if rec.ready and rec.vehicleId == currentVehicle then
      dashboardCall(rec, 'setView', view)
    end
  end
end

-- A channel's update due: 1 a new key, 2 the same again (not acknowledged after ACK_S), nil neither.
local function due(c, key, now)
  if key == nil then return nil end
  if c.sent ~= key then return 1 end
  if c.ack ~= key and now - (c.at or now) >= M.ACK_S and (c.tries or 0) < M.ACK_TRIES then return 2 end
  return nil
end
local function markSent(c, key, now, why)
  if why == 1 then c.sent, c.at, c.tries = key, now, 1
  else c.at, c.tries = now, (c.tries or 0) + 1; M.stats.resends = M.stats.resends + 1 end
end

local function jsValue(v) return v == nil and 'null' or jsonEncode(v) end

-- Asks the page whether it still has what it was sent (PROBE_S): navLost('page') where it is not ours any more,
-- navLost('data') where its roads or tiles are not the ones it acknowledged.
local function probe(rec, target, now)
  rec.probedAt = now
  rec.probeSerial = (rec.probeSerial or 0) + 1
  M.stats.probes = M.stats.probes + 1
  target:queueJSUITexture(rec.tag, '(function(){var d=window.beamngEarthDashboard,S=function(c){if(typeof beamng!=='
    .. '"undefined"&&beamng.sendEngineLua)beamng.sendEngineLua(c)};if(!d||typeof d.keys!=="function"){S('
    .. jsonEncode(luaCall(rec, 'navLost', 'page', rec.probeSerial)) .. ');return}var k=d.keys();if(k.roads!=='
    .. jsValue(rec.ch.roads.sent) .. '||k.tiles!==' .. jsValue(rec.ch.tiles.sent) .. ')S('
    .. jsonEncode(luaCall(rec, 'navLost', 'data', rec.probeSerial)) .. ')})()')
end

-- The player's screens: a page that never said it loaded is loaded again; a loaded one gets the newest tiles and
-- roads (each until it says it drew them) and is probed now and then.
local function sendScreens(now)
  for _, rec in pairs(screens) do
    if rec.applied and rec.vehicleId == currentVehicle then
      if not rec.ready then
        if now - (rec.appliedAt or now) >= M.APPLY_S and (rec.applyTries or 0) < M.APPLY_TRIES then
          rec.applied = false
          M.stats.reapplied = M.stats.reapplied + 1
          applyScreen(rec)
        end
      else
        local target = vehicleFor(rec.vehicleId)
        if target and type(target.queueJSUITexture) == 'function' then
          local ch = rec.ch
          local why = tilesUpdate and due(ch.tiles, tilesUpdate.key, now)
          if why then
            markSent(ch.tiles, tilesUpdate.key, now, why)       -- (first: the page's answer may come at once)
            target:queueJSUITexture(rec.tag, 'window.beamngEarthDashboard&&window.beamngEarthDashboard.setTiles('
              .. tilesUpdate.json .. ',' .. jsonEncode(luaCall(rec, 'navDrawn', tilesUpdate.key)) .. ')')
          end
          local built = roadsBuilt and roadsBuilt.text and roadsBuilt
          if built and rec.lostKey ~= nil and rec.lostKey ~= built.key then rec.lost, rec.lostKey = 0, nil end
          why = built and due(ch.roads, built.key, now)
          if why then
            markSent(ch.roads, built.key, now, why)
            local elapsed = timer()
            target:queueJSUITexture(rec.tag, 'window.beamngEarthDashboard&&window.beamngEarthDashboard.setRoads('
              .. built.text .. ',' .. jsonEncode(luaCall(rec, 'navDrawn', built.key)) .. ')')
            M.stats.roadsSendMs, M.stats.roadsSends = elapsed(), M.stats.roadsSends + 1
          end
          if ch.roads.ack == ch.roads.sent and ch.tiles.ack == ch.tiles.sent and (rec.lost or 0) < M.LOST_TRIES
              and now - (rec.probedAt or now) >= M.PROBE_S then
            probe(rec, target, now)
          end
        end
      end
    end
  end
end

local function updateDashboard(now, ctx)
  local id = playerVehicleId()
  if id ~= currentVehicle then
    if currentVehicle then restoreScreens(currentVehicle) end
    currentVehicle = id
    forVehicle(id, applyScreen)
  end

  local player = ctx.player
  if not player or type(player.lat) ~= 'number' or type(player.lon) ~= 'number' then
    sendView(ctx, nil, 'Waiting for vehicle position', now)
    return
  end
  if not ctx.connected or not ctx.mapPort then
    sendView(ctx, nil, 'Waiting for the Earth server', now)
    return
  end
  local layout = tileLayout(player.lat, player.lon, zoomFor(player.speed))
  if not layout then
    sendView(ctx, nil, 'Satellite map unavailable', now)
    return
  end
  updateTiles(ctx, layout, now)
  updateRoads()
  sendScreens(now)
  local status = (lastTilesSignature == '' and 'Satellite tiles are loading') or ''
  sendView(ctx, layout, status, now)
end

-- Navigation discards calls queued for the old page. Replay cached tiles once the actual new page is ready.
function M.screenReady(vehicleId, tag)
  local id = tonumber(vehicleId)
  if not id or type(tag) ~= 'string' then return false end
  local rec = screens[screenKey(id, tag)]
  if not rec then return false end
  rec.applied, rec.restoring = true, false
  if not active or not enabled or id ~= playerVehicleId() then
    restoreScreen(rec)
    return false
  end
  rec.ready, rec.applyTries, rec.probedAt = true, 0, os.clock()
  resetChannels(rec)                  -- (a new page: the tiles and the roads sent again)
  lastTilesAt, lastViewAt = 0, 0
  return true
end

-- A page drew an update (the Lua it was sent with it, luaCall).
function M.navDrawn(vehicleId, tag, key)
  local rec = type(tag) == 'string' and screens[screenKey(tonumber(vehicleId) or -1, tag)]
  if not rec or not rec.ch then return false end
  for _, c in pairs(rec.ch) do
    if c.sent ~= nil and c.sent == key then c.ack = key end
  end
  return true
end

-- A page answered a probe (what: 'page', our page is gone; 'data', its roads or tiles are not what it was sent):
-- loaded again, or sent everything again. LOST_TRIES per graph at most (probes stop after that).
function M.navLost(vehicleId, tag, what, serial)
  local rec = type(tag) == 'string' and screens[screenKey(tonumber(vehicleId) or -1, tag)]
  if not rec or not rec.ready or serial ~= rec.probeSerial then return false end
  rec.lost = (rec.lost or 0) + 1
  rec.lostKey = roadsBuilt and roadsBuilt.key or rec.lostKey
  M.stats.lost = M.stats.lost + 1
  if what == 'page' then
    rec.applied, rec.ready, rec.applyTries = false, false, 0
    if active and enabled and rec.vehicleId == playerVehicleId() then applyScreen(rec) end
  else
    resetChannels(rec)
  end
  return true
end

function M.setEnabled(on)
  enabled = on == true
  if not enabled then
    active = false
    restoreScreens()
  else
    local ctx = coreContext()
    active = shouldDisplay(ctx)
    if active then activateScreens() end
  end
  return enabled
end

local ctxAt, ctxCached = -1e9, nil

-- The player's car asked once to register its navigation screens again (askScreens). A navigator asks for its map
-- only once, at the car's spawn (beamNavigator.init, the gauges' navigationData); a request that missed our hook, or a
-- registration dropped meanwhile, left the stock map for good: grid lines on a blank screen, our level has no stock
-- map (the user, 2026-11-08: "the sat nav on cars with a screen is blank"; registered again by hand it showed the
-- photo map at once). Asked ASK_AFTER_S after the car became the player's, again after a respawn or replacement.
M.ASK_AFTER_S, M.ASK_RETRY_S = 1.5, 5.0
local ask = { id = nil, at = nil, done = {} }   -- the car waited for, when to ask it; done[id]: asked already
M.VEHICLE_ASK = [[
local id = obj:getID()
local function req(tag, initmap)
  if type(tag) == 'string' and tag ~= '' then
    obj:queueGameEngineLua(string.format('extensions.ui_uinavi.requestVehicleDashboardMap(%q, %s, %d)', tag,
      initmap and string.format('%q', initmap) or 'nil', id))
  end
end
for _, c in pairs(v and v.data and v.data.controller or {}) do
  if type(c) == 'table' then
    if c.fileName == 'beamNavigator' then req(c.screenMaterialName or '@screen_gps', nil) end
    if type(c.customModules) == 'table' then
      for _, m in pairs(c.customModules) do
        if type(m) == 'table' and m.moduleName == 'navigationData' then req(c.materialName, 'initMap') end
      end
    end
  end
end]]

local function hasScreen(id)
  local prefix = tostring(id) .. '\0'
  for key, rec in pairs(screens) do
    if rec.requested and key:sub(1, #prefix) == prefix then return true end
  end
  return false
end

-- The car's navigation screens by its jbeam (core_vehicle_manager's vehicle data: beamNavigator controllers, gauges
-- with a navigationData module, as VEHICLE_ASK asks them); nil where the data is not at hand.
M.ASK_TRIES = 3
local function navigatorCount(id)
  local mgr = rawget(_G, 'core_vehicle_manager')
  if not mgr or type(mgr.getVehicleData) ~= 'function' then return nil end
  local ok, vd = pcall(mgr.getVehicleData, id)
  local ctrl = ok and type(vd) == 'table' and type(vd.vdata) == 'table' and vd.vdata.controller
  if type(ctrl) ~= 'table' then return nil end
  local n = 0
  for _, c in pairs(ctrl) do
    if type(c) == 'table' then
      local file = c.fileName
      if type(file) == 'string' and (file == 'beamNavigator' or file:sub(-14) == '/beamNavigator') then n = n + 1 end
      for _, m in pairs(type(c.customModules) == 'table' and c.customModules or {}) do
        if type(m) == 'table' and m.moduleName == 'navigationData' then n = n + 1 end
      end
    end
  end
  return n
end

-- per car: its navigators' requests made again through our hook (registerScreen), when it has none registered;
-- a car whose jbeam lists one is asked again (ASK_RETRY_S apart, ASK_TRIES times) until one registers (PR #472: a
-- request can still miss the hook, the first car's loads ui_uinavi with that very request); one with none listed is
-- not asked; one whose data is not at hand, once
local function askScreens(now)
  local id = playerVehicleId()
  if not id or ask.done[id] then return end
  if ask.id ~= id then ask.id, ask.at, ask.tries = id, now + M.ASK_AFTER_S, 0 return end
  if now < ask.at then return end
  if hasScreen(id) then ask.done[id] = true return end
  local ctx = coreContext()
  if not (enabled and ctx and ctx.active == true) then ask.at = now + M.ASK_RETRY_S return end
  local target = vehicleFor(id)
  if not target or type(target.queueLuaCommand) ~= 'function' then ask.at = now + M.ASK_RETRY_S return end
  local n = navigatorCount(id)
  if n == 0 then ask.done[id] = true return end
  target:queueLuaCommand(M.VEHICLE_ASK)
  ask.tries = (ask.tries or 0) + 1
  if n == nil or ask.tries >= M.ASK_TRIES then ask.done[id] = true else ask.at = now + M.ASK_RETRY_S end
  M.asked = (M.asked or 0) + 1        -- (diagnostics)
end

local function forgetAsked(id)
  id = tonumber(id)
  if id == nil then return end
  ask.done[id] = nil
  if ask.id == id then ask.id = nil end
end
function M.onUpdate()
  installNavigationHook()
  if enabled then askScreens(os.clock()) end
  -- (no navigation screen registered, as in most cars: nothing to do, not even the context; else the context at most
  -- every VIEW_INTERVAL_S: it copies the credits and builds the attribution text)
  if not next(screens) then return end
  local now = os.clock()
  local restored = processRestores(now)
  if now - ctxAt >= M.VIEW_INTERVAL_S then ctxAt, ctxCached = now, coreContext() end
  local ctx = ctxCached
  local show = shouldDisplay(ctx)
  if show ~= active then
    active = show
    if active then activateScreens() else restoreScreens() end
    lastTilesAt, lastViewAt, lastTilesSignature = 0, 0, nil
  elseif active and restored then
    activateScreens()
  end
  if not active then
    return
  end
  updateDashboard(now, ctx)
end

function M.onClientStartMission()
  readSetting()
  ask.id, ask.done = nil, {}
  local ctx = coreContext()
  active = shouldDisplay(ctx)
  if active then activateScreens() end
end

function M.onClientEndMission()
  active = false
  restoreScreens()
  currentVehicle = nil
  lastTilesAt, lastViewAt, lastTilesSignature = 0, 0, nil
  roadsJob, roadsBuilt, tilesUpdate = nil, nil, nil
end

function M.onVehicleSwitched(oldId, newId)
  oldId, newId = tonumber(oldId), tonumber(newId)
  if oldId and oldId ~= newId then restoreScreens(oldId) end
  currentVehicle = newId
  if active and enabled and newId then forVehicle(newId, applyScreen) end
end

-- A vehicle replaced in place (the vehicle selector, core_vehicles.replaceVehicle) keeps its id but not its screens:
-- calls to the old car's screen tag then failed every frame ("UI texture '@scintilla_gps_screen' not found", ~5,000
-- log lines in a test session that swapped the car for a prop, 2026-11-03). Its records go, without a restore (the
-- screen is gone); a new vehicle's navigator registers its own (requestVehicleDashboardMap).
local function dropScreens(id)
  id = tonumber(id)
  if id == nil then return end
  local prefix = tostring(id) .. '\0'
  for key in pairs(screens) do
    if key:sub(1, #prefix) == prefix then screens[key] = nil end
  end
  if currentVehicle == id then lastTilesSignature = nil end
end
function M.onVehicleReplaced(id) dropScreens(id) forgetAsked(id) end
function M.onVehicleDestroyed(id) dropScreens(id) forgetAsked(id) end
M.onVehicleSpawned = forgetAsked     -- (a new car with the id of an old one asks again)

function M.onExtensionLoaded()
  readSetting()
  installNavigationHook()
end

function M.onExtensionUnloaded()
  restoreScreens()
  if navExtension and navExtension.requestVehicleDashboardMap == wrappedRequest then
    navExtension.requestVehicleDashboardMap = stockRequest
  end
end

readSetting()
return M
