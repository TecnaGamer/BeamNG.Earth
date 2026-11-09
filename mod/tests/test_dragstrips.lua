-- luajit mod/tests/test_dragstrips.lua  (drag strips for the stock drag system, dragstrips.lua): a tile's strip makes
-- the objects the stock display.lua / saveSystem.lua look up by name, writes the stock-format strip and settings files,
-- loads them through the stock loader, offers a stage trigger per lane; goes with its tile; a rebase waits for a run.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

scenetree.MissionGroup = S.newObject('SimGroup')
createObject = function(class) return S.newObject(class) end
log = function() end
local written = {}
jsonWriteFile = function(p, t) written[p] = t end
local loaded, started = {}, false
extensions = { load = function() end }
gameplay_drag_core = { getDragIsStarted = function() return started end,
                       getData = function() return { strip = { id = 'earth_137280810_main' } } end }
gameplay_drag_saveSystem = { loadDragStripData = function(p)
  loaded[#loaded + 1] = p
  local set = written[p]
  local strip = written[set.stripId]
  return { strip = { id = strip.id, lanes = strip.lanes }, timers = set.timers }
end }
local pois = {}
gameplay_drag_poi = { createStagePoi = function(data, lane, i) return { id = data._fnWithoutExt .. '-' .. i } end }
local cleared = 0
gameplay_rawPois = { clear = function() cleared = cleared + 1 end }

local ds = require('beamngEarth/dragstrips')
ds.now = function() return 100 end

-- a north-running quarter-mile strip, lanes 4.4 m off the centre, the shutdown 700 m past the stage
local rec = { id = 'earth_137280810', name = 'Bandimere Speedway', dist = 402.336, half = 4.4,
  lanes = { { stage = { 4.4, 0, 1800 }, finish = { 4.4, 402.336, 1810 } },
            { stage = { -4.4, 0, 1800 }, finish = { -4.4, 402.336, 1810 } } }, ['end'] = { 0, 700, 1820 } }
ds.onRows(1, '1:0_0', { rec })
ds.update(nil)

local need = { 'prestage_1', 'stage_1', 'amber1_1', 'amber2_1', 'amber3_1', 'green_1', 'red_1',
               'prestage_2', 'stage_2', 'amber1_2', 'amber2_2', 'amber3_2', 'green_2', 'red_2',
               'wp_spawn_1', 'wp_stage_1', 'wp_endLine_1', 'wp_spawn_2', 'wp_stage_2', 'wp_endLine_2',
               'displayTime1_1', 'displaySpeed5_2', 'winTimeboard_1' }
for _, n in ipairs(need) do
  T.check(scenetree.findObject('earth_137280810_main_' .. n) ~= nil, 'named object ' .. n)
end
local a = scenetree.findObject('earth_137280810_main_amber1_1')
T.check(a.hidden and a.fields.shapeName == '/art/shapes/race/dragstrip/christmas_tree_light_amber.dae',
  'tree bulbs hidden, global shape')
T.check(math.abs(a.pos.y - 15.19) < 0.1 and math.abs(a.pos.x - 0.2) < 0.1, 'tree 15 m past the stage, as Alder')
local wpe = scenetree.findObject('earth_137280810_main_wp_endLine_1')
T.check(math.abs(wpe.pos.y - 402.336) < 0.01 and math.abs(wpe.pos.x - 4.4) < 0.01, 'lane 1 end line: right, 1/4 mile')
local d1 = scenetree.findObject('earth_137280810_main_displayTime1_1')
T.check(math.abs(d1.pos.y - 402.48) < 0.2 and d1.pos.x > 7, 'boards at the finish, right of lane 1')

local set = written['/settings/beamngEarth/dragstrips/earth_137280810.dragSettings.json']
T.check(set and set.stripId == '/settings/beamngEarth/dragstrips/earth_137280810_main.strip.json', 'settings file')
local strip = written[set.stripId]
T.check(strip.id == 'earth_137280810_main' and #strip.lanes == 2 and #strip.stripZone.vertices == 4, 'strip file')
T.check(strip.lanes[1].waypoints[2].type == 'stage' and #strip.lanes[1].zone.vertices == 4, 'lane stage waypoint, zone')
local imp
for _, t in ipairs(set.timers) do if t.important then imp = t.id end end
T.check(imp == 'time_1_4', 'quarter mile timed: ' .. tostring(imp))
T.check(#loaded == 1 and ds.count() == 1, 'loaded through the stock loader')

ds.update(nil)
local el = {}
ds.pois('beamng_earth', el)
T.check(#el == 2 and el[1].id == 'earth_137280810-1' and cleared >= 1, 'a stage trigger per lane; POI list rebuilt')

-- a run holds the rebase; idle does not
started = true
T.check(ds.holdRebase() == true, 'rebase held during a run')
started = false
T.check(ds.holdRebase() == false, 'idle: rebase goes')

-- an eighth-mile strip times to 1/8
local r8 = { id = 'earth_9', name = 'X', dist = 201.168, half = 4.4,
  lanes = { { stage = { 1000, 0, 0 }, finish = { 1000, 201.168, 0 } }, { stage = { 991.2, 0, 0 }, finish = { 991.2, 201.168, 0 } } },
  ['end'] = { 995.6, 300, 0 } }
ds.onRows(1, '1:1_0', { r8 })
ds.update(nil)
local s8 = written['/settings/beamngEarth/dragstrips/earth_9.dragSettings.json']
imp = nil
for _, t in ipairs(s8.timers) do if t.important then imp = t.id end end
T.check(imp == 'time_1_8', 'eighth mile timed to 1/8')

-- gone with its tile
ds.dropKey('1:0_0')
T.check(scenetree.findObject('earth_137280810_main_amber1_1') == nil and ds.count() == 1, 'gone with its tile')

-- a rebase: objects go, built again from the new frame's rows
ds.transform({})
T.check(ds.count() == 0, 'rebase: gone until the new rows')
ds.onRows(2, '2:1_0', { r8 })
ds.update(nil)
T.check(ds.count() == 1, 'built again in the new frame')

ds.reset()
T.check(ds.count() == 0, 'reset')
T.done()
