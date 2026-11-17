-- Waypoint driving does not run the stock traffic mode's junction actions.
-- Read its public signal data and return a braking ceiling for the bound path.
local M={STOP_M=3,DECEL=3,REACTION_S=.25}
local function finite(x)return type(x)=='number'and x==x and math.abs(x)<math.huge end
local function position(p)return p and finite(p.x)and finite(p.y)and finite(p.z)end

-- Signals need the selected graph path, not obstacle raycasts or a physical passing corridor.
function M.forPath(md,ids,signals,a,b,p,heading,speed,cursor)
  if not md or not md.positions or not md.graph or type(ids)~='table' or not finite(speed)
    or not heading or not finite(heading.x)or not finite(heading.y)then return nil end
  local pa,pb=md and md.positions[a],md and md.positions[b]
  local edge=md and md.graph[a]and md.graph[a][b]
  if not position(pa)or not position(pb)or not position(p)or not heading or not edge or not ids then return nil end
  local dx,dy,dz=pb.x-pa.x,pb.y-pa.y,pb.z-pa.z
  local ll=dx*dx+dy*dy
  if ll<.01 or dx*heading.x+dy*heading.y<=0 or(edge.oneWay and edge.inNode~=a)then return nil end
  local t=((p.x-pa.x)*dx+(p.y-pa.y)*dy)/ll
  local radius=math.max((md.radius and md.radius[a])or 0,edge.inRadius or 0,edge.outRadius or 0)
  if radius<=0 or t<-.1 or t>1.1 or math.abs(p.z-pa.z-t*dz)>2
    or(p.x-pa.x-t*dx)^2+(p.y-pa.y-t*dy)^2>(radius+2)^2 then return nil end
  local index
  for i=math.max(1,(cursor or 1)-1),#ids-1 do
    if ids[i]==a and ids[i+1]==b then index=i+1;break end
  end
  if not index and ids[1]==b then index=1 end -- implicit car-to-first-waypoint approach
  if not index then return nil end
  local length=math.sqrt(ll+dz*dz)
  local along=-t*length
  local points={{x=pa.x,y=pa.y,z=pa.z,along=along,from=a,to=b}}
  along=along+length
  for i=index,#ids-1 do
    if along>250 then break end
    local u,v=ids[i],ids[i+1]
    local q,r=md.positions[u],md.positions[v]
    local e=md.graph[u]and md.graph[u][v]
    if not position(q)or not position(r)or not e or(e.oneWay and e.inNode~=u)then break end
    points[#points+1]={x=q.x,y=q.y,z=q.z,along=along,from=u,to=v}
    along=along+math.sqrt((r.x-q.x)^2+(r.y-q.y)^2+(r.z-q.z)^2)
  end
  return M.limit({points=points,radius=radius},md,signals,speed)
end

function M.limit(c,md,signals,speed)
  if not c or not c.points or not finite(c.radius)or not md or not md.positions or not md.graph
    or type(signals)~='table'or not finite(speed)then return nil end
  speed=math.max(0,speed)
  local seen,best={},nil
  for _,point in ipairs(c.points)do
    local a,b=point.from,point.to
    local key=a and b and(tostring(a)..'/'..tostring(b))
    if key and not seen[key]then
      seen[key]=true
      local edge=md.graph[a]and md.graph[a][b]
      local entries=signals[a]and signals[a][b]
      local signal=entries and entries[1] -- the same approach signal used by stock traffic
      local pa,pb=md.positions[a],md.positions[b]
      if edge and(not edge.oneWay or edge.inNode==nil or edge.inNode==a)
        and signal and(signal.action==1 or signal.action==2)and position(signal.pos)
        and position(pa)and position(pb)and position(point)and finite(point.along)then
        local dx,dy,dz=pb.x-pa.x,pb.y-pa.y,pb.z-pa.z
        local ll=dx*dx+dy*dy+dz*dz
        if ll>.01 then
          local q=signal.pos
          local t=((q.x-pa.x)*dx+(q.y-pa.y)*dy+(q.z-pa.z)*dz)/ll
          local length=math.sqrt(ll)
          local x,y,z=pa.x+t*dx,pa.y+t*dy,pa.z+t*dz
          local margin=math.max(4,c.radius+2)
          -- A roadside head can be offset from its edge. Another road level or
          -- a stale signal outside this edge is not a stop on the current path.
          if t>=-margin/length and t<=1+margin/length and math.abs(q.z-z)<=2
            and(q.x-x)^2+(q.y-y)^2<=margin*margin then
            local distance=point.along+((q.x-point.x)*dx+(q.y-point.y)*dy+(q.z-point.z)*dz)/length
            local stop=M.STOP_M+speed*M.REACTION_S
            local canStop=distance>=stop+speed*speed/(2*M.DECEL)
            -- Clear a yellow that is already too close to stop for. Never aim
            -- back at a stop line already passed, including on a graph handoff.
            if distance>=-.25 and(signal.action==2 or canStop)then
              local cap=math.sqrt(2*M.DECEL*math.max(0,distance-stop))
              if not best or cap<best.cap then
                best={cap=cap,distance=distance,instance=signal.instance,action=signal.action,
                  phase='signal',reason=signal.action==2 and'red light'or'yellow light'}
              end
            end
          end
        end
      end
    end
  end
  return best
end
return M
