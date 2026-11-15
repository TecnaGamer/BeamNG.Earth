-- Yield to crossing traffic before pulling forward out of an AI reversing
-- manoeuvre. Hold only at rest; the stock AI keeps its route and steering.
local M={SAMPLE_S=.05,HORIZON_S=2,FORWARD_M=3,REST_MPS=.15,MAX_HOLD_S=15}
local bound,owned,recent,elapsed,threat,holds,probeError=false,false,0,0,nil,0,nil
local suspended=false
local heldFor=0
local function finite(x)return type(x)=='number'and x==x and math.abs(x)<math.huge end
local function vector(p)return p and finite(p.x)and finite(p.y)and finite(p.z)end
local function dot(a,b)return a.x*b.x+a.y*b.y+a.z*b.z end
local function cross(a,b)return {x=a.y*b.z-a.z*b.y,y=a.z*b.x-a.x*b.z,z=a.x*b.y-a.y*b.x}end
local function unit(p)
  if not vector(p)then return nil end
  local length=math.sqrt(dot(p,p));if length<.1 then return nil end
  return {x=p.x/length,y=p.y/length,z=p.z/length}
end
local function axes(dir,up)
  local f=unit(dir);local u=unit(up)
  local r=f and u and unit(cross(f,u))
  if r then return {f,r,cross(r,f)}end
end
local function dimensions(o)
  return type(o)=='table'and finite(o.width)and o.width>0 and finite(o.length)and o.length>0 and finite(o.height)and o.height>0
end
local function interval(p,v,half)
  if math.abs(v)<.001 then return math.abs(p)<=half and 0 or nil,M.HORIZON_S end
  local a,b=(-half-p)/v,(half-p)/v;if a>b then a,b=b,a end
  a,b=math.max(0,a),math.min(M.HORIZON_S,b)
  if a<=b then return a,b end
end

-- Swept boxes in the vehicle's actual forward/right/up frame. Include the short
-- forward launch, both real vehicle footprints, and the crossing car's velocity.
-- A car already being yielded to stays relevant if it brakes while passing.
function M.crossing(ego,objects,heldId)
  if not ego or not dimensions(ego)or not vector(ego.pos)then return nil end
  local frame=axes(ego.dir,ego.up);if not frame then return nil end
  local nearest,when
  for id,o in pairs(objects or{})do if tonumber(id)~=ego.id and dimensions(o)and vector(o.pos)and vector(o.vel)then
    local other=axes(o.dirVec,o.dirVecUp)
    if other and (id==heldId or math.abs(dot(o.vel,frame[2]))>1)then
      local delta={x=o.pos.x-ego.pos.x-frame[1].x*M.FORWARD_M*.5,
        y=o.pos.y-ego.pos.y-frame[1].y*M.FORWARD_M*.5,z=o.pos.z-ego.pos.z-frame[1].z*M.FORWARD_M*.5}
      local half={ego.length*.5+M.FORWARD_M*.5+.3,ego.width*.5+.3,ego.height*.5+.3}
      local enter,leave=0,M.HORIZON_S
      for i,axis in ipairs(frame)do
        local extent=math.abs(dot(other[1],axis))*o.length*.5+math.abs(dot(other[2],axis))*o.width*.5
          +math.abs(dot(other[3],axis))*o.height*.5
        local a,b=interval(dot(delta,axis),dot(o.vel,axis),half[i]+extent)
        if not a then enter=nil;break end
        enter,leave=math.max(enter,a),math.min(leave,b)
        if enter>leave then enter=nil;break end
      end
      if enter and (not when or enter<when)then nearest,when=id,enter end
    end
  end end
  return nearest,when
end

local function release()
  if owned and controller and controller.isFrozen and controller.setFreeze then controller.setFreeze(0)end
  owned,threat,heldFor=false,nil,0
end
function M.bindMap()bound=true end
function M.suspend()if owned or recent>0 then suspended=true end end
function M.resume()suspended=false end
function M.onReset()release();recent,elapsed,probeError,suspended=0,0,nil,false end
function M.onExtensionUnloaded()M.onReset();bound=false end

local function probe()
  local velocity,heading=obj:getVelocity(),obj:getDirectionVector()
  local speed=velocity:length()
  local forward=velocity:dot(heading)
  if forward<-.3 then recent=8 end
  if not owned and (recent<=0 or speed>M.REST_MPS)then return end
  if not owned and controller.isFrozen then return end -- another feature owns this hold
  local tracked=mapmgr.getObjects();if not tracked then return end
  local ego={id=obj:getId(),pos=vec3(obj:getObjectCenterPositionXYZ(obj:getId())),dir=heading,up=obj:getDirectionVectorUp(),
    width=obj:getInitialWidth(),length=obj:getInitialLength(),height=obj:getInitialHeight()}
  local objects={}
  for id,o in pairs(tracked)do if tonumber(id)~=ego.id then
    objects[id]={pos=vec3(obj:getObjectCenterPositionXYZ(id)),dirVec=vec3(obj:getObjectDirectionVectorXYZ(id)),
      dirVecUp=vec3(obj:getObjectDirectionVectorUpXYZ(id)),vel=o.vel,width=obj:getObjectInitialWidth(id),
      length=obj:getObjectInitialLength(id),height=obj:getObjectInitialHeight(id)}
  end end
  local conflict=M.crossing(ego,objects,threat)
  if conflict then
    if not owned then controller.setFreeze(1);owned=true;holds=holds+1 end
    threat=conflict
  else release()end
end

function M.updateGFX(dt)
  if owned and controller and not controller.isFrozen then owned,threat,recent,suspended=false,nil,0,false;return end
  if suspended then
    if ai and(ai.mode=='stop'or ai.mode=='disabled')then return end
    M.onReset() -- an external mode change did not resume through the owning helper
  end
  if not bound or not ai or(ai.mode~='manual'and ai.mode~='traffic')or not controller or not controller.setFreeze then
    if owned or recent>0 or suspended or probeError then M.onReset() end
    return
  end
  if owned then
    heldFor=heldFor+(dt or 0)
    if heldFor>=M.MAX_HOLD_S then release();recent=0;return end
  end
  if not owned then recent=math.max(0,recent-(dt or 0))end
  elapsed=elapsed+(dt or 0);if elapsed<M.SAMPLE_S then return end;elapsed=0
  local ok,err=pcall(probe);probeError=not ok and tostring(err)or nil
  -- If a held car's tracking is temporarily unavailable, retain its existing
  -- hold. Manual cancellation and vehicle reset still release it above.
  if not ok then return end
end
function M.status()return {bound=bound,holding=owned,heldFor=heldFor,suspended=suspended,recent=recent,threat=threat,holds=holds,error=probeError}end
return M
