-- Conservative 3D clearances between separate signal assemblies.
local M={}
local mounts=require('beamngEarth/nl_signal_mounts')
local bounds={}
local headBounds={}
for _,h in pairs(mounts.catalog and mounts.catalog.heads or{})do bounds[h.shape]=h.bounds_m;if h.bare_shape then bounds[h.bare_shape]=h.bare_bounds_m end end
for _,h in pairs(mounts.catalog and mounts.catalog.heads or{})do
  local board=mounts.catalog.double_shields[tostring(h.lens_diameter_mm)]
  if h.bare_shape then headBounds[h.bare_shape]={h.bare_bounds_m}end
  headBounds[h.shape]={h.bare_bounds_m or h.bounds_m}
  if board and h.bare_shape then
    headBounds[h.shape][2]={{board.bounds_m[1][1],h.bounds_m[1][2],h.bounds_m[1][3]},
      {board.bounds_m[2][1],h.bounds_m[2][2],h.bounds_m[2][3]}}
  end
end
function M.headBounds(shape)return headBounds[shape]or{bounds[shape]}end
local function orientedBoxes(plan,p)
  local pose=mounts.worldPose(plan,p);local scale=p.scale or{1,1,1};local out={}
  local b=pose.basis;local axes={{b[1],b[4],b[7]},{b[2],b[5],b[8]},{b[3],b[6],b[9]}}
  for _,v in ipairs(headBounds[p.shape]or{bounds[p.shape]})do
    local cx,cy,cz=(v[1][1]+v[2][1])/2*scale[1],(v[1][2]+v[2][2])/2*scale[2],(v[1][3]+v[2][3])/2*scale[3]
    local centre={pose.x+b[1]*cx+b[2]*cy+b[3]*cz,pose.y+b[4]*cx+b[5]*cy+b[6]*cz,pose.z+b[7]*cx+b[8]*cy+b[9]*cz}
    local half={(v[2][1]-v[1][1])/2*scale[1],(v[2][2]-v[1][2])/2*scale[2],(v[2][3]-v[1][3])/2*scale[3]}
    out[#out+1]={centre=centre,axes=axes,half=half}
  end
  return out
end
for _,h in pairs(mounts.catalog and mounts.catalog.double_shields or{})do bounds[h.shape]=h.bounds_m end
for _,s in pairs(mounts.catalog and mounts.catalog.supports or{})do
  bounds[s.shape]=s.bounds_m
  if s.construction then local c=s.construction;bounds[c.leg_shape]=c.leg_bounds_m;bounds[c.beam_shape]=c.beam_bounds_m end
end
local function point(root,p,x,y,z)
  local pose=mounts.worldPose(root,p);local b=pose.basis;local s=p.scale or{1,1,1};x,y,z=x*s[1],y*s[2],z*s[3]
  return {pose.x+b[1]*x+b[2]*y+b[3]*z,pose.y+b[4]*x+b[5]*y+b[6]*z,pose.z+b[7]*x+b[8]*y+b[9]*z}
end
function M.envelope(plan,overrides)
  local out={}
  for _,item in ipairs(plan.parts or{})do
    local p=item.localPose or item
    local change=overrides and overrides[p.key]
    if change then local old=p;p={};for k,v in pairs(old)do p[k]=v end
      for _,k in ipairs({'shape','position','basis','shared_key'})do if change[k]then p[k]=change[k]end end
    end
    local b=bounds[p.shape]
    if b and(p.role=='beam'or p.role=='support'or p.role=='head'or p.role=='shield')then
      local lo,hi=b[1],b[2];local x,y,z=(lo[1]+hi[1])/2,(lo[2]+hi[2])/2,(lo[3]+hi[3])/2
      local a,c,r
      if p.role=='beam'then
        a=point(plan,p,x,lo[2],z);c=point(plan,p,x,hi[2],z);r=.18
      else
        r=(p.role=='head'or p.role=='shield')and math.max(hi[1]-lo[1],hi[2]-lo[2])/2+.05 or .20
        local inset=(p.role=='head'or p.role=='shield')and math.min(r,(hi[3]-lo[3])/2)or 0
        a=point(plan,p,x,y,lo[3]+inset);c=point(plan,p,x,y,hi[3]-inset)
      end
      out[#out+1]={a=a,b=c,r=r,role=p.role,key=p.key,shared_key=p.shared_key,pair_with=p.pair_with,
        boxes=(p.role=='head'or p.role=='shield')and orientedBoxes(plan,p)or nil}
    end
  end
  return out
end
local function dot(a,b)return a[1]*b[1]+a[2]*b[2]+a[3]*b[3]end
local function sub(a,b)return{a[1]-b[1],a[2]-b[2],a[3]-b[3]}end
local function boxesOverlap(a,b)
  local delta=sub(b.centre,a.centre);local axes={a.axes[1],a.axes[2],a.axes[3],b.axes[1],b.axes[2],b.axes[3]}
  for _,u in ipairs(a.axes)do for _,v in ipairs(b.axes)do
    axes[#axes+1]={u[2]*v[3]-u[3]*v[2],u[3]*v[1]-u[1]*v[3],u[1]*v[2]-u[2]*v[1]}
  end end
  for _,axis in ipairs(axes)do local len=math.sqrt(dot(axis,axis))
    if len>1e-7 then
      local radius=.015*len
      for i=1,3 do radius=radius+a.half[i]*math.abs(dot(axis,a.axes[i]))+b.half[i]*math.abs(dot(axis,b.axes[i]))end
      if math.abs(dot(delta,axis))>=radius then return false end
    end
  end
  return true
end
local function capsuleBox(segment,box)
  local p,q=sub(segment.a,box.centre),sub(segment.b,box.centre);local start,delta={},{};local cuts={0,1}
  for i=1,3 do
    start[i]=dot(p,box.axes[i]);delta[i]=dot(q,box.axes[i])-start[i]
    if math.abs(delta[i])>1e-10 then for _,sign in ipairs({-1,1})do
      local t=(sign*box.half[i]-start[i])/delta[i];if t>0 and t<1 then cuts[#cuts+1]=t end
    end end
  end
  table.sort(cuts)
  local best=math.huge
  local function distanceAt(t)local d=0
    for i=1,3 do local q=math.max(0,math.abs(start[i]+delta[i]*t)-box.half[i]);d=d+q*q end
    best=math.min(best,d)
  end
  for k=1,#cuts-1 do
    local lo,hi=cuts[k],cuts[k+1];local mid=(lo+hi)/2;local aa,bb=0,0
    for i=1,3 do local v=start[i]+delta[i]*mid
      if math.abs(v)>box.half[i]then
        local c=start[i]-(v>0 and box.half[i]or-box.half[i]);aa=aa+delta[i]^2;bb=bb+delta[i]*c
      end
    end
    distanceAt(lo);distanceAt(hi)
    if aa>1e-12 then distanceAt(math.max(lo,math.min(hi,-bb/aa)))end
  end
  return best<(segment.r+.015)^2
end
local function clamp(x)return math.max(0,math.min(1,x))end
local function distance(a,b)
  local u,v,w=sub(a.b,a.a),sub(b.b,b.a),sub(a.a,b.a)
  local aa,bb,cc,dd,ee=dot(u,u),dot(u,v),dot(v,v),dot(u,w),dot(v,w)
  local s,t
  if aa<1e-9 then s=0;t=cc>1e-9 and clamp(ee/cc)or 0
  elseif cc<1e-9 then t=0;s=clamp(-dd/aa)
  else
    local den=aa*cc-bb*bb;s=den>1e-9 and clamp((bb*ee-cc*dd)/den)or 0;t=(bb*s+ee)/cc
    if t<0 then t=0;s=clamp(-dd/aa)elseif t>1 then t=1;s=clamp((bb-dd)/aa)end
  end
  return (w[1]+s*u[1]-t*v[1])^2+(w[2]+s*u[2]-t*v[2])^2+(w[3]+s*u[3]-t*v[3])^2
end
local function trimJoint(segment,foot)
  local ax,ay=(segment.a[1]-foot[1])^2+(segment.a[2]-foot[2])^2,(segment.b[1]-foot[1])^2+(segment.b[2]-foot[2])^2
  if math.min(ax,ay)>.3^2 then return end
  local near,far=ax<ay and segment.a or segment.b,ax<ay and segment.b or segment.a
  local d=math.sqrt((far[1]-near[1])^2+(far[2]-near[2])^2+(far[3]-near[3])^2)
  if d<=.55 then return end
  local t=.55/d;local a={near[1]+(far[1]-near[1])*t,near[2]+(far[2]-near[2])*t,near[3]+(far[3]-near[3])*t}
  return{a=a,b=far,r=segment.r}
end
function M.conflict(plan,others,ignore)
  if plan.omit or plan.model=='stock'then return end
  local own=M.envelope(plan)
  for name,other in pairs(others or{})do
    if name~=ignore and other.model~='stock'and(other.x-plan.x)^2+(other.y-plan.y)^2<80^2 then
      local overrides={};for _,change in ipairs(plan.foreign_changes or{})do if change.owner==name then overrides[change.key]=change end end
      local theirs=M.envelope(other,overrides)
      for _,a in ipairs(own)do for _,b in ipairs(theirs)do
        local shared=a.shared_key and(a.shared_key==b.shared_key or a.shared_key==name..':'..tostring(b.key))
        local connected=shared and(b.role=='support'and(a.role=='support'or a.role=='beam'or a.role=='head'or a.role=='shield')
          or a.role=='support'and(b.role=='beam'or b.role=='head'or b.role=='shield'))
        if a.role=='support'and a.shared_key and b.role=='beam'and plan.shared_support and plan.shared_support.owner==name then
          local d1=(a.a[1]-b.a[1])^2+(a.a[2]-b.a[2])^2;local d2=(a.a[1]-b.b[1])^2+(a.a[2]-b.b[2])^2
          if math.min(d1,d2)<.3^2 then connected=true end
        end
        local paired=a.pair_with==name..':'..tostring(b.key)and b.role=='head'
          or b.pair_with==tostring(ignore)..':'..tostring(a.key)and a.role=='head'
        local aa,bb=a,b
        if a.role=='beam'and b.role=='beam'then
          local foot=plan.shared_support and plan.shared_support.owner==name and plan.shared_support.foot
          if not foot and other.saved_plan and other.saved_plan.shared_support and other.saved_plan.shared_support.owner==ignore then foot=other.saved_plan.shared_support.foot end
          if foot then aa=trimJoint(a,foot)or a;bb=trimJoint(b,foot)or b end
        end
        if not connected and not paired then
          local collision=false
          if a.boxes and b.boxes then
            for _,x in ipairs(a.boxes)do for _,y in ipairs(b.boxes)do if boxesOverlap(x,y)then collision=true end end end
          elseif a.boxes or b.boxes then
            for _,box in ipairs(a.boxes or b.boxes)do if capsuleBox(a.boxes and bb or aa,box)then collision=true end end
          else collision=distance(aa,bb)<(a.r+b.r)^2 end
          if collision then return name,a.role..'/'..b.role,a.key,b.key end
        end
      end end
    end
  end
end
return M
