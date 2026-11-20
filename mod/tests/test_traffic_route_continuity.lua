local dir=(arg[0]:match('^(.*)[/\\]')or'.')
local T=dofile(dir..'/testlib.lua')
local file=dir..'/../beamng_earth/lua/vehicle/extensions/beamngEarth/trafficBoundary.lua'
local B=dofile(file)
local south={x=0,y=-1,z=0}
local p={x=1,y=15,z=0}
local function road()
  local md={graph={},positions={p={x=0,y=100,z=0},a={x=0,y=60,z=0},j={x=0,y=0,z=0},
    s={x=0,y=-80,z=0},t={x=0,y=-160,z=0},e={x=80,y=0,z=0}},radius={}}
  for id in pairs(md.positions)do md.graph[id]={};md.radius[id]=3 end
  for _,pair in ipairs({{'p','a'},{'a','j'},{'j','s'},{'s','t'},{'j','e'}})do
    local a,b=pair[1],pair[2];local e={inNode=a,outNode=b,oneWay=false,drivability=1,lanes='-+',speedLimit=20,inRadius=3,outRadius=3}
    md.graph[a][b],md.graph[b][a]=e,e
  end
  return md
end
local old={'p','a','j','s','t'}
local function continue(md,path,start,origin,h)return B.continuation(md,path or old,start or'j',origin or p,h or south)end
local md=road();local path=continue(md)
T.check(path and table.concat(path,',')=='j,s,t','keep the committed straight exit at the captured kind of junction')
T.check(table.concat(continue(md,{'p','a','j','e'}),',')=='j,e','keep an already planned turn instead of forcing straight travel')
T.check(continue(md,old,'a',{x=1,y=80,z=0})[2]=='j','a replacement earlier on the same approach keeps its continuation')
T.check(continue(md,{'p','a','j','s','j','e'})==nil,'repeated starting waypoints remain with native route selection')
T.check(continue(md,{'j','s','a','j','e'})==nil,'a repeated first node cannot be mistaken for an unconsumed later occurrence')
T.check(continue(md,old,'p')==nil,'without a known incoming route edge there is no guessed continuation')
T.check(continue(md,old,'t')==nil,'a completed path is not restarted')
T.check(continue(md,old,'missing')==nil,'an unrelated start retains native planning')
T.check(continue(md,old,nil,{x=10,y=15,z=0})==nil,'a parallel road cannot inherit a nearby plan')
T.check(continue(md,old,nil,{x=1,y=15,z=8})==nil,'the road above cannot inherit the lower route')
T.check(continue(md,old,nil,p,{x=0,y=1})==nil,'a reversed car cannot be sent into an old forward route')
T.check(continue(md,old,nil,p,{x=1,y=0})==nil,'a sideways approach is left to native planning')
T.check(continue(md,old,nil,{x=1,y=-3,z=0})==nil,'a passed junction is not targeted behind the car')
md=road();md.graph.j.s.oneWay=true;md.graph.j.s.inNode='s'
T.check(continue(md)==nil,'a new one-way restriction overrides cached intent')
md=road();md.graph.j.s.lanes='--'
T.check(continue(md)==nil,'lanes no longer permitting the requested exit override cached intent')
md=road();md.graph.a.j.lanes='--'
T.check(continue(md)==nil,'the occupied incoming edge must also permit forward travel')
md=road();md.graph.j.s.drivability=0
T.check(continue(md)==nil,'a closed exit is never retained')
md=road();md.graph.a.j=nil
T.check(continue(md)==nil,'a disconnected incoming road is not joined by proximity')
md=road();md.graph.s.t=nil;md.graph.t=nil;md.positions.t=nil
T.check(table.concat(continue(md),',')=='j,s','retain a usable connected prefix when a distant road leaves the window')
md=road();md.radius.a=20;md.radius.j=20
T.check(continue(md,old,nil,{x=10,y=15,z=0})==nil,'edge widths take precedence over a wide junction node radius')
md=road();for _,q in pairs(md.positions)do q.x=q.x+1000;q.y=q.y-2000;q.z=q.z+50 end
T.check(continue(md,old,nil,{x=1001,y=-1985,z=50})[2]=='s','stable road IDs preserve intent through a coordinate rebase')

-- Public native adapters: only the first fresh path after a map replacement is carried over. Normal reroutes,
-- mode changes, physical resets and other helpers retain ownership of their own decisions and speed policy.
local calls,resets,nextPath,extensionPath=0,0,nil,nil
local function stock(self,start,origin,minimum,state)
  calls=calls+1
  if extensionPath then return extensionPath end
  if start=='p'then return nextPath or {'p','a','j','s','t'} end
  return {'j','e'}
end
local function fresh()local m=road();m.getPathTWithState=stock;return m end
ai={mode='traffic',speedMode='legal',routeSpeed=20,reset=function()resets=resets+1 end,
 setSpeed=function(s)ai.routeSpeed=s end,setSpeedMode=function(s)ai.speedMode=s end}
obj={getLastMailbox=function()end,getDirectionVector=function()return south end,getFrontPosition=function()return p end,
 getVelocity=function()return {length=function()return 20 end}end}
mapmgr={mapData=fresh(),setMap=function()mapmgr.mapData=fresh()end,findBestRoad=function()return'a','j'end}
B.onExtensionLoaded();B.setCuts({})
mapmgr.mapData:getPathTWithState('p',{x=0,y=100,z=0},100,south)
local oldMap=mapmgr.mapData;mapmgr.setMap();local n=calls
path=mapmgr.mapData:getPathTWithState('j',p,80,south)
T.check(table.concat(path,',')=='j,s,t'and calls==n,'a replacement retains the accepted route before native random path selection')
T.check(ai.mode=='traffic'and resets==1,'handoffs retain native traffic mode without another AI reset')
T.check(B.status().continuations==1,'a carried-over route is observable')
T.check(oldMap~=mapmgr.mapData,'the replacement uses the actual new map object')
path=mapmgr.mapData:getPathTWithState('j',p,80,south)
T.check(path[2]=='e'and calls==n+1,'a subsequent fresh reroute remains native')
mapmgr.setMap();path=mapmgr.mapData:getPathTWithState('j',p,80,south)
T.check(path[2]=='e','a speculative later query is not treated as accepted intent on the following map')
B.onExtensionUnloaded()

B=dofile(file);nextPath={'p','a','j'};mapmgr.mapData=fresh();B.onExtensionLoaded();B.setCuts({})
mapmgr.mapData:getPathTWithState('p',{x=0,y=100,z=0},100,south)
extensionPath={'j','s','t'};mapmgr.mapData:getPathTWithState('j',p,100,{'p','a','j'});extensionPath=nil
mapmgr.setMap();path=mapmgr.mapData:getPathTWithState('j',p,80,south)
T.check(table.concat(path,',')=='j,s,t','normal native path extensions are retained with the committed prefix')
B.onReset();mapmgr.setMap();path=mapmgr.mapData:getPathTWithState('j',p,80,south)
T.check(path[2]=='e','physical recovery discards old intent')
B.onExtensionUnloaded()

B=dofile(file);nextPath={'p','a','j','e'};mapmgr.mapData=fresh();B.onExtensionLoaded();B.setCuts({})
mapmgr.mapData:getPathTWithState('p',{x=0,y=100,z=0},100,south)
mapmgr.setMap();path=mapmgr.mapData:getPathTWithState('j',p,80,south)
T.check(path[2]=='e'and B.status().continuations==1,'an already accepted turn survives the adapter as well')
ai.mode='manual';B.updateGFX(.1);ai.mode='traffic';mapmgr.setMap();local before=calls
mapmgr.mapData:getPathTWithState('j',p,80,south)
T.check(calls==before+1,'a changed AI mode cannot reuse traffic intent')
B.onExtensionUnloaded()

B=dofile(file);nextPath=nil;mapmgr.mapData=fresh();B.onExtensionLoaded();B.setCuts({})
mapmgr.mapData:getPathTWithState('p',{x=0,y=100,z=0},100,south)
mapmgr.setMap();local m=mapmgr.mapData;m.graph.s.t=nil;m.graph.t=nil;m.positions.t=nil
B.setCuts({'s'});path=m:getPathTWithState('j',p,80,south);B.updateGFX(.1)
T.check(table.concat(path,',')=='j,s','the retained route can terminate at the new artificial boundary')
T.check(B.status().holding and ai.speedMode=='limit'and ai.routeSpeed<10,'the existing boundary brake still sees the retained path')
B.onExtensionUnloaded()
T.done('test_traffic_route_continuity')
