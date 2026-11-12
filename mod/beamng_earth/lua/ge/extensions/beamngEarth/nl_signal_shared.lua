-- Share verified mast feet; use the author's rigid mounting feet at quarter turns.
local M={MAX_FOOT_GAP_M=1.4}
local mounts=require('beamngEarth/nl_signal_mounts')
local catalog=mounts.catalog
local heads,beams={},{}
for _,h in pairs(catalog.heads)do heads[h.shape]=h;if h.bare_shape then heads[h.bare_shape]=h end end
for _,s in pairs(catalog.supports)do if s.construction then beams[s.construction.beam_shape]=s.construction.beam_bounds_m end end
local function copy(t)if type(t)~='table'then return t end;local r={};for k,v in pairs(t)do r[k]=copy(v)end;return r end
local function basis(angle)local c,s=math.cos(angle),math.sin(angle);return{c,-s,0,s,c,0,0,0,1}end
local function localPoint(p,x,y,z)
  local c,s=math.cos(math.rad(p.yaw)),math.sin(math.rad(p.yaw))
  return{c*(x-p.x)+s*(y-p.y),-s*(x-p.x)+c*(y-p.y),z-p.z}
end
local function localBasis(p,angle)return basis(angle-math.rad(p.yaw))end
local function lowHead(p,support)
  local best,dist
  for _,entry in ipairs(p.parts or{})do local part=entry.localPose or entry
    if part.role=='head'and part.position[3]-support.position[3]<4 and not part.paired_by then
      local d=(part.position[1]-support.position[1])^2+(part.position[2]-support.position[2])^2
      if d<1.5^2 and(not dist or d<dist)then best,dist=entry,d end
    end
  end;return best
end
local function shapeBounds(shape)
  for _,s in pairs(catalog.supports)do
    if s.shape==shape then return s.bounds_m end
    if s.construction and s.construction.leg_shape==shape then return s.construction.leg_bounds_m end
  end
end
local function boxBounds(b,pos,angle)
  local c,s=math.cos(angle),math.sin(angle)
  local cx,cy=(b[1][1]+b[2][1])/2,(b[1][2]+b[2][2])/2
  return{x=pos[1]+c*cx-s*cy,y=pos[2]+s*cx+c*cy,c=c,s=s,
    hx=(b[2][1]-b[1][1])/2,hy=(b[2][2]-b[1][2])/2,
    z0=pos[3]+b[1][3],z1=pos[3]+b[2][3]}
end
local function box(head,pos,angle)return boxBounds(head.bare_bounds_m,pos,angle)end
local function boxesClear(a,b)
  if a.z1+.015<=b.z0 or b.z1+.015<=a.z0 then return true end
  for _,axis in ipairs({{a.c,a.s},{-a.s,a.c},{b.c,b.s},{-b.s,b.c}})do
    local x,y=axis[1],axis[2]
    local ar=math.abs(x*a.c+y*a.s)*a.hx+math.abs(-x*a.s+y*a.c)*a.hy
    local br=math.abs(x*b.c+y*b.s)*b.hx+math.abs(-x*b.s+y*b.c)*b.hy
    if math.abs((a.x-b.x)*x+(a.y-b.y)*y)>=ar+br+.015 then return true end
  end
  return false
end
local function clearsPost(b,foot,top)
  if b.z0>=top-.002 then return true end -- a standing pole ends at its sockets
  local dx,dy=foot.x-b.x,foot.y-b.y
  local x=math.max(0,math.abs(dx*b.c+dy*b.s)-b.hx)
  local y=math.max(0,math.abs(-dx*b.s+dy*b.c)-b.hy)
  return x*x+y*y>=(.0725+.012)^2
end
local function shapePairClear(shape1,pos1,angle1,shape2,pos2,angle2,foot,top)
  local space=require('beamngEarth/nl_signal_space');local first,second={},{}
  for _,b in ipairs(space.headBounds(shape1))do local q=boxBounds(b,pos1,angle1);if not clearsPost(q,foot,top)then return false end;first[#first+1]=q end
  for _,b in ipairs(space.headBounds(shape2))do local q=boxBounds(b,pos2,angle2);if not clearsPost(q,foot,top)then return false end;second[#second+1]=q end
  for _,a in ipairs(first)do for _,b in ipairs(second)do if not boxesClear(a,b)then return false end end end
  return true
end
local function roadBearing(angle,root)
  local road=math.rad(root.yaw);local delta=math.atan2(math.sin(angle-road),math.cos(angle-road))
  local limit=math.rad(mounts.MAX_LOW_HEAD_YAW_DEGREES)
  return road+math.max(-limit,math.min(limit,delta))
end
local function pairMount(plan,current,other,entry,post,foot,top)
  local op=entry.localPose or entry;local h1,h2=heads[current.shape],heads[op.shape]
  if not(h1 and h2 and h1.bare_shape and h2.bare_shape)then return end
  local cp,po=mounts.worldPose(plan,current),mounts.worldPose(other,op)
  local ca,oa=math.atan2(cp.basis[4],cp.basis[1]),math.atan2(po.basis[4],po.basis[1])
  ca,oa=roadBearing(ca,plan),roadBearing(oa,other)
  local mechanical,mechanicalPart
  for _,e in ipairs(other.parts or{})do local p=e.localPose or e
    if p.role=='hardware'and p.key~='bracket_'and
      (p.position[1]-post.position[1])^2+(p.position[2]-post.position[2])^2<.01 and p.position[3]-post.position[3]<1 then
      local q=mounts.worldPose(other,p);mechanical=math.atan2(q.basis[4],q.basis[1]);mechanicalPart=e;break
    end
  end
  local bases=mechanical and{mechanical}or{math.rad(other.yaw)+math.pi/2,math.rad(other.yaw)-math.pi/2,math.rad(other.yaw)}
  -- A co-facing pair may use opposite sides of the original foot instead of
  -- retaining a forward-facing foot that leaves the two housings in tandem.
  -- Rotate both original feet together; their relative slots remain fixed.
  local bearing=math.abs(math.atan2(math.sin(ca-oa),math.cos(ca-oa)))
  local roadA,roadB=math.rad(plan.yaw),math.rad(other.yaw)
  local roadDelta=math.atan2(math.sin(roadA-roadB),math.cos(roadA-roadB))
  local aligned=math.abs(roadDelta)<math.rad(15)
  local common=roadB+roadDelta/2
  if aligned and not mechanical then bases={common+math.pi/2,common-math.pi/2,common}end
  if mechanical and bearing<math.rad(15)then
    bases[#bases+1]=mechanical+math.pi/2;bases[#bases+1]=mechanical-math.pi/2;bases[#bases+1]=mechanical+math.pi
  end
  local socket=catalog.low_repeat.position
  local function at(angle)local c,s=math.cos(angle),math.sin(angle);return{foot.x+c*socket[1]-s*socket[2],foot.y+s*socket[1]+c*socket[2],foot.z+socket[3]}end
  -- The original opposed Cylinder.053 sits on the upright of Cylinder.052.
  -- Lower the stand-off by the authored bar-to-socket distance, keeping both
  -- lanterns at the normal low mounting height. No mesh is stretched.
  local dual=catalog.double_mount
  if dual and mechanicalPart and math.abs(roadDelta)<math.rad(120)and top>foot.z+socket[3]+.1 then
    local angle=common
    local doublePart,doublePose
    for _,e in ipairs(other.parts or{})do local part=e.localPose or e
      if part.role=='hardware'and part.shape==dual.hardware_shape then
        local pose=mounts.worldPose(other,part)
        if (pose.x-foot.x)^2+(pose.y-foot.y)^2<1 and math.abs(pose.z-foot.z)<.05 then
          doublePart,doublePose=e,pose;angle=math.atan2(pose.basis[4],pose.basis[1]);break
        end
      end
    end
    local c,s=math.cos(angle),math.sin(angle)
    local ox,oy=socket[1]-dual.bar_centre[1],socket[2]-dual.bar_centre[2]
    local origin=doublePose and{doublePose.x,doublePose.y,doublePose.z}or{foot.x+c*ox-s*oy,foot.y+s*ox+c*oy,foot.z}
    local function point(p)return{origin[1]+c*p[1]-s*p[2],origin[2]+s*p[1]+c*p[2],origin[3]+p[3]}end
    local p1,p2=point(dual.sockets[1]),point(dual.sockets[2])
    local ha,hb=aligned and common or ca,aligned and common or oa
    if doublePose then
      if (po.x-p1[1])^2+(po.y-p1[2])^2<(po.x-p2[1])^2+(po.y-p2[2])^2 then p1,p2=p2,p1 end
    elseif (p1[1]-p2[1])*(math.cos(ha)-math.cos(hb))+(p1[2]-p2[2])*(math.sin(ha)-math.sin(hb))<0 then p1,p2=p2,p1 end
    local shape1,shape2
    if aligned and h1.lens_diameter_mm==h2.lens_diameter_mm then
      if shapePairClear(h1.bare_shape,p1,ha,h2.bare_shape,p2,hb,foot,top)then shape1,shape2=h1.bare_shape,h2.bare_shape end
    else
      for _,shapes in ipairs({{h1.shape,h2.shape},{h1.shape,h2.bare_shape},{h1.bare_shape,h2.shape},{h1.bare_shape,h2.bare_shape}})do
        if shapePairClear(shapes[1],p1,ha,shapes[2],p2,hb,foot,top)then shape1,shape2=shapes[1],shapes[2];break end
      end
    end
    if shape1 then
      return{current=p1,other=p2,current_angle=ha,other_angle=hb,current_foot=angle-math.pi/2,other_foot=angle+math.pi/2,
        quarter=180,existing_foot=true,mechanical_part=mechanicalPart,h1=h1,h2=h2,compound=true,
        double_origin=origin,double_part=doublePart,bracket_angle=angle,
        stem_origin={foot.x,foot.y,foot.z-dual.stand_off_drop_m},current_shape=shape1,other_shape=shape2}
    end
  end
  -- Mounting fit cannot steer a low lantern away from its incoming road axis.
  -- First try a common road bearing for aligned approaches, then small swivels.
  local turns={{ca,oa}}
  if aligned then table.insert(turns,1,{common,common})end
  for _,d in ipairs({5,-5})do turns[#turns+1]={roadBearing(ca+math.rad(d),plan),oa};turns[#turns+1]={ca,roadBearing(oa+math.rad(d),other)}end
  local best,bestScore
  for _,delta in ipairs(turns)do for _,base in ipairs(bases)do
    for _,quarter in ipairs({90,180,270})do
      local angle=base+math.rad(quarter);local p1,p2=at(angle),at(base)
      local ha,hb=delta[1],delta[2]
      local a,b=box(h1,p1,ha),box(h2,p2,hb)
      if boxesClear(a,b)and clearsPost(a,foot,top)and clearsPost(b,foot,top)then
        local candidate={current=p1,other=p2,current_angle=ha,other_angle=hb,current_foot=angle,other_foot=base,
          quarter=quarter,existing_foot=mechanical~=nil,mechanical_part=mechanicalPart,h1=h1,h2=h2}
        local diff=math.abs(math.atan2(math.sin(ha-hb),math.cos(ha-hb)))
        local along=math.abs((p1[1]-p2[1])*math.cos(hb)+(p1[2]-p2[2])*math.sin(hb))
        local score=0
        if diff<.001 and along<.01 and h1.lens_diameter_mm==h2.lens_diameter_mm then
          candidate.current_shape,candidate.other_shape=h1.bare_shape,h2.bare_shape;score=2
        else
          for _,shapes in ipairs({{h1.shape,h2.shape,2},{h1.shape,h2.bare_shape,1},{h1.bare_shape,h2.shape,1},{h1.bare_shape,h2.bare_shape,0}})do
            if shapePairClear(shapes[1],p1,ha,shapes[2],p2,hb,foot,top)then
              candidate.current_shape,candidate.other_shape=shapes[1],shapes[2];score=shapes[3];break
            end
          end
        end
        if candidate.current_shape and(not bestScore or score>bestScore)then best,bestScore=candidate,score end
        if bestScore==2 then return best end
      end
    end
  end end
  return best
end
function M.candidates(plan,others,ignore,frame)
  if plan.omit or not plan.layout or(plan.layout.kind~='straight'and plan.layout.kind~='cantilever')then return{}end
  local choices={}
  for _,part in ipairs(plan.parts)do if part.role=='support'and part.key=='support_right'then
    local pos=mounts.worldPose(plan,part)
    for name,other in pairs(others or{})do
      local layout=other.saved_plan and other.saved_plan.layout or other.layout
      if name~=ignore and other.model~='stock'and layout and(layout.kind=='straight'or layout.kind=='cantilever'
        or layout.kind=='portal'and plan.layout.kind=='straight')then
        for _,entry in ipairs(other.parts or{})do local op=entry.localPose or entry
          if op.role=='support'and(not op.shared_key or require('beamngEarth/nl_signal_scene').sharedCount(op.shared_key)==1)then
            local q=mounts.worldPose(other,op);local d=(pos.x-q.x)^2+(pos.y-q.y)^2
            if (d>.001 or op.shared_key)and d<=M.MAX_FOOT_GAP_M^2 and math.abs(pos.z-q.z)<.15 then
              local stop,dir=plan.stop,plan.dir;local len=math.sqrt(dir[1]^2+dir[2]^2)
              local along=((q.x-stop[1])*dir[1]+(q.y-stop[2])*dir[2])/len
              if along>=-20 and along<=3 then choices[#choices+1]={name=name,other=other,part=op,own=part,pos=q,d=d}end
            end
          end
        end
      end
    end
  end end
  table.sort(choices,function(a,b)if a.d~=b.d then return a.d<b.d end;return a.name<b.name end)
  local out={}
  for _,choice in ipairs(choices)do
    local p=copy(plan);local support
    for _,part in ipairs(p.parts)do if part.key==choice.own.key then support=part;break end end
    local q=choice.pos;local at=localPoint(p,q.x,q.y,q.z)
    local probe=require('beamngEarth/nl_signal_surfaces').begin(q.x,q.y,q.z,frame)
    local safe,z=true,q.z;if probe then safe,z=probe(q.x,q.y,q.z)end
    if safe and math.abs(z-q.z)<.04 then
      local current,otherHead=lowHead(p,choice.own),lowHead(choice.other,choice.part)
      local ownBounds,otherBounds=shapeBounds(support.shape),shapeBounds(choice.part.shape)
      local top=q.z+math.max(ownBounds[2][3],otherBounds[2][3])
      local pair=current and otherHead and pairMount(p,current,choice.other,otherHead,choice.part,q,top)
      if pair then
        local sharedKey=choice.part.shared_key or(choice.name..':'..choice.part.key)
        local old=copy(support.position);local dz=at[3]-old[3];support.position=at
        support.shared_key=sharedKey;support.share_with={owner=choice.name,key=choice.part.key}
        local valid=true;local beam
        for _,part in ipairs(p.parts)do if part.role=='beam'then beam=part;break end end
        if beam then
          local b=beam.basis;local length=beam.scale[2]
          local ex,ey=beam.position[1]-b[2]*length,beam.position[2]-b[5]*length
          local vx,vy=ex-at[1],ey-at[2];local reach=math.sqrt(vx*vx+vy*vy)
          if reach>=mounts.MAX_CANTILEVER_LENGTH_M then valid=false
          else
            local rotation=math.atan2(vy,vx)+math.pi/2;local nb=basis(rotation)
            local oldAngle=math.atan2(b[4],b[1]);local da=rotation-oldAngle;local c,s=math.cos(da),math.sin(da)
            for _,part in ipairs(p.parts)do if part~=beam and part~=support and part.position[3]>4 then
              local x,y=part.position[1]-beam.position[1],part.position[2]-beam.position[2]
              local t=math.max(0,math.min(1,-(x*b[2]+y*b[5])/length))
              local side=x*b[1]+y*b[4]
              part.position={at[1]+vx*t+nb[1]*side,at[2]+vy*t+nb[4]*side,part.position[3]+dz}
              if part.role=='hardware'or part.role=='head'then local pb=part.basis;local r={}
                for k=1,3 do r[k]=c*pb[k]-s*pb[k+3];r[k+3]=s*pb[k]+c*pb[k+3];r[k+6]=pb[k+6]end;part.basis=r
              end
              if part.role=='head'and part.aim_target then
                part.aim_limit_degrees=mounts.MAX_UPPER_HEAD_YAW_DEGREES;mounts.aimAt(part,p,part.aim_target)
              end
            end end
            beam.position={at[1],at[2],at[3]};beam.basis=nb;beam.scale={1,reach,1};beam.shared_key=sharedKey
          end
        end
        if valid then
          current.position=localPoint(p,unpack(pair.current));current.basis=localBasis(p,pair.current_angle)
          current.shape=pair.current_shape;current.pair_with=choice.name..':'..(otherHead.localPose or otherHead).key
          current.shared_key=sharedKey;support.pair_with=current.pair_with
          local op=otherHead.localPose or otherHead
          p.foreign_changes={{owner=choice.name,key=op.key,id=otherHead.id,shape=pair.other_shape,
            position=localPoint(choice.other,unpack(pair.other)),basis=localBasis(choice.other,pair.other_angle),shared_key=sharedKey}}
          if pair.mechanical_part and not pair.compound then
            local mount=pair.mechanical_part.localPose or pair.mechanical_part
            p.foreign_changes[#p.foreign_changes+1]={owner=choice.name,key=mount.key,id=pair.mechanical_part.id,
              shape=mount.shape,basis=localBasis(choice.other,pair.other_foot),shared_key=sharedKey}
          end
          local keep={};for _,part in ipairs(p.parts)do if part.key~='low_bracket_right'then keep[#keep+1]=part end end;p.parts=keep
          local function foot(angle,key)
            p.parts[#p.parts+1]={role='hardware',shape=catalog.low_repeat.hardware_shape,
              position=copy(at),basis=localBasis(p,angle),key=key,mount_angle_degrees=math.deg(angle),mount_group='support_right'}
          end
          if pair.compound then
            local stem=pair.mechanical_part.localPose or pair.mechanical_part
            local stemKey=sharedKey..':stand_off';local doubleKey=sharedKey..':double_bracket'
            p.parts[#p.parts+1]={role='hardware',shape=catalog.low_repeat.hardware_shape,key='shared_stand_off',
              position=localPoint(p,unpack(pair.stem_origin)),basis=localBasis(p,pair.bracket_angle),
              shared_key=stemKey,share_with={owner=choice.name,key=stem.key},mount_group='support_right'}
            local bracket={role='hardware',shape=catalog.double_mount.hardware_shape,key='shared_mount_current',
              position=localPoint(p,unpack(pair.double_origin)),basis=localBasis(p,pair.bracket_angle),
              shared_key=doubleKey,mount_group='support_right'}
            if pair.double_part then
              bracket.share_with={owner=choice.name,key=(pair.double_part.localPose or pair.double_part).key}
            else
              -- Only an existing low foot is repurposed; the shaft and beam
              -- retain their exact placement and native controller ownership.
              p.foreign_changes[#p.foreign_changes+1]={owner=choice.name,key=stem.key,id=pair.mechanical_part.id,
                shape=stem.shape,position=localPoint(choice.other,unpack(pair.stem_origin)),
                basis=localBasis(choice.other,pair.bracket_angle),shared_key=stemKey}
            end
            p.parts[#p.parts+1]=bracket
          else
            foot(pair.current_foot,'shared_mount_current')
            if not pair.existing_foot then foot(pair.other_foot,'shared_mount_other')end
          end
          local separation=math.sqrt((pair.current[1]-pair.other[1])^2+(pair.current[2]-pair.other[2])^2)
          local diff=math.abs(math.atan2(math.sin(pair.current_angle-pair.other_angle),math.cos(pair.current_angle-pair.other_angle)))
          local along=math.abs((pair.current[1]-pair.other[1])*math.cos(pair.other_angle)+(pair.current[2]-pair.other[2])*math.sin(pair.other_angle))
          local board=catalog.double_shields[tostring(pair.h1.lens_diameter_mm)]
          if diff<.001 and along<.01 and board and pair.h1.lens_diameter_mm==pair.h2.lens_diameter_mm then
            local centre=localPoint(p,(pair.current[1]+pair.other[1])/2,(pair.current[2]+pair.other[2])/2,pair.current[3])
            p.parts[#p.parts+1]={role='shield',shape=board.shape,key='shared_double_shield',position=centre,
              basis=localBasis(p,pair.other_angle),scale={1,(pair.h1.bounds_m[2][2]-pair.h1.bounds_m[1][2]+separation)/(board.bounds_m[2][2]-board.bounds_m[1][2]),1},
              pair_with=current.pair_with,shared_key=sharedKey,mount_group='support_right'}
          end
          p.shared_support={owner=choice.name,key=choice.part.key,foot={q.x,q.y,q.z}}
          p.mount_pair={relative_angle_degrees=pair.quarter,current_foot_angle=pair.current_foot,other_foot_angle=pair.other_foot,
            kind=pair.compound and'authored_double_bracket'or'quarter_turn_feet'}
          p.placement.feet={{q.x,q.y,z}};p.signature=mounts.signature({support=p.layout.support,parts=p.parts})
          out[#out+1]=p
        end
      end
    end
  end
  return out
end
function M.withoutShields(p,others,ignore)
  -- Remove only a board involved in an actual geometric collision. A blocked
  -- low repeater must never strip the unrelated overhead lane lanterns.
  local space=require('beamngEarth/nl_signal_space');p.foreign_changes=p.foreign_changes or{}
  for _=1,#p.parts+8 do
    local name,_,ownKey,otherKey=space.conflict(p,others,ignore)
    if not name then break end
    local changed=false;local keep={}
    for _,part in ipairs(p.parts)do
      if part.key==ownKey and part.role=='shield'then changed=true
      else
        local head=heads[part.shape]
        if part.key==ownKey and head and head.bare_shape and part.shape~=head.bare_shape then
          part.shape=head.bare_shape;changed=true
        end
        keep[#keep+1]=part
      end
    end
    p.parts=keep
    if not changed then
      for _,entry in ipairs(others[name].parts or{})do local part=entry.localPose or entry
        if part.key==otherKey and not part.paired_by then
          local change
          for _,c in ipairs(p.foreign_changes)do if c.owner==name and c.key==otherKey then change=c;break end end
          local shape=change and change.shape or part.shape;local head=heads[shape]
          if head and head.bare_shape and shape~=head.bare_shape then
            if change then change.shape=head.bare_shape
            else p.foreign_changes[#p.foreign_changes+1]={owner=name,key=part.key,id=entry.id,shape=head.bare_shape}end
            changed=true
          end
        end
      end
    end
    if not changed then break end
  end
  p.signature=mounts.signature({support=p.layout.support,parts=p.parts});return p
end
return M
