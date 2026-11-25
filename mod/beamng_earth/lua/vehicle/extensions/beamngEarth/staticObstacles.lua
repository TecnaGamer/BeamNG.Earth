-- Clearance and braking ceilings along the requested GPS road path. Native AI
-- owns braking and steering; raycasts, signals and lanes come from its APIs.
local M = {}
M.enabled = false
M.STEP_M = 4
M.RAY_STEP_M = 0.15
M.PROBE_HEIGHTS = {0.2, 1.1}
M.MARGIN_M = 0.25
M.SAMPLE_S = 0.2
M.MAX_REACH_M = 250
M.PROBE_BUDGET_MS = 0.75 -- cooperative slice; retain the full ray/surface grid
M.PROBE_MAX_S = 0.15    -- brake if a slow machine cannot finish within the look-ahead margin

local function finite(x) return type(x)=='number' and x==x and math.abs(x)<math.huge end
local function clamp(x,a,b) return math.max(a,math.min(b,x)) end
local function position(p) return p and finite(p.x) and finite(p.y) and finite(p.z) end

local function laneInfo(lanes,radius,offset)
  if type(lanes)~='string' or #lanes==0 or not finite(radius) or radius<=0 then return nil end
  local forward,closest,score={},nil,math.huge
  local width=2*radius/#lanes
  for i=1,#lanes do if lanes:sub(i,i)=='+' then
    forward[#forward+1]=i
    local centre=-radius+(i-.5)*width
    if math.abs(centre-offset)<=score then closest,score=#forward,math.abs(centre-offset) end
  end end
  if not closest then return nil end
  local index=forward[closest]
  local lo,hi=index,index
  while lo>1 and lanes:sub(lo-1,lo-1)=='+' do lo=lo-1 end
  while hi<#lanes and lanes:sub(hi+1,hi+1)=='+' do hi=hi+1 end
  return {forward=forward,rank=#forward-closest,lo=-radius+(lo-1)*width,
    hi=-radius+hi*width,width=width,index=index,centre=-radius+(index-.5)*width}
end

-- from/to are oriented along the vehicle's legal direction. Cursor distinguishes
-- a later visit to the same edge in a planned loop from an already consumed prefix.
function M.corridor(md,ids,from,to,p,width,reach,lanesForEdge,cursor)
  if not md or not md.graph or not md.positions or not position(p) or not finite(width)
    or width<=0 or not finite(reach) or reach<=0 or type(lanesForEdge)~='function' then return nil end
  local a,b=md.positions[from],md.positions[to]
  local edge=md.graph[from] and md.graph[from][to]
  if not edge or not position(a) or not position(b) then return nil end
  local dx,dy=b.x-a.x,b.y-a.y
  local ll=dx*dx+dy*dy
  if ll<.01 then return nil end
  local t=((p.x-a.x)*dx+(p.y-a.y)*dy)/ll
  if t<-.1 or t>1.1 then return nil end
  t=clamp(t,0,1)
  local z=a.z+t*(b.z-a.z)
  if math.abs(p.z-z)>3 then return nil end
  local length=math.sqrt(ll)
  local rx,ry=dy/length,-dx/length
  local lateral=(p.x-a.x-t*dx)*rx+(p.y-a.y-t*dy)*ry
  local function radii(e,u,v)
    local ra,rb=e.inRadius,e.outRadius
    if e.inNode==v then ra,rb=rb,ra end
    return ra or md.radius[u],rb or md.radius[v]
  end
  local ra,rb=radii(edge,from,to)
  if not finite(ra) or not finite(rb) then return nil end
  local radius=ra+t*(rb-ra)
  local lanes=lanesForEdge(from,to)
  local info=laneInfo(lanes,radius,lateral)
  if not info then return nil end
  local index
  for i=math.max(1,cursor or 1),#ids do
    if ids[i]==to and (i==1 or ids[i-1]==from) then index=i;break end
  end
  if not index then return nil end
  local out={points={},cursor=index,lateral=lateral,lane=info,lanes=lanes,radius=radius,width=width}
  local along=0
  local function junction(id)
    local count=0;for _ in pairs(md.graph[id] or {})do count=count+1 end
    return count>2
  end
  local u,v,first=from,to,true
  for _=1,256 do
    local pa,pb=md.positions[u],md.positions[v]
    local e=md.graph[u] and md.graph[u][v]
    if not e or not position(pa) or not position(pb) then break end
    local ex,ey,ez=pb.x-pa.x,pb.y-pa.y,pb.z-pa.z
    local horizontal=math.sqrt(ex*ex+ey*ey)
    local len=math.sqrt(ex*ex+ey*ey+ez*ez)
    if horizontal<.1 then break end
    local ar,br=radii(e,u,v)
    if not finite(ar) or not finite(br) then break end
    local laneString=lanesForEdge(u,v)
    local start=first and t or 0
    local available=(1-start)*len
    local use=math.min(available,reach-along)
    local steps=math.max(1,math.ceil(use/M.STEP_M))
    for k=(#out.points==0 and 0 or 1),steps do
      local travel=use*k/steps
      local f=start+travel/len
      local r=ar+(br-ar)*f
      local legal=laneInfo(laneString,r,lateral)
      if not legal then return #out.points>1 and out or nil end
      local rank=math.max(1,#legal.forward-info.rank)
      local laneIndex=legal.forward[rank]
      local nominal=-r+(laneIndex-.5)*2*r/#laneString
      local room=math.max(0,r-width*.5)
      nominal=clamp(nominal,-room,room)
      out.points[#out.points+1]={x=pa.x+ex*f,y=pa.y+ey*f,z=pa.z+ez*f,
        from=u,to=v,
        rx=ey/horizontal,ry=-ex/horizontal,fx=ex/horizontal,fy=ey/horizontal,
        along=along+travel,radius=r,lanes=laneString,lo=legal.lo,hi=legal.hi,
        junction=(junction(u) and f*len<8) or (junction(v) and (1-f)*len<8),
        -- Blend the occupied offset toward this lane's centre. Reusing an
        -- offset from the first lane displaces the forecast at lane transitions.
        -- Keep the full body on narrow roads, where stock AI straddles the line.
        nominal=clamp(nominal+(lateral-nominal)*math.max(0,1-(along+travel)/12),-room,room)}
    end
    along=along+use
    if along>=reach or use<available then break end
    index=index+1;u,v=v,ids[index];first=false
    if not v then break end
  end
  return #out.points>1 and out or nil
end

function M.inside(point,offset,halfWidth)
  return finite(offset) and offset-halfWidth>=point.lo and offset+halfWidth<=point.hi
end

-- Preserve the full body, trimming only extra clearance at a road/lane edge.
function M.probeHalfWidth(p,width,c)
  return math.max(width*.5,math.min(width*.5+M.MARGIN_M,
    p.radius-math.abs(p.nominal),p.nominal-p.lo,p.hi-p.nominal,
    c.lateral-c.lane.lo,c.lane.hi-c.lateral))
end

-- Graph heights interpolate between road nodes, not the physical bridge/deck.
-- Follow continuous support from the car's floor. A small sampling step and
-- bounded rise keep an obstacle's top from becoming the new road reference.
local function surfacePoints(c,offset,halfWidth,surface)
  local out,previous={},{surface.initial,surface.initial,surface.initial}
  local first=c.points[1]
  if not first then return out end
  if not finite(surface.initial) or math.abs(surface.initial-first.z)>3 then
    return out,{distance=0,x=first.x,y=first.y,z=first.z,kind='surface gap'}
  end
  for i=1,#c.points-1 do
    local a,b=c.points[i],c.points[i+1]
    local steps=math.max(1,math.ceil((b.along-a.along)/.5))
    local dense={out[#out]}
    for j=(i==1 and 0 or 1),steps do
      local t=j/steps
      local p={}
      for k,v in pairs(a)do p[k]=finite(v) and finite(b[k]) and v+(b[k]-v)*t or v end
      local side=offset and offset(p) or p.nominal
      local half=type(halfWidth)=='function' and halfWidth(p) or halfWidth
      local step=j==0 and 0 or (b.along-a.along)/steps
      local allowance=j==0 and .25 or step*.5+.04
      for k,key in ipairs({'zLeft','z','zRight'})do
        local lateral=side+(k-2)*half
        local x,y=p.x+p.rx*lateral,p.y+p.ry*lateral
        -- The car can already be banked: seed each support track from its
        -- floor plane, keeping the same bounded rise/drop checks after that.
        local up,origin=surface.up,surface.origin
        if j==0 and origin and up and finite(up.x) and finite(up.y) and finite(up.z) and up.z>.5 then
          previous[k]=surface.initial-((x-origin.x)*up.x+(y-origin.y)*up.y)/up.z
        end
        local z=surface.height(x,y,previous[k]+allowance)
        if not finite(z) or z>previous[k]+allowance or previous[k]-z>math.max(.25,step+.04) then
          for n=(i==1 and 1 or 2),#dense do out[#out+1]=dense[n]end
          return out,{distance=math.max(0,p.along-step),x=x,y=y,z=previous[k],kind='surface gap'}
        end
        p[key]=z;previous[k]=z
      end
      dense[#dense+1]=p
    end
    -- Keep the existing long casts on flat/linear grades. Add casts only where
    -- the measured surface bends enough for a chord to intersect the road.
    local function append(first,last)
      local a,b=dense[first],dense[last]
      local worst,index=.03,nil
      for k=first+1,last-1 do
        local p=dense[k]
        for _,key in ipairs({'zLeft','z','zRight'})do
          local error=math.abs(p[key]-a[key]-(b[key]-a[key])*(p.along-a.along)/math.max(.001,b.along-a.along))
          if error>worst then worst,index=error,k end
        end
      end
      if index then append(first,index);append(index,last)
      else out[#out+1]=b end
    end
    if i==1 then out[1]=dense[1]end
    append(1,#dense)
  end
  return out
end

-- Cast a grid over the whole vehicle width, along short curved-road segments.
-- The callback has numeric position, unit direction and distance arguments.
function M.trace(corridor,offset,halfWidth,cast,surface)
  if not corridor or (type(halfWidth)~='function' and (not finite(halfWidth) or halfWidth<=0))
    or type(cast)~='function' then return nil end
  local points,gap=corridor.points
  if surface then points,gap=surfacePoints(corridor,offset,halfWidth,surface)end
  local best=gap
  for i=1,#points-1 do
    local a,b=points[i],points[i+1]
    local ah=type(halfWidth)=='function' and halfWidth(a) or halfWidth
    local bh=type(halfWidth)=='function' and halfWidth(b) or halfWidth
    local count=math.max(1,math.ceil(2*math.max(ah,bh)/M.RAY_STEP_M))
    local oa=offset and offset(a) or a.nominal
    local ob=offset and offset(b) or b.nominal
    for j=0,count do
      local sa,sb=-ah+2*ah*j/count,-bh+2*bh*j/count
      local ax,ay=a.x+a.rx*(oa+sa),a.y+a.ry*(oa+sa)
      local bx,by=b.x+b.rx*(ob+sb),b.y+b.ry*(ob+sb)
      local f=2*j/count
      local az=a.zLeft and (f<=1 and a.zLeft+(a.z-a.zLeft)*f or a.z+(a.zRight-a.z)*(f-1))or a.z
      local bz=b.zLeft and (f<=1 and b.zLeft+(b.z-b.zLeft)*f or b.z+(b.zRight-b.z)*(f-1))or b.z
      local dx,dy,dz=bx-ax,by-ay,bz-az
      local length=math.sqrt(dx*dx+dy*dy+dz*dz)
      if length>.001 then for _,height in ipairs(M.PROBE_HEIGHTS) do
        local hit=cast(ax,ay,az+height,dx/length,dy/length,dz/length,length)
        if finite(hit) and hit>=0 and hit<length-.001 then
          local distance=a.along+(b.along-a.along)*hit/length
          if not best or distance<best.distance then
            best={distance=distance,x=ax+dx*hit/length,y=ay+dy*hit/length,z=az+height+dz*hit/length,
              side=sa+(sb-sa)*hit/length}
          end
        end
      end end
    end
    if best and best.distance<=b.along then return best end
  end
  return best
end

function M.brakeLimit(distance,speed,passing)
  if not finite(distance) or not finite(speed) then return 0 end
  local cap=math.sqrt(6*math.max(0,distance-4-math.max(speed,0)*.2))
  return passing and math.min(4,cap) or cap
end

-- Keep passing within adjacent lanes in the same legal direction. A transition
-- or sharp turn is left to the ordinary AI/recovery instead of guessing its lanes.
function M.passingCorridor(c,target,width,length,obstacleDistance,joinRemaining)
  if not c or not finite(target) or not finite(width) or not finite(length) then return nil end
  local delta=target-c.lane.centre
  if math.abs(delta)>c.lane.width*1.1 then return nil end
  local join=math.max(18,math.abs(delta)*9)
  -- Once committed, inspect the remaining shift, not a new full lane change
  -- from every position. A transition beyond the checked finish must not halt the pass early.
  if finite(joinRemaining) then join=math.max(4,math.min(join,joinRemaining)) end
  local needed=math.max(join+length+3,obstacleDistance+length+3)
  local last=c.points[#c.points]
  if last.along<needed then return nil end
  local out={points={}}
  local first=c.points[1]
  local angle=math.atan(math.abs(target-c.lateral)/join)
  local half=width*.5*math.cos(angle)+length*.5*math.sin(angle)+M.MARGIN_M
  local function offset(p)return c.lateral+(target-c.lateral)*math.min(p.along/join,1)end
  for _,p in ipairs(c.points)do
    if p.junction or p.lanes~=c.lanes or math.abs(p.radius-c.radius)>.25
      or p.fx*first.fx+p.fy*first.fy<.94 or not M.inside(p,offset(p),half) then return nil end
    out.points[#out.points+1]=p
    if p.along>=needed then break end
  end
  return out,offset,half,join,delta
end

function M.adjacentLanes(c)
  local out={}
  if not c then return out end
  local lane=c.lane
  for _,index in ipairs({lane.index-1,lane.index+1})do
    if c.lanes:sub(index,index)=='+' then
      local target=-c.radius+(index-.5)*lane.width
      if target>lane.lo and target<lane.hi then out[#out+1]=target end
    end
  end
  return out
end

local function interval(p,v,half,horizon)
  if math.abs(v)<.0001 then return math.abs(p)<=half and 0 or nil,horizon end
  local a,b=(-half-p)/v,(half-p)/v
  if a>b then a,b=b,a end
  a,b=math.max(0,a),math.min(horizon,b)
  if a>b then return nil end
  return a,b
end

-- Reject traffic which occupies, or will cross, any part of the passing corridor.
-- Boxes use actual stock vehicle dimensions; elevation excludes a separate bridge.
-- mapmgr's tracked-object `active` means player-seated, not physics-active: AI
-- and parked cars are obstacles too. Inactive pool vehicles do not send tracking.
function M.trafficClear(c,offset,ego,objects,horizon)
  if type(objects)~='table' or not ego or not finite(ego.width) or not finite(ego.length) then return false end
  horizon=horizon or (c.points[#c.points].along+ego.length)/4+2
  for id,o in pairs(objects)do if tonumber(id)~=ego.id then
    if not position(o.pos) or not o.dirVec or not o.vel or not finite(o.width) or not finite(o.length)
      or not finite(o.dirVec.x) or not finite(o.dirVec.y) or not finite(o.vel.x) or not finite(o.vel.y) then return false end
    local norm=math.sqrt(o.dirVec.x^2+o.dirVec.y^2)
    if norm<.1 then return false end
    local ox,oy=o.dirVec.x/norm,o.dirVec.y/norm
    local speed=math.sqrt(o.vel.x^2+o.vel.y^2)
    for i=1,#c.points-1 do
      local a,b=c.points[i],c.points[i+1]
      local oa,ob=offset(a),offset(b)
      local ax,ay=a.x+a.rx*oa,a.y+a.ry*oa
      local bx,by=b.x+b.rx*ob,b.y+b.ry*ob
      local dx,dy=bx-ax,by-ay
      local len=math.sqrt(dx*dx+dy*dy)
      if len>.01 then
        local fx,fy=dx/len,dy/len
        local rx,ry=fy,-fx
        local px,py=o.pos.x-(ax+bx)*.5,o.pos.y-(ay+by)*.5
        local grade=(b.z-a.z)/len
        local height=o.pos.z-(a.z+b.z)*.5-(px*fx+py*fy)*grade
        if math.abs(height)<math.max(3,((o.height or 2)+(ego.height or 2))*.5+1) then
          local forward=math.abs(ox*fx+oy*fy)*o.length*.5+math.abs(oy*fx-ox*fy)*o.width*.5
          local side=math.abs(ox*rx+oy*ry)*o.length*.5+math.abs(oy*rx-ox*ry)*o.width*.5
          local x0,x1=interval(px*fx+py*fy,o.vel.x*fx+o.vel.y*fy,len*.5+ego.length*.5+forward+2+speed*.5,horizon)
          local y0,y1=interval(px*rx+py*ry,o.vel.x*rx+o.vel.y*ry,ego.width*.5+side+.5,horizon)
          if x0 and y0 and math.max(x0,y0)<=math.min(x1,y1) then return false end
        end
      end
    end
  end end
  return true
end

local path,bound,cursor,elapsed=nil,nil,1,0
local policy,applied,lastCap,goal
local phase,reason,detections,passes='idle',nil,0,0
local lastProbe
local work,checkWork
local signalResult
local waitReported,waitElapsed=false,0

local function reportSignalWait(waiting,dt)
  waitElapsed=waitElapsed+(dt or 0)
  if waiting~=waitReported or(waiting and waitElapsed>=.5)then
    if obj and obj.getId and obj.queueGameEngineLua then
      obj:queueGameEngineLua("require('beamngEarth/gps').onAiSignalWait("..obj:getId()..','..tostring(waiting)..')')
    end
    waitReported,waitElapsed=waiting,0
  end
end

local gapReported
local function reportGap(active)
  if active==gapReported then return end
  if obj and obj.getId and obj.queueGameEngineLua then
    obj:queueGameEngineLua("require('beamngEarth/gps').onAiSurfaceGap("..obj:getId()..','..tostring(active)..')')
  end
  gapReported=active
end
local function release()
  reportSignalWait(false)
  if policy and ai and ai.routeSpeed==applied and ai.speedMode=='limit' then
    ai.setSpeed(policy.speed);ai.setSpeedMode(policy.mode)
  end
  policy,applied,lastCap=nil,nil,nil
end

local function limit(cap,legal)
  if not policy or ai.routeSpeed~=applied or ai.speedMode~='limit' then
    policy={speed=ai.routeSpeed,mode=ai.speedMode}
  end
  if finite(policy.speed) and policy.speed>=0 then cap=math.min(cap,policy.speed) end
  if (policy.mode==nil or policy.mode=='legal') and finite(legal) and legal>0 then cap=math.min(cap,legal) end
  applied,lastCap=cap,cap;ai.setSpeed(cap);ai.setSpeedMode('limit')
end

-- Optional passage helpers cancel an in-flight snapshot when their policy changes.
function M.cancelProbe()
  work,checkWork=nil,nil
  elapsed=M.SAMPLE_S
end

function M.speedCeiling()
  if policy then return policy.speed end
  return ai and ai.routeSpeed
end

function M.setEnabled(on)
  M.enabled = on == true
  if not M.enabled then
    reportGap(false)
    work,checkWork=nil,nil
    local oncoming=beamngEarth_oncomingPass
    local passing = goal ~= nil or (oncoming and oncoming.active())
    if oncoming then oncoming.reset() end
    goal, lastProbe = nil, nil
    if signalResult then
      limit(signalResult.cap,signalResult.legal);phase,reason='signal',signalResult.reason
    else release();phase,reason='disabled',nil end
    if passing and obj and obj.queueGameEngineLua then
      obj:queueGameEngineLua("local G=require('beamngEarth/gps');if G.autodrive()then G.retryAutodrive()end")
    end
  end
  elapsed = M.SAMPLE_S
end

function M.onExtensionLoaded()
  if obj and obj.queueGameEngineLua then
    obj:queueGameEngineLua("local S=beamngEarth_settings or package.loaded['beamngEarth/settings'];if S and be then local v=be:getObjectByID("..obj:getId()..");if v then v:queueLuaCommand(\"if beamngEarth_staticObstacles and beamngEarth_staticObstacles.setEnabled then beamngEarth_staticObstacles.setEnabled(\"..tostring(S.get('autodriveObstacles')==true)..\") end\")end end")
  end
end

function M.reset()
  work,checkWork=nil,nil
  gapReported=nil -- GPS retains its terrain reason until a fresh probe or explicit disable
  release();path,bound,cursor,elapsed,goal=nil,nil,1,0,nil;phase,reason='idle',nil
  lastProbe=nil
  signalResult=nil
end
M.onReset=M.reset
function M.onExtensionUnloaded()
  M.reset()
  reportGap(false)
  if beamngEarth_oncomingPass and beamngEarth_oncomingPass.active() then
    beamngEarth_oncomingPass.reset();ai.setMode('stop')
  end
end

function M.bindPath(ids,md,started)
  if not beamngEarth_trafficSignals and extensions and extensions.load then extensions.load('beamngEarth_trafficSignals')end
  if md~=bound then M.reset();bound=md end
  local changed=not path or #path~=#ids
  if not changed then for i,id in ipairs(ids)do if path[i]~=id then changed=true;break end end end
  if changed or started then work,checkWork=nil,nil end
  if changed then path={};for i,id in ipairs(ids)do path[i]=id end;cursor=1 end
  if started then
    local cap=lastCap
    policy,applied=nil,nil -- the fresh stock route now owns the underlying speed policy
    if goal then goal.needsChange=true end
    if cap then limit(cap) end -- a handoff must not discard an active braking ceiling
  end
  elapsed=M.SAMPLE_S
end

function M.alongRoute(c,p)
  local best,distance
  for i=1,#c.points-1 do
    local a,b=c.points[i],c.points[i+1]
    local dx,dy=b.x-a.x,b.y-a.y;local ll=dx*dx+dy*dy
    if ll>.001 then
      local t=((p.x-a.x)*dx+(p.y-a.y)*dy)/ll
      local f=clamp(t,0,1);local z=a.z+f*(b.z-a.z)
      local cost=(p.x-a.x-f*dx)^2+(p.y-a.y-f*dy)^2+(p.z-z)^2
      if not best or cost<best then
        best=cost;distance=a.along+(b.along-a.along)*(i==1 and math.min(t,1) or f)
      end
    end
  end
  if best and best<(c.radius+2)^2 then return distance end
end

local function trafficObjects()
  local objects=mapmgr.getObjects()
  if not objects then return nil end
  local out={}
  for id,o in pairs(objects)do if tonumber(id)~=obj:getId() then
    out[id]={pos=vec3(obj:getObjectCenterPositionXYZ(id)),dirVec=vec3(obj:getObjectDirectionVectorXYZ(id)),
      vel=o.vel,width=obj:getObjectInitialWidth(id),length=obj:getObjectInitialLength(id),height=obj:getObjectInitialHeight(id)}
  end end
  return out
end

local function probe()
  local oncoming=beamngEarth_oncomingPass
  local function unavailable()
    return oncoming and oncoming.active() and {cap=0,phase='waiting',reason='passing road unavailable'} or nil
  end
  local p,h=obj:getFrontPosition(),obj:getDirectionVector()
  local a,b=mapmgr.findBestRoad(p,h)
  lastProbe={from=a,to=b}
  if not a or not b then return unavailable() end
  local pa,pb=bound.positions[a],bound.positions[b]
  if not pa or not pb then return unavailable() end
  if (pb.x-pa.x)*h.x+(pb.y-pa.y)*h.y<0 then a,b=b,a end
  local speed=obj:getVelocity():length()
  local reach=math.min(M.MAX_REACH_M,math.max(70,speed*speed/6+speed*.4+12))
  local width,length=obj:getInitialWidth(),obj:getInitialLength()
  local c=M.corridor(bound,path,a,b,p,width,reach,ai.getEdgeLaneConfig,cursor)
  lastProbe={from=a,to=b,points=c and #c.points,reach=c and c.points[#c.points].along,lateral=c and c.lateral,first=path[1]}
  if not c then
    return unavailable()
  end
  cursor=c.cursor
  local function cast(x,y,z,dx,dy,dz,d)
    local hit=obj:castRayStatic(vec3(x,y,z),vec3(dx,dy,dz),d)
    if checkWork then checkWork() end
    return hit
  end
  local surface={initial=obj:getSurfaceHeightBelow(p),origin=p,up=obj:getDirectionVectorUp(),
    height=function(x,y,z)
      local height=obj:getSurfaceHeightBelow(vec3(x,y,z))
      if checkWork then checkWork() end
      return height
    end}
  local function halfWidth(p)
    return M.probeHalfWidth(p,width,c)
  end
  local hit=M.trace(c,nil,halfWidth,cast,surface)
  lastProbe.hit=hit and hit.distance
  local legal=bound.graph[a][b].speedLimit
  local ego={id=obj:getId(),width=width,length=length,height=obj:getInitialHeight(),surface=surface}
  if oncoming and oncoming.active() then
    return oncoming.update(M,bound,path,c,hit,ego,trafficObjects(),cast,speed,legal,a,b)
  end
  local distance=goal and M.alongRoute(c,goal.point)
  if goal and (not distance or distance < -length-1) then goal=nil end
  if goal then
    distance=math.max(0,distance)
  elseif hit then
    distance=hit.distance;detections=detections+1
  else return nil end
  local normalCap=M.brakeLimit(distance,speed,false)
  if not goal and hit and hit.kind=='surface gap' then
    return {cap=normalCap,legal=legal,phase='blocked',reason='surface gap',surfaceGap=true,brakeDistance=distance}
  end
  -- A roadside object in the extra buffer need not block a supported body
  -- corridor. Keep a cautious ceiling without forcing a lane change.
  if not goal and hit and finite(hit.side) and math.abs(hit.side)>width*.5
    and not M.trace(c,nil,width*.5,cast,surface) then
    return {cap=math.max(4,normalCap),legal=legal,phase='clearance',reason='body path clear'}
  end
  -- Let the stock plan settle into a definite occupied lane before requesting a
  -- relative lane change; a boundary start can seed either neighbouring lane.
  local boundary=c.lane.lo+math.floor((c.lateral-c.lane.lo)/c.lane.width+.5)*c.lane.width
  if not goal and math.abs(c.lateral-boundary)<.3 then
    return {cap=math.min(2,normalCap),legal=legal,phase='aligning',reason='lane boundary',brakeDistance=distance}
  end
  local objects=trafficObjects()
  local targets=goal and {goal.target} or M.adjacentLanes(c)
  for _,target in ipairs(targets)do
    local left=goal and (goal.join-math.max(0,goal.startDistance-distance))
    local pass,offset,half,join,delta=M.passingCorridor(c,target,width,length,distance,left)
    if pass and not M.trace(pass,offset,half,cast,surface) and M.trafficClear(pass,offset,ego,objects) then
      if not goal and speed>4.5 then
        return {cap=math.min(4,normalCap),legal=legal,phase='slowing',brakeDistance=distance}
      end
      if not goal then goal={point=hit,target=target,join=join,startDistance=distance,needsChange=true};passes=passes+1 end
      if goal.needsChange then
        if math.abs(delta)>.1 then ai.laneChange(nil,join,delta) end
        goal.needsChange=false
      end
      -- The checked passing corridor clears the obstacle; braking to its old
      -- longitudinal position would stop alongside it instead of completing.
      return {cap=4,legal=legal,phase='passing'}
    end
  end
  if not goal then
    if not oncoming and extensions and extensions.load then
      extensions.load('beamngEarth_oncomingPass');oncoming=beamngEarth_oncomingPass
    end
    if oncoming then
      local result=oncoming.update(M,bound,path,c,hit,ego,objects,cast,speed,legal,a,b)
      if result then return result end
    end
  end
  return {cap=goal and 0 or normalCap,legal=legal,phase='blocked',reason='no clear legal corridor',brakeDistance=distance}
end

local function startProbe()
  -- One coroutine owns one snapshot. Reset/rebind discards it before it can
  -- publish a stale result. Timers cover execution only, excluding frame waits.
  local timer=HighPerfTimer and HighPerfTimer()
  local current={age=0,timer=timer}
  work=current
  current.thread=coroutine.create(function()
    local p=obj:getFrontPosition()
    current.origin={x=p.x,y=p.y,z=p.z}
    local queries,spent=0,0
    if timer then checkWork=function()
      queries=queries+1
      if queries<32 then return end
      queries=0;spent=spent+timer:stopAndReset()
      if spent>=M.PROBE_BUDGET_MS then
        coroutine.yield();spent=0;timer:stopAndReset()
      end
    end end
    local result=probe()
    if result and result.brakeDistance then
      local p,o=obj:getFrontPosition(),current.origin
      local travel=math.sqrt((p.x-o.x)^2+(p.y-o.y)^2+(p.z-o.z)^2)
      result.cap=math.min(result.cap,M.brakeLimit(result.brakeDistance-travel,obj:getVelocity():length(),false))
    end
    return result
  end)
end

local function advanceProbe(dt)
  local current=work
  current.age=current.age+(dt or 0)
  if current.timer then current.timer:stopAndReset() end
  local ok,result=coroutine.resume(current.thread)
  if ok and coroutine.status(current.thread)~='dead' then
    local deadline=M.PROBE_MAX_S
    local pass=beamngEarth_oncomingPass
    -- A committed 3 m/s passage moves at most 1.75 m in this window, inside
    -- its checked stopping margin. Keep the shorter deadline at road speeds.
    if pass and pass.active() and obj:getVelocity():length()<=pass.SPEED+.5 then
      deadline=math.max(deadline,pass.PROBE_MAX_S or deadline)
    end
    if current.age>=deadline then limit(0);phase,reason='probing','probe budget exceeded' end
    return true
  end
  work,checkWork=nil,nil
  return false,ok,result
end

function M.updateGFX(dt)
  if not M.enabled then
    if work or goal or (phase~='disabled' and phase~='signal') then M.setEnabled(false) end
    if not path or not bound or not beamngEarth_trafficSignals then return end
  end
  if not path or not bound or not mapmgr or mapmgr.mapData~=bound or not ai or ai.mode~='manual' then
    if work or policy or goal or signalResult or waitReported or phase~='idle' then
      if ai and ai.mode~='manual' and beamngEarth_oncomingPass and not beamngEarth_oncomingPass.suspended() then
        beamngEarth_oncomingPass.reset()
      end
      work,checkWork=nil,nil
      release();goal=nil;signalResult=nil;phase='idle'
    end
    return
  end
  elapsed=elapsed+(dt or 0)
  local sampled=elapsed
  local sample=elapsed>=M.SAMPLE_S
  if not sample and not work then return end
  if sample then elapsed=0;signalResult=nil end
  local ok,result=pcall(function()
    if sample and beamngEarth_trafficSignals then
      local p,h=obj:getFrontPosition(),obj:getDirectionVector()
      local a,b=mapmgr.findBestRoad(p,h)
      local pa,pb=a and bound.positions[a],b and bound.positions[b]
      if pa and pb then
        if (pb.x-pa.x)*h.x+(pb.y-pa.y)*h.y<0 then a,b=b,a end
        signalResult=beamngEarth_trafficSignals.forPath(bound,path,mapmgr.signalsData,a,b,p,h,obj:getVelocity():length(),cursor)
        local edge=bound.graph[a]and bound.graph[a][b]
        if signalResult then signalResult.legal=edge and edge.speedLimit end
      end
    end
    if M.enabled and not work and sample then startProbe() end
  end)
  if not ok then
    reportSignalWait(false)
    reason=tostring(result);phase='unavailable'
    if applied then limit(0) end
    return
  end
  if work then
    local pending
    pending,ok,result=advanceProbe(dt)
    if pending then
      -- Signal braking is independent of the unfinished obstacle trace.
      if signalResult then limit(math.min(signalResult.cap,lastCap or math.huge),signalResult.legal) end
      reportSignalWait(signalResult~=nil and signalResult.cap<.5 and obj:getVelocity():length()<.5,sample and sampled or 0)
      return
    end
    if not ok then
      reportSignalWait(false);reason=tostring(result);phase='unavailable'
      if applied then limit(0) end
      return
    end
  end
  reportGap(result~=nil and result.surfaceGap==true and obj:getVelocity():length()<1)
  if signalResult and(not result or signalResult.cap<result.cap)then result=signalResult end
  if not result then release();phase,reason='idle',nil;return end
  reportSignalWait(signalResult~=nil and signalResult.cap<.5 and obj:getVelocity():length()<.5,sampled)
  phase,reason=result.phase,result.reason
  limit(result.cap,result.legal)
end

function M.status()
  return {enabled=M.enabled,phase=phase,reason=reason,holding=applied~=nil,speedCap=applied,detections=detections,passes=passes,
    bound=bound~=nil,probe=lastProbe,probePending=work~=nil,oncoming=beamngEarth_oncomingPass and beamngEarth_oncomingPass.status(),
    signal=signalResult,waitingForSignal=waitReported}
end

return M
