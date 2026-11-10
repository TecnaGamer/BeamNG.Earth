-- Facility panels use the existing road tile lifecycle. Endpoint rows permit true slope
-- rotation and longitudinal scaling without squashing height or copying the stock mesh.
local M = {}
M.MODEL = '/levels/Industrial/art/shapes/buildings/ind_mesh_fence_4m.dae'
M.COLLISION = 'Collision Mesh' -- stock Colmesh-1 (80 triangles), not the visible chain-link mesh
M.onCollide, M.clearForCollision = nil, nil -- core supplies the batched rebuild and vehicle guard
M.DRAW_M, M.UNLOAD_M, M.BUDGET = 600, 700, 12
M.made, M.failed, M.removed = 0, 0, 0
M.diagnostics = false -- enable from the GE console; status() is always queryable
local tiles, registry, ready, missing = {}, {}, false, false
local reports = {}

local function materials()
  if ready then return true end
  if not FS:fileExists(M.MODEL) then
    if not missing then log('E', 'beamngEarth.perimeters', 'missing required model: ' .. M.MODEL); missing = true end
    return false
  end
  for _, p in ipairs({ '/levels/Industrial/art/shapes/buildings/main.materials.json',
      '/levels/Industrial/art/shapes/main.materials.json',
      '/levels/west_coast_usa/art/shapes/objects/main.materials.json',
      '/art/shapes/garage_and_dealership/main.materials.json',
      '/art/shapes/objects/main.materials.json' }) do
    if FS:fileExists(p) then
      local ok, err = pcall(loadJsonMaterialsFile, p)
      if not ok then log('E','beamngEarth.perimeters','materials ' .. p .. ': ' .. tostring(err)) end
    end
  end
  ready = true
  return true
end

local function remove(panel)
  local o = panel.object and scenetree.findObjectById(panel.object)
  if o then o:delete(); M.removed = M.removed + 1; if panel.collisionReady and M.onCollide then M.onCollide() end end
  panel.object = nil
end

local function place(o, r, collide)
  local dx, dy, dz = r[4]-r[1], r[5]-r[2], r[6]-r[3]
  local horizontal = math.sqrt(dx*dx+dy*dy)
  local length = math.sqrt(dx*dx+dy*dy+dz*dz)
  if horizontal < 0.001 then error('degenerate perimeter panel') end
  local x = vec3(dx/length,dy/length,dz/length)
  local y = vec3(-dy/horizontal,dx/horizontal,0)
  local z = vec3(-dz*dx/(length*horizontal),-dz*dy/(length*horizontal),horizontal/length)
  local matrix = MatrixF(true)
  matrix:setColumn(0,x); matrix:setColumn(1,y); matrix:setColumn(2,z)
  matrix:setPosition(vec3((r[1]+r[4])/2,(r[2]+r[5])/2,(r[3]+r[6])/2))
  o:setTransform(matrix)
  o:setScale(vec3(length/4,1,1)) -- fabric runs x=-2..2; retain the stock height and thickness
  if o.updateInstanceRenderData then o:updateInstanceRenderData() end
  if collide and M.onCollide then M.onCollide() end
end

local function make(panel)
  local o = createObject('TSStatic')
  if not o then error('TSStatic creation returned nil') end
  -- Save immediately so failure cleanup never leaks a partially created object.
  panel.pending = o
  o:setField('shapeName',0,M.MODEL)
  panel.collisionReady = not M.clearForCollision or M.clearForCollision(panel.row)
  o:setField('collisionType',0,panel.collisionReady and M.COLLISION or 'None')
  o:setField('decalType',0,'None')
  o:setField('dynamic',0,'true')
  o:setField('isRenderEnabled',0,'true')
  o.useInstanceRenderData, o.canSave = true, false
  o:registerObject('')
  place(o,panel.row,panel.collisionReady)
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
  panel.object, panel.pending = o:getID(), nil
  M.made = M.made+1
end

function M.dropKey(key)
  local t = tiles[key]
  if t then for _,p in ipairs(t) do
    p.refs[key]=nil
    if not next(p.refs) then remove(p); registry[p.row[7]]=nil end
  end end
  tiles[key] = nil; reports[key] = nil
end

function M.onRows(key,rows,report)
  reports[key] = report
  -- Stable identities keep existing objects when a tile is replaced in the same frame.
  local old, nextRows = tiles[key] or {}, {}
  local seen = {}
  for _,r in ipairs(rows or {}) do
    if #r >= 7 and not seen[r[7]] then
      seen[r[7]] = true
      local p = registry[r[7]] or {refs={}}
      -- Copy wire rows: rebase transforms must never mutate cached road JSON.
      local copy={}; for i,v in ipairs(r) do copy[i]=v end
      p.row=copy; p.cx=(copy[1]+copy[4])/2; p.cy=(copy[2]+copy[5])/2
      p.refs[key]=true; registry[r[7]]=p
      if p.object then
        local o = scenetree.findObjectById(p.object)
        if o then place(o,copy,p.collisionReady) else p.object = nil end
      end
      nextRows[#nextRows+1] = p
    end
  end
  for _,p in ipairs(old) do if not seen[p.row[7]] then
    p.refs[key]=nil
    if not next(p.refs) then remove(p); registry[p.row[7]]=nil end
  end end
  tiles[key] = nextRows
  if M.diagnostics then
    log('I','beamngEarth.perimeters','tile=' .. tostring(key) .. ' received=' .. tostring(#nextRows)
      .. ' model=' .. M.MODEL .. ' rendering=UNVERIFIED')
  end
end

function M.transform(xf)
  for _,p in pairs(registry) do
    local r = p.row
    r[1],r[2],r[3] = xf:apply(r[1],r[2],r[3])
    r[4],r[5],r[6] = xf:apply(r[4],r[5],r[6])
    p.cx, p.cy = (r[1]+r[4])/2, (r[2]+r[5])/2
    local o = p.object and scenetree.findObjectById(p.object)
    if o then place(o,r,p.collisionReady) end
  end
end

-- Keep only the nearest frame's worth of panels in a max heap. The root is the
-- farthest retained candidate, so distant panels need no allocation or sorting.
local function offer(candidates, panel, distance, budget)
  if budget <= 0 then return end
  local n = #candidates
  if n < budget then
    local i = n+1
    local entry = {p=panel,d=distance}
    while i > 1 do
      local parent = math.floor(i/2)
      if candidates[parent].d >= distance then break end
      candidates[i] = candidates[parent]; i = parent
    end
    candidates[i] = entry
  elseif distance < candidates[1].d then
    local entry = candidates[1]
    entry.p, entry.d = panel, distance
    local i = 1
    while i*2 <= n do
      local child = i*2
      if child < n and candidates[child+1].d > candidates[child].d then child = child+1 end
      if candidates[child].d <= distance then break end
      candidates[i] = candidates[child]; i = child
    end
    candidates[i] = entry
  end
end

function M.update(allowCreates)
  if not next(tiles) or not materials() then return end
  local px,py = SFXSystem.getPositionXYZ()
  local candidates = {}
  local budget = allowCreates == false and 0 or math.max(0, math.floor(M.BUDGET))
  local draw2, unload2 = M.DRAW_M^2, M.UNLOAD_M^2
  for _,p in pairs(registry) do
    local r = p.row
    if p.object and not p.collisionReady and (not M.clearForCollision or M.clearForCollision(r)) then
      local o=scenetree.findObjectById(p.object)
      if o then
        o:setField('collisionType',0,M.COLLISION); p.collisionReady=true
        if M.onCollide then M.onCollide() end
      end
    end
    local distance = (p.cx-px)^2 + (p.cy-py)^2
    if distance > unload2 then
      if p.object then remove(p) end
    elseif budget > 0 and distance <= draw2 and not p.object and not p.failed then
      offer(candidates,p,distance,budget)
    end
  end
  if #candidates == 0 then return end
  table.sort(candidates,function(a,b) return a.d < b.d end)
  local started = os.clock()
  for i=1,math.min(M.BUDGET,#candidates) do
    if i>1 and os.clock()-started > .002 then break end
    local c = candidates[i]
    local ok,err = pcall(make,c.p)
    if not ok then
      if c.p.pending then pcall(function() c.p.pending:delete() end); c.p.pending=nil end
      c.p.failed=true; M.failed=M.failed+1
      log('E','beamngEarth.perimeters','create failed id=' .. tostring(c.p.row[7]) .. ': ' .. tostring(err))
    end
  end
  if M.diagnostics and #candidates > 0 then
    local s=M.status()
    log('I','beamngEarth.perimeters','instantiated=' .. tostring(s.instantiated) .. ' failed=' .. tostring(s.failed)
      .. ' streaming=near-camera rendering=UNVERIFIED')
  end
end

-- Read-only inspection. Does not create, delete or reload anything. Diagnostic
-- wire suffixes provide facility/classification when enabled on the server.
function M.trace(source)
  local result = {}
  local px,py=SFXSystem.getPositionXYZ()
  local assetAvailable=FS:fileExists(M.MODEL)
  for id,p in pairs(registry) do
    local r=p.row
    if source == nil or source == r[8] then
      local owners={}; for key in pairs(p.refs) do owners[#owners+1]=key end
      table.sort(owners)
      local distance=math.sqrt(((r[1]+r[4])/2-px)^2+((r[2]+r[5])/2-py)^2)
      local instantiated=p.object ~= nil and scenetree.findObjectById(p.object) ~= nil
      local reason=instantiated and 'instantiated' or p.failed and 'creation_failed'
        or not assetAvailable and 'missing_asset' or distance > M.DRAW_M and 'outside_creation_distance'
        or 'awaiting_creation'
      result[#result+1]={id=id, source=r[8], facilityId=r[12], classification=r[14],
        owners=owners, endpoints={r[1],r[2],r[3],r[4],r[5],r[6]},
        instantiated=instantiated, reason=reason, distance=distance,
        objectId=p.object, failed=p.failed == true,collisionReady=p.collisionReady == true,
        collisionReason=instantiated and (p.collisionReady and 'enabled; engine rebuild unverified' or 'awaiting_vehicle_clearance') or 'not_instantiated'}
    end
  end
  table.sort(result,function(a,b) return a.id < b.id end)
  return result
end

function M.tileTrace(key)
  return reports[key]
end

function M.status()
  local planned,live,areas,references = 0,0,{},0
  for _,p in pairs(registry) do
    planned=planned+1
    for _ in pairs(p.refs) do references=references+1 end
    local source=p.row[8] or 'unknown'
    local area=areas[source] or {received=0,instantiated=0,failed=0}
    area.received=area.received+1
    if p.object then live=live+1; area.instantiated=area.instantiated+1 end
    if p.failed then area.failed=area.failed+1 end
    areas[source]=area
  end
  return { planned=planned, instantiated=live, made=M.made, failed=M.failed, removed=M.removed,
    tileReferences=references, sharedReferences=references-planned, loadDistance=M.DRAW_M, unloadDistance=M.UNLOAD_M,
    areas=areas, model=M.MODEL, collisionType=M.COLLISION,collision='UNVERIFIED', rendering='UNVERIFIED', streaming='road-tiles', missingModel=missing }
end

function M.reset()
  for key in pairs(tiles) do M.dropKey(key) end
  ready, missing = false,false
end
return M
