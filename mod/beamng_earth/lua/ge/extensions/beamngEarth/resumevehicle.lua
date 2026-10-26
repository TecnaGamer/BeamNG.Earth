-- A server checkpoint carries a portable .pc table, not a path on another machine.
-- Spawn via core_vehicles so BeamMP registers the car normally and enforces its limits.
local M = {}

function M.record(value)
  if type(value) ~= 'table' or type(value.model) ~= 'string' or #value.model > 80
    or not value.model:match('^[%w_-]+$') or value.model == 'unicycle'
    or type(value.config) ~= 'table' or type(value.config.parts) ~= 'table' then return nil end
  local c = value.config
  -- In particular, do not pass a format-4 collection or linkedPCFile to the spawner.
  return {model=value.model, config={format=2, parts=c.parts,
    vars=type(c.vars)=='table' and c.vars or {}, paints=type(c.paints)=='table' and c.paints or nil,
    licenseName=type(c.licenseName)=='string' and c.licenseName:sub(1,64) or nil,
    mainPartName=type(c.mainPartName)=='string' and c.mainPartName:sub(1,128) or nil}}
end

function M.serverId(veh)
  local mp = rawget(_G, 'MPVehicleGE')
  if not veh or not mp or not mp.isOwn or not mp.isOwn(veh:getID()) or not mp.getServerVehicleID then return nil end
  local id = mp.getServerVehicleID(veh:getID())
  return type(id)=='string' and id:match('^%d+%-%d+$') and id or nil
end

local function available(api, model)
  if not api.getModel then return true end
  local ok, found = pcall(api.getModel, model)
  return ok and type(found)=='table' and next(found)~=nil
end

function M.spawn(saved, anchor, x, y, z)
  local api = rawget(_G, 'core_vehicles')
  if not api or not api.spawnNewVehicle then return nil, 'Vehicle spawner is not ready' end
  local record = M.record(saved.vehicle)
  local fallback = not record or not available(api, record.model)
  if fallback then record = {model=api.defaultVehicleModel or 'pickup'} end
  local dx, dy = require('beamngEarth/resume').direction(anchor, x, y, saved.heading)
  local opt = {config=record.config, pos=vec3(x,y,z),
    rot=quatFromDir(vec3(dx,dy,0),vec3(0,0,1)), autoEnterVehicle=true}
  if record.config then
    local paints = record.config.paints or {}
    opt.paint, opt.paint2, opt.paint3 = paints[1], paints[2], paints[3]
    opt.licenseText = record.config.licenseName
  end
  local ok, veh = pcall(api.spawnNewVehicle, record.model, opt)
  if not ok or not veh then return nil, 'Saved vehicle could not spawn; check the server vehicle limit and installed mods' end
  return veh, fallback and 'Saved vehicle unavailable; using the default vehicle' or nil
end

function M.enter(veh)
  if not veh then return end
  if be and be.enterVehicle then be:enterVehicle(0, veh) end
  if commands and commands.setGameCamera then commands.setGameCamera() end
end

return M
