-- Gas stations probe (2026-10-23; docs/research/2026-10-23-gas-station-probe.md): one West Coast USA canopy kit
-- (s_fuel_structure_03 + its six Apex pumps) 25 m ahead of the player, spawned at runtime, each pump with a
-- BeamNGGameplayArea + BeamNGPointOfInterest pair handed to the stock freeroam_gasStations through
-- onGetRawPoiListForLevel. Drive slowly up to a pump: the fuel icon and the "Refuel" prompt should appear.
-- Run: python tools/mcp.py luafile probes/fuel_pump.lua    Undo: the same with  _G.bneFuelProbeUndo = true  first.
local socket = require('socket')
local r = {}
local B = '/levels/west_coast_usa/art/shapes/buildings/'
if _G.bneFuelProbe then
  for _, id in ipairs(_G.bneFuelProbe.ids) do local o = scenetree.findObjectById(id); if o then o:delete() end end
  _G.bneFuelProbe = nil
  if gameplay_rawPois then gameplay_rawPois.clear() end
  if _G.bneFuelProbeUndo then _G.bneFuelProbeUndo = nil; return jsonEncode({ undone = true }) end
end
local t0 = socket.gettime()
for _, f in ipairs({ '/levels/west_coast_usa/art/shapes/buildings/main.materials.json',
                     '/levels/west_coast_usa/art/shapes/objects/main.materials.json' }) do
  loadJsonMaterialsFile(f)
end
r.materials_ms = (socket.gettime() - t0) * 1000

local veh = getPlayerVehicle(0)
local p, d = veh:getPosition(), veh:getDirectionVector()
d.z = 0; d = d:normalized()
local yaw = math.atan2(d.y, d.x) - math.pi / 2       -- the kit's long axis (local y) along the car's heading
local c = p + d * 25
local ca, sa = math.cos(yaw), math.sin(yaw)
local function at(lx, ly, dz) return vec3(c.x + ca * lx - sa * ly, c.y + sa * lx + ca * ly, c.z + (dz or 0)) end
local rot = quatFromEuler(0, 0, yaw)
local ids = {}
local function static(shape, pos, q, collide)
  local t = socket.gettime()
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, shape)
  o:setField('collisionType', 0, collide and 'Visible Mesh Final' or 'None')
  o.canSave = false
  o:setPosRot(pos.x, pos.y, pos.z, q.x, q.y, q.z, q.w)
  o:registerObject('')
  scenetree.MissionGroup:addObject(o)
  ids[#ids + 1] = o:getID()
  return (socket.gettime() - t) * 1000, o
end
r.canopy_ms, r.canopy = static(B .. 's_fuel_structure_03.dae', at(0, 0, 0), rot, true)
local canopyBox = r.canopy:getObjBox()
r.canopy = { minExtents = tostring(canopyBox.minExtents), maxExtents = tostring(canopyBox.maxExtents) }
local pumps = { { -2.92, -7.43 }, { -2.92, 0.03 }, { -2.92, 7.56 }, { 2.87, -7.44 }, { 2.87, 0.08 }, { 2.87, 7.56 } }
local prot = quatFromEuler(0, 0, yaw + math.pi)
local station = { id = 'bne_probe', name = 'Probe', description = 'BeamNG.Earth probe',
  energyTypes = { 'gasoline', 'diesel' }, pumps = {} }
r.pump_ms = {}
for i, lp in ipairs(pumps) do
  local pos = at(lp[1], lp[2], 0.16)
  r.pump_ms[i] = static(B .. 's_fuel_pump_2015.dae', pos, prot, true)
  static(B .. 's_fuel_pump_2015_apex.dae', pos, prot, false)
  -- the area as West Coast USA's (fs_3: scale 2.2 0.6 2.75 at the pump, its x along the row) but 1.6 across
  local a = createObject('BeamNGGameplayArea')
  a.canSave = false
  a:setPosRot(pos.x, pos.y, pos.z + 0.8, rot.x, rot.y, rot.z, rot.w)
  a:setScale(vec3(1.6, 2.2, 2.75))
  a:registerObject('bne_probe_area_' .. i)
  scenetree.MissionGroup:addObject(a)
  local ic = createObject('BeamNGPointOfInterest')
  ic.canSave = false
  ic:setPosition(vec3(pos.x, pos.y, pos.z + 2.5))
  ic:registerObject('bne_probe_icon_' .. i)
  scenetree.MissionGroup:addObject(ic)
  ids[#ids + 1] = a:getID(); ids[#ids + 1] = ic:getID()
  station.pumps[i] = { 'bne_probe_area_' .. i, 'bne_probe_icon_' .. i }
end
_G.bneFuelProbe = { ids = ids, station = station }
-- a provider for the stock POI list (gameplay/rawPois.lua gathers it through this hook)
extensions.unload('bneFuelProbe')
package.loaded['bneFuelProbe'] = { onGetRawPoiListForLevel = function(_, elements)
  if _G.bneFuelProbe then table.insert(elements, freeroam_gasStations.formatGasStationPoi(_G.bneFuelProbe.station)) end
end }
extensions.load('bneFuelProbe')
gameplay_rawPois.clear()
r.setting = settings.getValue('enableGasStationsInFreeroam')
r.total_ms = (socket.gettime() - t0) * 1000
return jsonEncode(r)
