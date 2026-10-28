-- Satellite imagery on the vehicle's own BeamNavigator WebView while BeamNG.Earth is active.
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

local OFFSETS = {
  { 0, 0 }, {-1, 0}, {1, 0}, {0, -1}, {0, 1},
  {-1, -1}, {1, -1}, {-1, 1}, {1, 1},
}

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
  rec.applied, rec.sentTiles, rec.lastViewAt = true, false, 0
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
  rec.restoring = false
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
  local cx, cy = math.floor(x), math.floor(y)
  local requests, tiles = {}, {}
  for _, d in ipairs(OFFSETS) do
    local worldX, tileY = cx + d[1], cy + d[2]
    if tileY >= 0 and tileY < n then
      local tileX = worldX % n
      requests[#requests + 1] = { zoom, tileX, tileY, 'sat' }
      tiles[#tiles + 1] = { x = worldX, y = tileY, tileX = tileX }
    end
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
      heading = tonumber(player.heading) or 0, attribution = imageryCredits(ctx), status = status or '' }
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
  rec.sentTiles = false
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
function M.onUpdate()
  installNavigationHook()
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
  local ctx = coreContext()
  active = shouldDisplay(ctx)
  if active then activateScreens() end
end

function M.onClientEndMission()
  active = false
  restoreScreens()
  currentVehicle = nil
  lastTilesAt, lastViewAt, lastTilesSignature = 0, 0, nil
end

function M.onVehicleSwitched(oldId, newId)
  oldId, newId = tonumber(oldId), tonumber(newId)
  if oldId and oldId ~= newId then restoreScreens(oldId) end
  currentVehicle = newId
  if active and enabled and newId then forVehicle(newId, applyScreen) end
end

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
