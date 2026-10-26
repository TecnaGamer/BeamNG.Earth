-- Whole-mile panels stream and rebase like guide signs, with an independent saved toggle.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local roads = require('beamngEarth/roads')
local settings = require('beamngEarth/settings')
local files = {}
readFile = function(path) return files[path] end
jsonDecode = function(text) return assert(load('return '..text))() end
jsonReadFile = function(path) return files[path] end
jsonWriteFile = function(path,value) files[path] = value; return true end
FS = { fileExists = function(_,path) return files[path] ~= nil end }
local gov = { try = function() return true end, charge = function() end, chargeMotion = function() end }
local function statics()
  local result = {}
  for _,obj in pairs(S.objects) do if obj.class == 'TSStatic' then result[#result+1] = obj end end
  return result
end
files['/t/miles.json'] = '{ chunks = {}, mileposts = { shape = "/t/mileposts.cdae", pos = { 256, 512, 0 } } }'
roads.onTile({anchorId=1,key='1_2',path='/t/miles.json',n=0},1)
roads.update(gov,1)
local objects = statics()
T.check(#objects == 1,'a mile marker loads even without pavement chunks')
T.check(objects[1].fields.collisionType == 'None','mile-marker meshes are render only')
T.check(objects[1].pos.x == 256 and objects[1].pos.y == 512,'marker placed in the anchor frame')
T.check(settings.defaults().mileMarkers == true,'enabled by default')
T.check(roads.setExitSignsEnabled(false),'exit-sign toggle changes independently')
T.check(roads.mileMarkersEnabled == true,'disabling exit signs keeps mile markers enabled')
roads.update(gov,1)
roads.onTile({anchorId=1,key='1_2',path='/t/miles.json',n=0},1)
roads.update(gov,1)
T.check(#statics() == 1,'mile markers remain available while exit signs are off')
settings.set('mileMarkers',false)
roads.update(gov,1)
T.check(#statics() == 0,'mile-marker option removes existing panels')
T.check(files[settings.FILE].mileMarkers == false,'mile-marker option is saved')
settings.load()
T.check(settings.get('mileMarkers') == false,'mile-marker option survives loading settings')
roads.onTile({anchorId=1,key='1_2',path='/t/miles.json',n=0},1)
roads.update(gov,1)
T.check(#statics() == 0,'new streamed tiles respect the disabled option')
roads.setMileMarkersEnabled(true)
roads.onTile({anchorId=1,key='1_2',path='/t/miles.json',n=0},1)
roads.update(gov,1)
T.check(#statics() == 1,'can enable the panels again')
roads.onDrop({anchorId=1,key='1_2'})
roads.update(gov,1)
T.check(#statics() == 0,'dropped road tiles remove their mile markers')
T.done('test_mile_markers')
