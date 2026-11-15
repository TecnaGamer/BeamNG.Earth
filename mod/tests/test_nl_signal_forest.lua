local dir=(arg[0]:match('^(.*)[/\\]') or '.')
local T=dofile(dir..'/testlib.lua')
local S=dofile(dir..'/stubs.lua')
local made,removed,items,types=0,0,{},0
local oldCreate=createObject
createObject=function(class)
  assert(class~='TSStatic' and class~='TSForestItemData')
  local f=oldCreate(class)
  if class=='Forest' then
    made=made+1
    local mine={}
    function f:getData() return {
      createNewItem=function(_,d,m,scale)
        local it={d=d,m=m,scale=scale};items[it]=true;mine[it]=true;return it
      end,
      removeItem=function(_,it)
        assert(mine[it] and items[it]);mine[it]=nil;items[it]=nil;removed=removed+1
      end} end
  end
  return f
end
worldEditorCppApi={createObject=function(class)
  assert(class=='TSForestItemData');types=types+1;return S.newObject(class)
end}
Sim={getDataBlockSet=function()return{addObject=function()end}end}
scenetree.MissionGroup=S.newObject('SimGroup')
scenetree.MissionGroup:registerObject('MissionGroup')
MatrixF=function()return{
  setColumn=function(self,c,v)self[c]=v end,
  setPosition=function(self,p)self.p=p end}end
local factory=require('beamngEarth/nl_signal_forest')
local backend=factory.new()
T.check(made==0 and types==0,'require and constructor leave engine untouched')
local p={shape='/levels/beamng_earth/art/shapes/beamngEarth/groenpol/beam.dae',
         pose={0,0,5.8,1,0,0,0,5,0,0,0,1}}
assert(backend.setFrame({10,20,30,0,0,0,1}))
local a=assert(backend.create(p));local b=assert(backend.create(p))
T.check(made==1 and types==1,'same tile reuses one Forest and shared datablock')
T.check(a.item.m[1].y==5 and a.item.scale==1,'preserve tested beam column scaling')
local f=scenetree.findObjectById(a.forest)
T.check(f.pos.x==10 and f.pos.z==30,'initial frame applied')
assert(backend.setFrame({-100,200,0,0,0,0,1}))
T.check(made==1 and types==1 and f.pos.x==-100 and items[a.item],
        'rebase transforms existing Forest without remaking parts')
T.check(not backend.setFrame({0,0,0,0,0,0,2}),'invalid frame cannot alter scene')
assert(backend.remove(a))
T.check(not backend.remove(a) and removed==1,'remove each owned item only once')
scenetree.MissionGroup=S.newObject('SimGroup');scenetree.MissionGroup:registerObject('MissionGroup2')
T.check(not backend.remove(b) and removed==1,'never dereference old map Forest items')
T.check(not backend.create(p),'require explicit reset after level change')
backend.forgetLevel()
assert(backend.create(p))
T.check(made==2 and types==2,'new level creates fresh engine ownership')
T.done('test_nl_signal_forest')
