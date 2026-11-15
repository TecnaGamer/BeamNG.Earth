-- Stock traffic requests a minimum look-ahead distance. At a streamed boundary the forward road can be shorter
-- than that distance, so its planner chooses a longer road behind the car. Keep the unique forward approach to
-- an artificial dead end until the next graph arrives or the existing GE boundary guard recycles the car.
-- The same speed-policy owner also yields before green turns across approaching traffic. Native traffic
-- handles red lights and stop signs, but a green crossing turn otherwise relies on late collision avoidance.
local M = {}
M.turnYieldEnabled = false
local cuts = {}
local installed, originalPath, wrappedPath, originalSetMap, wrappedSetMap
M.DECEL_MSS, M.STOP_BUFFER_M, M.SAMPLE_S = 2.5, 8, 0.1
local savedSpeed, savedMode, speedCap, remaining, elapsed, holds = nil, nil, nil, nil, 0, 0
local planned, first = {}, 1
local committed, pending, expectFresh, continuations = nil, nil, true, 0
local MAX_ROUTE_NODES = 512
M.TURN_BUFFER_M, M.TURN_LOOKAHEAD_M, M.TURN_GAP_S = 3, 80, 5
local turnDistance, turnThreat
local reportedStop

-- The ignored final edge can put this stop beyond GE's geometric cut radius.
-- Report the validated native reason; ordinary signal/following stops stay alone.
local function reportCutStop(cut)
  if not cut and not reportedStop then return end
  local p=cut and obj and obj.getPosition and obj:getPosition()
  if cut and not p then return end
  if cut and reportedStop and reportedStop[1]==cut
    and (p.x-reportedStop[2])^2+(p.y-reportedStop[3])^2+(p.z-reportedStop[4])^2<1 then return end
  reportedStop=cut and {cut,p.x,p.y,p.z} or nil
  if obj and obj.queueGameEngineLua and obj.getId then
    obj:queueGameEngineLua(string.format("require('beamngEarth/navgraph').onTrafficCutStop(%d,%q,%.8f,%.8f,%.8f)",
      obj:getId(),cut or '',p and p.x or 0,p and p.y or 0,p and p.z or 0))
  end
end

local function releaseSpeed()
  reportCutStop(nil)
  if speedCap ~= nil and ai.routeSpeed == speedCap and ai.speedMode == 'limit' then
    ai.setSpeed(savedSpeed)
    ai.setSpeedMode(savedMode)
  end
  savedSpeed, savedMode, speedCap = nil, nil, nil
end

local function legal(e, from)
  return (e.drivability or 1) > 0 and (not e.oneWay or e.inNode == from)
end

-- The stock AI discards its route when mapData changes. Retain a still-legal planned turn instead of letting
-- a fresh random path choose another exit when the car is already too close to brake for it.
function M.continuation(md, path, start, origin, heading)
  if not path or path[1] == start or not origin or not heading then return nil end
  local index
  for i = 2, #path do
    if path[i] == start then
      if index then return nil end -- repeated starts are ambiguous without the engine's consumed-path cursor
      index = i
    end
  end
  if not index or index == #path then return nil end
  local previous = path[index - 1]
  local e = md.graph[previous] and md.graph[previous][start]
  local a, b = md.positions[previous], md.positions[start]
  if not e or not legal(e, previous) or not a or not b then return nil end
  local entryDirection = e.inNode == previous and '+' or '-'
  if e.lanes and not e.lanes:find(entryDirection, 1, true) then return nil end
  local dx, dy, dz = b.x - a.x, b.y - a.y, b.z - a.z
  local lenSq = dx * dx + dy * dy
  if lenSq < 0.01 or dx * heading.x + dy * heading.y < 0.5 * math.sqrt(lenSq) then return nil end
  local t = ((origin.x - a.x) * dx + (origin.y - a.y) * dy) / lenSq
  local ra, rb = e.inRadius, e.outRadius
  if e.inNode == start then ra, rb = rb, ra end
  ra, rb = ra or (md.radius and md.radius[previous]) or 3, rb or (md.radius and md.radius[start]) or 3
  local radius = ra + t * (rb - ra)
  if t < 0 or t > 1 or (origin.x - a.x - t * dx)^2 + (origin.y - a.y - t * dy)^2 > radius * radius
    or math.abs(origin.z - a.z - t * dz) > 2 then return nil end
  local out = {start}
  for i = index + 1, #path do
    local from, to = path[i - 1], path[i]
    local link = md.graph[from] and md.graph[from][to]
    if not link or not legal(link, from) or not md.positions[to] then break end
    local direction = link.inNode == from and '+' or '-'
    if link.lanes and not link.lanes:find(direction, 1, true) then break end
    out[#out + 1] = to
  end
  if #out > 1 then return out end
end

local function retain(path, extending)
  if expectFresh and not extending then
    committed = {}
  elseif not extending or not committed or committed[#committed] ~= path[1] then
    committed = nil -- a later fresh query can be an unaccepted reroute; do not cache speculative intent
  end
  expectFresh = false
  if not committed then return end
  for i = #committed == 0 and 1 or 2, #path do committed[#committed + 1] = path[i] end
  if #committed > MAX_ROUTE_NODES then committed = nil end
end

-- Returns a short forward path only on an unbranched road ending at a known streaming cut. Real dead ends,
-- junction choices, one-way restrictions and routes with enough look-ahead remain the stock planner's work.
-- An illegal stock reversal may retry through a starting junction with one unambiguous forward exit.
function M.shortPath(md, start, origin, minimum, state, ends, initialFork)
  if not md.graph[start] or not state or not minimum or minimum <= 0 then return nil end
  local previous, heading
  if type(state) == 'table' and state[1] then
    if state[#state] ~= start or #state < 2 then return nil end
    previous = state[#state - 1]
  else
    heading = state
    if not heading.x or not heading.y then return nil end
  end
  local path, seen, node = {start}, {}, start
  origin = origin or md.positions[start]
  while node and not seen[node] do
    seen[node] = true
    local links, p = md.graph[node], md.positions[node]
    if not links or not p then return nil end
    local count, only = 0, nil
    for id in pairs(links) do count, only = count + 1, id end
    if count > 2 and not (initialFork and node == start and not previous) then return nil end
    if ends[node] and count == 1 then
      local facingEnd = previous == only
      if not previous and heading then
        local q = md.positions[only]
        local dx, dy = q.x - p.x, q.y - p.y
        facingEnd = dx * heading.x + dy * heading.y < -0.5 * math.sqrt(dx * dx + dy * dy)
      end
      if facingEnd then
        local dx, dy, dz = p.x - origin.x, p.y - origin.y, p.z - origin.z
        if dx * dx + dy * dy + dz * dz < minimum * minimum then return path end
        return nil
      end
    end
    local nextNode, score, forward = nil, 0.5, 0
    for id, e in pairs(links) do
      if id ~= previous and legal(e, node) then
        if previous then nextNode = id
        else
          local q = md.positions[id]
          local dx, dy = q.x - p.x, q.y - p.y
          local dot = (dx * heading.x + dy * heading.y) / math.max(1e-30, math.sqrt(dx * dx + dy * dy))
          if dot > 0.5 then forward = forward + 1 end
          if dot > score then nextNode, score = id, dot end
        end
      end
    end
    if count > 2 and forward ~= 1 then return nil end
    if not nextNode then return nil end
    previous, node = node, nextNode
    path[#path + 1] = node
    local q = md.positions[node]
    if not q then return nil end
    local dx, dy, dz = q.x - origin.x, q.y - origin.y, q.z - origin.z
    if dx * dx + dy * dy + dz * dz >= minimum * minimum then return nil end
  end
end

-- Stock traffic omits the final node without an outgoing continuation. Follow
-- the path it actually selected, including junction choices and planned loops.
function M.distanceToPlanEnd(md, path, firstIndex, a, b, p, heading, ends)
  local last=path[#path]
  local links=last and md.graph[last]
  if not links or not ends[last] then return nil end
  local previous=next(links)
  if not previous or next(links,previous) or (#path>1 and path[#path-1]~=previous) then return nil end
  if not legal(links[previous],previous) then return nil end
  local pa, pb = md.positions[a], md.positions[b]
  if not pa or not pb or not (md.graph[a] and md.graph[a][b]) then return nil end
  local dx, dy, dz = pb.x-pa.x, pb.y-pa.y, pb.z-pa.z
  local horizontal = math.sqrt(dx*dx+dy*dy)
  if horizontal < 0.1 then return nil end
  local dot = (dx*heading.x+dy*heading.y)/horizontal
  if math.abs(dot) < 0.5 then return nil end
  if dot < 0 then a,b,pa,pb=b,a,pb,pa;dx,dy,dz=-dx,-dy,-dz end
  if not legal(md.graph[a][b],a) then return nil end
  local t = ((p.x-pa.x)*dx+(p.y-pa.y)*dy)/(horizontal*horizontal)
  local sideSq = (p.x-pa.x-t*dx)^2+(p.y-pa.y-t*dy)^2
  local radius = math.max(md.radius and md.radius[a] or 3,md.radius and md.radius[b] or 3)
  if sideSq > (radius+0.5)^2 or math.abs(p.z-pa.z-t*dz)>4 then return nil end
  local index
  for i=firstIndex or 1,#path-1 do
    if path[i]==a and path[i+1]==b then index=i;break end
  end
  -- Fresh traffic paths can begin at the forward waypoint, omitting the
  -- occupied edge. Include that approach before summing the selected suffix.
  if not index and #path>1 and (firstIndex or 1)==1 and path[1]==b and path[2]~=a then index=0 end
  if not index then
    if #path==1 and b==last and a==previous then return 0,1 end
    return nil -- a nearby road outside the selected path cannot impose a limit
  end
  if index==#path-1 then return 0,index end
  local distance = math.max(0,1-t)*math.sqrt(dx*dx+dy*dy+dz*dz)
  for i=index+1,#path-2 do
    local e=md.graph[path[i]] and md.graph[path[i]][path[i+1]]
    if not e or not legal(e,path[i]) then return nil end
    local q,r=md.positions[path[i]],md.positions[path[i+1]]
    distance=distance+math.sqrt((r.x-q.x)^2+(r.y-q.y)^2+(r.z-q.z)^2)
  end
  return distance,index
end

-- Use the selected path and public signal/object data; never infer a turn from a nearby road.
-- Return the distance to stop before entering the opposing flow, only for a green crossing turn.
function M.turnYieldDistance(md, path, firstIndex, a, b, p, heading, objects, signals, leftHand, egoId)
  if not signals or not path or not path[2] then return nil end
  local pa, pb = md.positions[a], md.positions[b]
  local edge = md.graph[a] and md.graph[a][b]
  if not pa or not pb or not edge then return nil end
  local dx, dy = pb.x-pa.x, pb.y-pa.y
  local length = math.sqrt(dx*dx+dy*dy)
  if length < .1 or math.abs(dx*heading.x+dy*heading.y) < .5*length then return nil end
  if dx*heading.x+dy*heading.y < 0 then a,b,pa,pb=b,a,pb,pa;dx,dy=-dx,-dy end
  if not legal(edge,a) then return nil end
  local t=((p.x-pa.x)*dx+(p.y-pa.y)*dy)/(length*length)
  local radius=math.max(edge.inRadius or md.radius[a] or 3,edge.outRadius or md.radius[b] or 3)
  if t < 0 or t > 1.2 or (p.x-pa.x-t*dx)^2+(p.y-pa.y-t*dy)^2 > (radius+.5)^2
    or math.abs(p.z-pa.z-t*(pb.z-pa.z)) > 3 then return nil end
  local index
  for i=firstIndex or 1,#path-1 do if path[i]==a and path[i+1]==b then index=i;break end end
  if not index and (firstIndex or 1)==1 and path[1]==b and path[2]~=a then index=0 end
  if not index then return nil end
  local distance=math.max(0,1-t)*length
  for i=index+1,#path-1 do
    if distance > M.TURN_LOOKAHEAD_M then return nil,nil,index end
    local from=i==1 and a or path[i-1]
    local node,to=path[i],path[i+1]
    local q,r=md.positions[node],md.positions[to]
    local incoming=md.positions[from]
    local link=md.graph[node] and md.graph[node][to]
    if not q or not r or not incoming or not link or not legal(link,node) then return nil,nil,index end
    local ix,iy=q.x-incoming.x,q.y-incoming.y
    local ox,oy=r.x-q.x,r.y-q.y
    local il,ol=math.sqrt(ix*ix+iy*iy),math.sqrt(ox*ox+oy*oy)
    if il < .1 or ol < .1 then return nil,nil,index end
    ix,iy,ox,oy=ix/il,iy/il,ox/ol,oy/ol
    local signal=signals[from] and signals[from][node] and signals[from][node][1]
    if signal and signal.action==0 and signal.pos and (ix*oy-iy*ox)*(leftHand and -1 or 1)>.35
      and ix*ox+iy*oy>-.5 then
      local stop=distance-((q.x-signal.pos.x)*ix+(q.y-signal.pos.y)*iy)-M.TURN_BUFFER_M
      if stop < -2 then return nil,nil,index end -- already committed: do not stop in the opposing lane
      local reach=math.max(5,md.radius[node] or 3)+3
      for id,other in pairs(objects or {}) do
        local pos,vel,dir=other.pos,other.vel,other.dirVec
        if tonumber(id)~=egoId and pos and vel and dir and dir.x*ix+dir.y*iy<-.5 then
          local speedSq=vel.x*vel.x+vel.y*vel.y
          local ahead=(pos.x-q.x)*ix+(pos.y-q.y)*iy
          if speedSq>1 and ahead>-(other.length or 5)*.5-3 then
            local arrival=((q.x-pos.x)*vel.x+(q.y-pos.y)*vel.y)/speedSq
            if arrival>=-1 and arrival<=M.TURN_GAP_S then
              local ex,ey=pos.x+vel.x*arrival-q.x,pos.y+vel.y*arrival-q.y
              local z=pos.z+(vel.z or 0)*arrival
              if ex*ex+ey*ey<reach*reach and math.abs(z-q.z)<3 then return stop,id,index end
            end
          end
        end
      end
      return nil,nil,index
    end
    distance=distance+ol
  end
  return nil,nil,index
end

local function remember(path, extending)
  if not path or not path[1] then return end
  if not extending or planned[#planned]~=path[1] then planned,first={},1 end
  for i=(#planned==0 and 1 or 2),#path do planned[#planned+1]=path[i] end
end

local function install()
  local md = mapmgr.mapData
  if not md or md == installed then return false end
  releaseSpeed()
  pending, committed, expectFresh = committed, nil, true
  planned,first={},1
  installed, originalPath = md, md.getPathTWithState
  local original = originalPath
  wrappedPath = function(self, start, origin, minimum, state)
    local extending=type(state)=='table' and state[1]~=nil
    -- A held endpoint has no previous segment. Stock path history requires two nodes;
    -- use the vehicle heading to retry safely, including after the road extends.
    if ai.mode == 'traffic' and type(state) == 'table' and #state == 1 then
      state = obj:getDirectionVector()
    end
    local path
    if ai.mode=='traffic' then
      if pending and not extending then
        path=M.continuation(self,pending,start,origin,state)
        if path then continuations=continuations+1 end
      end
      pending=nil -- only the first query on this replacement map may continue the previous route
    end
    path=path or (ai.mode=='traffic' and next(cuts) and M.shortPath(self,start,origin,minimum,state,cuts))
    path=path or original(self,start,origin,minimum,state)
    if ai.mode=='traffic' and path and path[2] then
      local edge=self.graph[path[1]] and self.graph[path[1]][path[2]]
      -- Stock's minimum look-ahead can choose an illegal reversal when the
      -- forward road ends at a stream cut just past a starting junction.
      if edge and edge.oneWay and edge.inNode~=path[1] then
        path=M.shortPath(self,start,origin,minimum,state,cuts,true) or path
      end
    end
    if ai.mode=='traffic' then remember(path,extending);retain(path,extending) end
    return path
  end
  md.getPathTWithState = wrappedPath
  return true
end

function M.setCuts(ids)
  -- GE sends the next boundary before the vehicle receives its replacement map. Keep
  -- known cuts while their nodes remain in the active map; otherwise that delay briefly
  -- exposes the old dead end to stock routing. Extended cuts have two neighbours and
  -- shortPath already ignores them. Pruning bounds this history to the local graph.
  local md = mapmgr.mapData
  for id in pairs(cuts) do
    if not (md and md.graph[id]) then cuts[id] = nil end
  end
  for _, id in ipairs(ids or {}) do cuts[id] = true end
  -- Clear a plan made before the first protected-map install (e.g. a pool car's
  -- late acknowledgement). Repeated cuts do not reinstall; replacement maps are
  -- installed inside setMap below, so normal graph handoffs do not reset the AI.
  if install() and ai.mode == 'traffic' then ai.reset() end
end

function M.onExtensionLoaded()
  if obj and obj.queueGameEngineLua then
    obj:queueGameEngineLua("local S=beamngEarth_settings or package.loaded['beamngEarth/settings'];if S and be then local v=be:getObjectByID("..obj:getId()..");if v then v:queueLuaCommand(\"if beamngEarth_trafficBoundary and beamngEarth_trafficBoundary.setTurnYieldEnabled then beamngEarth_trafficBoundary.setTurnYieldEnabled(\"..tostring(S.get('trafficTurnYield')==true)..\") end\")end end")
  end
  originalSetMap = mapmgr.setMap
  wrappedSetMap = function(...)
    local metadata = obj:getLastMailbox('beamngEarthTrafficCuts')
    if metadata and metadata ~= '' then M.setCuts(lpack.decode(metadata)) end
    originalSetMap(...)
    install() -- install before the AI sees the replacement graph, not on the next graphics frame
  end
  mapmgr.setMap = wrappedSetMap
end

function M.setTurnYieldEnabled(on)
  M.turnYieldEnabled = on == true
  if not M.turnYieldEnabled then
    if turnDistance and not remaining then releaseSpeed() end
    turnDistance, turnThreat = nil, nil
  end
  elapsed = M.SAMPLE_S
end

function M.onExtensionUnloaded()
  releaseSpeed()
  if mapmgr.setMap == wrappedSetMap then mapmgr.setMap = originalSetMap end
  if installed and installed.getPathTWithState == wrappedPath then installed.getPathTWithState = originalPath end
end

function M.updateGFX(dt)
  if ai.mode ~= 'traffic' or not installed or mapmgr.mapData ~= installed then
    if committed or pending or not expectFresh or remaining or turnDistance or speedCap or planned[1] then
      committed,pending,expectFresh=nil,nil,true
      remaining,turnDistance,turnThreat=nil,nil,nil;planned,first={},1;releaseSpeed()
    end
    return
  end
  elapsed=elapsed+(dt or 0)
  if elapsed<M.SAMPLE_S then return end
  elapsed=0
  local ok,distance,legalSpeed,yieldDistance,threat=pcall(function()
    local p,heading=obj:getFrontPosition(),obj:getDirectionVector()
    local a,b=mapmgr.findBestRoad(p,heading)
    if not a or not b then return nil end
    local distance,index=M.distanceToPlanEnd(installed,planned,first,a,b,p,heading,cuts)
    local yieldDistance,threat,turnIndex
    if M.turnYieldEnabled then
      yieldDistance,threat,turnIndex=M.turnYieldDistance(installed,planned,first,a,b,p,heading,
        mapmgr.getObjects and mapmgr.getObjects(),mapmgr.signalsData,mapmgr.rules and mapmgr.rules.rightHandDrive,
        obj.getId and obj:getId())
    end
    -- Advance only from a validated occupied approach, including ordinary roads.
    -- A later visit to the same edge must use the turn from that lap of a loop.
    index=index or turnIndex
    if index then
      first=math.max(1,index-1)
      if first>16 then
        local tail={};for i=first-1,#planned do tail[#tail+1]=planned[i] end
        planned,first=tail,2
      end
    end
    local edge=installed.graph[a] and installed.graph[a][b]
    return distance,edge and edge.speedLimit,yieldDistance,threat
  end)
  remaining=ok and distance or nil
  turnDistance,turnThreat=ok and yieldDistance or nil,ok and threat or nil
  if not remaining and not turnDistance then releaseSpeed();return end
  if speedCap~=nil and (ai.routeSpeed~=speedCap or ai.speedMode~='limit') then
    savedSpeed,savedMode,speedCap=nil,nil,nil -- a newer external policy owns these fields
  end
  local stop=remaining and remaining-M.STOP_BUFFER_M or math.huge
  if turnDistance then stop=math.min(stop,turnDistance) end
  local cap=math.sqrt(2*M.DECEL_MSS*math.max(0,stop))
  if speedCap==nil then
    if cap>=obj:getVelocity():length()+1 then return end
    savedSpeed,savedMode=ai.routeSpeed,ai.speedMode;holds=holds+1
  end
  if type(savedSpeed)=='number' and savedSpeed>=0 then cap=math.min(cap,savedSpeed) end
  if (savedMode==nil or savedMode=='legal') and type(legalSpeed)=='number' and legalSpeed>0 then
    cap=math.min(cap,legalSpeed)
  end
  speedCap=cap;ai.setSpeed(cap);ai.setSpeedMode('limit')
  reportCutStop(remaining and remaining<=M.STOP_BUFFER_M and cap<.1 and obj:getVelocity():length()<.5
    and planned[#planned] or nil)
end

function M.onReset()
  releaseSpeed();planned,first={},1;elapsed=M.SAMPLE_S
  remaining,turnDistance,turnThreat=nil,nil,nil
  committed,pending,expectFresh=nil,nil,true
end
function M.status()
  return {turnYieldEnabled=M.turnYieldEnabled,holding=speedCap~=nil,speedCap=speedCap,remaining=remaining,holds=holds,continuations=continuations,
    turnDistance=turnDistance,turnThreat=turnThreat}
end

return M
