local dir=arg[0]:match('^(.*)[/\\]')or'.'
local T=dofile(dir..'/testlib.lua')
dofile(dir..'/stubs.lua')
local S=require('beamngEarth/nl_signal_surfaces')
local C=require('beamngEarth/nl_signal_clearance')
local L=require('beamngEarth/nl_signal_lanes')
local P=require('beamngEarth/nl_signals')
local function rectangle(x0,y0,x1,y1,z)
  local rows={{x0,y0,z,x1,y0,z,x1,y1,z},{x0,y0,z,x1,y1,z,x0,y1,z}}
  local grid={};for x=math.floor(x0/8),math.floor(x1/8)do for y=math.floor(y0/8),math.floor(y1/8)do grid[x..','..y]=rows end end
  return {grid=grid}
end
local parts={{kind='road',mesh=rectangle(-100,-6,100,6,10)}}
local coverage=true
S.provider=function()return parts,coverage end
local function walk(y0,y1,z)parts[#parts+1]={kind='walk',mesh=rectangle(-100,y0,100,y1,z or 10.2)}end
local a={'a',0,0,10,1,0,0,0,-4.3,10.5,0}
local g={nodes={a={-60,0,10,3.3},j={30,0,10,3.3}},edges={{'a','j',0,1,1,13,1,3.3,3.3}}}
local lane=L.describe(g,'j',a)
T.check(not C.fit(g,a,lane),'asphalt wider than AI graph is rejected; never plant a right pole there')
walk(-9,-6.5)
local fit=assert(C.fit(g,a,lane))
T.check(fit.y<=-6.7,'right foot moves onto actual sidewalk beyond drawn asphalt')
T.near(fit.z,10.185,1e-6,'foot uses drawn sidewalk height instead of floating source elevation')
T.check(not fit.left_span_m and #fit.feet==1,'left lantern skipped when no walk or median exists')
local frame={worldToGeodetic=function()return 51.37,6.14 end}
local plan=P.plan(a,frame,'stock',true,g,'j')
local left=0;for _,part in ipairs(plan.parts)do if part.key=='support_left'then left=left+1 end end
T.check(plan.model~='stock'and left==0 and plan.layout.total_heads==1,'safe right lantern remains without a left pole')
walk(.5,2,10.4)
walk(7,10,10.2)
fit=assert(C.fit(g,a,lane))
local leftY=fit.y+fit.left_span_m
T.check(leftY>.7 and leftY<1.8,'median takes priority over far sidewalk')
T.near(fit.left_z-fit.z,.2,1e-6,'each pole gets its own ground height')
plan=P.plan(a,frame,'stock',true,g,'j')
for _,part in ipairs(plan.parts)do if part.key=='support_left'then T.near(part.position[3],.2,1e-6,'left pole follows its median surface')end end
local probe=S.begin(0,0,10)
T.check(not probe(0,.51,10),'foot with insufficient room at kerb edge is rejected')
coverage=false;parts={}
T.check(not S.begin(0,0,10)(0,0,10),'unloaded surface cannot create a floating pole')
parts={{kind='road',mesh=rectangle(-10,-10,10,10,10)}}
coverage=function(x)return x<20 end
castRayStatic=function()return 2 end
T.check(not S.begin(0,0,10)(30,0,10),'a nearby loaded tile does not verify ground outside its own footprint')
coverage=true;parts={{kind='road',mesh=rectangle(-10,-10,10,10,5)}}
castRayStatic=function()return 7 end
T.check(not S.begin(0,0,10)(0,0,10),'lower carriageway cannot be mistaken for verge at old graph height')
castRayStatic=nil
parts={{kind='road',mesh=rectangle(-100,-6,100,6,10)},
  {kind='walk',mesh=rectangle(-100,-9,100,-6.5,11.2)}}
castRayStatic=function()return 1 end
T.check(not S.begin(0,-7,11)(0,-7,11),'walk at the wrong physical level cannot float a mast')
castRayStatic=function()return .3 end
T.check(S.begin(0,-7,11)(0,-7,11),'visible supported walk is accepted')
castRayStatic=nil;parts[2].pending=true
T.check(not S.begin(0,-7,11)(0,-7,11),'queued replacement skin is not used before it is drawn')
parts={{kind='road',mesh=rectangle(-100,-6,100,6,10)}}
local function terrain(height,material)
  local names={'bngEarthGroundAsphalt','bngEarthGroundGrass'}
  local block={getMaterialIdxWs=function(_,p)return material and material(p)or 1 end,
    getMaterialName=function(_,i)return names[i+1]end,getMaterialCount=function()return #names end}
  return {getTerrainHeight=function()return height end,getTerrain=function()return block end}
end
core_terrain=terrain(10.2)
castRayStatic=function()return 0 end -- a nearby static object must not lift a verge pole
local okGround,actualGround,groundKind=S.begin(0,-8,10)(0,-8,10)
T.check(okGround and groundKind=='verge','terrain provides a grounded verge when a static ray is misleading')
T.near(actualGround,10.185,1e-6,'verge follows actual terrain instead of another static object')
T.check(not S.begin(0,-6.05,10)(0,-6.05,10),'verge centre outside asphalt is insufficient when the foot still overlaps the road')
parts={{kind='road',mesh=rectangle(.1,.1,1,1,10)}}
T.check(not S.begin(0,0,10)(0,0,10),'diagonal asphalt corner also rejects an overlapping verge foot')
-- Painted carriageways have no filled asphalt mesh: use the current native
-- terrain material rather than mistaking the gaps between markings for grass.
parts={{kind='road',mesh=rectangle(-100,-.05,100,.05,10)}}
core_terrain=terrain(10,function(p)return math.abs(p.y)<6 and 0 or 1 end)
local painted=S.begin(0,-4,10)
local paintOk,_,paintReason=painted(0,-4,10)
T.check(not paintOk and paintReason=='terrain_road','painted asphalt between road markings is rejected')
T.check(not painted(0,-6.05,10),'whole foot must clear the painted carriageway edge too')
T.check(painted(0,-6.5,10),'a measured grass verge beside painted asphalt remains usable')
core_terrain=terrain(10,function(p)return p.x>.1 and p.y>.1 and 0 or 1 end)
T.check(not S.begin(0,0,10)(0,0,10),'diagonal painted asphalt corner rejects an overlapping foot')
core_terrain={getTerrainHeight=function()return 10 end}
T.check(not S.begin(0,-4,10)(0,-4,10),'missing terrain material information does not certify a verge')
core_terrain={getTerrainHeight=function()return 10 end,getTerrain=function()return {
  getMaterialIdxWs=function()return 0 end,getMaterialName=function()return 'beamngEarthDirt'end,
  getMaterialCount=function()return 1 end}end}
T.check(not S.begin(0,-4,10)(0,-4,10),'default dirt without a road mask cannot certify a verge')
core_terrain=terrain(10,function()return 0 end)
walk(-9,-6.5,10.2);castRayStatic=function()return .3 end
T.check(S.begin(0,-7,10)(0,-7,10),'precise drawn sidewalk survives a coarse asphalt terrain cell')
parts={};coverage=false;castRayStatic=nil
core_terrain=terrain(10,function(p)return math.abs(p.y)<6 and 0 or 1 end)
T.check(S.begin(0,-8,10)(0,-8,10),'loaded native grass is usable when the painted tile has no road shape')
T.check(not S.begin(0,0,10)(0,0,10),'missing road shape never makes native asphalt a verge')
parts={{kind='road',pending=true,footprint={{-2,-10},{2,-10},{2,-7},{-2,-7}}}}
T.check(not S.begin(0,-8,10)(0,-8,10),'pending drawn shape still blocks its own foot even above native grass')
coverage=true
core_terrain=nil;castRayStatic=nil
-- Six heads fit a procedural portal. Two three-head masts remain an alternative
-- even where the broader AI strip still covers a measured grass island.
parts={{kind='road',mesh=rectangle(-100,-.05,100,.05,10)}}
core_terrain=terrain(10,function(p)return math.abs(p.y)<9.9 and 0 or 1 end)
local sixA={'a',0,0,10,1,0,0,0,-10.9,10.15,0}
local sixG={nodes={a={-60,0,10,10.5},j={30,0,10,10.5}},edges={{'a','j',1,6,0,13,1,10.5,10.5}}}
local defaultSix=P.plan(sixA,frame,'stock',true,sixG,'j')
T.check(not defaultSix.omit and defaultSix.layout.kind=='portal'and defaultSix.layout.head_count==6,'six lanes use a portal when both feet and span fit')
local six=P.plan(sixA,frame,'stock',true,sixG,'j',true)
T.check(not six.omit and six.layout.kind=='paired_cantilevers','six-lane approach uses measured grass verges for two mirrored masts')
T.check(six.layout.head_count==6 and six.layout.arms[1].heads==3 and six.layout.arms[2].heads==3,'six lanes retain exactly six main lanterns, three per mast')
core_terrain=nil
-- A portal leg can stand across the opposing road while a short median begins
-- a few metres before the stop line. Add a separately grounded standing lamp.
local function remotePortal(rightWalk,nearWalk)
  parts={{kind='road',mesh=rectangle(-100,-8.25,100,8.25,10)}}
  walk(rightWalk[1],rightWalk[2]);walk(12,15)
  if nearWalk then parts[#parts+1]={kind='walk',mesh=rectangle(-6,nearWalk[1],-2,nearWalk[2],11)}end
  local aa={'a',0,0,10,1,0,0,0,-9.25,10.5,0}
  local gg={nodes={a={-60,0,10,8.25},j={30,0,10,8.25}},edges={{'a','j',0,4,1,13,1,8.25,8.25}}}
  return P.plan(aa,frame,'stock',true,gg,'j')
end
local remote=remotePortal({-11,-8.5},{5.3,7.2})
local extra=remote.placement.roadside_repeaters
T.check(remote.layout.kind=='portal'and #extra==1 and extra[1].side=='left','remote portal foot gets one near-median lamp')
T.check(extra[1].edge_distance_m<=3 and extra[1].portal_foot_distance_m>3,'extra lamp follows incoming edge, not opposing verge')
T.check(extra[1].foot[1]<-2 and extra[1].foot[2]>5.3,'nearby median before stop line is used')
T.near(extra[1].foot[3],10.985,1e-6,'supplemental pole follows its own higher ground')
local nearPole,nearHead
for _,p in ipairs(remote.parts)do
  if p.key=='support_left_near_repeat'then nearPole=p end
  if p.key=='head_left_near_repeat'then nearHead=p end
end
T.check(nearPole and nearHead and nearHead.repeat_head and remote.layout.total_heads==6,'nearby repeater replaces attached lamp without increasing head count')
local attachedLeft=false
for _,p in ipairs(remote.parts)do if p.key=='head_left'or p.key=='low_bracket_left'then attachedLeft=true end end
T.check(not attachedLeft,'left attached low lamp and bracket disappear when standing repeater is used')
T.near(nearHead.position[3]-nearPole.position[3],P and require('beamngEarth/nl_signal_mounts').catalog.supports.straight.mounts[1].position[3],1e-6,'extra lamp uses original standing height')
T.check(#remote.placement.feet==3,'all actual feet included in placement audit')
-- No aligned portal foot exists here; the island only starts before the stop line.
parts={{kind='road',mesh=rectangle(-100,-8.25,100,8.25,10)},
  {kind='walk',mesh=rectangle(-100,-11,100,-8.5,10.2)},
  {kind='walk',mesh=rectangle(-6,5.3,-2,7.2,10.4)}}
local fa={'a',0,0,10,1,0,0,0,-9.25,10.5,0}
local fg={nodes={a={-60,0,10,8.25},j={30,0,10,8.25}},edges={{'a','j',0,4,1,13,1,8.25,8.25}}}
local fallback=P.plan(fa,frame,'stock',true,fg,'j')
T.check(fallback.layout.kind=='cantilever_and_pole'and #fallback.layout.mounts==4,'missing portal foot uses three overhead plus one standing lane head')
local occupied={existing=fallback}
local shifted=P.place(fa,frame,'stock',true,fg,'j',occupied,'another')
T.check(shifted.omit or not require('beamngEarth/nl_signal_space').conflict(shifted,occupied,'another'),'neighboring placement never morphs into existing assembly')
local unsafe=remotePortal({-11,-8.5})
T.check(#unsafe.placement.roadside_repeaters==0,'no extra left pole without safe median')
parts[#parts+1]={kind='walk',mesh=rectangle(-100,6,100,8,10.2)}
local aa={'a',0,0,10,1,0,0,0,-9.25,10.5,0}
local gg={nodes={a={-60,0,10,8.25},j={30,0,10,8.25}},edges={{'a','j',0,4,1,13,1,8.25,8.25}}}
local close=P.plan(aa,frame,'stock',true,gg,'j')
T.check(#close.placement.roadside_repeaters==0,'portal foot already near lanes does not get a duplicate')
local right=remotePortal({-18,-15},{-10.5,-8.5})
T.check(#right.placement.roadside_repeaters==1 and right.placement.roadside_repeaters[1].side=='right','remote right foot also gets a safe closer lamp')
local attachedRight=false
for _,p in ipairs(right.parts)do if p.key=='head_right_repeat'or p.key=='low_bracket_right'then attachedRight=true end end
T.check(not attachedRight and right.layout.total_heads==6,'same replacement rule removes the right attached lamp and bracket')
local footprint=right.placement.roadside_repeaters[1].foot
T.check(S.begin(footprint[1],footprint[2],footprint[3])(unpack(footprint)),'supplemental right pole verified on drawn walk')
-- A costly context/graph update happens before the surface reader every frame.
-- It must not spend the reader's own allowance and leave all VRI requests pending.
local oldClock,oldOpen,oldDecode=os.clock,io.open,S.decode
local clock,reads=0,0
os.clock=function()return clock end
io.open=function(path)
  reads=reads+1
  return {read=function()return path end,close=function()end}
end
S.decode=function(path)
  clock=clock+.003
  return path=='walk'and rectangle(-10,-9,10,-6.5,10.2)or rectangle(-10,-6,10,6,10)
end
local roadOwner,walkOwner={},{}
parts={{kind='road',shape='road',owner=roadOwner},{kind='walk',shape='walk',owner=walkOwner}}
S.newFrame();clock=clock+.010
local ready=S.begin(0,-7,10)
T.check(reads==1 and not ready(0,-7,10),'reader makes progress despite other work and keeps own per-frame bound')
S.newFrame();clock=clock+.010
ready=S.begin(0,-7,10)
T.check(reads==2 and ready(0,-7,10),'deferred surface finishes next frame; VRI placement resumes')
parts[#parts+1]={kind='road',shape='not_loaded',owner={},
  footprint={{20,20},{30,20},{30,30},{20,30}}}
ready=S.begin(0,-7,10)
T.check(ready(0,-7,10),'pending neighboring tile does not hide a verified local mast foot')
T.check(not ready(25,25,10),'pending tile still blocks a foot inside its own footprint')
os.clock,io.open,S.decode=oldClock,oldOpen,oldDecode
S.provider=nil;S.reset()
T.done('test_nl_signal_surfaces')
