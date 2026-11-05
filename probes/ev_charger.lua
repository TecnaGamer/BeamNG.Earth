-- EV charger probe (2026-10-29; docs/research/2026-10-29-ev-chargers.md): the stock charger shapes in a row 12 m ahead
-- of the player, 4 m apart, each with a BeamNGGameplayArea + BeamNGPointOfInterest pair handed to the stock
-- freeroam_gasStations through onGetRawPoiListForLevel with energyTypes { 'electricEnergy' } (freeroam/gasStations.lua:
-- the "recharge" prompt, the bolt icon). Drive an electric car slowly into an area: the charge prompt should appear.
-- Run: python tools/mcp.py luafile probes/ev_charger.lua    Undo: the same with  _G.bneEvProbeUndo = true  first.
local socket = require('socket')
local r = {}
if _G.bneEvProbe then
  for _, id in ipairs(_G.bneEvProbe.ids) do local o = scenetree.findObjectById(id); if o then o:delete() end end
  _G.bneEvProbe = nil
  extensions.unload('bneEvProbePois')
  if gameplay_rawPois then gameplay_rawPois.clear() end
  if _G.bneEvProbeUndo then _G.bneEvProbeUndo = nil; return jsonEncode({ undone = true }) end
end
local SHAPES = {
  '/levels/east_coast_usa/art/shapes/buildings/eca_charging_station.DAE',
  '/levels/west_coast_usa/art/shapes/buildings/s_fuel_charger_car_2022.dae',
  '/levels/west_coast_usa/art/shapes/buildings/s_fuel_charger_car_2020.dae',
  '/levels/west_coast_usa/art/shapes/buildings/s_fuel_charger_concrete_base.dae',
  '/levels/italy/art/shapes/buildings/charging_station_a.DAE',
  '/levels/italy/art/shapes/buildings/charging_station_b.DAE',
  '/levels/hirochi_raceway/art/shapes/buildings/hr_charging_station.DAE',
}
local t0 = socket.gettime()
r.materials = {}
for _, f in ipairs({ '/levels/east_coast_usa/art/shapes/buildings/main.materials.json',
                     '/levels/west_coast_usa/art/shapes/buildings/main.materials.json',
                     '/levels/italy/art/shapes/buildings/main.materials.json',
                     '/levels/hirochi_raceway/art/shapes/buildings/main.materials.json' }) do
  r.materials[f] = FS:fileExists(f)
  if FS:fileExists(f) then loadJsonMaterialsFile(f) end
end
r.materials_ms = (socket.gettime() - t0) * 1000

local veh = getPlayerVehicle(0)
local p, d = veh:getPosition(), veh:getDirectionVector()
d.z = 0; d = d:normalized()
local side = vec3(d.y, -d.x, 0)                 -- the row runs across the car's heading
local yaw = math.atan2(d.y, d.x)
local rot = quatFromEuler(0, 0, yaw)
local ids = {}
local station = { id = 'bne_ev_probe', name = 'EV probe', description = 'BeamNG.Earth EV probe',
  energyTypes = { 'electricEnergy' }, pumps = {} }
r.shapes = {}
for i, shape in ipairs(SHAPES) do
  local pos = p + d * 12 + side * ((i - (#SHAPES + 1) / 2) * 4)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, shape)
  o:setField('collisionType', 0, 'Visible Mesh Final')
  o.canSave = false
  o:setPosRot(pos.x, pos.y, pos.z, rot.x, rot.y, rot.z, rot.w)
  o:registerObject('')
  scenetree.MissionGroup:addObject(o)
  ids[#ids + 1] = o:getID()
  local box = o:getObjBox()
  r.shapes[i] = { shape = shape, exists = FS:fileExists(shape), minExtents = tostring(box.minExtents),
    maxExtents = tostring(box.maxExtents) }
  -- the area toward the car: a stall in front of the charger
  local apos = pos - d * 3.0
  local a = createObject('BeamNGGameplayArea')
  a.canSave = false
  a:setPosRot(apos.x, apos.y, apos.z + 0.8, rot.x, rot.y, rot.z, rot.w)
  a:setScale(vec3(5.0, 2.4, 2.75))
  a:registerObject('bne_ev_area_' .. i)
  scenetree.MissionGroup:addObject(a)
  local ic = createObject('BeamNGPointOfInterest')
  ic.canSave = false
  ic:setPosition(vec3(apos.x, apos.y, apos.z + 2.5))
  ic:registerObject('bne_ev_icon_' .. i)
  scenetree.MissionGroup:addObject(ic)
  ids[#ids + 1] = a:getID(); ids[#ids + 1] = ic:getID()
  station.pumps[i] = { 'bne_ev_area_' .. i, 'bne_ev_icon_' .. i }
end
_G.bneEvProbe = { ids = ids, station = station }
-- (the extension's name must not be the global's: a loaded extension is a global of its name, and replaced
-- _G.bneEvProbe with the module, so the hook handed formatGasStationPoi nil every frame; in game, 2026-10-29)
extensions.unload('bneEvProbePois')
package.loaded['bneEvProbePois'] = { onGetRawPoiListForLevel = function(_, elements)
  local probe = rawget(_G, 'bneEvProbe')
  if probe and probe.station then
    table.insert(elements, freeroam_gasStations.formatGasStationPoi(probe.station))
  end
end }
extensions.load('bneEvProbePois')
gameplay_rawPois.clear()
r.setting = settings.getValue('enableGasStationsInFreeroam')
r.total_ms = (socket.gettime() - t0) * 1000
return jsonEncode({ total_ms = r.total_ms, setting = r.setting, shapes = #r.shapes })
