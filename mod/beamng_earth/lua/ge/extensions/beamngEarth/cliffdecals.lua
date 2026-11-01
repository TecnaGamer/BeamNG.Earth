-- D0206: projected fallen-rock patches bridging the terrain / cliff skin seam.
-- Installed game maps are copied privately; no proprietary texture is shipped.
local M = {}
local talus = require('beamngEarth/clifftalus')
local geotile = require('beamngEarth/geotile')
local clock = require('beamngEarth/stopwatch').clock()
M.ROOT = '/temp/beamngEarth/cliff_decals/'
M.SOURCE = '/assets/materials/decal/rock/nat_decal_rocks_01/t_fallen_rocks_01_decal'
M.RANGE = 180
M.LIMIT = 400
M.PER_FRAME = 2
-- Adjacent 7-9 m stamps still overlap generously at half-width spacing.
M.SPACING = .5
M.RESELECT_DISTANCE = 2
M.RETRY_MS = 1000
M.enabled = true
local templates, active, ready = {}, {}, false
local count = 0
local dirty, pending, nextPending, selectedAt = true, {}, 1, nil
local selections, candidateCount = 0, 0
local retryAt
local maps = { {'_b.color.dds','base.color.dds'}, {'_nm.normal.dds','normal.normal.dds'},
  {'_r.data.dds','roughness.data.dds'}, {'_ao.data.dds','ao.data.dds'}, {'_o.data.dds','opacity.data.dds'} }

function M.material(group)
  local tint = talus.tint(group)
  if not tint then return nil end
  local name = 'bne_cliff_decal_'..group
  -- Same exposure and mineral tint as the loose rocks. Keep this source's
  -- natural sediment colour; independent channel boosts turn lit seams lilac.
  local calibration = 2.4
  return { [name]={name=name,mapTo=name,class='Material',version=1.5,
    translucent=true,translucentZWrite=true,alphaRef=0,
    Stages={{baseColorMap=M.ROOT..'base.color.dds',normalMap=M.ROOT..'normal.normal.dds',
      roughnessMap=M.ROOT..'roughness.data.dds',ambientOcclusionMap=M.ROOT..'ao.data.dds',
      opacityMap=M.ROOT..'opacity.data.dds',metallicFactor=0,roughnessFactor=1,useAnisotropic=true,
      baseColorFactor={tint[1]*calibration,tint[2]*calibration,tint[3]*calibration,1}}, {},{},{} } } }
end

local function template(group)
  if templates[group] then return templates[group] end
  local material = M.material(group)
  if not material then return nil end
  if not ready then
    FS:directoryCreate(M.ROOT)
    local changed = {}
    for _, map in ipairs(maps) do
      local dst = M.ROOT..map[2]
      if not FS:fileExists(dst) then
        FS:copyFile(M.SOURCE..map[1],dst)
        if not FS:fileExists(dst) then return nil end
        changed[#changed+1] = dst
      end
    end
    if #changed>0 and FS.triggerFilesChanged then FS:triggerFilesChanged(changed) end
    ready = true
  end
  local name = 'bne_cliff_decal_'..group
  local path = M.ROOT..group..'.materials.json'
  writeFile(path,jsonEncode(material))
  loadJsonMaterialsFile(path)
  local obj = scenetree.findObject(name..'_data')
  if not obj then
    obj = createObject('DecalData')
    obj:setField('Material',0,name)
    obj:setField('size',0,'1')
    obj:setField('textureCoordCount',0,'0')
    obj:setField('clippingAngle',0,'80')
    obj:setField('renderPriority',0,'1')
    obj:setField('fadeStartPixelSize',0,'24')
    obj:setField('fadeEndPixelSize',0,'12')
    obj.canSave = false
    obj:registerObject(name..'_data')
  end
  templates[group] = obj
  return obj
end

local function remove(state, i)
  if state.instances[i] then
    Engine.Render.DecalMgr.removeDecal(state.instances[i])
    state.instances[i] = nil
    count = count-1
  end
end

function M.drop(key)
  local state = active[key]
  if not state then return end
  for i in pairs(state.instances) do remove(state,i) end
  active[key] = nil
  dirty = true
end

function M.invalidate()
  for key in pairs(active) do M.drop(key) end
  dirty, pending, nextPending, selectedAt = true, {}, 1, nil
  retryAt = nil
end

function M.setEnabled(on)
  on = on == true
  if M.enabled == on then return false end
  M.enabled = on
  if not on then M.invalidate() end
  return true
end

function M.reset()
  -- The level has already destroyed its decal manager's instances.
  active, templates, count = {}, {}, 0
  dirty, pending, nextPending, selectedAt = true, {}, 1, nil
  selections, candidateCount = 0, 0
  retryAt = nil
end

function M.placed(row, pl)
  local x,y,z,nx,ny,nz = row[2],row[3],row[4],row[5],row[6],row[7]
  if pl then
    x,y,z = geotile.apply(pl,x,y,z)
    local r = pl.r
    nx,ny,nz = r[1]*nx+r[2]*ny+r[3]*nz,r[4]*nx+r[5]*ny+r[6]*nz,r[7]*nx+r[8]*ny+r[9]*nz
  end
  return x,y,z,nx,ny,nz
end

local function cellKey(x,y,z) return x..':'..y..':'..z end

-- Only merge stamps on a similar surface: different ledges and opposite faces
-- can be close in space while needing independent seam coverage.
local function alreadyCovered(p, grid, gx, gy, gz)
  for x=gx-1,gx+1 do
    for y=gy-1,gy+1 do
      for z=gz-1,gz+1 do
        local bucket = grid[cellKey(x,y,z)]
        if bucket then
          for _,q in ipairs(bucket) do
            local dx,dy,dz = p.x-q.x,p.y-q.y,p.z-q.z
            local spacing = math.min(p.size,q.size)*M.SPACING
            if dx*dx+dy*dy+dz*dz < spacing*spacing
              and p.nx*q.nx+p.ny*q.ny+p.nz*q.nz > .9
              and math.abs(dx*q.nx+dy*q.ny+dz*q.nz)<.75
              and math.abs(dx*p.nx+dy*p.ny+dz*p.nz)<.75 then return true end
          end
        end
      end
    end
  end
  return false
end

local function selectPatches(player)
  retryAt = nil
  local candidates = {}
  local cell = 1
  for _,state in pairs(active) do
    for i,p in ipairs(state.points) do
      local d = (p.x-player.x)^2+(p.y-player.y)^2+(p.z-player.z)^2
      if d < M.RANGE^2 or (state.instances[i] and d < (M.RANGE+20)^2) then
        p.d = d*(state.instances[i] and .95 or 1)
        candidates[#candidates+1] = p
        cell = math.max(cell,p.size*M.SPACING)
      else
        remove(state,i)
      end
    end
  end
  table.sort(candidates,function(a,b)
    if a.d~=b.d then return a.d<b.d end
    if a.key~=b.key then return a.key<b.key end
    return a.i<b.i
  end)
  local grid, kept = {}, 0
  pending, nextPending = {}, 1
  for _,p in ipairs(candidates) do
    local gx,gy,gz = math.floor(p.x/cell),math.floor(p.y/cell),math.floor(p.z/cell)
    local covered = kept>=M.LIMIT or alreadyCovered(p,grid,gx,gy,gz)
    if covered then remove(p.state,p.i)
    else
      kept = kept+1
      local key = cellKey(gx,gy,gz)
      grid[key] = grid[key] or {}
      grid[key][#grid[key]+1] = p
      if not p.state.instances[p.i] then pending[#pending+1] = p end
    end
  end
  selectedAt = {x=player.x,y=player.y,z=player.z,range=M.RANGE,limit=M.LIMIT,spacing=M.SPACING}
  dirty, selections, candidateCount = false, selections+1, #candidates
end

function M.update(tiles, player, paused, gov)
  if not M.enabled then return end
  if not Engine or not Engine.Render or not Engine.Render.DecalMgr then return end
  for key, state in pairs(active) do
    local t = tiles[key]
    local pl = t and t.geo and t.rowPl and t.rowPl.pl
    if not t or t.ghost or t.cliff ~= state.receiver or t.cliffDecals ~= state.rows or pl ~= state.pl then M.drop(key) end
  end
  if paused or not player then return end
  for key,t in pairs(tiles) do
    if t.cliff and t.cliffDecals and not t.ghost and not active[key] then
      local state = {receiver=t.cliff,rows=t.cliffDecals,pl=t.geo and t.rowPl and t.rowPl.pl,instances={},points={}}
      active[key], dirty = state, true
      for i,row in ipairs(state.rows) do
        local x,y,z,nx,ny,nz = M.placed(row,state.pl)
        state.points[i] = {state=state,i=i,key=tostring(key),x=x,y=y,z=z,nx=nx,ny=ny,nz=nz,size=row[9]}
      end
    end
  end
  local at = selectedAt
  if dirty or not at or at.range~=M.RANGE or at.limit~=M.LIMIT or at.spacing~=M.SPACING
    or (retryAt and clock()>=retryAt)
    or (at.x-player.x)^2+(at.y-player.y)^2+(at.z-player.z)^2>=M.RESELECT_DISTANCE^2 then selectPatches(player) end
  local made = 0
  while nextPending<=#pending do
    local c = pending[nextPending]
    if made>=M.PER_FRAME or (gov and not gov:try('cliffDecal')) then break end
    nextPending = nextPending+1
    local started = clock()
    local row = c.state.rows[c.i]
    local data = template(row[1])
    local inst
    if data then
      -- Permanent runtime instance (flag 1), without the save-to-level flag 2.
      inst = Engine.Render.DecalMgr.addDecal(vec3(c.x,c.y,c.z),vec3(c.nx,c.ny,c.nz),row[8],data,row[9],-1,1,1)
      if inst then
        Engine.Render.DecalMgr.notifyDecalModified(inst)
        c.state.instances[c.i] = inst
        count = count+1
      end
    end
    -- Assets or receivers may still be loading. Retry without holding the
    -- streaming budget open or rebuilding every failed stamp every frame.
    if not inst and not retryAt then retryAt=clock()+M.RETRY_MS end
    made = made+1
    if gov then gov:charge('cliffDecal',clock()-started) end
  end
end

function M.hasWork() return M.enabled and nextPending<=#pending end

function M.status() return {enabled=M.enabled,instances=count,limit=M.LIMIT,range=M.RANGE,
  selections=selections,candidates=candidateCount,pending=math.max(0,#pending-nextPending+1)} end
return M
