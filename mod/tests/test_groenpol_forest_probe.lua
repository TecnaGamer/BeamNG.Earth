-- Offline ownership/transform contract; the proprietary renderer needs a visual test.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local items, objects, result = {}, {}, nil
local live = 0
local level = 'gridmap_v2'
getCurrentLevelIdentifier = function() return level end
vec3 = function(x,y,z) return {x=x,y=y,z=z} end
MatrixF = function()
  return {setColumn=function(self,i,v) self[i]=v end,
          setPosition=function(self,p) self.p=p end}
end
be = {getPlayerVehicle=function() return {
  getPosition=function() return vec3(0,0,0) end,
  getDirectionVector=function() return vec3(0,1,0) end} end}
core_terrain = {getTerrainHeight=function() return 0 end}
scenetree.MissionGroup = {addObject=function() end}
Sim = {getDataBlockSet=function() return {addObject=function() end} end}
jsonReadFile = function() return {cap_z=2.58,beam_centre_z=5.81} end
jsonWriteFile = function(_,v) result=v end
loadJsonMaterialsFile = function() end
local function makeObject(class)
  local o={class=class,fields={}}
  function o:setField(k,_,v) self.fields[k]=v end
  function o:registerObject(name) self.name=name;objects[name]=self;live=live+1 end
  function o:delete()
    assert(not self.deleted); self.deleted=true;live=live-1
    if class=='Forest' then items={} end
  end
  function o:getData() return {createNewItem=function(_,d,m,scale)
    local item={d=d,m=m,scale=scale}; items[#items+1]=item;return item
  end} end
  return o
end
createObject = function(class)
  assert(class ~= 'TSForestItemData', 'BeamNG global factory cannot create this datablock')
  return makeObject(class)
end
worldEditorCppApi = {createObject=function(class)
  assert(class == 'TSForestItemData')
  return makeObject(class)
end}
local probe=dir .. '/../../tools/groenpol_forest_probe.lua'
do
  dofile(probe)
  assert(result.ok and result.items==11 and result.visualCheckRequired)
  assert(#items==11 and live==7) -- one Forest and six shared model types
  local a,b=items[10],items[11]
  assert(a.d==b.d and a.scale==1 and b.scale==1)
  assert(a.m[1].x==1 and b.m[1].x==5)
  assert(a.m[0].y==b.m[0].y and a.m[2].z==b.m[2].z)
  for _,it in ipairs(items) do
    assert(it.d.fields.collidable=='0')
    assert(it.d.fields.shapeFile:match('^/temp/nl_forest_probe_assets_20261114/.*%.dae$'))
  end
  dofile(probe) -- repeat replaces the diagnostic, never accumulates copies
  assert(#items==11 and live==7)
  bneNlForestProbe.cleanup()
  assert(#items==0 and live==0)
  level = 'beamng_earth'
  dofile(probe)
  assert(not result.ok and result.items==0 and live==0)
  assert(result.error:find('stock map',1,true))
  level = 'gridmap_v2'
  worldEditorCppApi = nil
  dofile(probe)
  assert(not result.ok and result.items==0 and live==0)
  assert(result.error:find('factory unavailable',1,true))
end
T.check(true, 'shared models, independent stretch and cleanup')
T.done('test_groenpol_forest_probe')
