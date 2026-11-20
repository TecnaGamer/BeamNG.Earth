local dir=arg[0]:match('^(.*)[/\\]')or'.'
local T=dofile(dir..'/testlib.lua')
local source=dir..'/../beamng_earth/lua/vehicle/extensions/beamngEarth/staticObstacles.lua'
local function fixture(budgeted)
  local n=0
  local p={x=1.55,y=5,z=.2}
  local md={graph={},positions={},radius={}}
  for i=1,12 do md.graph[i]={};md.positions[i]={x=0,y=(i-1)*20,z=0};md.radius[i]=3.1 end
  for i=1,11 do local e={inNode=i,inRadius=3.1,outRadius=3.1,speedLimit=30};md.graph[i][i+1]=e;md.graph[i+1][i]=e end
  local A={mode='manual',routeSpeed=30,speedMode='legal',getEdgeLaneConfig=function()return '-+'end}
  A.setSpeed=function(s)A.routeSpeed=s end;A.setSpeedMode=function(s)A.speedMode=s end
  local obj={getId=function()return 1 end,getFrontPosition=function()return p end,
    getDirectionVector=function()return {x=0,y=1,z=0}end,getDirectionVectorUp=function()return {x=0,y=0,z=1}end,
    getVelocity=function()return {length=function()return 20 end}end,
    getInitialWidth=function()return 2 end,getInitialLength=function()return 5 end,getInitialHeight=function()return 2 end,
    getSurfaceHeightBelow=function()n=n+1;return 0 end,
    castRayStatic=function(_,o,d,length)
      n=n+1
      if d.y>0 and o.y<=80 and o.y+d.y*length>=80 then return (80-o.y)/d.y end
      return length
    end}
  local E=setmetatable({ai=A,obj=obj,mapmgr={mapData=md,findBestRoad=function()return 1,2 end,getObjects=function()return {}end},
    beamngEarth_trafficSignals=false,beamngEarth_oncomingPass=false,extensions=false,
    vec3=function(x,y,z)return {x=x,y=y,z=z}end,HighPerfTimer=budgeted and function()
      return {stopAndReset=function()return .8 end}
    end or false},{__index=_G})
  local M=setfenv(assert(loadfile(source)),E)()
  M.setEnabled(true);M.PROBE_MAX_S=10
  local ids={2,3,4,5,6,7,8,9,10,11,12};M.bindPath(ids,md)
  local function tick()M.updateGFX(.016);return M.status()end
  return M,A,p,md,ids,tick,function()return n end,E
end
local S,A,_,_,_,tick,count=fixture(false)
local expected=tick();local queries=count();local cap=A.routeSpeed
T.check(not expected.probePending and expected.phase=='blocked','synchronous reference completes a full obstruction probe')
local M,B,p,md,ids,step,calls,E=fixture(true)
local status=step();local frames=1
T.check(status.probePending and calls()<=33,'a budgeted frame yields after one batch of native queries')
T.check(B.routeSpeed==30,'an incomplete short probe does not publish a partial lane or speed decision')
while status.probePending and frames<200 do status=step();frames=frames+1 end
T.check(not status.probePending and frames>2,'the complete probe is distributed over several graphics frames')
T.check(calls()==queries,'budgeting preserves the complete ray and surface grid')
T.near(B.routeSpeed,cap,1e-9,'stationary completion matches the synchronous speed ceiling')
T.check(status.phase==expected.phase,'completed obstacle decision matches the synchronous reference')
M,B,p,md,ids,step,calls,E=fixture(true);status=step();p.y=p.y+4
while status.probePending do status=step()end
T.check(B.routeSpeed<cap,'motion during a probe reduces the available braking distance')
M,B,p,md,ids,step,calls,E=fixture(true);step();M.setEnabled(false);local before=calls();step()
T.check(not M.status().probePending and calls()==before,'disabling discards suspended work without more sensing')
M,B,p,md,ids,step,calls,E=fixture(true);step();M.reset();before=calls();step()
T.check(not M.status().probePending and calls()==before,'reset discards suspended work')
M,B,p,md,ids,step,calls,E=fixture(true);step();B.mode='stop';before=calls();step()
T.check(not M.status().probePending and calls()==before,'AI cancellation cannot resume old sensing work')
M,B,p,md,ids,step,calls,E=fixture(true);step();E.mapmgr.mapData={};before=calls();step()
T.check(not M.status().probePending and calls()==before,'replacement maps discard pending work before it can steer')
M,B,p,md,ids,step,calls,E=fixture(true);step();M.bindPath({2,3,4},md,true)
T.check(not M.status().probePending,'a new route discards the previous route snapshot')
M,B,p,md,ids,step,calls,E=fixture(true);M.PROBE_MAX_S=.03;step();step()
T.check(B.routeSpeed==0 and M.status().reason=='probe budget exceeded','a delayed probe brakes within its look-ahead time margin')
M,B,p,md,ids,step,calls,E=fixture(false)
E.obj.getFrontPosition=function()error('position unavailable')end
local ok,row=pcall(step)
T.check(ok and row.phase=='unavailable','snapshot API errors stay inside the protected probe')
M,B,p,md,ids,step,calls,E=fixture(false)
local positions=0
E.obj.getFrontPosition=function()positions=positions+1;if positions>=3 then error('position unavailable at completion')end;return p end
ok,row=pcall(step)
T.check(ok and row.phase=='unavailable','completion API errors stay inside the protected probe')
M,B,p,md,ids,step,calls,E=fixture(true);M.reset()
if jit then jit.off(M.updateGFX,true) end
collectgarbage('collect');collectgarbage('stop')
local memory=collectgarbage('count');local before=calls()
for i=1,10000 do M.updateGFX(.016) end
local allocation=collectgarbage('count')-memory
collectgarbage('restart')
T.check(calls()==before and allocation<1,'an enabled but unbound helper has a plain allocation-free idle return')
-- A slow, already committed passage may finish its long visibility scan
-- across 30 Hz frames without repeatedly braking. Overdue work still stops.
M,B,p,md,ids,step,calls,E=fixture(true);M.PROBE_MAX_S=.03
E.obj.getVelocity=function()return {length=function()return 3 end}end
E.beamngEarth_oncomingPass={SPEED=3,PROBE_MAX_S=.2,active=function()return true end,status=function()return {}end}
for _=1,4 do step()end
T.check(M.status().probePending and B.routeSpeed==30,'a slow committed pass gets a bounded longer scan window')
for _=1,9 do step()end
T.check(B.routeSpeed==0,'the slow-pass extension still stops when its own bounded window expires')
M,B,p,md,ids,step,calls,E=fixture(true);M.PROBE_MAX_S=.03
E.beamngEarth_oncomingPass={SPEED=3,PROBE_MAX_S=.2,active=function()return true end,status=function()return {}end}
step();step()
T.check(B.routeSpeed==0,'an active passing helper cannot extend the deadline at ordinary road speeds')
-- Loading the signal module does not make a cancelled/disabled obstacle
-- controller alternate between 'idle' and 'disabled' and rerun cleanup each frame.
M,B,p,md,ids,step,calls,E=fixture(false);M.reset();M.setEnabled(false)
E.beamngEarth_trafficSignals={stop=function()error('unbound signal sensing ran')end}
local cleanups=0;local disable=M.setEnabled
M.setEnabled=function(on)cleanups=cleanups+1;return disable(on)end
for _=1,1000 do M.updateGFX(.016)end
T.check(cleanups==0,'a disabled unbound helper stays idle without repeating lifecycle cleanup')
T.done('obstacle probe frame budget')
