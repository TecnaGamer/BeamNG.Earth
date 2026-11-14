-- Far server-planned signal parts. Shared fixed-material models, no TSStatics.
-- Lazy: merely requiring this file or constructing a backend creates nothing.
local M={}
local serial=0
local ROOT='/levels/beamng_earth/art/shapes/beamngEarth/groenpol/'
local function finite(v)return type(v)=='number' and v==v and math.abs(v)<1e8 end
function M.new()
  serial=serial+1
  local name='bneNlAssemblyForest_'..serial
  local forestId,epoch,types,frame=nil,nil,{},nil
  local function levelId()
    return scenetree.MissionGroup and scenetree.MissionGroup:getID()
  end
  local function liveForest()
    if not epoch or epoch~=levelId() then return nil end
    return forestId and scenetree.findObjectById(forestId)
  end
  local backend={}
  function backend.setFrame(p)
    if type(p)~='table' or #p~=7 then return false end
    for i=1,7 do if not finite(p[i]) then return false end end
    local n=p[4]^2+p[5]^2+p[6]^2+p[7]^2
    if math.abs(n-1)>1e-4 then return false end
    frame={unpack(p)}
    local f=liveForest()
    if f then f:setPosRot(unpack(frame)) end
    return true
  end
  function backend.create(part)
    local shape=part.shape
    if type(shape)~='string' or shape:sub(1,#ROOT)~=ROOT or shape:find('..',1,true)
        or not shape:match('^[%w_/%.-]+%.dae$') then return nil end
    local current=levelId()
    if not current or epoch and epoch~=current then return nil end
    if not (worldEditorCppApi and worldEditorCppApi.createObject) then return nil end
    local d=types[shape]
    if not d then
      d=worldEditorCppApi.createObject('TSForestItemData')
      if not d then return nil end
      d:setField('shapeFile',0,shape)
      for _,field in ipairs({'collidable','windScale','branchAmp','detailAmp'}) do d:setField(field,0,'0') end
      local n=0;for _ in pairs(types) do n=n+1 end
      d:registerObject(name..'_type_'..n)
      if Sim and Sim.getDataBlockSet then Sim.getDataBlockSet():addObject(d) end
      types[shape]=d
    end
    local f=liveForest()
    if not f then
      if forestId then return nil end -- never reuse a removed Forest's userdata
      f=createObject('Forest')
      if not f then return nil end
      f.canSave=false;f:registerObject(name)
      scenetree.MissionGroup:addObject(f)
      forestId=f:getID();epoch=current
      if frame then f:setPosRot(unpack(frame)) end
    end
    local data=f:getData()
    if not data then return nil end
    local p=part.pose;local m=MatrixF(true)
    for c=0,2 do local b=4+c*3;m:setColumn(c,vec3(p[b],p[b+1],p[b+2])) end
    m:setPosition(vec3(p[1],p[2],p[3]))
    local it=data:createNewItem(d,m,1)
    if not it then return nil end
    return {item=it,forest=forestId,epoch=epoch,active=true}
  end
  function backend.remove(handle)
    if not handle or not handle.active then return false end
    handle.active=false
    if handle.epoch~=levelId() or handle.forest~=forestId then return false end
    local f=liveForest()
    local data=f and f:getData()
    if not data then return false end
    data:removeItem(handle.item)
    return true
  end
  function backend.forgetLevel()
    -- Level teardown owns the engine objects. Do not delete a Forest with live
    -- physics references or retain its native data/item handles across maps.
    forestId,epoch,types,frame=nil,nil,{},nil
  end
  return backend
end
return M
