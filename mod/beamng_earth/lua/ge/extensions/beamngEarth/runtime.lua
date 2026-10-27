-- modScript executes from the newly mounted files, while manual extensions and
-- require() helpers may still belong to the local app loaded at the main menu.
local M = {}
M.REVISION = 4 -- bump when the mounted client must replace a previously loaded runtime

function M.refresh()
  local core = rawget(_G, 'beamngEarth_core')
  if core and core.RUNTIME_REVISION == M.REVISION then return false end
  local cached = {}
  for name in pairs(package.loaded) do
    if type(name)=='string' and name:sub(1,12)=='beamngEarth/' then cached[#cached+1]=name end
  end
  if not core and #cached==0 then return false end -- ordinary startup: nothing to replace
  -- Mounting a server mod normally happens before loading its level. Never tear
  -- down terrain under a live vehicle if a mod is activated manually mid-drive.
  local level = rawget(_G,'getCurrentLevelIdentifier') and getCurrentLevelIdentifier()
  if scenetree and scenetree.MissionGroup and level and level~='' then
    log('E','beamngEarth','Updated client is mounted but an older runtime is still active; fully restart BeamNG before joining')
    return false
  end
  if not extensions or not extensions.unload then return false end
  local names = {}
  for name,value in pairs(extensions) do
    if type(name)=='string' and name:sub(1,12)=='beamngEarth_' and type(value)=='table' then
      names[#names+1]=name
    end
  end
  -- Core cleanup closes the companion connection before its helpers are evicted.
  if core then extensions.unload('beamngEarth_core') end
  for _,name in ipairs(names) do
    if name~='beamngEarth_core' then extensions.unload(name) end
  end
  for _,name in ipairs(cached) do package.loaded[name]=nil end
  log('I','beamngEarth','Mounted client runtime '..M.REVISION..': cleared older BeamNG.Earth modules; loading the updated client')
  -- The mod manager calls loadManualUnloadExtensions after modScript returns.
  return true
end

return M
