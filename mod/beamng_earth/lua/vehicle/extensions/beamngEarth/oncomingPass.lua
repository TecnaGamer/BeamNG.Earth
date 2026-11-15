-- Temporary opposite-side passage around a static obstruction. The native AI
-- still steers, brakes and avoids cars. Unknown geometry leaves recovery intact.
local M={SPEED=3,MAX_SIGHT_M=1000,PLAN_S=1,PROBE_MAX_S=.5}
M.enabled=false
local goal,why
local waiting,waitLeft
local function clamp(x,a,b)return math.max(a,math.min(b,x))end
local function result(cap,phase,reason,legal)return {cap=cap,phase=phase,reason=reason,legal=legal}end

-- Beyond the return point, inspect the road oncoming cars approach on, even if
-- our GPS takes a turn later. This changes only sensing, never the driving path.
function M.visibilityPath(md,ids,a,b,p,fx,fy,finish)
  local index
  for i,id in ipairs(ids)do if id==b and (i==1 or ids[i-1]==a)then index=i;break end end
  if not index then return ids end
  local out,seen={b},{[a]=true,[b]=true}
  local previous,current=a,b
  local pb=md.positions[b]
  local along=math.sqrt((pb.x-p.x)^2+(pb.y-p.y)^2+(pb.z-p.z)^2)
  for _=1,255 do
    local nextId
    if along<finish+8 then nextId=ids[index+1]
    else
      local best=.995
      for id,edge in pairs(md.graph[current] or {})do if id~=previous and not seen[id] and not edge.oneWay then
        local q,r=md.positions[current],md.positions[id]
        local dx,dy=r.x-q.x,r.y-q.y;local len=math.sqrt(dx*dx+dy*dy)
        local dot=(dx*fx+dy*fy)/math.max(.01,len)
        if dot>best then best,nextId=dot,id end
      end end
    end
    if not nextId or seen[nextId] or not md.graph[current][nextId] then break end
    local q,r=md.positions[current],md.positions[nextId]
    along=along+math.sqrt((q.x-r.x)^2+(q.y-r.y)^2+(q.z-r.z)^2)
    out[#out+1]=nextId;seen[nextId]=true;index=index+1;previous,current=current,nextId
    if along>M.MAX_SIGHT_M then break end
  end
  return out
end

function M.plan(S,c,hit,ego,objects,cast,legal,continuing)
  if not c or not hit or not legal or legal<=0 then return nil,'unknown road' end
  local index=c.lane.index
  local other
  for _,i in ipairs({index-1,index+1})do
    if c.lanes:sub(i,i)=='-' then other=i;break end
  end
  if not other or c.lanes:find('[^%+%-]') then return nil,'no adjacent opposing lane' end
  local target=-c.radius+(other-.5)*c.lane.width
  local room=c.lane.width*.5-ego.width*.5-S.MARGIN_M
  if room<=0 then return nil,'insufficient lane width' end
  local fullJoin=math.max(24,math.abs(target-c.lane.centre)*ego.length*.6/room)
  local join=fullJoin
  if not continuing and hit.distance<join+3 then return nil,'too close to enter safely' end
  if continuing then
    join=math.max(4,fullJoin*math.min(1,math.abs(target-c.lateral)/math.max(.1,math.abs(target-c.lane.centre))))
  end
  local back=math.max(0,hit.distance)+ego.length+4
  local finish=back+fullJoin
  local passSpeed=ego.passSpeed or M.SPEED
  if passSpeed<1 then return nil,'speed ceiling too low for a bounded passage' end
  local horizon=finish/passSpeed+3
  local sight=finish+math.max(legal,10)*horizon+10
  if sight>M.MAX_SIGHT_M or c.points[#c.points].along<sight then
    return nil,'insufficient loaded sight distance',fullJoin+6
  end
  local first=c.points[1]
  local grade=(c.points[2].z-first.z)/math.max(.01,c.points[2].along)
  local visible={points={}}
  local pass={points={}}
  local function offset(p)
    if p.along<join then return c.lateral+(target-c.lateral)*p.along/join end
    return target+(c.lane.centre-target)*clamp((p.along-back)/fullJoin,0,1)
  end
  local function half(p)
    local slope=p.along<join and (target-c.lateral)/join or (p.along>=back and p.along<finish and (c.lane.centre-target)/fullJoin or 0)
    local angle=math.atan(math.abs(slope))
    return ego.width*.5*math.cos(angle)+ego.length*.5*math.sin(angle)+S.MARGIN_M
  end
  for _,p in ipairs(c.points)do
    if (p.junction and p.along<=finish+8) or p.lanes~=c.lanes or math.abs(p.radius-c.radius)>.2
      or p.fx*first.fx+p.fy*first.fy<.995 then return nil,'bend, junction or lane transition' end
    local t=(p.z-first.z)/math.max(1,p.along)
    if math.abs(t-grade)>.025 then return nil,'limited view over a crest' end
    visible.points[#visible.points+1]=p
    local ground=cast(p.x+p.rx*target,p.y+p.ry*target,p.z+1,0,0,-1,2.5)
    if ground<.65 or ground>1.35 then return nil,'opposing road surface is not loaded or level' end
    if p.along<=finish+S.STEP_M then
      if math.abs(offset(p))+half(p)>p.radius then return nil,'insufficient road width' end
      ground=cast(p.x+p.rx*offset(p),p.y+p.ry*offset(p),p.z+1,0,0,-1,2.5)
      if ground<.65 or ground>1.35 then return nil,'pass or return surface unavailable' end
      pass.points[#pass.points+1]=p
    end
    if p.along>=sight then break end
  end
  if S.trace(pass,offset,half,cast,ego.surface) or S.trace(visible,function()return target end,ego.width*.5+S.MARGIN_M,cast,ego.surface) then
    return nil,'static obstruction in pass or return'
  end
  -- Check the straight sightline as well as the road-following rays: terrain or
  -- a building between us and a distant point must not count as visibility.
  local a,b=visible.points[1],visible.points[#visible.points]
  local ax,ay=a.x+a.rx*target,a.y+a.ry*target
  local dx,dy,dz=b.x+b.rx*target-ax,b.y+b.ry*target-ay,b.z-a.z
  local len=math.sqrt(dx*dx+dy*dy+dz*dz)
  if cast(ax,ay,a.z+1.1,dx/len,dy/len,dz/len,len)<len-.1 then return nil,'blocked sightline' end
  local wide={id=ego.id,width=ego.width+2*S.MARGIN_M,length=ego.length,height=ego.height}
  if not S.trafficClear(pass,offset,wide,objects,horizon) then
    return nil,'traffic conflicts with pass or return',fullJoin+6
  end
  for id,o in pairs(objects)do if tonumber(id)~=ego.id then
    local dx,dy=o.pos.x-first.x,o.pos.y-first.y
    local ahead=dx*first.fx+dy*first.fy
    local side=dx*first.rx+dy*first.ry
    local height=o.pos.z-first.z-ahead*grade
    local heading=o.dirVec.x*first.fx+o.dirVec.y*first.fy
    local closingSide=side*(o.dirVec.x*first.rx+o.dirVec.y*first.ry)<0
    local velocity=math.sqrt(o.vel.x^2+o.vel.y^2)
    if math.abs(height)<3 and ahead>0 and math.sqrt(dx*dx+dy*dy)<finish+math.max(legal,velocity)*horizon
      and (heading<-.25 or (math.abs(heading)<.7 and closingSide)) then
      return nil,'oncoming or junction traffic can arrive during passage',fullJoin+6
    end
  end end
  return {target=target,original=c.lane.centre,join=join,finish=finish,horizon=horizon,sight=sight,pass=pass,offset=offset}
end

local function retry()
  obj:queueGameEngineLua("local G=require('beamngEarth/gps');if G.autodrive()then G.retryAutodrive()end")
end

local function anchor(md,ids,p,from,to)
  local best,out
  local function try(u,v)
    local a,b=md.positions[u],md.positions[v]
    if a and b then
      local dx,dy=b.x-a.x,b.y-a.y;local ll=dx*dx+dy*dy
      if ll>.01 then
        local t=clamp(((p.x-a.x)*dx+(p.y-a.y)*dy)/ll,0,1)
        local ex,ey=p.x-a.x-t*dx,p.y-a.y-t*dy
        local cost=ex*ex+ey*ey+(p.z-a.z-t*(b.z-a.z))^2
        if not best or cost<best then
          best=cost;out={a=u,b=v,t=t,side=(ex*dy-ey*dx)/math.sqrt(ll),height=p.z-a.z-t*(b.z-a.z)}
        end
      end
    end
  end
  -- Requested waypoints start at the forward endpoint. The obstruction can be
  -- on the implicit car-to-first-waypoint edge, which also needs a rebase anchor.
  try(from,to)
  for i=1,#ids-1 do try(ids[i],ids[i+1]) end
  return out
end

function M.active()return goal~=nil end
function M.suspend()if goal then goal.suspended=true end end
function M.suspended()return goal and goal.suspended or false end
function M.offLane()return goal~=nil and goal.phase~='returning' end
function M.reset()
  -- An unloaded/reset helper must not be resumed by a suspended obstacle probe.
  local S=beamngEarth_staticObstacles
  if S and S.cancelProbe then S.cancelProbe() end
  if goal and ai and ai.driveInLane then ai.driveInLane('on') end
  goal,why,waiting,waitLeft=nil,nil,nil,nil
end
M.onReset=M.reset
M.onExtensionUnloaded=M.reset

function M.setEnabled(on)
  M.enabled=on==true
  if not M.enabled then
    local active=goal~=nil
    M.reset()
    if active then retry() end -- discard the temporary lane policy through the normal GPS handoff
  end
end

function M.onExtensionLoaded()
  if obj and obj.queueGameEngineLua then
    obj:queueGameEngineLua("local S=beamngEarth_settings or package.loaded['beamngEarth/settings'];if S and be then local v=be:getObjectByID("..obj:getId()..");if v then v:queueLuaCommand(\"if beamngEarth_oncomingPass and beamngEarth_oncomingPass.setEnabled then beamngEarth_oncomingPass.setEnabled(\"..tostring(S.get('autodriveOncomingPass')==true)..\") end\")end end")
  end
end

function M.bindPath(md,started)
  if waiting and (md~=waiting.map or started) then waiting,waitLeft=nil,nil end
  if not goal then return end
  if md~=goal.map then
    local ref=goal.anchor
    local a,b=ref and md.positions[ref.a],ref and md.positions[ref.b]
    if not a or not b then goal.unavailable=true;return end
    local dx,dy=b.x-a.x,b.y-a.y;local len=math.sqrt(dx*dx+dy*dy)
    if len<.1 then goal.unavailable=true;return end
    goal.point={x=a.x+ref.t*dx+ref.side*dy/len,y=a.y+ref.t*dy-ref.side*dx/len,z=a.z+ref.t*(b.z-a.z)+ref.height}
    goal.map=md;goal.unavailable=nil
  end
  if started then goal.pendingChange=M.offLane();goal.pendingRoute=false;goal.suspended=false end
end

function M.updateGFX(dt)
  if not M.enabled then
    if goal or waiting then M.setEnabled(false) end
    return
  end
  if not waiting then return end
  waitLeft=math.max(0,waitLeft-(dt or 0))
end

function M.update(S,md,ids,c,hit,ego,objects,cast,speed,legal,a,b)
  if not M.enabled then
    if goal then M.setEnabled(false) end
    return nil
  end
  ego.passSpeed=math.min(M.SPEED,S.speedCeiling() or M.SPEED,legal or M.SPEED)
  if ego.passSpeed<1 then return goal and result(0,'waiting','speed ceiling too low for passage',legal) or nil end
  local distance
  if goal then
    if goal.unavailable then return result(0,'waiting','passing anchor unavailable',legal) end
    local p=c.points[1]
    distance=(goal.point.x-p.x)*p.fx+(goal.point.y-p.y)*p.fy
    if goal.phase=='returning' then
      local centre=vec3(obj:getObjectCenterPositionXYZ(ego.id))
      local h=obj:getDirectionVector();local norm=math.max(.01,math.sqrt(h.x*h.x+h.y*h.y))
      local forward=(h.x*p.fx+h.y*p.fy)/norm
      local side=(centre.x-p.x)*p.rx+(centre.y-p.y)*p.ry
      local half=ego.width*.5*math.abs(forward)+ego.length*.5*math.abs((h.x*p.rx+h.y*p.ry)/norm)+S.MARGIN_M
      if forward>.95 and S.inside(p,side,half) then
        goal=nil;return nil
      end
      local returning={points={}}
      -- Use the same swept-body margin as entry. A fixed return slope can
      -- reject an empty road after a wide car settles slightly outside the lane centre.
      local room=math.min(c.radius-math.abs(c.lateral),c.lane.width*.5)-ego.width*.5-S.MARGIN_M
      if room<=0 then return result(0,'waiting','return road unavailable',legal) end
      local join=math.max(24,math.abs(c.lane.centre-c.lateral)*ego.length*.6/room)
      if c.points[#c.points].along<join+ego.length+3 then
        return result(0,'waiting','return road unavailable',legal)
      end
      local function offset(q)return c.lateral+(c.lane.centre-c.lateral)*clamp(q.along/join,0,1)end
      local angle=math.atan(math.abs(c.lane.centre-c.lateral)/join)
      local half=ego.width*.5*math.cos(angle)+ego.length*.5*math.sin(angle)+S.MARGIN_M
      for _,q in ipairs(c.points)do
        if q.junction or q.lanes~=c.lanes or math.abs(offset(q))+half>q.radius then
          return result(0,'waiting','return road unavailable',legal)
        end
        returning.points[#returning.points+1]=q
        if q.along>=join+ego.length+3 then break end
      end
      local wide={id=ego.id,width=2*half,length=ego.length,height=ego.height}
      if S.trace(returning,offset,half,cast,ego.surface)
        or not S.trafficClear(returning,offset,wide,objects,(join+ego.length+3)/ego.passSpeed+3) then
        return result(0,'waiting','return corridor blocked',legal)
      end
      return result(M.SPEED,'returning',nil,legal)
    end
    if distance < -ego.length-3 then
      goal.phase='returning';goal.pendingRoute=true;retry()
      return result(M.SPEED,'returning',nil,legal)
    end
    if goal.pendingRoute then return result(0,'waiting','preparing temporary passage',legal) end
  elseif not hit then waiting,waitLeft=nil,nil;return nil
  else distance=hit.distance end
  -- Only a stationary, uncommitted refusal may wait between full plans. An
  -- active passage rechecks every probe; a changed road/obstacle retries at once.
  if not goal and waiting and waitLeft>0 and speed<.5 and waiting.map==md and waiting.a==a and waiting.b==b
    and (hit.x-waiting.x)^2+(hit.y-waiting.y)^2+(hit.z-waiting.z)^2<4 then
    why=waiting.reason
    return waiting.gap and result(S.brakeLimit(distance-waiting.gap+4,speed,false),'waiting',why,legal) or nil
  end
  waiting,waitLeft=nil,nil
  local reach=M.MAX_SIGHT_M
  local finish=math.max(0,distance)+ego.length+4+math.max(24,c.lane.width*ego.length*.6/math.max(.01,c.lane.width*.5-ego.width*.5-S.MARGIN_M))
  local first=c.points[1]
  local viewIds=M.visibilityPath(md,ids,a,b,obj:getFrontPosition(),first.fx,first.fy,finish)
  local long=S.corridor(md,viewIds,a,b,obj:getFrontPosition(),ego.width,reach,ai.getEdgeLaneConfig)
  local plan,reason,entryGap=M.plan(S,long,{distance=math.max(0,distance)},ego,objects,cast,legal,goal~=nil)
  why=reason
  if not plan then
    if not goal and speed<.5 then
      waiting={map=md,a=a,b=b,x=hit.x,y=hit.y,z=hit.z,reason=reason,gap=entryGap};waitLeft=M.PLAN_S
    end
    if not goal and entryGap then
      return result(S.brakeLimit(distance-entryGap+4,speed,false),'waiting',reason,legal)
    end
    return goal and result(0,'waiting',reason,legal) or nil
  end
  if not goal then
    if speed>M.SPEED+.5 then return result(M.SPEED,'slowing','preparing opposite-side passage',legal) end
    local ref=anchor(md,ids,hit,a,b)
    if not ref then return nil end
    goal={point=hit,anchor=ref,map=md,phase='outbound',pendingRoute=true,target=plan.target}
    retry();return result(0,'waiting','preparing temporary passage',legal)
  end
  if goal.pendingChange then
    ai.laneChange(nil,plan.join,goal.target)
    goal.pendingChange=false
  end
  -- A public lane-change request edits the current native plan. As that plan
  -- grows it can converge toward the centre: also check the actual vehicle's
  -- projected footprint and retain enough distance to brake before the obstacle.
  local actual=S.trace(c,function()return c.lateral end,ego.width*.5+S.MARGIN_M,cast,ego.surface)
  local cap=actual and S.brakeLimit(actual.distance,speed,false) or M.SPEED
  return result(math.min(M.SPEED,cap),'passing oncoming side',nil,legal)
end

function M.status()return {enabled=M.enabled,phase=goal and goal.phase or waiting and 'waiting' or 'idle',reason=why,
  retryIn=waiting and waitLeft,suspended=M.suspended()}end
return M
