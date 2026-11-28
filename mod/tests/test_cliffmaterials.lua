local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir..'/testlib.lua')
local selected, reads, loads, warnings = {}, 0, 0, 0
local ground = false
local root = '/temp/beamngEarth/shapes/cliff_ground_v1/'
FS = { fileExists=function() return ground end, directoryCreate=function() end }
readFile = function() reads=reads+1; return '{}' end
writeFile = function() end
log = function() warnings=warnings+1 end
loadJsonMaterialsFile = function() loads=loads+1 end
jsonEncode = function(value) selected=value; return '{}' end
jsonDecode = function()
  local result = {}
  for _, name in ipairs({'bne_cliff_strata', 'bne_cliff_roadcut', 'bne_cliff_roadcut_arid'}) do
    result[name] = {Stages={{normalMap='authored.normal.dds', detailMap=root..'detail.data.dds',
      detailBaseColorMapStrength=.2}}}
  end
  return result
end
local materials = require('beamngEarth/cliffmaterials')
materials.configure(nil)
materials.configure({natural=false,roadCuts=false})
T.check(reads==0 and loads==0, 'disabled categories do not prepare materials')
materials.configure({roadCuts=true})
T.check(not selected.bne_cliff_strata and selected.bne_cliff_roadcut and selected.bne_cliff_roadcut_arid,
  'road-cut opt-in selects road-cut and shared arid families')
local stage = selected.bne_cliff_roadcut_arid.Stages[1]
T.check(stage.detailMap=='' and stage.detailBaseColorMapStrength==0 and stage.normalMap=='authored.normal.dds',
  'missing private detail preserves authored maps without missing texture references')
ground = true
materials.configure({natural=true})
T.check(selected.bne_cliff_strata and not selected.bne_cliff_roadcut and selected.bne_cliff_roadcut_arid,
  'natural opt-in excludes limestone road cuts')
T.check(selected.bne_cliff_roadcut_arid.Stages[1].detailMap==root..'detail.data.dds', 'prepared private detail retained')
T.check(warnings==1 and loads==2, 'missing detail reports once for its configuration')
T.done('test_cliffmaterials')
