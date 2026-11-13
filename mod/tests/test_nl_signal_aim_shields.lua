local dir=arg[0]:match('^(.*)[/\\]')or'.'
local T=dofile(dir..'/testlib.lua');local S=dofile(dir..'/stubs.lua')
function MatrixF()return{setColumn=function()end,setPosition=function()end,setFromEuler=function()end}end
local create=createObject;createObject=function(class)local o=create(class);o.setTransform=function()end;return o end
local M=require('beamngEarth/nl_signal_mounts');local shared=require('beamngEarth/nl_signal_shared')
local space=require('beamngEarth/nl_signal_space');local scene=require('beamngEarth/nl_signal_scene')
local function physical(y,n,yaw)
  local hs={};for i=1,n do hs[i]={id='lane_'..i,lane_id='lane_'..i,variant='vehicle_300',distance_m=i*3}end
  local layout=M.plan(hs,{kind=n==1 and'straight'or'cantilever',include_left=false,arm_length_m=n==1 and 0 or n*3})
  return{x=0,y=y,z=10,yaw=yaw or 0,model='groenpol_vehicle',stop={0,0,10},dir={-math.cos(math.rad(yaw or 0)),-math.sin(math.rad(yaw or 0))},
    layout=layout,parts=layout.parts,lane_plan={synchronized=true,count=n},placement={feet={{0,y,10}}}}
end
local function part(h,key)for _,e in ipairs(h.parts)do local p=e.localPose or e;if p.key==key then return p,e end end end
local function inst(name)return{name=name,createSignalObject=function(_,shape)local o=createObject('TSStatic');o:setField('shapeName',0,shape);o:registerObject('');return o end}end
local function bearing(h,p)local q=M.worldPose(h,p);return math.deg(math.atan2(q.basis[4],q.basis[1]))end
local function delta(a,b)return math.deg(math.atan2(math.sin(math.rad(a-b)),math.cos(math.rad(a-b))))end
-- A remote left repeater formerly swivelled about 55 degrees across the road.
local root={x=0,y=0,z=10,yaw=0};local heads={{id='a',lane_id='a',variant='vehicle_300',distance_m=3},
  {id='b',lane_id='b',variant='vehicle_300',distance_m=6},{id='c',lane_id='c',variant='vehicle_300',distance_m=9}}
local layout=M.plan(heads,{kind='portal',left_span_m=32,include_left=true,arm_length_m=9})
local before={};for _,p in ipairs(layout.parts)do before[p.key]={unpack(p.position)}end
M.aimHeads(layout,root,{'a',0,0,10,-1,0,0,0,0,10,0},{heads=heads,incoming_node={60,0,10}})
for _,p in ipairs(layout.parts)do
  if p.role=='head'and(p.repeat_head or p.position[3]<4)then T.check(math.abs(bearing(root,p))<=10.00001,'every low repeater remains within ten degrees of its road axis')end
  for i=1,3 do T.near(p.position[i],before[p.key][i],1e-10,'aiming preserves socket and steel positions')end
end
local left=assert(part(layout,'head_left'));T.near(math.abs(bearing(root,left)),10,1e-5,'wide portal uses bounded aim instead of the sharp cross-road bearing')
-- A distorted lane target must never swivel an upper head into its own boom.
-- Repeat for the left-side steel: reflection does not reverse the front faces.
for _,mirror in ipairs({false,true})do
  local hs={{id='a',lane_id='a',variant='vehicle_300',distance_m=3,original_distance_m=-40}}
  local arm=M.plan(hs,{kind='cantilever',include_left=false,arm_length_m=3})
  if mirror then M.mirrorLeft(arm)end
  local steel=assert(part(arm,'beam'));local steelPosition={unpack(steel.position)};local steelBasis={unpack(steel.basis)}
  M.aimHeads(arm,root,{'a',0,0,10,-1,0,0,0,0,10,0},{heads=hs,incoming_node={60,0,10}})
  local head=assert(part(arm,'head_a'));local pose=M.worldPose(root,head)
  T.check(math.abs(bearing(root,head))<=15.1,'upper lantern follows incoming road instead of the far lateral lane target')
  T.check(pose.basis[1]>.95,'upper lantern front never points along or into its boom')
  local minimum=math.huge;local b=M.catalog.heads.vehicle_300.bare_bounds_m
  for _,x in ipairs({b[1][1],b[2][1]})do for _,y in ipairs({b[1][2],b[2][2]})do for _,z in ipairs({b[1][3],b[2][3]})do
    minimum=math.min(minimum,pose.x+pose.basis[1]*x+pose.basis[2]*y+pose.basis[3]*z)
  end end end
  T.check(minimum>.085,'complete upper housing stays in front of the original 150mm boom')
  for i=1,3 do T.near(steel.position[i],steelPosition[i],1e-10,'upper correction does not move the steel')end
  for i=1,9 do T.near(steel.basis[i],steelBasis[i],1e-10,'upper correction does not turn the steel')end
end
-- Thin backboards may overlap in projection while their actual depth is free.
local shape=M.catalog.heads.vehicle_300.shape
local function lone(x,shape)return{x=x,y=0,z=10,yaw=0,model='groenpol_vehicle',parts={{role='head',key='lamp',shape=shape,position={0,0,0},basis={1,0,0,0,1,0,0,0,1}}}}end
T.check(not space.conflict(lone(0,shape),{other=lone(.6,shape)},'own'),'depth-separated full backboards are retained despite overlapping old capsule bounds')
T.check(space.conflict(lone(0,shape),{other=lone(.2,shape)},'own')~=nil,'real body overlap remains forbidden')
-- Opposed low lanterns can each retain their individual source backboard.
local first=assert(scene.create(physical(0,1),inst('first')))
local opposite=assert(shared.candidates(physical(.8,1,180),{first=first},'second')[1])
T.check(part(opposite,'head_lane_1').shape==shape and opposite.foreign_changes[1].shape==shape,'opposed heads keep both individually fitting shields')
T.check(not space.conflict(opposite,{first=first},'second'),'individually shielded pair has actual geometric clearance')
scene.delete(first)
-- Perpendicular approaches share authored hardware without a 20/30 degree
-- steering workaround. Their shields may conflict with each other, but one fits.
first=assert(scene.create(physical(0,3),inst('first')))
local pair=assert(shared.candidates(physical(.8,3,90),{first=first},'second')[1])
local current=part(pair,'head_right_repeat');local change=pair.foreign_changes[1]
T.check(math.abs(delta(bearing(pair,current),90))<=10.00001,'combined current lantern keeps its own approach bearing')
local otherAngle=math.deg(math.atan2(change.basis[4],change.basis[1]))+first.yaw
T.check(math.abs(delta(otherAngle,0))<=10.00001,'combined existing lantern keeps its own approach bearing')
local shields=(current.shape==shape and 1 or 0)+(change.shape==shape and 1 or 0)
T.check(shields>=1,'perpendicular low heads retain an individual shield wherever it fits')
T.check(not space.conflict(pair,{first=first},'second'),'bounded perpendicular pair physically fits')
for i=1,3 do T.check(part(pair,'head_lane_'..i).shape==shape,'unrelated overhead lantern keeps its source shield')end
local combined=assert(scene.create(pair,inst('second'),{first=first}))
T.check(not space.conflict(first.saved_plan,{second=combined},'first'),'head and foot sharing do not overwrite the shared beam identity')
scene.delete(combined);scene.delete(first)
-- Block only one low shield corner. Removing that shield must leave all lane
-- lanterns, and the neighbouring controller's other shields, unchanged.
first=assert(scene.create(physical(0,3),inst('first')))
pair=assert(shared.candidates(physical(.8,3,90),{first=first},'second')[1])
local blocker=lone(.45,M.catalog.heads.vehicle_300.bare_shape);blocker.y=.53;blocker.z=10+M.catalog.low_repeat.position[3]
local name,role,key=space.conflict(pair,{first=first,blocker=blocker},'second')
T.check(name=='blocker'and key=='head_right_repeat','fixture obstructs only the current low backboard')
pair=shared.withoutShields(pair,{first=first,blocker=blocker},'second')
T.check(not space.conflict(pair,{first=first,blocker=blocker},'second'),'local bare fallback clears the actual obstruction')
T.check(part(pair,'head_right_repeat').shape==M.catalog.heads.vehicle_300.bare_shape,'only obstructed low board is removed')
for i=1,3 do
  T.check(part(pair,'head_lane_'..i).shape==shape,'low-board fallback preserves own overhead shield')
  T.check(part(first,'head_lane_'..i).shape==shape,'low-board fallback preserves other controller overhead shield')
end
for _,c in ipairs(pair.foreign_changes)do T.check(c.owner~='blocker','fallback does not strip the unrelated neighbour')end
scene.delete(first)
T.check(#S.liveOf('TSStatic')==0,'all test assemblies clean up')
T.done('test_nl_signal_aim_shields')
