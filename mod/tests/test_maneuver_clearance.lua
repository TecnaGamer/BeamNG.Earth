local root=arg[0]:match('^(.*[/\\])')..'../beamng_earth/lua/vehicle/extensions/beamngEarth/'
local M=dofile(root..'maneuverClearance.lua')
local n=0
local function check(v,msg)n=n+1;assert(v,msg)end
local function p(x,y,z)return {x=x,y=y,z=z or 0}end
local ego={id=1,pos=p(0,0),dir=p(0,1),up=p(0,0,1),width=2,length=5,height=1.8}
local car={pos=p(-12,4),dirVec=p(1,0),dirVecUp=p(0,0,1),vel=p(8,0),width=2,length=5,height=1.8,active=false}
local objects={[2]=car}
check(M.crossing(ego,objects)==2,'crossing unseated traffic will enter the forward launch footprint')
car.pos.x=-40;check(not M.crossing(ego,objects),'traffic beyond the prediction window is not a launch conflict')
car.pos=p(-12,-10);check(not M.crossing(ego,objects),'traffic passing well behind is left to the native AI')
car.pos=p(-12,4,10);check(not M.crossing(ego,objects),'a separate bridge does not block launch')
car.pos=p(-12,4);car.vel=p(0,-8)
check(not M.crossing(ego,objects),'ordinary along-road oncoming vehicles do not activate a crossing hold')
car.vel=p(0,0);car.pos=p(0,4)
check(not M.crossing(ego,objects),'a new parked obstacle retains stock manoeuvring behaviour')
check(M.crossing(ego,objects,2)==2,'a yielded-to car braking in the corridor still blocks launch')
check(not M.crossing(ego,{},2),'removed traffic no longer blocks launch')
car.vel=p(-8,0);car.pos=p(-12,4)
check(not M.crossing(ego,objects),'crossing traffic moving away is already clear')
ego.dir,ego.up=p(0,.8,.6),p(0,-.6,.8)
car.pos,car.vel,car.dirVecUp=p(-12,3.2,2.4),p(8,0),p(0,-.6,.8)
check(M.crossing(ego,objects)==2,'physical road grade is accounted for in the separation axes')
ego.dir,ego.up=p(0,1),p(0,0,1);car.pos,car.dirVecUp=p(-12,4),p(0,0,1)
check(not M.crossing(ego,nil),'missing traffic cannot introduce a new hold')
local invalid=car.width;car.width=0/0
check(not M.crossing(ego,objects),'invalid dimensions do not create an arbitrary conflict')
car.width=invalid

-- Exercise the real adapter's public controller ownership and AI lifecycle.
function vec3(x,y,z)
 local v=p(x,y,z)
 function v:length()return math.sqrt(self.x^2+self.y^2+self.z^2)end
 function v:dot(o)return self.x*o.x+self.y*o.y+self.z*o.z end
 return v
end
local velocity=0
ai={mode='manual'}
local commands={}
controller={isFrozen=false,setFreeze=function(value)controller.isFrozen=value==1;commands[#commands+1]=value end}
mapmgr={getObjects=function()return objects end}
obj={getVelocity=function()return vec3(0,velocity,0)end,getDirectionVector=function()return vec3(0,1,0)end,
 getDirectionVectorUp=function()return vec3(0,0,1)end,getId=function()return 1 end,
 getInitialWidth=function()return 2 end,getInitialLength=function()return 5 end,getInitialHeight=function()return 1.8 end,
 getObjectCenterPositionXYZ=function(_,id)if id==1 then return 0,0,0 end;return car.pos.x,car.pos.y,car.pos.z end,
 getObjectDirectionVectorXYZ=function()return car.dirVec.x,car.dirVec.y,car.dirVec.z end,
 getObjectDirectionVectorUpXYZ=function()return car.dirVecUp.x,car.dirVecUp.y,car.dirVecUp.z end,
 getObjectInitialWidth=function()return car.width end,getObjectInitialLength=function()return car.length end,
 getObjectInitialHeight=function()return car.height end}
local function tick()M.updateGFX(.1);return M.status()end
M.bindMap()
check(not tick().holding,'a routine stationary AI vehicle is not affected')
velocity=-1;tick()
check(not controller.isFrozen and #commands==0,'reversing motion is not abruptly locked')
velocity=.3;tick()
check(not controller.isFrozen,'a moving vehicle remains under stock braking')
velocity=0
check(tick().holding and controller.isFrozen,'at rest after reversing, hold before launching into crossing traffic')
check(ai.mode=='manual','holding preserves the native AI mode and active route')
local count=#commands;tick()
check(#commands==count,'continued waiting does not repeatedly change controller state')
M.bindMap();check(M.status().holding,'a graph acknowledgement does not release an unsafe launch')
car.pos.x=-2;car.vel=p(0,0);check(tick().holding,'a car braking during the pass remains protected')
for _=1,100 do tick()end
check(M.status().holding,'elapsed waiting does not force an unsafe launch')
local tracked=objects;objects=nil
check(tick().holding,'temporarily unavailable tracking retains the existing hold')
objects=tracked
local centre=obj.getObjectCenterPositionXYZ;obj.getObjectCenterPositionXYZ=function()error('temporarily unavailable')end
check(tick().holding and M.status().error,'a sensing failure retains the existing hold and is diagnosable')
obj.getObjectCenterPositionXYZ=centre
car.pos=p(12,4);check(not tick().holding and not controller.isFrozen,'release once the passing car has fully cleared')
check(not M.status().error,'successful sensing clears the temporary diagnostic error')

M.onReset();car.pos,car.vel=p(-12,4),p(8,0);controller.isFrozen=true
velocity=-1;tick();velocity=0;tick();M.onReset()
check(controller.isFrozen,'an external freeze is never claimed or released by this helper')
controller.isFrozen=false;velocity=-1;tick();velocity=0;tick()
controller.setFreeze(0);tick();tick()
check(not M.status().holding and not controller.isFrozen,'external release is respected without immediately reacquiring the hold')
velocity=-1;tick();velocity=0;tick();ai.mode='disabled';tick()
check(not controller.isFrozen and not M.status().holding,'manual cancellation releases an owned hold')
ai.mode='traffic';velocity=-1;tick();velocity=0
check(tick().holding,'stock traffic receives the same launch protection')
M.onExtensionUnloaded()
check(not controller.isFrozen and not M.status().bound,'unloading releases its hold and binding')

local H=dofile(root..'autodrive.lua')
beamngEarth_maneuverClearance=M
ai.mode='manual';ai.setMode=function(mode)ai.mode=mode end
ai.driveUsingPath=function()ai.mode='manual'end
mapmgr.mapData={graph={a={b={}},b={a={}}}}
mapmgr.requestMap=function()end
M.bindMap();velocity=-1;tick();velocity=0;tick()
H.hold();tick()
check(M.status().holding and controller.isFrozen,'a streamed-path hold retains the crossing-traffic yield')
ai.mode='disabled';tick()
check(M.status().holding and M.status().suspended,'the stock stop controller becoming disabled does not end an intentional hold')
H.drive({'a','b'},1,true,nil,.3);tick()
check(M.status().holding and ai.mode=='manual','a usable route resumes without discarding its pending yield')
H.stop('disabled');tick()
check(not M.status().holding and not controller.isFrozen,'explicit Autodrive cancellation releases a suspended yield')
ai.mode='manual';velocity=-1;tick();velocity=0;tick();H.hold();tick()
ai.mode='manual';tick()
check(not M.status().holding and not controller.isFrozen,'an external AI mode change releases a suspended hold')
velocity=-1;tick();velocity=0;tick();H.hold();tick();controller.setFreeze(0);tick()
check(not M.status().holding and not M.status().suspended,'an external controller release is respected during a streamed pause too')
M.onReset();M.bindMap();ai.mode='manual';velocity=-1;tick();velocity=0;tick()
check(M.status().holding,'a fresh reversing encounter can acquire the hold')
M.updateGFX(M.MAX_HOLD_S)
check(not controller.isFrozen and not M.status().holding,'the maximum hold hands control back to the stock AI')
tick()
check(not M.status().holding,'the timed-out encounter cannot immediately reacquire the same hold')
print(string.format('manoeuvre clearance: %d checks passed',n))
