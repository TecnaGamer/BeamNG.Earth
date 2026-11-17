-- Physical road height can differ from the interpolated navigation graph.
local root=arg[0]:match('^(.*[/\\])')..'../beamng_earth/lua/vehicle/extensions/beamngEarth/'
local S=dofile(root..'staticObstacles.lua')
local n=0
local function check(v,msg)n=n+1;assert(v,msg)end
local function corridor(length,z)
  local c={points={}}
  for y=0,length,4 do c.points[#c.points+1]={x=0,y=y,z=z(y),rx=1,ry=0,along=y,nominal=0}end
  return c
end
local function ray(ground,obstacle)
  return function(x,y,z,dx,dy,dz,len)
    local steps=math.max(1,math.ceil(len/.02))
    for j=0,steps do
      local t=len*j/steps
      local qx,qy,qz=x+dx*t,y+dy*t,z+dz*t
      if qz<ground(qx,qy) or (obstacle and obstacle(qx,qy,qz))then return t end
    end
    return len
  end
end
local function surface(ground,initial)
  return {initial=initial or ground(0,0),height=function(x,y)return ground(x,y)end}
end
-- Measured bridge deck/graph heights from the Innsbruck stopped-pose capture.
local heights={579.019836,579.044434,578.945251,578.829651,578.609009,578.388489,578.168457,577.948242}
local function deck(_,y)
  local f=math.min(#heights-1,math.max(0,y/3.96642649))
  local i=math.min(#heights-1,math.floor(f)+1)
  return heights[i]+(heights[i+1]-heights[i])*(f-(i-1))
end
local c=corridor(24,function(y)return 578.747313-y*.03901025848 end)
check(S.trace(c,nil,1.2,ray(deck))~=nil,'old graph-height probe intersects the bridge deck')
check(not S.trace(c,nil,1.2,ray(deck),surface(deck)),'continuous physical deck does not become an obstacle')
check(c.points[1].z==578.747313,'surface adjustment never mutates the graph corridor')
local function post(x,y,z)return math.abs(x)<.1 and y>=12 and y<=12.2 and z<deck(x,y)+2 end
local hit=S.trace(c,nil,1.2,ray(deck,post),surface(deck))
check(hit and math.abs(hit.distance-12)<.1,'thin real post on the bridge remains detected')
check(not S.trace(c,function()return 3.2 end,1.2,ray(deck,post),surface(deck)),
  'adjacent clear passing path uses the same physical deck reference')
for _,grade in ipairs({-.6,-.3,0,.3})do
  local function road(_,y)return 10+grade*y end
  local straight=corridor(40,function(y)return 9.4+grade*y end)
  local casts=0
  check(not S.trace(straight,nil,1.2,function(...)casts=casts+1;return ray(road)(...)end,surface(road)),
    'measured road supports linear grades in either direction')
  check(casts==10*2*(math.ceil(2.4/S.RAY_STEP_M)+1),'linear surface sampling does not multiply width raycasts')
end
local flat=corridor(24,function()return 0 end)
local function ground()return 0 end
-- A narrow concrete base can hit the body without changing the three sampled
-- support tracks. The Chicago collision was below the original 0.45m probe.
for _,side in ipairs({-1,1})do
  for _,height in ipairs({.3,.4})do
    local function base(x,y,z)return math.abs(x-side*.6)<.14 and y>=12 and y<=12.3 and z<height end
    local obstacle=S.trace(flat,nil,1.2,ray(ground,base),surface(ground))
    check(obstacle and math.abs(obstacle.distance-12)<.1,'low concrete base is detected on either side of the footprint')
    check(not S.trace(flat,function()return side*3.2 end,1.2,ray(ground,base),surface(ground)),
      'a low base outside the driven footprint does not block a clear passing lane')
  end
end
for _,height in ipairs({.04,.1,.15})do
  local function bump(_,y,z)return y>=12 and y<=12.3 and z<height end
  check(not S.trace(flat,nil,1.2,ray(ground,bump),surface(ground)),
    'small road features below the clearance probe do not add an obstacle hold')
end
for _,height in ipairs({.5,1,2})do
  local function block(x,y,z)return y>=10 and y<=18 and math.abs(x)<=2 and z<height end
  -- Even a surface API returning the block top above its query ceiling cannot
  -- lift the floor reference onto an obstacle and make it disappear.
  local function top(x,y)return block(x,y,0)and height or 0 end
  local obstruction=S.trace(flat,nil,1.2,ray(ground,block),surface(top))
  check(obstruction and obstruction.distance<=10.1,'wide raised block remains an obstruction')
end
local function missing(_,y)return y>=12 and -1e20 or 0 end
local gap=S.trace(flat,nil,1.2,ray(ground),surface(missing))
check(gap and gap.kind=='surface gap' and gap.distance<12,'unloaded support fails closed before the gap')
local earlier=S.trace(flat,nil,1.2,ray(ground,function(_,y,z)return y>=8 and z<2 end),surface(missing))
check(earlier and earlier.distance<8.1,'real earlier obstruction wins over a later support gap')
local invalid=S.trace(flat,nil,1.2,ray(ground),{initial=0/0,height=ground})
check(invalid and invalid.distance==0,'invalid initial floor cannot cast below the world')
local below=S.trace(flat,nil,1.2,ray(ground),surface(ground,-1e20))
check(below and below.distance==0,'missing initial surface cannot count as clear road')
local function crest(_,y)return .25*math.sin(y/4)end
check(not S.trace(flat,nil,1.2,ray(crest),surface(crest)),'gentle physical curvature inserts support casts as needed')
local function bank(x,y)return x*.5*math.min(1,y/10)end
check(not S.trace(flat,nil,1.2,ray(bank),surface(bank)),
  'physical bank across the footprint does not look like a wall to the low side rays')
local bankPost=S.trace(flat,nil,1.2,ray(bank,function(x,y,z)
  return math.abs(x-1)<.1 and y>=12 and y<=12.2 and z<bank(x,y)+2 end),surface(bank))
check(bankPost and bankPost.distance<12.1,'real obstacle on banked side remains detectable')
-- A car stopped halfway through a banked ramp already has unequal floor
-- heights across its footprint. Starting all tracks at the centre fails here.
for _,crossfall in ipairs({-.5,.5})do
  for _,grade in ipairs({-.2,0,.2})do
    local function road(x,y)return crossfall*x+grade*y end
    local support=surface(road)
    support.origin={x=0,y=0};support.up={x=-crossfall,y=-grade,z=1}
    local ramp=corridor(24,function(y)return grade*y end)
    check(not S.trace(ramp,nil,1.2,ray(road),support),'already banked footprint starts on its supported floor')
    local post=S.trace(ramp,nil,1.2,ray(road,function(x,y,z)
      return math.abs(x-.6)<.1 and y>=12 and y<=12.2 and z<road(x,y)+.4 end),support)
    check(post and post.distance<12.1,'low obstacle remains detected on an already banked road')
    local broken={initial=0,origin=support.origin,up=support.up,height=function(x,y)
      return y>=12 and -1e20 or road(x,y)end}
    local gap=S.trace(ramp,nil,1.2,ray(road),broken)
    check(gap and gap.kind=='surface gap' and gap.distance<12,'banked start retains missing-ground detection')
  end
end
local step=S.trace(flat,nil,1.2,ray(ground),{initial=0,origin={x=0,y=0},up={x=0,y=0,z=1},
  height=function(x,y)return x>1 and .5 or 0 end})
check(step and step.distance==0,'a level car still rejects an unexpected raised edge at its starting footprint')
print(string.format('static obstacle surface: %d checks passed',n))
