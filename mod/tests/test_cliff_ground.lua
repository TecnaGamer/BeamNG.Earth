-- Terrain availability gates cliff imports without consuming their shape budget.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local far = require('beamngEarth/farfield')
local roads = require('beamngEarth/roads')
local lattice = require('beamngEarth/lattice')
local gov = { clock=function() return 0 end, try=function() return true end,
  charge=function() end, chargeMotion=function() end }
local function step(n) for _=1,n or 40 do far.update(gov,1/60) end end
far.SPARE_MAX=0
far.setNearVisual('mesh')
far.setExactLevel(0)
far.switchAnchor(1,false)
local box={10,10,0,700,80,100}
local function tile(i)
  far.onTile({anchorId=1,key='0_'..i..'_0',level=0,shape='/t/ground'..i..'.cdae',pos={i*256,0,0}})
end
local function show(i)
  far.onSwap({anchorId=1,id=i+1,add={'0_'..i..'_0'},remove={}})
end
T.check(not far.groundReady(box),'no terrain cannot admit a cliff')
tile(0);tile(2);show(0);show(2);step()
T.check(not far.groundReady(box),'loaded corners do not conceal a missing middle tile')
tile(1);step()
T.check(not far.groundReady(box),'hidden preload is not visible ground')
show(1);step()
T.check(far.groundReady(box),'complete shown terrain admits the whole piece')
local middle=far._tile('0_1_0')
middle.failed=true
T.check(not far.groundReady(box),'failed terrain cannot admit a cliff')
middle.failed=false;middle.grp={}
T.check(not far.groundReady(box),'a terrain crossover finishes before cliffs appear')
middle.grp=nil
far.onBlockCommit(0,0);far.setNearVisual('detail')
local x0,y0,x1,y1=lattice.coverBox(0,0)
T.check(far.groundReady({x0+1,y0+1,0,x1-1,y1-1,100}),'committed visible terrain supports nearby cliffs')
far.setRawView(true)
T.check(not far.groundReady({x0+1,y0+1,0,x1-1,y1-1,100}),'hidden physics block cannot cover missing visual ground')
far.setRawView(false)
far.clear();far.setNearVisual('mesh');far.switchAnchor(1,false)
readFile=function() return '{chunks={},cliff={shape="/t/cliff.cdae",pos={0,0,0},bounds={10,10,0,80,80,100}},cliffStrip1={shape="/t/strip.cdae",pos={0,0,0},bounds={10,10,0,80,80,1}}}' end
jsonDecode=function(s) return assert(load('return '..s))() end
local function count()
 local n=0;for _,o in pairs(S.objects) do
  if o.fields.shapeName=='/t/cliff.cdae' or o.fields.shapeName=='/t/strip.cdae' then n=n+1 end
 end;return n
end
roads.onTile({anchorId=1,key='0_0',path='/t/roads.json',n=0},1)
for _=1,12 do roads.update(gov,1) end
T.check(count()==0 and roads.hasWork(),'cliffs and strips wait while ground is missing')
tile(0);show(0);step()
for _=1,12 do roads.update(gov,1) end
T.check(count()==2,'waiting cliffs and strips load once supporting ground is shown')
roads.setCliffsEnabled(false);roads.setCliffsEnabled(true)
T.check(count()==2,'graphics toggles retain loaded pieces')
roads.clear();far.clear()
T.check(count()==0,'waiting and loaded cliff state clears with the scene')
far.setNearVisual('mesh');far.switchAnchor(1,false)
far.onTile({anchorId=1,key='1_0_0',level=1,shape='/t/coarse.cdae',pos={0,0,0}})
far.onSwap({anchorId=1,id=99,add={'1_0_0'},remove={}});step()
local small={10,10,0,80,80,1}
T.check(far.groundReady(small),'coarse terrain can support broad cliff visibility')
T.check(not far.groundReady(small,true),'coarse placeholders cannot admit fitted rocks or strips')
tile(0);show(0);step()
T.check(far.groundReady(small,true),'exact terrain admits fitted rocks and strips')
local exact=far._tile('0_0_0')
exact.cover='partial';exact.off=-5
T.check(not far.groundReady(small,true),'sunken block-edge under-floors cannot admit floating rocks')
far.clear()
T.done('test_cliff_ground')
