-- Optional local BGT ground context. Does not create controllers or infer lanes.
-- Public context/classify/candidates API can also serve sign placement.
local M={}
local context=require('beamngEarth/nl_signal_context')
local function u16(s,i)local a,b=s:byte(i,i+1);return a+b*256 end
local function u32(s,i)local a,b,c,d=s:byte(i,i+3);return a+b*256+c*65536+d*16777216 end
local names={'pedestrian','supporting','carriageway','cycleway','parking','access','unknown'}
local function segment(x,y,a,b)
  local dx,dy=b[1]-a[1],b[2]-a[2];local l=dx*dx+dy*dy
  local t=l>1e-12 and math.max(0,math.min(1,((x-a[1])*dx+(y-a[2])*dy)/l))or 0
  local px,py=a[1]+dx*t,a[2]+dy*t
  return (x-px)^2+(y-py)^2,px,py
end
local function ringInfo(r,x,y)
  local inside,dist=false,math.huge
  local a=r[#r]
  for _,b in ipairs(r)do
    if (a[2]>y)~=(b[2]>y)and x<(b[1]-a[1])*(y-a[2])/(b[2]-a[2])+a[1]then inside=not inside end
    dist=math.min(dist,segment(x,y,a,b));a=b
  end
  return inside,dist
end
local function polygon(row,x,y)
  local inside,dist=ringInfo(row.rings[1],x,y)
  for k=2,#row.rings do local hole,d=ringInfo(row.rings[k],x,y);inside=inside and not hole;dist=math.min(dist,d)end
  return inside,dist
end
function M.new(index,data)
  assert(data:sub(1,8)=='BNEBGT1\0'and #data==index.data_bytes,'invalid BGT context')
  local cache,order={},{}
  local function row(at)
    if cache[at]then return cache[at]end
    local kind,n,rc=data:byte(at),data:byte(at+1),u16(data,at+2);at=at+4
    local out={role=names[kind],id=data:sub(at,at+n-1),rings={},bounds={math.huge,math.huge,-math.huge,-math.huge}};local key=at-4;at=at+n
    assert(out.role and rc>0 and rc<4096,'invalid BGT polygon')
    for _=1,rc do
      local count=u16(data,at);at=at+2;assert(count>=3,'invalid BGT ring')
      local r={};for _=1,count do
        local x,y=u32(data,at)/100,u32(data,at+4)/100;r[#r+1]={x,y};at=at+8
        local b=out.bounds;b[1],b[2],b[3],b[4]=math.min(b[1],x),math.min(b[2],y),math.max(b[3],x),math.max(b[4],y)
      end
      out.rings[#out.rings+1]=r
    end
    cache[key]=out;order[#order+1]=key
    if #order>256 then cache[table.remove(order,1)]=nil end
    return out
  end
  return function(x,y,radius)
    local out,seen={},{}
    for gx=math.floor((x-radius)/index.cell),math.floor((x+radius)/index.cell)do
      for gy=math.floor((y-radius)/index.cell),math.floor((y+radius)/index.cell)do
        for _,at in ipairs(index.tiles[gx..'_'..gy]or{})do if not seen[at]then seen[at]=true;out[#out+1]=row(at)end end
      end
    end
    return out
  end
end
local lookup,attempted
local function source()
  if M.lookup then return M.lookup end -- fixture/provider seam
  if not attempted then
    attempted=true
    local ok,idx=pcall(require,'beamngEarth/nl_groenpol_bgt_index')
    if ok then local data=context.readBinary(idx.data)
      if data then local good,fn=pcall(M.new,idx,data);if good then lookup=fn end end
    end
  end
  return lookup
end
function M.context(frame,x,y,z)
  if not(frame and frame.worldToGeodetic and frame.geodeticToWorld)then return end
  local fn=source();if not fn then return end
  local lat,lon=frame:worldToGeodetic(x,y,z);local mx,my=context.mercator(lat,lon)
  local scale=math.cos(math.rad(lat))
  local good,rows=pcall(fn,mx,my,70/scale);if not good or #rows==0 then return end
  -- Local affine coordinates follow the current anchor orientation, including
  -- rebase. At this 65 m search radius curvature is below the source precision.
  local la,lo=frame:worldToGeodetic(x+1,y,z);local ax,ay=context.mercator(la,lo);ax,ay=ax-mx,ay-my
  la,lo=frame:worldToGeodetic(x,y+1,z);local bx,by=context.mercator(la,lo);bx,by=bx-mx,by-my
  local det=ax*by-ay*bx;if math.abs(det)<1e-8 then return end
  local function toMap(px,py)return mx+(px-x)*ax+(py-y)*bx,my+(px-x)*ay+(py-y)*by end
  local function toWorld(px,py)return x+((px-mx)*by-(py-my)*bx)/det,y+((py-my)*ax-(px-mx)*ay)/det end
  local out={}
  function out.classify(px,py,radius)
    local u,v=toMap(px,py);local r=(radius or 0)/scale;local safe
    for _,row in ipairs(rows)do
      local b=row.bounds
      if not b or(u>=b[1]-r-.03 and v>=b[2]-r-.03 and u<=b[3]+r+.03 and v<=b[4]+r+.03)then
      local inside,d=polygon(row,u,v)
      if row.role=='pedestrian'or row.role=='supporting'then
        if inside and d>(r+.015/scale)^2 then safe=safe or row end
      elseif inside or d<=(r+.015/scale)^2 then return false,row.role,row.id end
      end
    end
    if safe then return true,safe.role,safe.id end
    return nil -- unknown gaps and polygon holes do not prove free ground
  end
  function out.candidates(px,py,radius)
    local u,v=toMap(px,py);local found,edges={},{};local distance=radius/scale
    for _,row in ipairs(rows)do if row.role=='pedestrian'or row.role=='supporting'then
      for _,ring in ipairs(row.rings)do
        local a=ring[#ring]
        for _,b in ipairs(ring)do
          local d,qx,qy=segment(u,v,a,b)
          if d<=distance^2 then edges[#edges+1]={d=d,x=qx,y=qy,a=a,b=b,role=row.role}end
          a=b
        end
      end
    end end
    table.sort(edges,function(a,b)return a.d<b.d end)
    -- Bound candidate work even for detailed BGT kerbs. The regular placement
    -- search remains available if these nearest edge suggestions do not fit.
    for k=1,math.min(#edges,64)do local e=edges[k]
      local dx,dy=e.b[1]-e.a[1],e.b[2]-e.a[2];local len=math.sqrt(dx*dx+dy*dy)
      if len>.01 then for _,gap in ipairs({.35,.65,1})do for _,sign in ipairs({-1,1})do
        local wx,wy=toWorld(e.x-sign*dy/len*gap/scale,e.y+sign*dx/len*gap/scale)
        found[#found+1]={wx,wy,source='BGT_'..e.role}
      end end end
    end
    table.sort(found,function(a,b)return (a[1]-px)^2+(a[2]-py)^2<(b[1]-px)^2+(b[2]-py)^2 end)
    local result,seen={},{}
    for _,p in ipairs(found)do local key=math.floor(p[1]*4)..','..math.floor(p[2]*4)
      if not seen[key]then seen[key]=true
        if out.classify(p[1],p[2],.22)then result[#result+1]=p;if #result>=96 then break end end
      end
    end
    return result
  end
  return out
end
function M.reset()lookup,attempted=nil,nil end
return M
