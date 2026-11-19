local root=(arg[0]:match('^(.*[/\\])') or './')..'../beamng_earth/lua/vehicle/extensions/beamngEarth/'
local S=dofile(root..'staticObstacles.lua')
local M=dofile(root..'oncomingPass.lua')
local H=dofile(root..'autodrive.lua')
assert(M.enabled==false and S.enabled==false,'expensive controllers default off')
S.setEnabled(true);M.setEnabled(true)
local n=0
local function check(v,msg)n=n+1;assert(v,msg)end
beamngEarth_staticObstacles,beamngEarth_oncomingPass=S,M
local p={x=1.55,y=5,z=.2}
local heading={x=0,y=1,z=0}
local speed,requests,changes,originShift=0,0,{},0
local md={graph={},positions={},radius={}}
local ids={}
for i=1,51 do md.graph[i]={};md.positions[i]={x=0,y=(i-1)*20,z=0};md.radius[i]=6.2;ids[i]=i end
for i=1,50 do local e={inNode=i,inRadius=6.2,outRadius=6.2,speedLimit=14};md.graph[i][i+1]=e;md.graph[i+1][i]=e end
function vec3(x,y,z)return {x=x,y=y,z=z}end
local objects={[11]={pos=vec3(4.65,50,.8),vel=vec3(0,0,0),dirVec=vec3(0,1,0)}}
local lastArgs
ai={mode='manual',speedMode='legal'}
function ai.setSpeed(v)ai.routeSpeed=v end
function ai.setSpeedMode(v)ai.speedMode=v end
function ai.setMode(v)ai.mode=v end
function ai.driveInLane(v)ai.driveInLaneFlag=v end
function ai.getEdgeLaneConfig()return '--++'end
function ai.laneChange(_,dist,delta)changes[#changes+1]={dist=dist,delta=delta}end
function ai.driveUsingPath(args)lastArgs=args;ai.mode='manual';ai.routeSpeed=args.routeSpeed;ai.speedMode=args.routeSpeedMode;ai.driveInLaneFlag=args.driveInLane end
function ai.setPath()end
mapmgr={mapData=md,requestMap=function()end}
function mapmgr.findBestRoad()local a=math.floor((p.y-originShift)/20)+1;return a,a+1 end
function mapmgr.getObjects()return objects end
obj={}
function obj:getFrontPosition()return p end
function obj:getDirectionVector()return heading end
function obj:getDirectionVectorUp()return vec3(0,0,1)end
function obj:getVelocity()return {length=function()return speed end}end
function obj:getInitialWidth()return 2 end
function obj:getInitialLength()return 5 end
function obj:getInitialHeight()return 1.7 end
function obj:getSurfaceHeightBelow()return 0 end
function obj:getId()return 10 end
function obj:queueGameEngineLua(command)if command:find('retryAutodrive',1,true)then requests=requests+1 end end
function obj:getObjectCenterPositionXYZ(id)local q=id==10 and p or objects[id].pos;return q.x,q.y,q.z end
function obj:getObjectDirectionVectorXYZ(id)local q=objects[id].dirVec;return q.x,q.y,q.z end
function obj:getObjectInitialWidth()return 2 end
function obj:getObjectInitialLength()return 5 end
function obj:getObjectInitialHeight()return 1.7 end
function obj:castRayStatic(o,d,len)
 if d.z<0 then return math.min(len,o.z/-d.z)end
 if d.y>0 and o.y<=50+originShift and o.y+d.y*len>=50+originShift then
  local t=(50+originShift-o.y)/d.y;local x=o.x+d.x*t
  if x>1.3 and x<1.8 then return t end
 end
 return len
end
local function tick()S.updateGFX(.2);return S.status()end
H.drive(ids,1,true)
check(lastArgs.wpTargetList~=nil and lastArgs.driveInLane=='on','ordinary start retains lane-aware route')
check(tick().phase=='waiting' and M.offLane(),'safe alternate requires fresh temporary route')
check(requests==1 and #changes==0,'request GPS handoff before native lane-change command')
H.drive(ids,1,false)
check(lastArgs.path~=nil and lastArgs.wpTargetList==nil and lastArgs.driveInLane=='off','handoff uses public explicit-path interface only during bypass')
check(lastArgs.routeSpeed==3 and lastArgs.routeSpeedMode=='limit','temporary route starts with slow ceiling')
check(tick().phase=='passing oncoming side','active safe passage is automatic')
check(#changes==1 and changes[1].delta<0,'native change points toward opposing side')
tick();check(#changes==1,'no cumulative relative shifts on unchanged route')
H.hold();tick()
check(M.active() and M.suspended() and ai.mode=='stop','temporary GPS hold keeps the obstruction and brakes')
H.drive(ids,1,true);tick()
check(M.offLane() and not M.suspended() and ai.driveInLaneFlag=='off','fresh usable path resumes the same opposite-side pass')
check(H.drive({'unavailable'},1,true)=='waiting' and M.suspended(),'unavailable vehicle-map waypoints preserve the manoeuvre while requesting the map')
H.drive(ids,1,true);tick()
check(M.offLane() and not M.suspended(),'map acknowledgement can resume after an unavailable path')
p.x,p.y=-1.55,47
check(tick().phase=='passing oncoming side','stay alongside obstacle at low speed')
objects[12]={pos=vec3(-1.55,200,.8),vel=vec3(0,-14,0),dirVec=vec3(0,-1,0)}
check(tick().phase=='waiting' and ai.routeSpeed==0,'new oncoming traffic stops unsafe continuation')
objects[12]=nil
check(tick().phase=='passing oncoming side','clearance is rechecked after traffic leaves')
p.y=60
check(tick().phase=='returning' and not M.offLane(),'rear clearance requests normal lane-aware return')
H.drive(ids,1,false)
check(lastArgs.wpTargetList~=nil and lastArgs.driveInLane=='on','return restores waypoint lane context despite matching IDs')
objects[12]={pos=vec3(1.55,72,.8),vel=vec3(0,0,0),dirVec=vec3(0,1,0)}
check(tick().phase=='waiting' and ai.routeSpeed==0,'new traffic blocks the return safely')
objects[12]=nil
check(tick().phase=='returning','return resumes when road is clear')
p.x,p.y=1.35,80;heading.x,heading.y=.2,.98
check(tick().phase=='returning' and M.active(),'front entering the lane does not release a rear corner still across the centreline')
p.x,p.y=1.55,80;heading.x,heading.y=0,1
check(tick().phase=='idle' and not M.active(),'full vehicle returns to correct side before releasing control')
check(ai.routeSpeed==nil and ai.speedMode=='legal','normal legal speed policy restored')

H.reset();p.x,p.y=1.55,5;H.drive(ids,2,true);tick();H.drive(ids,2,false);tick()
local replacement={graph=md.graph,positions={},radius=md.radius}
for id,pos in pairs(md.positions)do replacement.positions[id]=vec3(pos.x,pos.y+500,pos.z)end
mapmgr.mapData=replacement;p.y=p.y+500;originShift=500;objects[11].pos.y=objects[11].pos.y+500
H.drive(ids,2,false)
check(lastArgs.driveInLane=='off','map replacement retains temporary passage policy')
check(tick().phase=='passing oncoming side','obstacle anchor survives coordinate rebase')
ai.mode='disabled';tick()
check(not M.active(),'external disable discards temporary opposite-side state')
check(ai.driveInLaneFlag=='on','temporary lane policy restored on disable')
H.reset();originShift=0;ai.mode='manual';p.x,p.y=1.55,5
local approach={graph=md.graph,positions={},radius=md.radius}
approach.positions[1]=vec3(0,0,0)
for i=2,51 do approach.positions[i]=vec3(0,200+(i-2)*20,0)end
mapmgr.mapData=approach
function mapmgr.findBestRoad()return 1,2 end
objects[11].pos.y=50
local forward={};for i=2,51 do forward[#forward+1]=i end
H.drive(forward,3,true);tick();H.drive(forward,3,false);tick()
check(M.offLane(),'pass starts on implicit approach before distant first waypoint')
local shifted={graph=approach.graph,positions={},radius=approach.radius}
for id,pos in pairs(approach.positions)do shifted.positions[id]=vec3(pos.x,pos.y+500,pos.z)end
mapmgr.mapData=shifted;originShift=500;p.y=505;objects[11].pos.y=550
H.drive(forward,3,true);tick()
p.x,p.y=-1.55,560
check(tick().phase=='returning','implicit approach anchor preserves actual obstruction position through rebase')
check(M.active(),'the final fixture has an active temporary passage')
local before=requests
M.setEnabled(false)
check(not M.active() and ai.driveInLaneFlag=='on' and requests==before+1,'disabling clears the temporary lane policy and requests a normal handoff')
local originalPlan=M.plan
M.plan=function()error('disabled opposing-lane sensing ran')end
check(M.update()==nil,'disabled updates skip corridor and traffic sensing')
M.plan=originalPlan
-- Stationary refusals replan at 1 Hz; active passage checks above remain per probe.
M.setEnabled(true);M.reset()
local attempts=0
M.plan=function()attempts=attempts+1;return nil,'blocked sightline',20 end
local savedCorridor,savedView=S.corridor,M.visibilityPath
S.corridor=function()return {}end;M.visibilityPath=function()return {}end
local c={lane={centre=1.55,width=3.1},points={{x=0,y=0,z=0,fx=0,fy=1}}}
local hit={x=0,y=45,z=0,distance=45};local ego={width=2,length=5}
local function waitingStep(v)return M.update(S,md,ids,c,hit,ego,{},function()end,v or 0,14,1,2)end
ai.routeSpeed=12
local first=waitingStep()
for i=1,4 do M.updateGFX(.2);waitingStep()end
check(attempts==1 and M.status().phase=='waiting','a stationary refusal avoids four repeated full visibility plans')
M.updateGFX(.21);waitingStep()
check(attempts==2,'the waiting planner retries after one simulated second')
hit.y=50;waitingStep()
check(attempts==3,'a different obstruction invalidates the waiting cache immediately')
waitingStep(1)
check(attempts==4,'a moving car does not use the stationary wait cache')
waitingStep();local total=attempts;M.bindPath(md,true);waitingStep()
check(attempts==total+1,'a new route invalidates a stationary refusal')
M.setEnabled(false);M.updateGFX(1)
check(M.status().retryIn==nil,'disabling discards the waiting planner state')
M.plan,S.corridor,M.visibilityPath=originalPlan,savedCorridor,savedView
local cancel=S.cancelProbe
local cancelled=0
S.cancelProbe=function()cancelled=cancelled+1 end
M.onReset();M.onExtensionUnloaded()
check(cancelled==2,'reset and unload cancel a suspended obstacle coroutine before releasing passage state')
S.cancelProbe=cancel
-- Native Roamer reproduction: the old fixed slope inflated the swept
-- width beyond a 3.3 m road half-width, despite an empty and supported return.
H.reset();M.setEnabled(true);S.setEnabled(true);mapmgr.mapData=md
originShift=0;objects={};p.x,p.y=1.65,5;heading.x,heading.y=0,1
function mapmgr.findBestRoad()local a=math.floor(p.y/20)+1;return a,a+1 end
for i=1,51 do md.radius[i]=3.3 end
for i=1,50 do md.graph[i][i+1].inRadius=3.3;md.graph[i][i+1].outRadius=3.3 end
function ai.getEdgeLaneConfig()return '-+'end
function obj:getInitialWidth()return 2.2 end
function obj:getInitialLength()return 5.49 end
H.drive(ids,4,true);tick();H.drive(ids,4,false);tick()
check(M.offLane(),'wide car commits only after checking the full passage and return')
p.x,p.y=-1.735,60
check(tick().phase=='returning','wide car requests a lane-aware return after clearing the bollard')
H.drive(ids,4,false)
check(tick().phase=='returning' and ai.routeSpeed==3,
 'wide car keeps moving through a shallow supported return instead of stopping on the opposing side')
p.x,p.y=1.65,130
check(tick().phase=='idle' and not M.active(),'wide car releases the passing policy after its whole body returns')
print(string.format('oncoming pass controller: %d checks passed',n))
