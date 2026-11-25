local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local G = dofile(dir .. '/../beamng_earth/lua/ge/extensions/beamngEarth/gps.lua')
local edge = { inNode='a', lanes='-+', inRadius=3.3, outRadius=3.3 }
local nodes = {
  a={pos={x=0,y=0,z=0},radius=3.3,links={b=edge}},
  b={pos={x=100,y=0,z=0},radius=3.3,links={}},
}
local p = {segment={a='a',b='b'},t=.5,dx=100,dy=0}
local function close(actual,expected,message)
  T.check(actual and math.abs(actual-expected)<1e-9,message)
end
close(G.recoveryOffset(nodes,p,1.1,false),1.65,'right-hand recovery body centres in the forward lane')
p.dx=-100
close(G.recoveryOffset(nodes,p,1.1,false),1.65,'opposite heading uses its own right-hand lane')
edge.lanes='+-'
close(G.recoveryOffset(nodes,p,1.1,true),-1.65,'left-hand recovery uses the opposite physical side')
p.dx=100
close(G.recoveryOffset(nodes,p,1.1,true),-1.65,'left-hand lane selection works in both directions')
edge.lanes=nil;edge.inNode='b'
close(G.recoveryOffset(nodes,p,1.1,false),1.65,'missing lane metadata follows right-hand road rules')
close(G.recoveryOffset(nodes,p,1.1,true),-1.65,'missing lane metadata follows left-hand road rules')
edge.inNode='a';edge.lanes='++';edge.oneWay=true
close(math.abs(G.recoveryOffset(nodes,p,1.1,false)),1.65,'one-way recovery selects an actual lane')
p.dx=-100
T.check(G.recoveryOffset(nodes,p,1.1,false)==nil,'a reversed one-way lane string cannot provide a legal recovery lane')
p.dx=100;edge.oneWay=false;edge.lanes='---++';edge.inRadius=8.25;edge.outRadius=8.25
close(G.recoveryOffset(nodes,p,1.1,false),3.3,'asymmetric carriageway uses an inner forward lane, not the centre line')
edge.lanes='-+';edge.inRadius=3.3;edge.outRadius=6.6
close(G.recoveryOffset(nodes,p,1.1,false),2.475,'lane centre follows interpolated edge width')
p.t=.25
close(G.recoveryOffset(nodes,p,1.1,false),2.0625,'taper interpolation uses distance from the first endpoint')
edge.inNode='b'
close(G.recoveryOffset(nodes,p,1.1,false),2.8875,'reversed storage swaps the taper endpoint widths')
edge.inRadius=1.66;edge.outRadius=1.66
close(G.recoveryOffset(nodes,p,1.1,false),.56,'narrow-road recovery keeps the full body within the road')
edge.inRadius=.9;edge.outRadius=.9
T.check(G.recoveryOffset(nodes,p,1.1,false)==nil,'recovery refuses a road narrower than the vehicle body')
T.done('test_gps_recovery_lane')
