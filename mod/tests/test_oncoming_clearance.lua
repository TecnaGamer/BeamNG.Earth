local dir = arg[0]:match('^(.*)[/\\]') or '.'
local T = dofile(dir .. '/testlib.lua')
local C = dofile(dir .. '/../beamng_earth/lua/vehicle/extensions/beamngEarth/oncomingClearance.lua')
T.check(C.enabled == false, 'experimental narrow-road adjustment defaults off')
C.setEnabled(true)
local road = {halfWidth=2.5,lanes='-+',oneWay=false}
local ego = {id=1,pos={x=0,y=0,z=0},dir={x=0,y=1},width=2,speed=8}
local car = {pos={x=2,y=20,z=0},dirVec={x=0,y=-1},vel={x=0,y=-8},active=false}
local objects = {[2]=car}
T.check(C.restricted(road,ego,objects),'a close moving oncoming car on a narrow two-way road is guarded')
car.active=true
T.check(C.restricted(road,ego,objects),'ordinary traffic active flags do not change physical clearance decisions')
car.vel.y=0
T.check(not C.restricted(road,ego,objects),'a parked car does not suppress ordinary obstacle passing')
car.vel.y=-8;car.dirVec.y=1
T.check(not C.restricted(road,ego,objects),'same-direction traffic retains ordinary passing behaviour')
car.dirVec.y=-1;car.vel.y=8
T.check(not C.restricted(road,ego,objects),'a car moving away is not an oncoming encounter')
car.vel.y=-8;car.pos.z=8
T.check(not C.restricted(road,ego,objects),'a road above the car is ignored')
road.slopeY=.4
T.check(C.restricted(road,ego,objects),'an oncoming car on the same steep grade is recognised')
road.slopeY=nil
car.pos.z=0;car.pos.x=8
T.check(not C.restricted(road,ego,objects),'separated parallel roads are ignored')
car.pos.x=2;car.pos.y=120
T.check(not C.restricted(road,ego,objects),'distant oncoming traffic does not change the controller')
car.pos.y=-20
T.check(not C.restricted(road,ego,objects),'after the car passes the guard releases')
car.pos.y=20;road.oneWay=true
T.check(not C.restricted(road,ego,objects),'one-way roads retain normal lane changes')
road.oneWay=false;road.lanes='-++'
T.check(not C.restricted(road,ego,objects),'multi-lane roads retain normal lane changes')
road.lanes='-+';road.halfWidth=4
T.check(not C.restricted(road,ego,objects),'wide roads retain normal lateral avoidance')
road.halfWidth=2.5;car.pos.x=-2
T.check(C.restricted(road,ego,objects),'the guard also covers left-hand traffic')
T.check(not C.restricted(nil,ego,objects),'missing road metadata leaves stock control intact')

-- Exercise public map binding and parameter ownership, not a copy of the update implementation.
local parameters={awarenessForceCoef=0.25,unrelated=7}
ai={mode='traffic',getParameters=function() return parameters end,
  setParameters=function(p) for k,v in pairs(p) do parameters[k]=v end end}
local function pos(x,y) return {x=x,y=y,z=0} end
local md={graph={a={b={inNode='a',inRadius=2.5,outRadius=2.5,lanes='-+',oneWay=false}}},
  positions={a=pos(0,-30),b=pos(0,50)},radius={a=2.5,b=2.5}}
mapmgr={mapData=md,findBestRoad=function() return 'a','b' end,getObjects=function() return objects end}
obj={getPosition=function() return ego.pos end,getDirectionVector=function() return ego.dir end,
  getInitialWidth=function() return ego.width end,getId=function() return 1 end,
  getVelocity=function() return {length=function() return 8 end} end}
C.bindMap();C.updateGFX(.1)
T.check(parameters.awarenessForceCoef==.0625 and C.status().holding,'the adapter retains a quarter of stock lateral avoidance')
T.check(parameters.unrelated==7,'no other AI parameter is changed')
car.vel.y=0;C.updateGFX(.1)
T.check(parameters.awarenessForceCoef==.25 and not C.status().holding,'parked traffic restores the original coefficient')
car.vel.y=-8;C.updateGFX(.1);parameters.awarenessForceCoef=.4
car.pos.y=120;C.updateGFX(.1)
T.check(parameters.awarenessForceCoef==.4,'release does not overwrite a newer external setting')
car.pos.y=20;C.updateGFX(.1);mapmgr.mapData={}
C.updateGFX(.1)
T.check(parameters.awarenessForceCoef==.4 and not C.status().holding,'an unbound replacement map releases the override')
mapmgr.mapData=md;C.bindMap();C.updateGFX(.1);ai.mode='disabled';C.updateGFX(.1)
T.check(parameters.awarenessForceCoef==.4,'manual driving restores the original configuration')
ai.mode='manual';C.updateGFX(.1)
T.check(parameters.awarenessForceCoef==.1,'Autodrive manual-path mode receives the same protection')
C.setEnabled(false)
T.check(parameters.awarenessForceCoef==.4 and not C.status().holding,'disabling restores the owned coefficient')
local oldProbe=mapmgr.findBestRoad
mapmgr.findBestRoad=function()error('disabled clearance sensing ran')end
for _=1,100 do C.updateGFX(.1) end
T.check(parameters.awarenessForceCoef==.4,'disabled updates skip all road and traffic sensing')
mapmgr.findBestRoad=oldProbe
C.setEnabled(true);C.updateGFX(.1)
T.check(parameters.awarenessForceCoef==.1,'re-enabling uses the current map binding')
C.onExtensionUnloaded()
T.check(parameters.awarenessForceCoef==.4 and not C.status().bound,'unloading releases its own setting and map binding')
T.done('oncoming clearance')
