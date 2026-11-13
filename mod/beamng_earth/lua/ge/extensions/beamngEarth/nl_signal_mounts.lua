-- Physical component layout only. Traffic controllers and per-lane AI routing stay separate.
local MAX_HEADS=3 -- hard construction limit; configuration cannot increase it
local M={MAX_CANTILEVER_HEADS=MAX_HEADS,MAX_CANTILEVER_LENGTH_M=12,MAX_LOW_HEAD_YAW_DEGREES=10,MAX_UPPER_HEAD_YAW_DEGREES=15}
local ok,catalog=pcall(require,'beamngEarth/nl_groenpol_catalog')
M.catalog=ok and catalog or nil
local function finite(n)return type(n)=='number' and n==n and math.abs(n)<math.huge end
local IDENTITY={1,0,0,0,1,0,0,0,1}

-- Interim horizontal reach threshold chosen for this game: 12 m. The limit counts
-- overhead lane heads; low repeaters have separate mounting points.
function M.supportKind(count,length,limit)
  if not finite(count) or count<1 or count%1~=0 then return nil,'invalid_head_count' end
  if not finite(length) or length<0 then return nil,'invalid_arm_length' end
  if limit~=nil and (not finite(limit) or limit<=0) then return nil,'invalid_length_limit' end
  if count>MAX_HEADS then return 'portal','more_than_three_heads' end
  if limit and length>=limit then return 'portal','length_limit_reached' end
  return 'cantilever',limit and 'within_configured_limit' or 'length_limit_unset'
end

function M.plan(heads,options,data)
  options=options or {};data=data or M.catalog
  if type(heads)~='table' or #heads==0 then return {ready=false,reason='no_heads',parts={}} end
  local n=#heads;local reach=options.arm_length_m or 0
  local limit=options.max_cantilever_length_m
  if limit==nil then limit=M.MAX_CANTILEVER_LENGTH_M end
  local kind,reason=M.supportKind(n,reach,limit)
  local function fail(why)return {ready=false,kind=kind,reason=why,parts={},head_count=n}end
  if not kind then return fail(reason) end
  if not data then return fail('component_catalog_unavailable') end
  if options.kind=='straight' then
    if n~=1 or reach~=0 then return fail('invalid_straight_layout') end
    kind,reason='straight','single_pole_cap'
  elseif options.kind and options.kind~='portal' and options.kind~='cantilever' then return fail('unknown_support_kind') end
  if options.kind=='portal' then kind,reason='portal','explicit_portal' end
  local selected
  for key,s in pairs(data.supports)do
    if s.kind==kind and (s.max_heads>=n or kind=='portal') and (kind~='cantilever' or s.max_heads<=MAX_HEADS)then
      local rank=s.max_heads>=n and s.max_heads or 1000-s.max_heads
      if not selected or rank<selected.rank or rank==selected.rank and key<selected.key then
        selected={key=key,asset=s,rank=rank}
      end
    end
  end
  if not selected then return fail('no_matching_'..kind..'_asset') end
  local support=selected.asset
  local farthest=0;local ids={};local placements={}
  for i,h in ipairs(heads)do
    local head=data.heads[h.variant or 'vehicle'];local mount=support.mounts[i]
    -- Reuse the source socket frame beyond the prefab's count on a procedural bar.
    if not mount and kind=='portal'then mount=support.mounts[#support.mounts]end
    if not head or not mount or head.interface~=mount.interface then return fail('incompatible_head_or_mount') end
    local id=h.id or tostring(i)
    if ids[id] then return fail('duplicate_head_id') end;ids[id]=true
    local distance=h.distance_m or -mount.position[2]
    if kind=='straight' then distance=0
    elseif not finite(distance) or distance<=0 then return fail('invalid_mount_distance') end
    farthest=math.max(farthest,distance)
    placements[i]={head=head,mount=mount,id=id,distance=distance}
  end
  local span=options.left_span_m or math.max(options.carriageway_width_m or 6.6,farthest)+2
  if not finite(span) or span<=0 or span>40 then return fail('invalid_roadside_span') end
  if kind=='cantilever' and math.max(reach,farthest)>=limit then
    local copy={};for k,v in pairs(options)do copy[k]=v end;copy.kind='portal'
    local p=M.plan(heads,copy,data);p.reason=p.ready and 'length_limit_reached' or p.reason;return p
  end
  if kind~='straight' and not support.construction then return fail('component_catalog_outdated') end
  if kind=='portal' and (farthest>=span-.3 or reach>span) then return fail('head_outside_portal_span') end
  local includeLeft=options.include_left~=false
  if kind=='portal' and not includeLeft then return fail('no_safe_portal_left_foot')end
  local leftZ=options.left_height_delta_m or 0
  local plan={ready=true,kind=kind,reason=reason,head_count=n,overhead_count=kind=='straight' and 0 or n,
    support=selected.key,parts={},mounts={},feet={{0,0,0},{0,-span,0}},left_span_m=span,
    arm_length_m=kind=='cantilever' and farthest or nil,length_limit_configured=true}
  local function part(role,shape,position,basis,key,extra)
    local p={role=role,shape=shape,position=position,basis=basis or IDENTITY,key=key}
    for k,v in pairs(extra or {})do p[k]=v end
    plan.parts[#plan.parts+1]=p;return p
  end
  local cap=data.supports.straight.mounts[1].position[3]
  if kind=='straight' then
    part('support',support.shape,{0,0,0},nil,'support_right')
  else
    local c=support.construction
    part('support',c.leg_shape,{0,0,0},nil,'support_right')
    local length=kind=='portal' and span or farthest
    part('beam',c.beam_shape,{0,0,0},nil,'beam',{scale={1,length,1}})
  end
  for i,h in ipairs(heads)do
    local p=placements[i];local m=p.mount
    local position=kind=='straight' and {0,0,cap} or {m.position[1],-p.distance,m.position[3]}
    if m.hardware and kind~='straight' then
      local hardware=data.hardware[m.hardware]
      if not hardware then return fail('mount_hardware_unavailable') end
      part('hardware',hardware.shape,position,m.basis,'bracket_'..p.id)
    end
    part('head',p.head.shape,position,m.basis,'head_'..p.id,{head_id=p.id,lane_id=h.lane_id,participant=p.head.participant})
    plan.mounts[#plan.mounts+1]={head_id=p.id,lane_id=h.lane_id,slot=m.id,position=position,basis=m.basis}
  end
  for i=1,n do for j=i+1,n do
    local a,b=placements[i],placements[j]
    local aw=a.head.bounds_m[2][2]-a.head.bounds_m[1][2];local bw=b.head.bounds_m[2][2]-b.head.bounds_m[1][2]
    if math.abs(a.distance-b.distance)<(aw+bw)/2 then return fail('overlapping_heads') end
  end end
  if includeLeft then
  local leftShape=data.supports.straight.shape
  if kind=='portal' then leftShape=support.construction.leg_shape end
  local leftSupport=part('support',leftShape,{0,-span,leftZ},nil,'support_left')
  if kind=='portal' and math.abs(leftZ)>.001 then
    local height=support.construction.leg_bounds_m and support.construction.leg_bounds_m[2][3] or support.bounds_m[2][3]
    leftSupport.scale={1,1,(height-leftZ)/height}
  end
  local leftVariant=options.left_variant or heads[1].variant or 'vehicle'
  local left=data.heads[leftVariant];if not left then return fail('incompatible_repeat_variant') end
  local leftSocket={0,-span,cap+leftZ}
  if kind=='portal' then
    local low=data.low_repeat
    if not low then return fail('low_bracket_unavailable') end
    part('hardware',low.hardware_shape,{0,-span,leftZ},nil,'low_bracket_left')
    leftSocket={low.position[1],low.position[2]-span,low.position[3]+leftZ}
  end
  part('head',left.shape,leftSocket,nil,'head_left',{head_id='left_repeat',participant=left.participant,repeat_head=true})
  end
  if kind~='straight' then
    local low=data.low_repeat
    if not low then return fail('low_bracket_unavailable') end
    local right=data.heads[options.right_variant or heads[n].variant or 'vehicle']
    part('hardware',low.hardware_shape,{0,0,0},nil,'low_bracket_right')
    part('head',right.shape,{low.position[1],low.position[2],low.position[3]},nil,'head_right_repeat',
      {head_id='right_repeat',participant=right.participant,repeat_head=true})
  end
  if not includeLeft then plan.feet[2]=nil end
  plan.total_heads=n+(includeLeft and 1 or 0)+(kind~='straight' and 1 or 0)
  if kind=='portal'then
    local used={}
    for _,repeatPole in ipairs(options.roadside_repeaters or {})do
      local side,pos=repeatPole.side,repeatPole.position
      if (side~='left'and side~='right')or used[side]or type(pos)~='table'
        or not(finite(pos[1])and finite(pos[2])and finite(pos[3]))then return fail('invalid_roadside_repeater')end
      used[side]=true
      local variant=side=='left'and(options.left_variant or heads[1].variant)or(options.right_variant or heads[n].variant)
      local head=data.heads[variant or 'vehicle'];if not head then return fail('incompatible_repeat_variant')end
      local key=side..'_near_repeat'
      part('support',data.supports.straight.shape,{pos[1],pos[2],pos[3]},nil,'support_'..key)
      part('head',head.shape,{pos[1],pos[2],pos[3]+cap},nil,'head_'..key,
        {head_id=key,participant=head.participant,repeat_head=true})
      plan.feet[#plan.feet+1]={pos[1],pos[2],pos[3]}
      plan.total_heads=plan.total_heads+1
      -- The nearby standing repeater replaces this side's attached low lamp.
      local headKey=side=='left'and'head_left'or'head_right_repeat'
      local bracketKey=side=='left'and'low_bracket_left'or'low_bracket_right'
      local keep={}
      for _,p in ipairs(plan.parts)do
        if p.key~=headKey and p.key~=bracketKey then keep[#keep+1]=p
        elseif p.key==headKey then plan.total_heads=plan.total_heads-1 end
      end
      plan.parts=keep
    end
  end
  return plan
end

-- The socket is at the head origin. Transform a complete 3D mount frame into the current
-- world's flat-yaw support frame. This keeps the authored overhead hanger tilt intact.
function M.worldPose(root,part)
  local c,s=math.cos(math.rad(root.yaw or 0)),math.sin(math.rad(root.yaw or 0))
  local p=part.position;local b=part.basis or IDENTITY
  local r={}
  for col=0,2 do r[col+1]=c*b[col+1]-s*b[col+4];r[col+4]=s*b[col+1]+c*b[col+4];r[col+7]=b[col+7] end
  return {x=root.x+c*p[1]-s*p[2],y=root.y+s*p[1]+c*p[2],z=root.z+p[3],basis=r}
end

-- Compose two independently grounded roadside supports. Reflect only the steel's
-- yaw and mounting positions: lane arrows and front faces keep facing the driver.
function M.alternatives(heads,feet,data)
  data=data or M.catalog
  local out={};local n=#heads
  if not data or not feet or not feet.right or not feet.left or n<2 or n>6 then return out end
  local function group(first,last,side,standing)
    local foot=feet[side];local subset={}
    for i=first,last do
      local h=heads[i];local distance=side=='right'and h.distance_m+foot.position[2]or -foot.position[2]-h.distance_m
      subset[#subset+1]={id=h.id,lane_id=h.lane_id,variant=h.variant,distance_m=standing and 0 or distance}
    end
    local reach=0;for _,h in ipairs(subset)do reach=math.max(reach,h.distance_m)end
    if not standing and (reach<=0 or reach>=M.MAX_CANTILEVER_LENGTH_M)then return end
    local p=M.plan(subset,{kind=standing and'straight'or'cantilever',include_left=false,arm_length_m=reach,
      right_variant=side=='left'and subset[1].variant or nil},data)
    if not p.ready or not standing and p.kind~='cantilever'then return end
    for _,part in ipairs(p.parts)do
      local q=part.position
      part.position={q[1]+foot.position[1],(side=='left'and -q[2]or q[2])+foot.position[2],q[3]+foot.position[3]}
      if side=='left'and(part.role=='support'or part.role=='beam')then part.basis={-1,0,0,0,-1,0,0,0,1}end
      part.key=side..'_'..part.key
      if part.repeat_head then part.head_id=side..'_repeat'end
    end
    for _,mount in ipairs(p.mounts)do
      mount.position={mount.position[1]+foot.position[1],(side=='left'and -mount.position[2]or mount.position[2])+foot.position[2],mount.position[3]+foot.position[3]}
      mount.side=side;mount.standing=standing or false
    end
    return p,reach
  end
  local function add(k,standSide)
    local left,lr=group(1,k,'left',standSide=='left')
    local right,rr=group(k+1,n,'right',standSide=='right')
    if not left or not right then return end
    local p={ready=true,kind=standSide and'cantilever_and_pole'or'paired_cantilevers',support='split_'..k..'_'..(n-k)..(standSide or''),
      reason='independent_roadside_supports',parts={},mounts={},feet={feet.right.position,feet.left.position},
      head_count=n,overhead_count=left.overhead_count+right.overhead_count,total_heads=left.total_heads+right.total_heads,
      max_arm_length_m=math.max(lr,rr),arms={{side='left',heads=k,standing=standSide=='left'},
      {side='right',heads=n-k,standing=standSide=='right'}},length_limit_configured=true}
    for _,g in ipairs({right,left})do
      for _,part in ipairs(g.parts)do p.parts[#p.parts+1]=part end
      for _,m in ipairs(g.mounts)do p.mounts[#p.mounts+1]=m end
    end
    out[#out+1]=p
  end
  if n==4 then
    if feet.left.edge_distance_m and feet.left.edge_distance_m<=3 then add(1,'left')end
    if feet.right.edge_distance_m and feet.right.edge_distance_m<=3 then add(3,'right')end
  end
  for k=1,n-1 do if k<=3 and n-k<=3 then add(k)end end
  table.sort(out,function(a,b)
    if a.kind~=b.kind then return a.kind=='cantilever_and_pole'end
    if a.max_arm_length_m~=b.max_arm_length_m then return a.max_arm_length_m<b.max_arm_length_m end
    return a.support<b.support
  end)
  return out
end

function M.mirrorLeft(plan)
  for _,p in ipairs(plan.parts or{})do
    p.position={p.position[1],-p.position[2],p.position[3]}
    if p.role=='support'or p.role=='beam'then p.basis={-1,0,0,0,-1,0,0,0,1}end
  end
  for _,m in ipairs(plan.mounts or{})do m.position={m.position[1],-m.position[2],m.position[3]};m.side='left'end
  plan.support='left_'..plan.support;plan.mirrored=true
  return plan
end
function M.aimAt(part,root,target)
  local c,s=math.cos(math.rad(root.yaw)),math.sin(math.rad(root.yaw))
  local lx=c*(target[1]-root.x)+s*(target[2]-root.y)-part.position[1]
  local ly=-s*(target[1]-root.x)+c*(target[2]-root.y)-part.position[2]
  if part.aim_limit_degrees then
    local angle=math.atan2(ly,lx);local limit=math.rad(part.aim_limit_degrees)
    angle=math.max(-limit,math.min(limit,angle));local reach=math.sqrt(lx*lx+ly*ly)
    lx,ly=reach*math.cos(angle),reach*math.sin(angle)
  end
  if lx*lx+ly*ly>1 then
    local b=part.basis;local up=lx*b[3]+ly*b[6]
    local x,y,z=lx-up*b[3],ly-up*b[6],-up*b[9]
    if part.aim_limit_degrees and math.abs(b[9])>1e-6 then
      -- Preserve the authored tilt without letting its plane projection turn
      -- the requested horizontal road bearing beyond the limit again.
      x,y,z=lx,ly,-(b[3]*lx+b[6]*ly)/b[9]
    end
    local ca,sa=x*b[1]+y*b[4]+z*b[7],x*b[2]+y*b[5]+z*b[8]
    local len=math.sqrt(ca*ca+sa*sa)
    if len>1e-8 then
      ca,sa=ca/len,sa/len
      -- Swivel about the authored socket axis, keeping the head on its foot.
      part.basis={ca*b[1]+sa*b[2],-sa*b[1]+ca*b[2],b[3],
        ca*b[4]+sa*b[5],-sa*b[4]+ca*b[5],b[6],ca*b[7]+sa*b[8],-sa*b[7]+ca*b[8],b[9]}
      part.aim_target={target[1],target[2]}
    end
  end
end
function M.aimHeads(plan,root,a,lane)
  if not(lane.heads and #lane.heads>0)then return end
  local yaw=math.rad(root.yaw);local c,s=math.cos(yaw),math.sin(yaw)
  local len=math.sqrt(a[5]^2+a[6]^2);local dx,dy=a[5]/len,a[6]/len
  local back=math.min(20,math.max(8,lane.incoming_node and
    ((a[2]-lane.incoming_node[1])*dx+(a[3]-lane.incoming_node[2])*dy)*.6 or 20))
  back=math.max(back,-((root.x-a[2])*dx+(root.y-a[3])*dy)+10)
  local first,last=lane.heads[1],lane.heads[#lane.heads]
  for _,part in ipairs(plan.parts or{})do if part.role=='head'then
    part.aim_limit_degrees=(part.repeat_head or part.position[3]<4)and M.MAX_LOW_HEAD_YAW_DEGREES or M.MAX_UPPER_HEAD_YAW_DEGREES
    local selected
    for _,h in ipairs(lane.heads)do if h.lane_id==part.lane_id then selected=h;break end end
    selected=selected or (part.key:find('left')and first or last)
    -- Aim at the corresponding incoming lane before its controller stop point.
    -- Only the head's basis changes; the steel and all socket positions stay put.
    local lateral=selected.original_distance_m or selected.distance_m
    local tx=a[8]-dy*lateral-dx*back;local ty=a[9]+dx*lateral-dy*back
    local source=(a[8]-a[2])*dx+(a[9]-a[3])*dy
    tx,ty=tx-dx*source,ty-dy*source
    M.aimAt(part,root,{tx,ty})
  end end
end
function M.signature(plan)
  local out={plan.support or ''}
  for _,p in ipairs(plan.parts or {})do
    out[#out+1]=p.role..':'..p.shape
    for _,n in ipairs(p.position)do out[#out+1]=string.format('%.7f',n) end
    for _,n in ipairs(p.basis)do out[#out+1]=string.format('%.7f',n) end
    for _,n in ipairs(p.scale or {1,1,1})do out[#out+1]=string.format('%.7f',n) end
  end
  return table.concat(out,'|')
end
return M
