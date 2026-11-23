-- Prepared by cliffstock.py before the server starts. Never cook stock textures
-- in a streaming callback; missing private assets only disable contact strips.
local M = { ready = false }
local enabled = false
local ROOT = '/temp/beamngEarth/shapes/cliff_contact_v1/'
local FILES = { 'sources.json', 'source.color.dds', 'normal.dds', 'ao.data.dds', 'roughness.data.dds',
  'coverage.data.dds', 'threshold.data.dds', 'contact.materials.json' }

function M.configure(flags)
  M.ready = false
  enabled = flags and (flags.natural or flags.roadCuts) or false
  if not enabled then return end
  for _, file in ipairs(FILES) do
    if not FS:fileExists(ROOT..file) then
      log('W', 'beamngEarth.cliffcontact', 'Cliff contact strips skipped: private textures are missing. Restart the launcher to prepare them.')
      return
    end
  end
  local ok, result = pcall(loadJsonMaterialsFile, ROOT..'contact.materials.json')
  M.ready = ok and result ~= false
  if not M.ready then
    log('W', 'beamngEarth.cliffcontact', 'Cliff contact strips skipped: private material could not be loaded.')
  end
end

function M.onMissionStart()
  -- A reconnect can arrive before the old mission deletes its materials.
  -- Recreate the prepared material after the new mission has finished loading.
  M.configure({natural=enabled})
end

return M
