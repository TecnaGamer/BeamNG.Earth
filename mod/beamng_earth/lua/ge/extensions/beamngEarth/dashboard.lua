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
M.ROADS_YIELD = 600       -- the roads' conversion pauses for a frame after this many nodes / edges (no hitch per graph)

local enabled = true
local active = false
local screens = {}             -- vehicle id + WebView/material tag -> stock registration and current page state
local navExtension, stockRequest, wrappedRequest
local lastViewAt, lastTilesAt = 0, 0
local lastTilesSignature = nil
local currentVehicle = nil

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

local function restoreScreen(rec)
  if not rec.applied or rec.restoring then return end
  local target = vehicleFor(rec.vehicleId)
  if not target or type(target.queueJSUITexture) ~= 'function' then
    rec.applied, rec.restoring = false, false
    return
  end
  target:queueJSUITexture(rec.tag, 'window.beamngEarthDashboard&&window.beamngEarthDashboard.restore()')
  rec.applied, rec.restoring = false, true
  rec.restoreAt = os.clock() + M.RESTORE_RELOAD_S
  rec.sentTiles, rec.lastViewAt = false, 0
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
      rec.sentTiles, rec.lastViewAt = false, 0
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
  rec.applied, rec.sentTiles, rec.lastViewAt, rec.roadsKey = true, false, 0, nil
  lastTilesSignature = nil
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
local function r1(v) return tostring(math.floor(v * 10 + 0.5) / 10) end

-- Pure: the page's setRoads argument (JSON text) for graph g ({ anchor, nodes = { name = {x, y, z, radius} },
-- edges = { {a, b, oneway, lo, li, speed, drivability, half width} } }): n = node x, y pairs in metres from (ox, oy)
-- (0.1 m), e = (node a, node b, class, half width in dm) per road, each road once (not its reverse), only nodes a road
-- uses. Also the road count. yieldEvery: a coroutine.yield after that many edges / nodes (in a coroutine).
function M.roadsText(g, key, yieldEvery)
  local nodes = type(g) == 'table' and type(g.nodes) == 'table' and g.nodes or {}
  local index, order, edges, seen = {}, {}, {}, {}
  local ox, oy
  local function id(name, p)
    local i = index[name]
    if not i then
      i = #order
      index[name] = i
      order[#order + 1] = p
      if not ox then ox, oy = math.floor(p[1]), math.floor(p[2]) end
    end
    return i
  end
  local roads = 0
  for k, e in ipairs(type(g) == 'table' and type(g.edges) == 'table' and g.edges or {}) do
    local na, nb = e[1], e[2]
    local a, b = nodes[na], nodes[nb]
    if na ~= nb and type(a) == 'table' and type(b) == 'table' and finite(a[1]) and finite(a[2]) and finite(b[1])
        and finite(b[2]) and not seen[tostring(nb) .. '\0' .. tostring(na)] then
      seen[tostring(na) .. '\0' .. tostring(nb)] = true
      local half = tonumber(e[8]) or math.max(tonumber(a[4]) or 0, tonumber(b[4]) or 0)
      if not finite(half) or half <= 0 then half = 3 end
      local ia, ib = id(na, a), id(nb, b)
      edges[#edges + 1] = ia .. ',' .. ib .. ',' .. M.roadClass(e[7]) .. ',' .. math.floor(math.min(half, 40) * 10 + 0.5)
      roads = roads + 1
    end
    if yieldEvery and k % yieldEvery == 0 then coroutine.yield() end
  end
  local n = {}
  for i, p in ipairs(order) do
    n[i] = r1(p[1] - ox) .. ',' .. r1(p[2] - oy)
    if yieldEvery and i % yieldEvery == 0 then coroutine.yield() end
  end
  local anchor = type(g) == 'table' and g.anchor
  return '{"key":' .. string.format('%q', tostring(key or '')) .. ',"anchor":'
    .. (type(anchor) == 'number' and tostring(anchor) or string.format('%q', tostring(anchor)))
    .. ',"ox":' .. (ox or 0) .. ',"oy":' .. (oy or 0) .. ',"n":[' .. table.concat(n, ',') .. '],"e":['
    .. table.concat(edges, ',') .. ']}', roads
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
    roadsJob = { key = key, co = coroutine.create(function() return M.roadsText(g, key, M.ROADS_YIELD) end) }
  end
  local okR, text, roads = coroutine.resume(roadsJob.co)
  if not okR then
    if log then log('W', 'beamngEarth.dashboard', 'roads for the car screens: ' .. tostring(text)) end
    roadsJob, roadsBuilt = nil, { key = key, text = nil }
  elseif coroutine.status(roadsJob.co) == 'dead' then
    roadsJob, roadsBuilt = nil, { key = key, text = text, roads = roads }
  end
end

local function sendRoads()
  if not roadsBuilt or not roadsBuilt.text then return end
  for _, rec in pairs(screens) do
    if rec.applied and rec.vehicleId == currentVehicle and rec.roadsKey ~= roadsBuilt.key then
      local target = vehicleFor(rec.vehicleId)
      if target and type(target.queueJSUITexture) == 'function' then
        target:queueJSUITexture(rec.tag, 'window.beamngEarthDashboard&&window.beamngEarthDashboard.setRoads('
          .. roadsBuilt.text .. ')')
        rec.roadsKey = roadsBuilt.key
      end
    end
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
    if readySet[key] then
      local path = settings.mapTilePath and settings.mapTilePath('sat', layout.zoom, tile.tileX, tile.y)
        or string.format('/temp/beamngEarth/maptiles/%d/%d/%d.jpg', layout.zoom, tile.tileX, tile.y)
      out[#out + 1] = { x = tile.x, y = tile.y, src = path }
      parts[#parts + 1] = key .. '@' .. tostring(tile.x)
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
    return { zoom = layout.zoom, tiles = files }
  end
  return nil
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
    if rec.applied and rec.vehicleId == currentVehicle then
      dashboardCall(rec, 'setView', view)
    end
  end
end

local function sendTiles(data)
  if not data then return end
  for _, rec in pairs(screens) do
    if rec.applied and rec.vehicleId == currentVehicle then
      dashboardCall(rec, 'setTiles', data)
      rec.sentTiles = true
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
  local tileUpdate = updateTiles(ctx, layout, now)
  sendTiles(tileUpdate)
  updateRoads()
  sendRoads()
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
  rec.sentTiles, rec.roadsKey = false, nil
  lastTilesSignature, lastTilesAt, lastViewAt = nil, 0, 0
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
  for key in pairs(screens) do
    if key:sub(1, #prefix) == prefix then return true end
  end
  return false
end

-- once per car: its navigators' requests made again through our hook (registerScreen), when it has none registered
local function askScreens(now)
  local id = playerVehicleId()
  if not id or ask.done[id] then return end
  if ask.id ~= id then ask.id, ask.at = id, now + M.ASK_AFTER_S return end
  if now < ask.at then return end
  if hasScreen(id) then ask.done[id] = true return end
  local ctx = coreContext()
  if not (enabled and ctx and ctx.active == true) then ask.at = now + M.ASK_RETRY_S return end
  local target = vehicleFor(id)
  if not target or type(target.queueLuaCommand) ~= 'function' then ask.at = now + M.ASK_RETRY_S return end
  target:queueLuaCommand(M.VEHICLE_ASK)
  ask.done[id] = true
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
  roadsJob, roadsBuilt = nil, nil
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
