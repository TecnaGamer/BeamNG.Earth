local dir=(arg[0]:match('^(.*)[/\\]') or '.')
local T=dofile(dir..'/testlib.lua')
local B=dofile(dir..'/../beamng_earth/lua/vehicle/extensions/beamngEarth/trafficBoundary.lua')
local north={x=0,y=1,z=0}
local function road()
  local md={graph={a={},b={},c={}},positions={a={x=0,y=0,z=0},b={x=0,y=100,z=0},c={x=0,y=300,z=0}},radius={a=3,b=3,c=3}}
  for _,pair in ipairs({{'a','b'},{'b','c'}}) do
    local a,b=pair[1],pair[2];local e={inNode=a,oneWay=false,drivability=1,speedLimit=15}
    md.graph[a][b],md.graph[b][a]=e,e
  end
  md.getPathTWithState=function() return {'stock'} end
  return md
end
local function distance(md,p,heading,ends,path,cursor)
  return B.distanceToPlanEnd(md,path or {'a','b','c'},cursor or 1,'a','b',p,heading or north,ends or {c=true})
end
local md=road();local p={x=0,y=30,z=0}
T.near(distance(md,p),70,1e-9,'brake for the penultimate node, not the end of a 200m ignored edge')
T.near(distance(md,p,north,nil,{'b','c'}),70,1e-9,'a fresh path beginning ahead includes the occupied car-to-waypoint approach')
T.near(B.distanceToPlanEnd(md,{'a','b','c'},1,'b','a',p,north,{c=true}),70,1e-9,'road endpoint ordering does not change the approach')
T.check(distance(md,p,{x=0,y=-1})==nil,'traffic travelling away from the cut retains its speed')
T.check(distance(md,p,north,{})==nil,'real dead ends retain native behaviour')
T.check(distance(md,{x=10,y=30,z=0})==nil,'an offset parallel road cannot impose a limit')
T.check(distance(md,{x=0,y=30,z=10})==nil,'a bridge on a different level cannot impose a limit')
T.check(distance(md,p,{x=1,y=0})==nil,'a sideways car is not a forward approach')
T.near(B.distanceToPlanEnd(md,{'a','b','c'},1,'b','c',{x=0,y=120,z=0},north,{c=true}),0,1e-9,'already beyond the usable plan end must stop')
md.graph.b.c.oneWay=true;md.graph.b.c.inNode='c'
T.check(distance(md,p)==nil,'the guard never follows an illegal forward edge')
md=road();md.graph.a.b.oneWay=true;md.graph.a.b.inNode='b'
T.check(distance(md,p)==nil,'the occupied approach must also allow the direction of travel')
md=road();md.graph.b.side={inNode='b'}
T.near(distance(md,p),70,1e-9,'a selected cut beyond a junction still gets a safe braking approach')
T.check(distance(md,p,north,nil,{'a','b','side'})==nil,'traffic selecting the through road at that junction is unaffected')
T.check(distance(md,p,north,nil,{'side','b','c'})==nil,'a nearby approach outside the selected path cannot impose a limit')
md=road();md.graph.c.a={inNode='c'};md.graph.a.c=md.graph.c.a
T.check(distance(md,p)==nil,'loops cannot be mistaken for terminal approaches')
md=road();md.positions.c={x=100,y=100,z=0};md.positions.d={x=100,y=200,z=0};md.radius.d=3
md.graph.c.d={inNode='c'};md.graph.d={c=md.graph.c.d}
T.near(distance(md,p,north,{d=true},{'a','b','c','d'}),170,1e-9,'remaining distance follows the selected road around a bend')
md=road();md.positions.x={x=100,y=100,z=0};md.graph.x={};md.radius.x=3
for _,id in ipairs({'a','b'}) do local e={inNode=id};md.graph[id].x=e;md.graph.x[id]=e end
local loop={'a','b','x','a','b','c'}
T.near(distance(md,p,north,nil,loop,4),70,1e-9,'progress after a planned loop uses the later occurrence of its edge')
T.check(distance(md,p,north,nil,loop,1)>300,'before traversing the loop its remaining distance includes that loop')

-- Run the production public-API adapter, including restoration on every exit.
md=road();p={x=0,y=90,z=0};local speed=10
ai={mode='traffic',routeSpeed=20,speedMode='legal',reset=function() end,
  setSpeed=function(s) ai.routeSpeed=s end,setSpeedMode=function(s) ai.speedMode=s end}
obj={getFrontPosition=function() return p end,getDirectionVector=function() return north end,
  getVelocity=function() return {length=function() return speed end} end,getLastMailbox=function() end}
mapmgr={mapData=md,findBestRoad=function() return 'a','b' end,setMap=function() mapmgr.mapData=road() end}
B.onExtensionLoaded();B.setCuts({'c'})
local function plan() local m=mapmgr.mapData;m:getPathTWithState('b',p,550,north) end
plan();B.updateGFX(.1)
T.near(ai.routeSpeed,math.sqrt(10),1e-9,'the speed ceiling follows a service-braking profile')
T.check(ai.speedMode=='limit' and ai.mode=='traffic','native traffic steering, route planning and avoidance stay active')
p.y=92;B.updateGFX(.1)
T.check(ai.routeSpeed==0,'stop before the steering target can fall behind the car')
md:getPathTWithState('c',md.positions.c,100,{'c'});B.updateGFX(.1)
T.check(ai.routeSpeed==0,'singleton continuation requests retain the occupied approach and its braking policy')
md.graph.c.d={inNode='c'};md.graph.d={c=md.graph.c.d};md.positions.d={x=0,y=400,z=0};md.radius.d=3
B.updateGFX(.1)
T.check(ai.routeSpeed==20 and ai.speedMode=='legal','extending the old cut restores the original speed policy')
md.graph.c.d=nil;p.y=30;B.updateGFX(.1)
T.check(ai.routeSpeed==20 and ai.speedMode=='legal','distant cuts do not slow ordinary traffic')
p.y=84;md.graph.a.b.speedLimit=4;B.updateGFX(.1)
T.check(ai.routeSpeed==4,'a temporary cap cannot raise the legal speed limit')
B.onReset();md.graph.a.b.speedLimit=15;p.y=92;plan()
md.graph.c.d=nil;B.updateGFX(.1);ai.mode='manual';B.updateGFX(.1)
T.check(ai.routeSpeed==20 and ai.speedMode=='legal','player Autodrive is never governed by the traffic limiter')
ai.mode='traffic';plan();B.updateGFX(.1);ai.routeSpeed=7;ai.speedMode='set';ai.mode='disabled';B.updateGFX(.1)
T.check(ai.routeSpeed==7 and ai.speedMode=='set','release does not overwrite a newer external speed policy')
ai.mode='traffic';plan();B.updateGFX(.1);B.onReset()
T.check(ai.routeSpeed==7 and ai.speedMode=='set','a vehicle reset releases its owned override')
plan();B.updateGFX(.1);mapmgr.setMap()
T.check(ai.routeSpeed==7 and ai.speedMode=='set','a replacement map releases the previous map override before planning')
ai.routeSpeed=nil;ai.speedMode=nil;plan();B.updateGFX(.1);B.onExtensionUnloaded()
T.check(ai.routeSpeed==nil and ai.speedMode==nil,'unloading restores even an unset native speed policy')

-- A long ignored final edge can leave the held car outside GE's geometric
-- cut radius. Relay the actual boundary stop, not an ordinary signal wait.
local reports={};local nav0=package.loaded['beamngEarth/navgraph']
package.loaded['beamngEarth/navgraph']={onTrafficCutStop=function(...)reports[#reports+1]={...}end}
obj.getId=function()return 77 end;obj.getPosition=function()return p end
obj.queueGameEngineLua=function(_,code)assert(loadstring(code))()end
md=road();mapmgr.mapData=md;ai.mode='traffic';ai.routeSpeed=20;ai.speedMode='legal'
p={x=0,y=92,z=0};speed=0;B.onExtensionLoaded();B.setCuts({'c'});plan();B.updateGFX(.1)
T.check(#reports==1 and reports[1][1]==77 and reports[1][2]=='c','a native zero-speed cut hold notifies GE of the exact cut')
T.check(reports[1]and reports[1][3]==0 and reports[1][4]==92 and reports[1][5]==0,'the notification identifies the actual vehicle position and elevation')
B.updateGFX(.1)
T.check(#reports==1,'an unchanged stop does not flood the engine queue')
p.y=94;B.updateGFX(.1)
T.check(#reports==2 and reports[2][4]==94,'a car that creeps before stopping refreshes its reported position')
md.graph.c.d={inNode='c'};md.graph.d={c=md.graph.c.d};md.positions.d={x=0,y=400,z=0};md.radius.d=3
B.updateGFX(.1)
T.check(#reports==3 and reports[3][2]=='','an extended road clears the previous cut-stop notification')
md.graph.c.d=nil;B.updateGFX(.1);B.onReset()
T.check(#reports==5 and reports[5][2]=='','vehicle reset invalidates a reported stop')
plan();B.updateGFX(.1);ai.mode='manual';B.updateGFX(.1)
T.check(#reports==7 and reports[7][2]=='','switching out of stock traffic clears the report instead of governing the player')
B.onExtensionUnloaded();package.loaded['beamngEarth/navgraph']=nav0
T.done('traffic boundary braking')
