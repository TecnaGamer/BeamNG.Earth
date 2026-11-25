-- Winter's snow on the ground, the mod's part (winter plan W2; server BNE_WINTER, server/snowcover.py). The server
-- whitens the ground photos (the block's and the far tiles') and moves the samples under deep snow to the snow
-- terrain layer (surface.snow_layers); here, from hello.snow and the 'snow' message (seasonlook.setSnow):
--   - the snow layer (bngEarthGroundSnow) takes stock snow textures (Automation Test Track's t_snow / t_macro_snow,
--     read from the player's install, nothing copied) instead of the sand it borrows the rest of the year: its grain,
--     normals and roughness over the white photo; the engine takes them with the next block swap;
--   - the 3D grass thins under a light cover (groundcover.setSnow): deep snow has none (its samples are the snow
--     layer's, where no GroundCover grows).
-- Off (no hello.snow) this module is never loaded. Nothing per frame: a state change is one call.
local M = {}

M.MATERIAL = 'bngEarthGroundSnow'
local ATT = '/levels/automation_test_track/art/terrains/'
-- grass element factor by the snow's depth bin (D0..D6, snowcover.py): a dusting lets most blades show
M.GRASS = { [0] = 1.0, 0.6, 0.3, 0.12, 0.12, 0.12, 0.12 }

-- Pure: the snow layer's look (terrain.lua's look() fields; sizes in metres: 4 m grain as Automation Test Track's own,
-- a 32 m macro, both dividing the 128 m block step), or nil when the stock textures are not there (exists(path)).
function M.snowLook(exists)
  local l = { detailDistances = '0 0 50 80', macroDistances = '0 0 100 200', macroDistAtten = '0 1' }
  for _, k in ipairs({ { 'baseColor', '_b' }, { 'normal', '_nm' }, { 'roughness', '_r' }, { 'ao', '_ao' } }) do
    l[k[1] .. 'DetailTex'] = ATT .. 't_snow' .. k[2] .. '.png'
    l[k[1] .. 'MacroTex'] = ATT .. 't_macro_snow' .. k[2] .. '.png'
    l[k[1] .. 'DetailTexSize'] = 4
    l[k[1] .. 'MacroTexSize'] = 32
  end
  if exists then
    for _, v in pairs(l) do
      if type(v) == 'string' and v:sub(1, 1) == '/' and not exists(v) then return nil end
    end
  end
  -- (the colour is the photo's: the grain only, mean 0.42 so a hint darker; the normals and roughness carry the look)
  l.baseColorDetailStrength, l.baseColorMacroStrength = '0.15 0.05', '0.1 0.05'
  l.normalDetailStrength, l.roughnessDetailStrength, l.aoDetailStrength = '1 0.3', '1 0.4', '0.6 0.2'
  l.normalMacroStrength, l.roughnessMacroStrength = '0.5 0.3', '0 0.8'
  return l
end

-- Pure: the grass factor for a snow state {mode, cover 0..8, depth 0..6} (nil or no cover: 1).
function M.grassFactor(s)
  if type(s) ~= 'table' then return 1 end
  local c, d = tonumber(s.cover) or 0, tonumber(s.depth) or 0
  if c <= 0 then return 1 end
  d = math.max(0, math.min(6, math.floor(d + 0.5)))
  return M.GRASS[d] or 1
end

M.state = nil           -- the server's last snow state (hello.snow / 'snow')
M.looked = false        -- the snow layer wears the stock snow textures

-- The server's snow state (nil: winter off or gone: everything back).
function M.set(s)
  M.state = type(s) == 'table' and s or nil
  local terrain = require('beamngEarth/terrain')
  local gc = require('beamngEarth/groundcover')
  if M.state then
    if not M.looked then
      local look = M.snowLook(function(p) return FS and FS:fileExists(p) end)
      if look then M.looked = terrain.setLookOverride(M.MATERIAL, look) or false end
    end
    gc.setSnow(M.grassFactor(M.state))
  else
    M.restore()
  end
end

-- The level ended or winter went off: the snow layer's own look and the full grass again.
function M.restore()
  if M.looked then
    pcall(function() require('beamngEarth/terrain').setLookOverride(M.MATERIAL, nil) end)
    M.looked = false
  end
  pcall(function() require('beamngEarth/groundcover').setSnow(1) end)
end

-- Another snow state live (dev / tests: the console or MCP, beamngEarth_winterlook is not an extension, so
-- require('beamngEarth/winterlook').request('heavy')): the server textures what is shown again with it and answers with
-- 'snow'. mode: a preset (dusting light heavy deep alpine spring), 'none' or 'real'; only while the server has winter on.
function M.request(mode)
  require('beamngEarth/net').send({ type = 'snow_mode', mode = tostring(mode) })
end

function M.status()
  return { state = M.state, looked = M.looked }
end

return M
