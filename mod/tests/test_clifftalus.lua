local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir..'/testlib.lua')
local talus = require('beamngEarth/clifftalus')
local group = 'talus_c0a080'
T.check(talus.isGroup(group),'regional tint group accepted')
T.check(not talus.isGroup('talus_../../') and not talus.isGroup('rock'),'only bounded hexadecimal colour keys')
local text = '<material id="rocks_desert_ca"><instance target="lod_vertcol"/></material>'
local remapped = talus.remapDae(text,'bne_'..group)
T.check(remapped:find('bne_'..group..'_lod',1,true) ~= nil,'LOD material remapped too')
T.check(not remapped:find('rocks_desert_ca',1,true),'private mesh does not bind stock material')
T.check(text:find('rocks_desert_ca',1,true) ~= nil,'source unchanged')
local materials = talus.materials(group)
local stage = materials['bne_'..group].Stages[1]
T.check(stage.baseColorMap:find('/temp/beamngEarth/',1,true) ~= nil,'uses copied texture')
T.check(stage.baseColorFactor[1] > stage.baseColorFactor[3],'regional warm tint retained')
T.check(stage.detailNormalMapStrength < .3,'micro normals restrained')

local files, copies, loads, notifications = {}, 0, 0, 0
FS = {
 fileExists=function(_,p) return files[p] ~= nil end,
 directoryCreate=function() end,
 triggerFilesChanged=function(_,paths) notifications=notifications+#paths end,
 copyFile=function(_,src,dst) copies=copies+1; files[dst]='copied '..src end,
}
for _,shape in ipairs(talus.SHAPES) do files[talus.STOCK..shape[1]..'.dae']=text end
readFile=function(p) return files[p] end
writeFile=function(p,t) files[p]=t end
jsonEncode=function() return '{}' end
loadJsonMaterialsFile=function() loads=loads+1 end
local catalog = talus.catalog(group)
T.check(#catalog==3 and copies==6,'three copied collections share six copied maps')
T.check(notifications==6,'resource cache notified before initial material load')
T.check(talus.catalog(group)==catalog and copies==6 and loads==1,'cached assets are not copied per instance')
T.check(files[talus.STOCK..talus.SHAPES[1][1]..'.dae']==text,'installed game mesh never overwritten')
T.done('clifftalus')
