-- Own every piece of an assembly together. Only heads are native signal objects.
local M={}
local mounts=require('beamngEarth/nl_signal_mounts')
local shared={}
function M.sharedCount(key)return shared[key]and #shared[key].refs or 0 end
local heights={};for _,s in pairs(mounts.catalog.supports)do
  heights[s.shape]=s.bounds_m[2][3]
  if s.construction then heights[s.construction.leg_shape]=s.construction.leg_bounds_m[2][3]end
end
local function signature(h)
  local parts={};for _,entry in ipairs(h.parts)do parts[#parts+1]=entry.localPose end
  h.signature=mounts.signature({support=h.support,parts=parts})
  if h.saved_plan then h.saved_plan.signature=h.signature end
end
local function localPoint(h,p)
  local c,s=math.cos(math.rad(h.yaw)),math.sin(math.rad(h.yaw))
  return{c*(p[1]-h.x)+s*(p[2]-h.y),-s*(p[1]-h.x)+c*(p[2]-h.y),(p[3]or h.z)-h.z}
end
local function mountGroup(part)
  if part.mount_group then return part.mount_group end
  local key=part.key or''
  if part.role=='support'then return key end
  if key:find('^left_')then return'left_support_right'end
  if key:find('^right_')then return'right_support_right'end
  if key:find('_near_repeat$')then return key:gsub('^head_','support_')end
  if key=='head_left'or key=='low_bracket_left'then return'support_left'end
  return'support_right'
end

function M.delete(h)
  for _,change in ipairs(h.foreign_changes or{})do
    local o=scenetree.findObjectById(change.id)
    if o and change.part.localPose.paired_by==h.instance then
      o:setField('shapeName',0,change.old_shape);change.part.localPose.shape=change.old_shape;change.part.localPose.paired_by=nil
      change.part.localPose.position=change.old_position;change.part.localPose.basis=change.old_basis
      signature(change.owner)
    end
  end
  for _,part in ipairs(h.parts or {})do
    local row=part.shared_key and shared[part.shared_key]
    if row then
      for i=#row.refs,1,-1 do if row.refs[i].part==part then table.remove(row.refs,i)end end
      if #row.refs==0 then local o=scenetree.findObjectById(row.id);if o then o:delete()end;shared[part.shared_key]=nil
      else M.move(row.refs[1].owner)end
    else local o=scenetree.findObjectById(part.id);if o then o:delete()end end
  end
end

function M.reconcile(h)
  local keep,missingPartner={},false
  for _,change in ipairs(h.foreign_changes or{})do
    if scenetree.findObjectById(change.id)then keep[#keep+1]=change
    elseif change.mount_partner then missingPartner=true end
  end
  if #keep==#(h.foreign_changes or{})then return end
  h.foreign_changes=keep
  local entries,poses={},{}
  for _,entry in ipairs(h.parts)do local key=entry.localPose.key
    if missingPartner and(key=='shared_double_shield'or key=='shared_mount_other')then
      local o=scenetree.findObjectById(entry.id);if o then o:delete()end
    else entries[#entries+1]=entry;poses[#poses+1]=entry.localPose end
  end
  h.parts=entries;h.saved_plan.parts=poses
  local specs={}
  for _,spec in ipairs(h.saved_plan.foreign_changes or{})do
    for _,change in ipairs(keep)do if change.owner_name==spec.owner and change.key==spec.key then specs[#specs+1]=spec;break end end
  end
  h.saved_plan.foreign_changes=specs
  if missingPartner then h.saved_plan.partner_unloaded=true end
  signature(h);M.move(h)
end
function M.alive(h)
  for _,part in ipairs(h.parts or {})do
    if not scenetree.findObjectById(part.id) then return false end
  end
  return h.id~=nil
end

function M.move(h)
  if h.saved_plan then
    h.saved_plan.x,h.saved_plan.y,h.saved_plan.z,h.saved_plan.yaw=h.x,h.y,h.z,h.yaw
    if h.stopLocal then local q=mounts.worldPose(h,{position=h.stopLocal,basis={1,0,0,0,1,0,0,0,1}});h.saved_plan.stop={q.x,q.y,q.z}end
    h.saved_plan.dir={-math.cos(math.rad(h.yaw)),-math.sin(math.rad(h.yaw))}
    if h.saved_plan.placement then
      local feet={};for _,part in ipairs(h.parts)do if part.role=='support'then local p=mounts.worldPose(h,part.localPose);feet[#feet+1]={p.x,p.y,p.z}end end
      h.saved_plan.placement.feet=feet
    end
    if h.saved_plan.shared_support then
      for _,entry in ipairs(h.parts)do if entry.role=='support'and entry.localPose.share_with then
        local q=mounts.worldPose(h,entry.localPose);h.saved_plan.shared_support.foot={q.x,q.y,q.z};break
      end end
    end
  end
  for _,part in ipairs(h.parts)do
    local o=scenetree.findObjectById(part.id)
    if o then
      local m=MatrixF(true)
      if h.model=='stock' then
        m:setFromEuler(vec3(0,0,-math.rad(h.yaw)))
        m:setPosition(vec3(h.x,h.y,h.z))
      else
        local owner,pose=h,part.localPose
        local row=part.shared_key and shared[part.shared_key]
        if row then
          local best=-math.huge
          for _,ref in ipairs(row.refs)do
            local lp=ref.part.localPose;local top=ref.owner.z+lp.position[3]+(heights[lp.shape]or 0)*(lp.scale and lp.scale[3]or 1)
            if top>best then best=top;owner,pose=ref.owner,lp end
          end
          o:setField('shapeName',0,pose.shape)
        end
        local p=mounts.worldPose(owner,pose);local b=p.basis
        for col=0,2 do m:setColumn(col,vec3(b[col+1],b[col+4],b[col+7])) end
        m:setPosition(vec3(p.x,p.y,p.z))
        if row then local scale=pose.scale or{1,1,1};o:setScale(vec3(unpack(scale)))end
      end
      o:setTransform(m)
      if not part.shared_key and part.localPose and part.localPose.scale then
        local s=part.localPose.scale;o:setScale(vec3(s[1],s[2],s[3]))
      end
    end
  end
end

function M.adjustGround(h,updates)
  local moved={}
  for _,update in ipairs(updates)do
    local target=h.parts[update.index]
    local row=target.shared_key and shared[target.shared_key]
    local refs=row and row.refs or{{owner=h,part=target}}
    for _,ref in ipairs(refs)do
      local owner,key=ref.owner,ref.part.localPose.key
      local q=mounts.worldPose(owner,ref.part.localPose);local delta=update.z-q.z
      if math.abs(delta)>.001 then
        for _,entry in ipairs(owner.parts)do if mountGroup(entry.localPose)==key then entry.localPose.position[3]=entry.localPose.position[3]+delta end end
        for _,r in ipairs(refs)do for _,change in ipairs(r.owner.foreign_changes or{})do
          if change.owner==owner and mountGroup(change.part.localPose)==key then change.old_position={change.old_position[1],change.old_position[2],change.old_position[3]+delta}end
        end end
        moved[owner]=true
      end
    end
  end
  for owner in pairs(moved)do
    if owner.saved_plan.layout.kind=='portal'then
      local right,left
      for _,entry in ipairs(owner.parts)do
        if entry.localPose.key=='support_right'then right=entry.localPose elseif entry.localPose.key=='support_left'then left=entry.localPose end
      end
      if right and left then
        local top=right.position[3]+heights[right.shape]*(right.scale and right.scale[3]or 1)
        left.scale={1,1,(top-left.position[3])/heights[left.shape]}
      end
    end
    signature(owner);M.move(owner)
  end
end

function M.create(p,inst,others)
  local h={x=p.x,y=p.y,z=p.z,yaw=p.yaw,shape=p.shape,signature=p.signature,model=p.model,parts={},instance=inst.name,foreign_changes={},
    support=p.layout and p.layout.support,saved_plan=p,shared=p.shared_support~=nil}
  if p.stop then h.stopLocal=localPoint(h,p.stop)end
  local parts=p.parts or {{role='head',shape=p.shape}}
  local created={}
  local headCount=0;for _,part in ipairs(parts)do if part.role=='head' then headCount=headCount+1 end end
  -- Lane heads of ONE incoming approach follow its existing native phase together.
  -- Independent turn phases require additional routing/controller data, never inferred here.
  if headCount~=1 and not (p.lane_plan and p.lane_plan.synchronized) then return nil,'multiple heads require an explicit lane plan' end
  local ok,err=pcall(function()
    for _,part in ipairs(parts)do
      local o
      if part.role=='head' then
        o=inst:createSignalObject(part.shape,vec3(p.x,p.y,p.z),quat(0,0,0,1))
        if o then created[#created+1]=o end
      elseif part.shared_key and part.share_with then
        local key=part.shared_key;local row=shared[key]
        if not row then
          local other=others and others[part.share_with.owner];local target
          for _,entry in ipairs(other and other.parts or{})do if entry.localPose.key==part.share_with.key then target=entry;break end end
          assert(target and scenetree.findObjectById(target.id),'shared support disappeared')
          target.shared_key=key;target.localPose.shared_key=key
          other.shared=true
          if part.role=='support'then
            for _,entry in ipairs(other.parts)do local lp=entry.localPose
              if lp.role=='beam'and (lp.position[1]-target.localPose.position[1])^2+(lp.position[2]-target.localPose.position[2])^2<.1^2 then lp.shared_key=key end
            end
          end
          row={id=target.id,refs={{owner=other,part=target}}};shared[key]=row
        end
        o=scenetree.findObjectById(row.id)
        local entry={id=row.id,localPose=part,role=part.role,shared_key=key}
        h.parts[#h.parts+1]=entry;row.refs[#row.refs+1]={owner=h,part=entry}
      else
        o=createObject('TSStatic')
        if o then
          created[#created+1]=o
          -- Own it before any fallible setup, so partial failures leave no stray mast.
          h.parts[#h.parts+1]={id=o:getID(),localPose=part,role=part.role}
          o:setField('shapeName',0,part.shape)
          o:setField('collisionType',0,'None');o:setField('decalType',0,'None')
          o.canSave=false
          o.useInstanceRenderData=false
          o:registerObject('')
          scenetree.MissionGroup:addObject(o)
        end
      end
      if not o then error('Unable to create '..part.role..': '..part.shape) end
      o:setField('bneSignalRole',0,part.role)
      o:setField('bneSignalPart',0,part.key or part.role)
      if part.role=='head' then
        h.parts[#h.parts+1]={id=o:getID(),localPose=part,role=part.role};h.id=o:getID()
        if p.model~='stock' then
          for _,field in ipairs({'instanceColor','instanceColor1','instanceColor2'})do o:setField(field,0,'0 0 0 1') end
        end
      end
    end
    M.move(h)
    local movedOwners={}
    for _,change in ipairs(p.foreign_changes or{})do
      local other=others and others[change.owner];local target
      for _,entry in ipairs(other and other.parts or{})do if entry.localPose.key==change.key then target=entry;break end end
      assert(target and scenetree.findObjectById(target.id),'paired head disappeared')
      local o=scenetree.findObjectById(target.id)
      h.foreign_changes[#h.foreign_changes+1]={id=target.id,part=target,owner=other,old_shape=target.localPose.shape,
        old_position=target.localPose.position,old_basis=target.localPose.basis,owner_name=change.owner,key=change.key,mount_partner=change.shared_key~=nil}
      target.localPose.shape=change.shape;target.localPose.paired_by=inst.name
      if change.position then target.localPose.position=change.position end
      if change.basis then target.localPose.basis=change.basis end
      if change.shared_key then target.localPose.shared_key=change.shared_key end
      o:setField('shapeName',0,change.shape);signature(other)
      movedOwners[other]=true
    end
    -- Foreign heads and their feet must move together on creation, without
    -- waiting for a later navgraph refresh. Their controller objects stay live.
    for owner in pairs(movedOwners)do M.move(owner)end
  end)
  if not ok then
    M.delete(h)
    -- Includes objects whose setup failed before registration in the scene tree.
    for _,o in ipairs(created)do pcall(o.delete,o) end
    return nil,err
  end
  return h
end

function M.visualObject(_,shape,pos)
  local o=createObject('TSStatic');if not o then return end
  o.useInstanceRenderData=false;o.canSave=false
  o:setField('shapeName',0,shape);o:setField('collisionType',0,'None');o:setField('decalType',0,'None')
  o:registerObject('');scenetree.MissionGroup:addObject(o);return o
end

function M.syncVisual(h)
  local c,s=h.visualController,h.visualSequence
  local state=c and s and s.controllerStates and s.controllerStates[c.name]
  local base=state and c.states and c.states[state.stateIdx]
  local data=base and c.getStateData and c:getStateData(base.state)
  if not data or not data.lights then return false end
  local signature=table.concat(data.lights,'|');if h.visualState==signature then return true end
  h.visualState=signature
  for _,part in ipairs(h.parts)do if part.role=='head' then
    local o=scenetree.findObjectById(part.id)
    if o then for i,key in ipairs({'instanceColor','instanceColor1','instanceColor2'})do
      o:setField(key,0,data.lights[i] and data.lights[i]~='black' and '1 1 1 1' or '0 0 0 1')
    end end
  end end
  return true
end
return M
