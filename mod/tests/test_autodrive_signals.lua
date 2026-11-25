local root=(arg[0]:match('^(.*[/\\])') or './')..'../beamng_earth/lua/vehicle/extensions/beamngEarth/'
local M=dofile(root..'trafficSignals.lua')
local n=0
local function check(v,msg)n=n+1;assert(v,msg)end
local function near(a,b,msg)check(math.abs(a-b)<1e-6,msg)end
local md={positions={a={x=0,y=0,z=0},b={x=0,y=50,z=0},c={x=-50,y=50,z=0}},graph={a={b={}},b={c={}},c={}}}
local c={radius=4,points={{x=0,y=10,z=0,along=0,from='a',to='b'},{x=0,y=50,z=0,along=40,from='a',to='b'},
  {x=-10,y=50,z=0,along=50,from='b',to='c'}}}
local light={action=2,state='redTrafficLight',instance='light',pos={x=2,y=40,z=0}}
local signals={a={b={light}}}
md.radius={a=4,b=4,c=4}
local direct=M.forPath(md,{'a','b','c'},signals,'a','b',{x=0,y=10,z=0},{x=0,y=1},6)
check(direct and direct.cap>0,'signals can use the graph path without an obstacle corridor')
check(not M.forPath(md,{'c'},signals,'a','b',{x=0,y=10,z=0},{x=0,y=1},6), 'a nearby road outside the selected path has no signal owner')
check(not M.forPath(md,{'a','b','c'},signals,'a','b',{x=0,y=10,z=8},{x=0,y=1},6), 'an elevated vehicle cannot inherit a ground signal')
local r=M.limit(c,md,signals,6)
near(r.distance,30,'roadside head projects onto the selected road')
check(r.reason=='red light'and r.cap>0,'red signal supplies a finite approach ceiling')
light.pos.y=14;r=M.limit(c,md,signals,6)
near(r.cap,0,'native controller must stop before the line')
light.action=0;check(not M.limit(c,md,signals,0),'green removes the constraint')
light.action=1;light.pos.y=40
check(M.limit(c,md,signals,6),'yellow stops when braking room remains')
check(not M.limit(c,md,signals,20),'close yellow does not demand an impossible stop')
light.action=2;light.pos.y=8
check(not M.limit(c,md,signals,6),'a passed signal does not stop the car inside the junction')
light.pos.y=40;light.pos.z=-8
check(not M.limit(c,md,signals,6),'lower road signals do not stop a bridge route')
light.pos.z=0;light.pos.x=20
check(not M.limit(c,md,signals,6),'distant parallel signal is excluded')
light.pos.x=0
check(not M.limit(c,md,{b={a={light}}},6),'opposite approach does not control this path')
local edge=md.graph.a.b;md.graph.a.b=nil
check(not M.limit(c,md,signals,6),'retained signal data cannot restore a missing road')
md.graph.a.b=edge;edge.oneWay=true;edge.inNode='b'
check(not M.limit(c,md,signals,6),'wrong-way signal edge is not driven')
edge.oneWay=nil
signals={b={c={{action=2,pos={x=-20,y=50,z=0}}}}}
r=M.limit(c,md,signals,6);near(r.distance,60,'distance follows the bend instead of cutting across it')
check(not M.limit(c,md,nil,6),'unavailable signals leave ordinary driving alone')
check(not M.limit(c,md,signals,0/0),'invalid speed cannot create a speed command')
print(string.format('Autodrive traffic signals: %d checks passed',n))
