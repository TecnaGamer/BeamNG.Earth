local dir=(arg[0]:match('^(.*)[/\\]')or'.')
local T=dofile(dir..'/testlib.lua')
local B=dofile(dir..'/../beamng_earth/lua/vehicle/extensions/beamngEarth/trafficBoundary.lua')
local function setup(mirror)
  local x=mirror and -1 or 1
  local md={positions={a={x=0,y=-60,z=0},j={x=0,y=0,z=0},e={x=40*x,y=10,z=0},
    n={x=0,y=60,z=0}},graph={},radius={a=3,j=6,e=3,n=3}}
  for id in pairs(md.positions)do md.graph[id]={}end
  for _,pair in ipairs({{'a','j'},{'j','e'},{'j','n'}})do
    local a,b=pair[1],pair[2];local e={inNode=a,drivability=1,speedLimit=14,lanes='+-',inRadius=3,outRadius=3}
    md.graph[a][b],md.graph[b][a]=e,e
  end
  local signals={a={j={{action=0,pos={x=0,y=-8,z=0}}}}}
  local objects={[2]={pos={x=-2*x,y=40,z=0},vel={x=0,y=-12,z=0},dirVec={x=0,y=-1,z=0},length=5}}
  return md,signals,objects
end
local p,h={x=1,y=-30,z=0},{x=0,y=1,z=0}
local function distance(md,s,o,path,left,origin,a,b)
  return B.turnYieldDistance(md,path or{'a','j','e'},1,a or'a',b or'j',origin or p,h,o,s,left~=false,1)
end
local md,s,o=setup()
T.near(distance(md,s,o),19,1e-9,'left-hand traffic yields before a green right turn crosses opposing flow')
T.check(select(2,distance(md,s,o))==2,'the threatening vehicle is recorded')
T.near(distance(md,s,o,{'j','e'}),19,1e-9,'native paths beginning at the forward waypoint include the occupied approach')
T.near(distance(md,s,o,nil,nil,nil,'j','a'),19,1e-9,'closest-road endpoint order does not change the stop point')
T.check(distance(md,s,o,{'a','j','n'})==nil,'straight traffic keeps its native policy')
T.check(distance(md,s,o,nil,false)==nil,'the near-side turn in right-hand traffic needs no opposing-flow hold')
md,s,o=setup(true)
T.near(distance(md,s,o,nil,false),19,1e-9,'right-hand traffic yields on the mirrored left turn')
T.check(distance(md,s,o)==nil,'the mirrored near-side turn remains native')
md,s,o=setup();s.a.j[1].action=2
T.check(distance(md,s,o)==nil,'native red-light stopping retains ownership')
s.a.j[1].action=3
T.check(distance(md,s,o)==nil,'native stop-sign yielding retains ownership')
T.check(distance(md,nil,o)==nil,'missing signals do not invent controlled turns')
md,s,o=setup();o[2].pos.z=8
T.check(distance(md,s,o)==nil,'traffic on a different level does not block the turn')
md,s,o=setup();o[2].vel.z=2
T.check(distance(md,s,o)==nil,'a projected different level is excluded')
md,s,o=setup();o[2].pos.x=25
T.check(distance(md,s,o)==nil,'a parallel road outside the junction cannot block the turn')
md,s,o=setup();o[2].pos.y=90
T.check(distance(md,s,o)==nil,'distant traffic beyond the gap window does not cause early braking')
md,s,o=setup();o[2].pos.y=-15
T.check(distance(md,s,o)==nil,'traffic that has cleared the junction releases the turn')
md,s,o=setup();o[2].vel.y=12
T.check(distance(md,s,o)==nil,'traffic moving away from the junction is excluded')
md,s,o=setup();o[2].vel.y=0
T.check(distance(md,s,o)==nil,'a stopped queue outside the intersection does not create a mutual yield deadlock')
md,s,o=setup();o[2].dirVec.y=1
T.check(distance(md,s,o)==nil,'same-direction traffic is left to native avoidance')
md,s,o=setup()
T.check(distance(md,s,o,nil,nil,{x=10,y=-30,z=0})==nil,'an offset road cannot inherit another road turn')
T.check(distance(md,s,o,nil,nil,{x=1,y=-30,z=8})==nil,'the ego vehicle on a bridge cannot inherit the lower turn')
T.check(distance(md,s,o,nil,nil,{x=1,y=-5,z=0})==nil,'an already committed vehicle is not stopped in opposing traffic')
T.check(distance(md,s,{})==nil,'an empty junction does not alter speed')
T.check(distance(md,s,{[1]=o[2]})==nil,'the ego vehicle is never treated as opposing traffic')
md.graph.a.j.oneWay=true;md.graph.a.j.inNode='j'
T.check(distance(md,s,o)==nil,'an illegal occupied approach is not used to impose a turn hold')
md,s,o=setup();md.graph.j.e=nil
T.check(distance(md,s,o)==nil,'a disconnected planned turn cannot impose a hold')

-- Exercise the actual speed-policy adapter, including release and shared ownership
-- with the pre-existing streamed-end ceiling. Native steering and avoidance stay active.
md,s,o=setup();p={x=1,y=-30,z=0}
md.getPathTWithState=function()return {'a','j','e'}end
ai={mode='traffic',routeSpeed=20,speedMode='legal',reset=function()end,
  setSpeed=function(v)ai.routeSpeed=v end,setSpeedMode=function(v)ai.speedMode=v end}
obj={getFrontPosition=function()return p end,getDirectionVector=function()return h end,
  getVelocity=function()return {length=function()return 14 end}end,getId=function()return 1 end,
  getLastMailbox=function()end}
mapmgr={mapData=md,signalsData=s,rules={rightHandDrive=true},getObjects=function()return o end,
  findBestRoad=function()return 'a','j'end,setMap=function()end}
B.onExtensionLoaded();B.setCuts({});md:getPathTWithState('j',p,80,h);B.updateGFX(.1)
T.check(ai.routeSpeed==20 and not B.status().holding,'green-turn yielding defaults off without changing boundary control')
B.setTurnYieldEnabled(true);B.updateGFX(.1)
T.near(ai.routeSpeed,math.sqrt(95),1e-9,'the actual green-turn adapter applies a service-braking ceiling')
T.check(ai.mode=='traffic'and ai.speedMode=='limit'and B.status().turnThreat==2,'native traffic mode is preserved and the threat is observable')
B.setTurnYieldEnabled(false)
T.check(ai.routeSpeed==20 and ai.speedMode=='legal','switching off releases an owned turn ceiling')
local oldObjects=mapmgr.getObjects
mapmgr.getObjects=function()error('disabled turn-yield sensing ran')end
B.updateGFX(.1)
T.check(not B.status().turnThreat,'disabled turn yielding skips object sensing')
mapmgr.getObjects=oldObjects;B.setTurnYieldEnabled(true)
p.y=-11;B.updateGFX(.1)
T.check(ai.routeSpeed==0,'the car holds before entering opposing traffic')
o[2].pos.y=-20;B.updateGFX(.1)
T.check(ai.routeSpeed==20 and ai.speedMode=='legal'and not B.status().turnThreat,'clearing opposing traffic restores the previous policy')
o[2].pos.y=40;p.y=-30;B.setCuts({'e'});B.updateGFX(.1)
T.near(ai.routeSpeed,math.sqrt(95),1e-9,'turn and boundary ceilings share one owner and choose the earlier stopping point')
o[2].pos.y=-20;B.updateGFX(.1)
T.near(ai.routeSpeed,math.sqrt(110),1e-9,'releasing the turn cannot release a remaining streamed-boundary hold')
md.graph.e.n=md.graph.j.n;B.updateGFX(.1)
T.check(ai.routeSpeed==20 and ai.speedMode=='legal','extending the terminal road releases its remaining ceiling')
o[2].pos.y=40;B.updateGFX(.1);s.a.j[1].action=2;B.updateGFX(.1)
T.check(ai.routeSpeed==20 and ai.speedMode=='legal','red stopping returns to the native signal policy')
s.a.j[1].action=0;B.updateGFX(.1);ai.routeSpeed=4;ai.speedMode='set';o[2].pos.y=-20;B.updateGFX(.1)
T.check(ai.routeSpeed==4 and ai.speedMode=='set','releasing a turn never overwrites a newer external speed setting')
ai.mode='manual';B.updateGFX(.1)
T.check(not B.status().turnThreat and not B.status().holding,'manual player Autodrive does not inherit traffic-only limits')
B.onExtensionUnloaded()

-- The first visit goes straight; after a real planned loop the next visit turns.
-- Progress must advance even when this route does not end at a streaming cut.
md,s,o=setup();md.positions.x={x=-60,y=-60,z=0};md.radius.x=3;md.graph.x={}
for _,pair in ipairs({{'n','x'},{'x','a'}})do local a,b=pair[1],pair[2];local e={inNode=a,speedLimit=14};md.graph[a][b],md.graph[b][a]=e,e end
md.getPathTWithState=function()return {'a','j','n','x','a','j','e'}end
local a,b='a','j'
mapmgr.mapData, mapmgr.signalsData, mapmgr.getObjects=md,s,function()return o end
mapmgr.findBestRoad=function()return a,b end
ai.mode,ai.routeSpeed,ai.speedMode='traffic',20,'legal';p,h={x=1,y=-30,z=0},{x=0,y=1,z=0}
B.onExtensionLoaded();B.setCuts({});md:getPathTWithState('a',p,80,h);B.updateGFX(.1)
T.check(not B.status().turnThreat,'the first straight visit is not mistaken for the later turn')
for _,pair in ipairs({{'j','n'},{'n','x'},{'x','a'},{'a','j'}})do
  a,b=pair[1],pair[2];local q,r=md.positions[a],md.positions[b]
  local dx,dy=r.x-q.x,r.y-q.y;local l=math.sqrt(dx*dx+dy*dy)
  p={x=(q.x+r.x)*.5,y=(q.y+r.y)*.5,z=0};h={x=dx/l,y=dy/l,z=0};B.updateGFX(.1)
end
T.check(B.status().turnThreat==2,'after the loop, the later crossing turn uses the consumed-path cursor')
B.onExtensionUnloaded()
T.done('traffic turn yielding')
