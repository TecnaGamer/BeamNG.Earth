-- Exercise the controller with real geometry and simulated public vehicle APIs.
local root=(arg[0]:match('^(.*[/\\])') or './')..'../beamng_earth/lua/vehicle/extensions/beamngEarth/'
local M=dofile(root..'staticObstacles.lua')
M.setEnabled(true)
local n=0
local function check(v,msg)n=n+1;assert(v,msg)end
local function near(a,b,msg)check(math.abs(a-b)<1e-6,msg)end
local p={x=1.55,y=5,z=.2}
local speed,changes,objects=0,{},{}
local md={graph={},positions={},radius={}}
for i=1,8 do md.graph[i]={};md.positions[i]={x=0,y=(i-1)*20,z=0};md.radius[i]=6.2 end
for i=1,7 do local e={inNode=i,inRadius=6.2,outRadius=6.2,speedLimit=13};md.graph[i][i+1]=e;md.graph[i+1][i]=e end
local ids={1,2,3,4,5,6,7,8}
function vec3(x,y,z)return {x=x,y=y,z=z}end
ai={mode='manual',speedMode='legal'}
function ai.setSpeed(v)ai.routeSpeed=v end
function ai.setSpeedMode(v)ai.speedMode=v end
function ai.getEdgeLaneConfig()return '--++'end
function ai.laneChange(_,dist,delta)changes[#changes+1]={dist=dist,delta=delta}end
mapmgr={mapData=md}
function mapmgr.findBestRoad()local a=math.floor(p.y/20)+1;return a,a+1 end
function mapmgr.getObjects()return objects end
obj={}
function obj:getFrontPosition()return p end
function obj:getDirectionVector()return vec3(0,1,0)end
function obj:getDirectionVectorUp()return vec3(0,0,1)end
function obj:getVelocity()return {length=function()return speed end}end
function obj:getInitialWidth()return 2 end
function obj:getInitialLength()return 5 end
function obj:getInitialHeight()return 1.7 end
function obj:getSurfaceHeightBelow()return 0 end
function obj:getId()return 10 end
function obj:getObjectCenterPositionXYZ(id)local q=objects[id].pos;return q.x,q.y,q.z end
function obj:getObjectDirectionVectorXYZ()return 0,1,0 end
function obj:getObjectInitialWidth()return 2 end
function obj:getObjectInitialLength()return 5 end
function obj:getObjectInitialHeight()return 1.7 end
function obj:castRayStatic(o,d,len)
 if d.y>0 and o.y<=30 and o.y+d.y*len>=30 then
  local t=(30-o.y)/d.y;local x=o.x+d.x*t
  if x>1.3 and x<1.8 then return t end
 end
 return len
end
local function bind()M.bindPath(ids,md,true)end
local function tick()M.updateGFX(.2);return M.status()end
bind();speed=10
check(tick().phase=='slowing','slow before initiating lane change')
near(ai.routeSpeed,4,'passing ceiling before merge')
check(#changes==0,'no merge requested at high speed')
speed=4
check(tick().phase=='passing','automatic clear-lane pass')
check(#changes==1,'one native lane-change request')
near(changes[1].delta,3.1,'legal adjacent displacement')
tick();check(#changes==1,'do not cumulatively shift active plan')
p.x,p.y=4.65,15
ai.routeSpeed,ai.speedMode=nil,'legal';bind()
near(ai.routeSpeed,4,'fresh route preserves active ceiling')
check(tick().phase=='passing','handoff while already in target lane continues')
check(#changes==1,'handoff in target lane does not shift another lane')
p.y=30
check(tick().phase=='passing','keep moving alongside cleared obstacle')
near(ai.routeSpeed,4,'no stop beside obstacle')
p.y=38
check(tick().phase=='idle','release after rear clears')
check(ai.routeSpeed==nil and ai.speedMode=='legal','original legal policy restored')

M.reset();p.x,p.y=1.55,5;speed=0;changes={}
objects={[11]={pos=vec3(4.65,30,.8),vel=vec3(0,0,0),active=false}}
ai.routeSpeed,ai.speedMode=12,'limit';bind()
check(tick().phase=='blocked','occupied adjacent lane blocks pass')
check(#changes==0,'blocked traffic receives no lane-change command')
p.y=27
check(tick().phase=='blocked','continue holding without safe corridor')
near(ai.routeSpeed,0,'native AI brakes before obstruction')
for _=1,100 do tick()end
check(#changes==0,'waiting never forces a pass or creates a recovery timer')
M.reset();check(ai.routeSpeed==12 and ai.speedMode=='limit','reset restores user ceiling')

p.y=5;bind();tick()
ai.routeSpeed,ai.speedMode=7,'legal'
M.reset();check(ai.routeSpeed==7 and ai.speedMode=='legal','external speed policy is not overwritten')
bind();tick();ai.mode='stop';tick()
check(ai.routeSpeed==7 and ai.speedMode=='legal','destination stop releases owned ceiling')
check(M.status().phase=='idle','stop mode never steers')
ai.mode='manual';bind();tick()
mapmgr.mapData={};tick()
check(not M.status().holding,'map replacement releases stale ceiling')
check(ai.routeSpeed==7 and ai.speedMode=='legal','map replacement restores policy')
M.reset();mapmgr.mapData=md;objects={};p.x,p.y=4.65,5
bind();tick()
check(M.status().phase=='idle','clear-road control leaves stock AI alone')
check(ai.routeSpeed==7 and ai.speedMode=='legal','clear road preserves speed mode')

-- Traffic controls share this speed policy with obstacle clearance. A green
-- signal must release its limit without lifting a separate obstacle's limit.
beamngEarth_trafficSignals=dofile(root..'trafficSignals.lua')
M.reset();ai.mode='manual';ai.routeSpeed,ai.speedMode=8,'legal';p.x,p.y=4.65,35;speed=6
local light={action=2,pos=vec3(0,40,0),instance='test-light'}
mapmgr.signalsData={[2]={[3]={light}}}
bind();check(tick().phase=='signal','real signal decision joins the native controller')
check(ai.routeSpeed>0 and ai.routeSpeed<3,'red light brakes while retaining the original ceiling')
p.y=38;speed=0;tick();near(ai.routeSpeed,0,'remain stopped before red line')
light.action=0;tick()
check(ai.routeSpeed==8 and ai.speedMode=='legal','green restores the original speed policy')
light.action=2;tick();ai.routeSpeed,ai.speedMode=7,'legal';tick();light.action=0;tick()
check(ai.routeSpeed==7 and ai.speedMode=='legal','green preserves a newer user policy')
M.reset();ai.routeSpeed,ai.speedMode=8,'legal';p.x,p.y=1.55,27;light.action=2
objects={[11]={pos=vec3(4.65,30,.8),vel=vec3(0,0,0),active=false}}
bind();check(tick().phase=='blocked','the nearer static obstruction still controls braking')
near(ai.routeSpeed,0,'a signal cannot lift the obstacle ceiling')
light.action=0;tick();near(ai.routeSpeed,0,'green does not release the independent obstruction')
M.reset();objects={};p.x,p.y=4.65,38;light.action=2;bind();tick();ai.mode='stop';tick()
check(ai.routeSpeed==8 and ai.speedMode=='legal','arrival releases signal ceiling for native stop mode')
check(not M.status().signal,'arrival discards the signal observation')

-- Chicago's 3.32m alley has 1.66m lanes, narrower than the stock Roamer.
-- Its body fits when centred as the stock AI does; the extra clearance margin
-- must not cast into buildings beyond the physical road on either driving side.
M.reset();mapmgr.signalsData={};objects={};speed=0
function obj:getInitialWidth()return 2.2 end
for i=1,8 do md.radius[i]=1.66 end
for i=1,7 do md.graph[i][i+1].inRadius=1.66;md.graph[i][i+1].outRadius=1.66 end
local realBase=false
function obj:castRayStatic(o,d,len)
 if math.abs(o.x)>1.660001 then return 0 end
 if realBase and d.y>0 and o.y<=30 and o.y+d.y*len>=30 and o.z<.3 then
  local t=(30-o.y)/d.y
  if math.abs(o.x+d.x*t)<.15 then return t end
 end
 return len
end
for _,side in ipairs({-1,1})do
 function ai.getEdgeLaneConfig()return side==1 and '-+'or'+-'end
 p.x,p.y=side*.56,5;ai.mode='manual';ai.routeSpeed,ai.speedMode=8,'legal'
 bind();check(tick().phase=='idle','narrow clear road does not stop for a roadside building')
 check(ai.routeSpeed==8 and ai.speedMode=='legal','clear narrow road preserves the stock speed policy')
 local c=M.corridor(md,ids,1,2,p,2.2,55,ai.getEdgeLaneConfig)
 near(c.points[1].nominal,p.x,'narrow-road forecast retains the actual initial position')
 near(c.points[#c.points].nominal,side*.56,'forecast body remains within the full road width')
 realBase=true;p.y=27;M.reset();bind()
 check(tick().phase=='blocked','a low obstruction inside the real body footprint still blocks driving')
 near(ai.routeSpeed,0,'narrow-road correction retains braking for a real obstruction')
 realBase=false;M.reset()
end
-- A road narrower than the vehicle cannot be declared clear by shrinking its body.
for i=1,8 do md.radius[i]=.9 end
for i=1,7 do md.graph[i][i+1].inRadius=.9;md.graph[i][i+1].outRadius=.9 end
p.x,p.y=0,5
function obj:castRayStatic(o,_,len)return math.abs(o.x)>.900001 and 0 or len end
bind();check(tick().phase~='idle','genuinely insufficient physical clearance remains blocked')
near(ai.routeSpeed,0,'the probe never shrinks below actual vehicle width')

-- Dunman Road: a low roadside contact clips only the extra buffer while the
-- full body corridor is clear. Do not stop or force a lane change for it.
M.reset();objects={};mapmgr.signalsData={};speed=0
for i=1,8 do md.radius[i]=6.2 end
for i=1,7 do md.graph[i][i+1].inRadius=6.2;md.graph[i][i+1].outRadius=6.2 end
function ai.getEdgeLaneConfig()return '--++'end
function obj:getInitialWidth()return 2 end
local fringe,fringeY,wide=0,8.6,false
function obj:castRayStatic(o,d,len)
 if d.y>0 and o.y<=fringeY and o.y+d.y*len>=fringeY then
  local t=(fringeY-o.y)/d.y
  if wide or math.abs(o.x+d.x*t-fringe)<.05 then return t end
 end
 return len
end
for _,side in ipairs({-1,1})do
 p.x,p.y=1.55,5;fringe=p.x+side*(1+M.MARGIN_M)
 ai.mode='manual';ai.routeSpeed,ai.speedMode=8,'legal';changes={};bind()
 check(tick().phase=='clearance','buffer-only contact allows cautious movement on either side')
 near(ai.routeSpeed,4,'nearby buffer contact retains a low speed ceiling')
 check(#changes==0,'body-clear corridor does not require a lane change')
 p.y=12;tick()
 check(ai.routeSpeed==8 and ai.speedMode=='legal','passing the contact restores the prior speed policy')
 M.reset()
end
p.x,p.y=1.55,5;wide=true;bind()
check(tick().phase=='blocked','an obstruction across the body still stops the car')
near(ai.routeSpeed,0,'a body collision cannot inherit the buffer-only movement allowance')
M.reset();wide=false;p.y=38;fringeY=41.6;fringe=p.x-1-M.MARGIN_M
light.pos=vec3(0,40,0);light.action=2;mapmgr.signalsData={[2]={[3]={light}}};bind()
check(tick().phase=='signal','a red signal still overrides buffer-only clearance')
near(ai.routeSpeed,0,'buffer clearance cannot move through a red light')
light.action=0;tick();near(ai.routeSpeed,4,'green releases only the signal hold')
M.reset();mapmgr.signalsData={};p.y=5
function obj:castRayStatic(_,_,len)return len end
function obj:getSurfaceHeightBelow(q)return q.x<.4 and -1e20 or 0 end
bind();check(tick().phase=='blocked','missing support at the buffer edge remains a closed path')
near(ai.routeSpeed,0,'unloaded ground is never treated as harmless roadside clearance')
-- Turning the controller off restores its speed owner and skips all native sensing calls.
M.setEnabled(false)
check(ai.routeSpeed==8 and ai.speedMode=='legal','switching off restores the prior speed policy')
local originalFront=obj.getFrontPosition
local signalController=beamngEarth_trafficSignals;beamngEarth_trafficSignals=nil
obj.getFrontPosition=function()error('disabled obstacle sensing ran')end
for _=1,100 do M.updateGFX(.2) end
check(not M.status().holding,'the disabled path performs no obstacle sensing')
obj.getFrontPosition=originalFront
beamngEarth_trafficSignals=signalController
function obj:getSurfaceHeightBelow()return 0 end
M.setEnabled(true);tick()
check(M.status().enabled,'the saved path can be used immediately after re-enabling')
M.reset();p.x,p.y=4.65,38;speed=0;ai.routeSpeed,ai.speedMode=8,'legal'
light.action=2;mapmgr.signalsData={[2]={[3]={light}}};M.setEnabled(false);bind()
local oldCast,oldFloor=obj.castRayStatic,obj.getSurfaceHeightBelow
obj.castRayStatic=function()error('disabled obstacle raycast ran')end
obj.getSurfaceHeightBelow=function()error('disabled obstacle ground query ran')end
check(tick().phase=='signal' and ai.routeSpeed==0,'red-light braking works with obstacle sensing off and no rays')
light.action=0;tick()
check(ai.routeSpeed==8 and ai.speedMode=='legal','green releases the signal ceiling while obstacle sensing stays off')
obj.castRayStatic,obj.getSurfaceHeightBelow=oldCast,oldFloor
light.action=2;M.setEnabled(true)
local oldCorridor=M.corridor;M.corridor=function()return nil end
check(tick().phase=='signal' and ai.routeSpeed==0,'an unavailable obstacle corridor does not hide a red light')
M.corridor=oldCorridor
M.setEnabled(false)
near(ai.routeSpeed,0,'disabling obstacle avoidance does not release the red-light hold')
-- Signals must brake without waiting for a budgeted obstacle trace to finish.
HighPerfTimer=function()return {stopAndReset=function()return .8 end}end
M.reset();M.PROBE_MAX_S=10;M.setEnabled(true);p.x,p.y=1.55,5;speed=0
light.action=2;light.pos=vec3(0,8,0);mapmgr.signalsData={[1]={[2]={light}}}
ai.mode='manual';ai.routeSpeed,ai.speedMode=8,'legal';bind()
M.updateGFX(.016)
check(M.status().probePending,'the obstacle trace is deliberately suspended')
check(ai.routeSpeed==0,'red-light braking applies while obstacle sensing is unfinished')
M.setEnabled(false)
check(not M.status().probePending and ai.routeSpeed==0,'disabling pending obstacle work preserves the independent signal hold')
HighPerfTimer=nil
M.setEnabled(true);mapmgr.signalsData={}
local gapReports={}
obj.queueGameEngineLua=function(_,command)gapReports[#gapReports+1]=command end
M.reset();M.bindPath(ids,md);speed=0
function obj:getSurfaceHeightBelow()return -1e20 end
tick()
check(M.status().reason=='surface gap','terrain support failures have an explicit reason')
check(gapReports[#gapReports]:find('onAiSurfaceGap(10,true)',1,true),'a stopped terrain hold is reported to GPS')
local before=#gapReports;M.reset()
check(#gapReports==before,'a route hold/reset does not claim that the terrain has cleared')
M.bindPath(ids,md)
function obj:getSurfaceHeightBelow()return 0 end
tick()
check(gapReports[#gapReports]:find('onAiSurfaceGap(10,false)',1,true),'a fresh supported probe clears the terrain reason')
function obj:getSurfaceHeightBelow()return -1e20 end
tick();M.onExtensionUnloaded()
check(gapReports[#gapReports]:find('onAiSurfaceGap(10,false)',1,true),'unloading explicitly clears its terrain reason without another probe')
print(string.format('static obstacle controller: %d checks passed',n))
