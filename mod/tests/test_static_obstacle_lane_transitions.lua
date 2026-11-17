local root=arg[0]:match('^(.*[/\\])')..'../beamng_earth/lua/vehicle/extensions/beamngEarth/'
local M=dofile(root..'staticObstacles.lua')
local n=0
local function check(v,msg)n=n+1;assert(v,msg)end

-- The native AI turns smoothly from the occupied lane into the narrower road.
-- An offset relative to the old lane centre must not pull its forecast inward
-- a second time when the new lane centre changes.
for _,case in ipairs({{1,2.72},{1,2.29},{-1,2.72},{-1,2.29}})do
  local side,lateral=case[1],case[2]
  local md={positions={},radius={},graph={}}
  for i,y in ipairs({0,20,28,45})do
    md.positions[i]={x=0,y=y,z=0};md.radius[i]=i<=2 and 4.95 or 3.3;md.graph[i]={}
  end
  for i=1,3 do
    local r=i==1 and 4.95 or 3.3
    local e={inNode=i,inRadius=r,outRadius=r}
    md.graph[i][i+1]=e;md.graph[i+1][i]=e
  end
  local function lanes(a)
    return a==1 and(side==1 and '--+'or'+--')or(side==1 and '-+'or'+-')
  end
  local c=M.corridor(md,{2,3,4},1,2,{x=side*lateral,y=17.6,z=.2},2.2,25,lanes)
  check(c~=nil,'lane transition has a usable corridor')
  local function obstacle(at)
    return function(x,y,z,dx,dy,dz,length)
      if dy>0 and y<=21.4 and y+dy*length>=21.4 then
        local t=(21.4-y)/dy;local lateral=(x+dx*t)*side
        if not at and lateral<1.05 or at and math.abs(lateral-at)<.09 then return t end
      end
      return length
    end
  end
  local function half(p)return M.probeHalfWidth(p,2.2,c)end
  check(not M.trace(c,nil,half,obstacle()),'median beside the native lane is not an obstacle on its forecast')
  check(M.trace(c,nil,half,obstacle(2.2))~=nil,'an actual obstacle within the lane still blocks driving')
  for _,p in ipairs(c.points)do
    check(math.abs(p.nominal)+1.1<=p.radius+1e-6,'forecast body fits the physical road through the taper')
  end
  check(math.abs(c.points[#c.points].nominal-side*1.65)<1e-6,'forecast settles into the new lane')
end
print(string.format('static obstacle lane transitions: %d checks passed',n))
