-- luajit mod/tests/test_dashboard.lua (vehicle satellite-map projection and tile neighborhood)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local dashboard = require('beamngEarth/dashboard')

local x, y = dashboard.project(0, 0, 3)
T.near(x, 4, 1e-12, 'equator and prime meridian project to the center tile')
T.near(y, 4, 1e-12, 'equator projects to the vertical world center')

local layout = dashboard.tileLayout(0, 0, 3)
T.check(layout and #layout.requests == 9 and #layout.tiles == 9, 'normal view requests a 3-by-3 tile neighborhood')
T.check(layout.requests[1][1] == 3 and layout.requests[1][2] == 4 and layout.requests[1][3] == 4
  and layout.requests[1][4] == 'sat', 'center satellite tile is requested first')
local seen = {}
for _, request in ipairs(layout.requests) do
  local key = request[2] .. '/' .. request[3]
  seen[key] = (seen[key] or 0) + 1
end
for key, count in pairs(seen) do T.check(count == 1, 'tile request is deduplicated: ' .. key) end

local edge = dashboard.tileLayout(0, 179.9, 3)
local wrapped = false
for _, request in ipairs(edge.requests) do
  if request[2] == 0 then wrapped = true end
end
T.check(wrapped, 'the neighborhood wraps satellite tile x across the antimeridian')

local pole = dashboard.tileLayout(90, 0, 3)
T.check(pole and #pole.requests == 6, 'north-pole view omits tile rows outside the Mercator world')
T.check(dashboard.zoomFor(0) == dashboard.ZOOM_CITY and dashboard.zoomFor(60) == dashboard.ZOOM_FAST,
  'the live speed selects an appropriate dashboard zoom')

-- a vehicle replaced in place keeps its id, not its screens: no more calls to the old screen afterwards
do
  local calls = 0
  local veh = { queueJSUITexture = function() calls = calls + 1 end }
  local nav = { requestVehicleDashboardMap = function() return true end }
  local saved = { scenetree = rawget(_G, 'scenetree'), be = rawget(_G, 'be'), extensions = rawget(_G, 'extensions'),
    jsonEncode = rawget(_G, 'jsonEncode') }
  scenetree = { findObjectById = function(id) return id == 7 and veh or nil end }
  be = { getPlayerVehicleID = function() return 7 end }
  jsonEncode = jsonEncode or function() return '{}' end
  extensions = { ui_uinavi = nav, beamngEarth_core = { dashboardContext = function()
    return { active = true, connected = true, mapPort = 1, player = { lat = 39.28, lon = -76.6, speed = 0 } } end } }
  dashboard.onExtensionLoaded()
  dashboard.onClientStartMission()
  nav.requestVehicleDashboardMap('@scintilla_gps_screen', {}, 7)   -- (the car's navigator registers its screen)
  T.check(calls > 0, 'the screen of the player\'s car is taken over')
  dashboard.onVehicleReplaced(7)
  local before, clock, t = calls, os.clock, 1000
  os.clock = function() return t end          -- (the view goes out every VIEW_INTERVAL_S)
  for _ = 1, 5 do t = t + 1; dashboard.onUpdate() end
  os.clock = clock
  T.check(calls == before, 'replaced in place: the old screen is not called again (' .. (calls - before) .. ' calls)')
  dashboard.onClientEndMission()
  scenetree, be, extensions, jsonEncode = saved.scenetree, saved.be, saved.extensions, saved.jsonEncode
end

T.done('test_dashboard')
