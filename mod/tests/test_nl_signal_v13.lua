local dir=arg[0]:match('^(.*)[/\\]')or'.'
local T=dofile(dir..'/testlib.lua');local S=dofile(dir..'/stubs.lua')
function MatrixF()return{setColumn=function()end,setPosition=function()end,setFromEuler=function()end}end
local create=createObject;createObject=function(c)local o=create(c);o.setTransform=function()end;return o end
local M=require('beamngEarth/nl_signal_mounts');local P=require('beamngEarth/nl_signals')
local scene=require('beamngEarth/nl_signal_scene');local share=require('beamngEarth/nl_signal_shared')
local surfaces=require('beamngEarth/nl_signal_surfaces');local space=require('beamngEarth/nl_signal_space')
local frame={worldToGeodetic=function()return 51.36351,6.12339,0 end}
local function physical(name,y,n,portal)
  local hs={};for i=1,n do hs[i]={id='lane_'..i,lane_id=name..i,variant='vehicle_300',distance_m=i*3}end
  local layout=M.plan(hs,{kind=portal and'portal'or n==1 and'straight'or'cantilever',include_left=portal or false,arm_length_m=n==1 and 0 or n*3,left_span_m=portal and 18 or nil})
  assert(layout.ready)
  return{x=0,y=y,z=10,yaw=0,model='groenpol_vehicle',shape=M.catalog.heads.vehicle_300.shape,stop={-1,0,10},dir={-1,0},
    layout=layout,parts=layout.parts,signature=M.signature(layout),lane_plan={synchronized=true,count=n,lens_diameter_mm=300},source_foot={0,y,10},placement={feet={{0,y,10}}}}
end
local function inst(name)return{name=name,createSignalObject=function(_,shape)local o=createObject('TSStatic');o:setField('shapeName',0,shape);o:registerObject('');return o end}end
local function part(h,key)for _,entry in ipairs(h.parts)do local p=entry.localPose or entry;if p.key==key then return p,entry end end end
local function nearSocket(h,headKey,owner,footKey)
  local head=M.worldPose(h,assert(part(h,headKey)));local fp=assert(part(owner,footKey));local foot=M.worldPose(owner,fp);local s=M.catalog.low_repeat.position;local b=foot.basis
  if fp.shape==M.catalog.double_mount.hardware_shape then
    local best=math.huge
    for _,candidate in ipairs(M.catalog.double_mount.sockets)do
      local x,y=foot.x+b[1]*candidate[1]+b[2]*candidate[2],foot.y+b[4]*candidate[1]+b[5]*candidate[2]
      local d=(x-head.x)^2+(y-head.y)^2;if d<best then best=d;s=candidate end
    end
  end
  T.near(head.x,foot.x+b[1]*s[1]+b[2]*s[2]+b[3]*s[3],1e-7,'head X stays on the authored foot socket')
  T.near(head.y,foot.y+b[4]*s[1]+b[5]*s[2]+b[6]*s[3],1e-7,'head Y stays on the authored foot socket')
  T.near(head.z,foot.z+b[7]*s[1]+b[8]*s[2]+b[9]*s[3],1e-7,'head Z stays on the authored foot socket')
end
T.check(M.catalog.low_repeat.source_object=='Cylinder.052','actual loose source mounting foot is selected')
local tilt=math.rad(15);local tilted={position={0,0,0},basis={math.cos(tilt),0,math.sin(tilt),0,1,0,-math.sin(tilt),0,math.cos(tilt)}}
M.aimAt(tilted,{x=0,y=0,z=0,yaw=0},{10,20})
T.near(tilted.basis[3],math.sin(tilt),1e-10,'head swivel preserves authored socket axis X')
T.near(tilted.basis[6],0,1e-10,'head swivel preserves authored socket axis Y')
T.near(tilted.basis[9],math.cos(tilt),1e-10,'head swivel preserves authored socket axis Z')
local first=physical('first',0,3);local one=assert(scene.create(first,inst('first')))
local second=physical('second',.8,3);second.yaw=90;second.dir={0,-1}
local plan=assert(share.candidates(second,{first=one},'second')[1]);plan=share.withoutShields(plan,{first=one})
T.check(not space.conflict(plan,{first=one},'second'),'real feet and straight arms fit before creation')
local two=assert(scene.create(plan,inst('second'),{first=one}))
local a,b=M.worldPose(two,part(two,'shared_mount_current')),M.worldPose(one,part(one,'low_bracket_right'))
local angle=math.abs(math.deg(math.atan2(a.basis[4],a.basis[1])-math.atan2(b.basis[4],b.basis[1])))%360
angle=math.min(angle,360-angle)
if plan.mount_pair.kind=='authored_double_bracket'then
  local d=M.catalog.double_mount;local p,q=d.sockets[1],d.sockets[2];local c=d.bar_centre
  local dot=(p[1]-c[1])*(q[1]-c[1])+(p[2]-c[2])*(q[2]-c[2])
  local length=math.sqrt(((p[1]-c[1])^2+(p[2]-c[2])^2)*((q[1]-c[1])^2+(q[2]-c[2])^2))
  T.check(dot/length<-.9999999,'authored double-foot sockets are physically opposed by 180 degrees')
else T.check(math.abs(angle-90)<1e-7 or math.abs(angle-180)<1e-7,'feet are exactly 90 or 180 degrees apart')end
nearSocket(two,'head_right_repeat',two,'shared_mount_current')
nearSocket(one,'head_right_repeat',plan.mount_pair.kind=='authored_double_bracket'and two or one,plan.mount_pair.kind=='authored_double_bracket'and'shared_mount_current'or'low_bracket_right')
T.check(not part(two,'shared_double_fork')and not part(two,'shared_low_crossbar'),'no invented kink or low crossbar remains')
local beam,support=part(two,'beam'),part(two,'support_right')
T.near(beam.position[1],support.position[1],1e-9,'straight beam starts at shared post X')
T.near(beam.position[2],support.position[2],1e-9,'straight beam starts at shared post Y')
T.near(beam.scale[3],1,1e-9,'no raised or stretched beam offset')
for _,p in ipairs(two.parts)do local q=p.localPose
  if q.role=='head'and q.position[3]>4 then
    local old=part(second,q.key);local oldBeam=part(second,'beam')
    local oldSide=(old.position[1]-oldBeam.position[1])*oldBeam.basis[1]+(old.position[2]-oldBeam.position[2])*oldBeam.basis[4]
    local newSide=(q.position[1]-beam.position[1])*beam.basis[1]+(q.position[2]-beam.position[2])*beam.basis[4]
    T.near(newSide,oldSide,1e-8,'overhead head follows its straight beam with original socket offset')
  end
end
T.check(#share.candidates(physical('third',.4,1),{first=one,second=two},'third')==0,'a third owner cannot destabilize an existing double mast')
local graph={nodes={a={60,-20,10,6.6},j={-20,-20,10,6.6}},edges={{'a','j',0,3,1,13,1,6.6,6.6}}}
local graph2={nodes={a={20,60,10,6.6},j={20,-20,10,6.6}},edges={{'a','j',0,3,1,13,1,6.6,6.6}}}
local aa={'a',-1,0,10,-1,0,0,0,0,10,0,{zone='bubeko'}}
local bb={'a',-1,0,10,0,-1,0,0,.8,10,0,{zone='bubeko'}}
local begin,planner=surfaces.begin,P.plan
surfaces.begin=function()return function()return false,nil,'pending'end end
P.plan=function()error('accepted geometry was replanned during pending terrain')end
local id1,id2=one.id,two.id;local postId=select(2,part(one,'support_right')).id
for i=1,100 do
  aa[8],aa[9],aa[10]=.4*math.sin(i),.3*math.cos(i),10+.2*math.sin(i)
  bb[8],bb[9],bb[10]=.4*math.cos(i),.8+.3*math.sin(i),10+.2*math.cos(i)
  assert(P.place(aa,frame,'stock',true,graph,'j',{first=one,second=two},'first')==one.saved_plan)
  assert(P.place(bb,frame,'stock',true,graph2,'j',{first=one,second=two},'second')==two.saved_plan)
end
T.check(one.id==id1 and two.id==id2 and scenetree.findObjectById(postId),'100 alternating refreshes retain every existing head and the shared post')
T.check(one.saved_plan.placement.surface_pending and two.saved_plan.placement.surface_pending,'incomplete terrain is deferred rather than moving a valid double mast')
P.plan=planner
surfaces.begin=function()return function()return true,10.12,'walk'end end
P.place(aa,frame,'stock',true,graph,'j',{first=one,second=two},'first')
T.near(M.worldPose(one,part(one,'support_right')).z,10.12,1e-8,'first shared foot follows newly verified ground')
T.near(M.worldPose(two,part(two,'support_right')).z,10.12,1e-8,'second shared owner follows the same ground atomically')
nearSocket(two,'head_right_repeat',two,'shared_mount_current');nearSocket(one,'head_right_repeat',plan.mount_pair.kind=='authored_double_bracket'and two or one,plan.mount_pair.kind=='authored_double_bracket'and'shared_mount_current'or'low_bracket_right')
scene.delete(two)
T.near(part(one,'head_right_repeat').position[3],M.catalog.low_repeat.position[3]+.12,1e-8,'restoring a remaining head preserves the corrected ground level')
scene.delete(one);T.check(#S.liveOf('TSStatic')==0,'all real mounting feet and shared objects are released')
surfaces.begin=begin
local original=physical('first',0,3);one=assert(scene.create(original,inst('first')))
local attached=physical('second',.8,3);attached.yaw=90;attached.dir={0,-1}
local attachedPlan=assert(share.candidates(attached,{first=one},'second')[1]);attachedPlan=share.withoutShields(attachedPlan,{first=one})
two=assert(scene.create(attachedPlan,inst('second'),{first=one}))
local persistentPost=select(2,part(two,'support_right')).id;local persistentHead=two.id
scene.delete(one)
surfaces.begin=function()return function()return false,nil,'pending'end end
P.plan=function()error('remaining owner moved after partner unloaded')end
local held=P.place(bb,frame,'stock',true,graph2,'j',{second=two},'second')
T.check(held==two.saved_plan and scene.alive(two),'remaining owner keeps its geometry when partner controller unloads')
T.check(scenetree.findObjectById(persistentPost)and two.id==persistentHead,'shared post and remaining native head retain their identities')
P.plan=planner;surfaces.begin=begin
local returned=physical('first',0,3)
local returnPlan=assert(share.candidates(returned,{second=two},'first')[1])
one=assert(scene.create(returnPlan,inst('first'),{second=two}))
T.check(select(2,part(one,'support_right')).id==persistentPost and two.id==persistentHead,'returning partner reuses the exact existing foot without shifting the survivor')
scene.delete(one);scene.delete(two)
T.check(#S.liveOf('TSStatic')==0,'partner unload and reload leave no stray feet or shields')
local partial=physical('partial',0,1);partial.lane_plan.count=3
local incomplete=assert(scene.create(partial,inst('partial')))
local expanded=P.place(aa,frame,'stock',true,graph,'j',{partial=incomplete},'partial')
T.check(expanded~=incomplete.saved_plan and expanded.layout and expanded.layout.head_count==3,'incomplete fallback remains eligible to grow to the full lane count')
scene.delete(incomplete)
local portal=physical('portal',0,5,true);local h=assert(scene.create(portal,inst('portal')))
local gp={nodes={a={60,-20,10,9.9},j={-20,-20,10,9.9}},edges={{'a','j',0,5,1,13,1,9.9,9.9}}}
local ap={'a',-1,0,10,-1,0,0,0,0,10,0,{zone='bubeko'}}
surfaces.begin=function()return function(x,y)return true,y<-10 and 10.3 or 10.1,'walk'end end
P.place(ap,frame,'stock',true,gp,'j',{portal=h},'portal')
local right,left=part(h,'support_right'),part(h,'support_left')
T.near(M.worldPose(h,right).z,10.1,1e-8,'portal right foot follows its own surface')
T.near(M.worldPose(h,left).z,10.3,1e-8,'portal left foot follows its own surface')
local height=M.catalog.supports[portal.layout.support].construction.leg_bounds_m[2][3]
T.near(right.position[3]+height*(right.scale and right.scale[3]or 1),left.position[3]+height*left.scale[3],1e-8,'independent portal feet retain one straight top beam')
scene.delete(h);surfaces.begin=begin;T.check(#S.liveOf('TSStatic')==0,'portal refresh leaves no orphan parts')
T.done('test_nl_signal_v13')
