local root=arg[0]:match('^(.*[/\\])')..'../beamng_earth/lua/vehicle/extensions/beamngEarth/'
local S=dofile(root..'staticObstacles.lua')
local M=dofile(root..'oncomingPass.lua')
local checks=0
local function check(v,msg)checks=checks+1;assert(v,msg)end
local md={graph={},positions={},radius={}}
local ids={}
for i=1,51 do md.graph[i]={};md.positions[i]={x=0,y=(i-1)*20,z=0};md.radius[i]=6.2;ids[i]=i end
for i=1,50 do local e={inNode=i,inRadius=6.2,outRadius=6.2};md.graph[i][i+1]=e;md.graph[i+1][i]=e end
local function lanes()return '--++'end
local function corridor()return S.corridor(md,ids,1,2,{x=1.55,y=5,z=.2},2,900,lanes)end
local function clear(_,_,z,_,_,dz,d)return dz<0 and math.min(d,z/-dz) or d end
local ego={id=10,width=2,length=5,height=1.7}
local function plan(c,objects,cast)return M.plan(S,c or corridor(),{distance=42},ego,objects or {},cast or clear,14)end
local p=plan()
check(p~=nil,'straight visible empty opposing road permits a complete pass')
check(p.target<0 and p.original>0,'pass enters opposite side and returns to original side')
check(p.sight>p.finish+14*p.horizon,'observation covers oncoming closing distance plus the manoeuvre')
local last=p.pass.points[#p.pass.points]
check(p.offset(last)>0,'complete return is part of checked geometry')
local short=corridor();while short.points[#short.points].along>120 do table.remove(short.points)end
check(not plan(short),'nearby empty lane is insufficient when distant road is unloaded')
local _,_,waitGap=plan(short)
check(waitGap and waitGap>25,'waiting for visibility preserves enough room to enter the opposite side later')
local bend=corridor();bend.points[30].fx,bend.points[30].fy=.5,.866
check(not plan(bend),'blind bend beyond the obstacle rejects the pass')
local junction=corridor();junction.points[10].junction=true
check(not plan(junction),'junction in the observation area rejects the pass')
local crest=corridor();crest.points[30].z=12
check(not plan(crest),'crest cannot count as clear visibility')
local taper=corridor();taper.points[30].radius=5
check(not plan(taper),'lane-width transition rejects the pass')
local function car(x,y,vy,z)return {pos={x=x,y=y,z=z or .8},dirVec={x=0,y=vy and vy<0 and -1 or 1},vel={x=0,y=vy or 0},width=2,length=5,height=1.7,active=false}end
check(not plan(nil,{[11]=car(-1.55,400,-14)}),'distant oncoming AI car arriving during pass blocks entry')
check(not plan(nil,{[11]=car(1.55,74,0)}),'parked car in return lane blocks entry')
check(plan(nil,{[11]=car(4.65,47,0)})~=nil,'car blocking other forward lane does not preclude clear opposite-side pass')
check(plan(nil,{[11]=car(-1.55,400,-14,14)})~=nil,'separate bridge traffic does not block passage')
check(not M.plan(S,corridor(),{distance=42},ego,nil,clear,14),'unknown traffic blocks passage')
check(not M.plan(S,corridor(),{distance=10},ego,{},clear,14),'too-close obstacle leaves braking and recovery available')
check(not plan(nil,nil,function(x,y,z,dx,dy,dz,d)
 if dy>0 and y<=35 and y+dy*d>=35 and x<0 and z<2 then return (35-y)/dy end
 return d
end),'solid median or obstruction in candidate path blocks passage')
local left=S.corridor(md,ids,1,2,{x=-1.55,y=5,z=.2},2,900,function()return '++--'end)
local lp=plan(left)
check(lp and lp.target>0 and lp.original<0,'left-driving road mirrors out-and-back decision')
local oneway=S.corridor(md,ids,1,2,{x=1.55,y=5,z=.2},2,900,function()return '++++'end)
check(not plan(oneway),'one-way road has no opposing-lane exception')
check(not plan(nil,nil,function(_,_,_,_,_,_,d)return d end),'graph without road collision cannot count as loaded visibility')
local farJunction=corridor();farJunction.points[45].junction=true
check(plan(farJunction)~=nil,'visible junction beyond the completed return does not itself prevent a pass')
local waiting=car(-1.55,400,0);waiting.dirVec.y=-1
check(not plan(nil,{[11]=waiting}),'stopped oncoming car is not assumed to remain stopped')
local turning=car(150,200,0);turning.dirVec.x=-1;turning.dirVec.y=0
check(not plan(nil,{[11]=turning}),'approaching side-road car may turn toward the manoeuvre')
local narrow={graph={},positions=md.positions,radius={}}
for i=1,51 do narrow.graph[i]={};narrow.radius[i]=3.3 end
for i=1,50 do local e={inNode=i,inRadius=3.3,outRadius=3.3};narrow.graph[i][i+1]=e;narrow.graph[i+1][i]=e end
local starting=S.corridor(narrow,ids,1,2,{x=1.63,y=5,z=.2},2,900,function()return '-+'end)
check(M.plan(S,starting,{distance=29},ego,{},clear,14,true)~=nil,
 'handoff before lateral motion preserves a gentle join instead of demanding a sharper turn')
print(string.format('oncoming pass geometry: %d checks passed',checks))
