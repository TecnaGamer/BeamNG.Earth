local script=arg[0]
local root=(script:match('^(.*[/\\])') or './')..'../beamng_earth/lua/vehicle/extensions/beamngEarth/'
local M=dofile(root..'staticObstacles.lua')
local checks=0
local function check(v,msg)checks=checks+1;assert(v,msg)end
local function near(a,b,msg)check(math.abs(a-b)<1e-6,msg)end
local md={graph={},positions={},radius={}}
for i=1,5 do md.graph[i]={};md.positions[i]={x=0,y=(i-1)*20,z=0};md.radius[i]=6.2 end
for i=1,4 do local e={inNode=i,inRadius=6.2,outRadius=6.2,lanes='--++'};md.graph[i][i+1]=e;md.graph[i+1][i]=e end
local function lanes(a,b)return a<b and '--++' or '--++'end
local c=M.corridor(md,{1,2,3,4,5},1,2,{x=1.55,y=5,z=.2},2,55,lanes)
check(c~=nil,'valid loaded path')
near(c.points[1].along,0,'starts at car projection')
near(c.points[#c.points].along,55,'bounded lookahead')
near(c.lateral,1.55,'right lane offset')
for _,p in ipairs(c.points)do check(M.inside(p,1.55,1.2),'inner lane footprint legal');check(M.inside(p,4.65,1.2),'adjacent forward lane legal');check(not M.inside(p,-1.55,1.2),'oncoming lane excluded')end
check(not M.corridor(md,{1,2,3},1,2,{x=1.55,y=5,z=12},2,55,lanes),'lower road rejected on bridge')
check(not M.corridor(md,{3,4,5},1,2,{x=1.55,y=5,z=.2},2,55,lanes),'disconnected route not guessed')
local advanced=M.corridor(md,{1,2,3,4,5},3,4,{x=1.55,y=45,z=.2},2,20,lanes)
check(advanced and advanced.cursor==4,'consumed requested prefix supported')
local loop=M.corridor(md,{1,2,3,2,1,2,3},1,2,{x=1.55,y=5,z=.2},2,10,lanes,5)
check(loop and loop.cursor==6,'loop cursor preserves later starting-edge visit')
local function plane(x,y,z,dx,dy,dz,length)
 if dy>0 and y<=30 and y+dy*length>=30 and x>1.3 and x<1.8 and z<2 then return (30-y)/dy end
 return length
end
local hit=M.trace(c,nil,1.2,plane)
check(hit~=nil,'thin post hit across footprint')
near(hit.distance,25,'post distance measured from projected front')
check(not M.trace(c,function()return 4.65 end,1.2,plane),'adjacent lane is clear')
local between=M.corridor(md,{1,2,3,4,5},1,2,{x=3.05,y=5,z=.2},2,55,lanes)
check(M.trace(between,nil,1.2,plane)~=nil,'forecast aligns with occupied lane after a between-lanes start')
check(not M.trace(c,nil,1.2,function(_,_,_,_,_,_,l)return l end),'unobstructed road is clear')
near(M.brakeLimit(4,0,false),0,'standstill buffer')
near(M.brakeLimit(30,10,true),4,'passing speed bounded')
check(M.brakeLimit(12,10,false)<M.brakeLimit(30,10,false),'braking reduces with available distance')
near(M.brakeLimit(0/0,10,false),0,'invalid sensing cannot accelerate')
local options=M.adjacentLanes(c)
check(#options==1,'one adjacent lane in forward direction')
near(options[1],4.65,'passing stays in same direction')
local pass,offset,half,join,delta=M.passingCorridor(c,4.65,2,5,25)
check(pass~=nil,'clear wide road permits passing corridor')
near(delta,3.1,'one native lane width displacement')
check(join>=27,'gentle passing transition')
check(not M.trace(pass,offset,half,plane),'joining adjacent lane clears actual obstacle')
local ego={id=10,width=2,length=5,height=1.7}
check(M.trafficClear(pass,offset,ego,{}),'empty traffic confirmed clear')
check(not M.trafficClear(pass,offset,ego,nil),'unknown traffic is not assumed clear')
local function car(x,y,vx,vy,z)return {pos={x=x,y=y,z=z or .8},dirVec={x=0,y=1},vel={x=vx or 0,y=vy or 0},width=2,length=5,height=1.7}end
check(not M.trafficClear(pass,offset,ego,{[11]=car(4.65,30)}),'stopped car blocks candidate lane')
check(not M.trafficClear(pass,offset,ego,{[11]=car(4.65,-70,0,15)}),'fast approaching follower blocks merge')
check(not M.trafficClear(pass,offset,ego,{[11]=car(30,30,-8,0)}),'crossing traffic blocks passing')
check(M.trafficClear(pass,offset,ego,{[11]=car(-4.65,30,0,-10)}),'opposite side separated from passing lane')
check(M.trafficClear(pass,offset,ego,{[11]=car(4.65,30,0,0,12)}),'traffic on separate bridge excluded')
check(M.trafficClear(pass,offset,ego,{[10]=car(4.65,30)}),'ego excluded')
local inactive=car(4.65,30);inactive.active=false
check(not M.trafficClear(pass,offset,ego,{[11]=inactive}),'unseated AI or parked vehicle still blocks passage')
check(not M.passingCorridor(c,-1.55,2,5,25),'oncoming crossing never selected')
local curved=M.corridor(md,{1,2,3,4,5},1,2,{x=1.55,y=5,z=.2},2,55,lanes)
curved.points[4].fx,curved.points[4].fy=1,0
check(not M.passingCorridor(curved,4.65,2,5,25),'sharp turn prevents passing')
local taper=M.corridor(md,{1,2,3,4,5},1,2,{x=1.55,y=5,z=.2},2,55,lanes)
taper.points[4].radius=4
check(not M.passingCorridor(taper,4.65,2,5,25),'width transition prevents blind lane change')
local left=M.corridor(md,{1,2,3,4,5},1,2,{x=-1.55,y=5,z=.2},2,55,function()return '++--'end)
local leftOptions=M.adjacentLanes(left)
check(#leftOptions==1,'left-driving road has one adjacent same-direction lane')
near(leftOptions[1],-4.65,'left-driving pass remains on correct road side')
check(M.passingCorridor(left,-4.65,2,5,25)~=nil,'left-driving same-direction pass supported')
check(not M.passingCorridor(left,1.55,2,5,25),'left-driving oncoming lane excluded')
md.graph[2][9]={};md.graph[9]={[2]={}};md.positions[9]={x=30,y=20,z=0}
local junction=M.corridor(md,{1,2,3,4,5},1,2,{x=1.55,y=5,z=.2},2,55,lanes)
check(not M.passingCorridor(junction,4.65,2,5,25),'passing through a junction is rejected')
-- Native I-405 reproduction: a road widens beyond the original checked
-- finish while the car is already moving into the adjacent forward lane.
do
 local function road(lateral,transition)
  local c={lateral=lateral,radius=9.66,lanes='++++',lane={centre=7.245,width=4.83},points={}}
  for along=0,100,5 do
   local radius=along>=transition and 12.97 or 9.66
   c.points[#c.points+1]={x=0,y=along,z=0,along=along,fx=0,fy=1,rx=1,ry=0,
    lanes='++++',radius=radius,lo=-radius,hi=radius,junction=false}
  end
  return c
 end
 check(M.passingCorridor(road(7.245,60),2.415,2.2,5.49,42)~=nil,
  'entry checks the complete lane change and obstacle clearance before the later widening')
 check(M.passingCorridor(road(6.336,48),2.415,2.2,5.49,29,31.47)~=nil,
  'a committed pass checks its remaining shift instead of restarting the full look-ahead distance')
 check(M.passingCorridor(road(6.336,20),2.415,2.2,5.49,29,31.47)==nil,
  'a width change inside the remaining manoeuvre still rejects the corridor')
 check(M.passingCorridor(road(5.204,28),2.415,2.2,5.49,11.59,13.47)~=nil,
  'later rechecks retain the original finish rather than extending it with each position')
end

print(string.format('static obstacle geometry: %d checks passed',checks))
