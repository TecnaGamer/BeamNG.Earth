-- Pure geometric planning. Source coordinates retained separately from adjusted pole positions.
local M={}
local function nearest(px,py,e)
 local dx,dy=e.bx-e.ax,e.by-e.ay;local l=dx*dx+dy*dy
 if l<1e-8 then return e.ax,e.ay,0 end
 local t=math.max(0,math.min(1,((px-e.ax)*dx+(py-e.ay)*dy)/l))
 return e.ax+t*dx,e.ay+t*dy,t
end
function M.fromMap(nodes,x,y,radius)
 local edges,seen={},{};radius=radius or 180
 for id,n in pairs(nodes or {})do if n.pos then for other,link in pairs(n.links or {})do
  local b=nodes[other];local key=tostring(id)<tostring(other)and tostring(id)..':'..tostring(other)or tostring(other)..':'..tostring(id)
  if b and b.pos and not seen[key] then
   seen[key]=true
   local width=math.max(tonumber(link.inRadius)or 0,tonumber(link.outRadius)or 0,tonumber(n.radius)or 0,tonumber(b.radius)or 0)
   if width>0 then
    local nav=package.loaded['beamngEarth/navgraph']
    local context=link.nlRoadContext or (nav and nav.nlContextFor and nav.nlContextFor(id,other))
    local e={ax=n.pos.x,ay=n.pos.y,bx=b.pos.x,by=b.pos.y,half=width+1.0,key=key,context=context,ai_speed_mps=link.speedLimit}
    if link.oneWay and link.inPos and link.outPos then
      local dx,dy=link.outPos.x-link.inPos.x,link.outPos.y-link.inPos.y;local l=math.sqrt(dx*dx+dy*dy)
      if l>.01 then e.flow_x,e.flow_y=dx/l,dy/l end
    end
    local qx,qy=nearest(x,y,e);if(qx-x)^2+(qy-y)^2<=(radius+e.half)^2 then edges[#edges+1]=e end
   end
  end
 end end end
 table.sort(edges,function(a,b)return a.ax<b.ax or a.ax==b.ax and(a.ay<b.ay or a.ay==b.ay and(a.bx<b.bx or a.bx==b.bx and a.by<b.by))end)
 return edges
end
function M.clearOfRoad(x,y,edges,clearance)
 if #edges==0 then return false,'road_geometry_unavailable' end
 for _,e in ipairs(edges)do local qx,qy=nearest(x,y,e);if(x-qx)^2+(y-qy)^2<(e.half+(clearance or .5))^2 then return false end end
 return true
end
function M.roadside(x,y,edges,bearing)
 if #edges==0 then return nil,'road_geometry_unavailable' end
 local best,distance
 for _,e in ipairs(edges)do local qx,qy=nearest(x,y,e);local d=(x-qx)^2+(y-qy)^2;if not distance or d<distance then best,distance={x=qx,y=qy,e=e},d end end
 if distance>30^2 then return nil,'source_too_far_from_known_road' end
 if M.clearOfRoad(x,y,edges)then return{x=x,y=y,shift_m=0}end
 local e=best.e;local dx,dy=e.bx-e.ax,e.by-e.ay;local l=math.sqrt(dx*dx+dy*dy);if l<.01 then return nil,'degenerate_road' end
 local nx,ny=-dy/l,dx/l;local side=(x-best.x)*nx+(y-best.y)*ny
 if math.abs(side)<.1 then local b=math.rad(bearing or 0);side=math.cos(b)*nx-math.sin(b)*ny end
 if side<0 then nx,ny=-nx,-ny end
 local candidates={}
 for _,sign in ipairs({1,-1})do for extra=0,8,.5 do
  local cx,cy=best.x+nx*sign*(e.half+.6+extra),best.y+ny*sign*(e.half+.6+extra)
  local shift=math.sqrt((cx-x)^2+(cy-y)^2)
  if shift<=15 and M.clearOfRoad(cx,cy,edges)then candidates[#candidates+1]={x=cx,y=cy,shift_m=shift,preferred=sign==1}break end
 end end
 table.sort(candidates,function(a,b)if a.preferred~=b.preferred then return a.preferred end;return a.shift_m<b.shift_m end)
 return candidates[1],candidates[1]and nil or'no_clear_roadside_candidate'
end
-- Later entries in RVV Annex 1 are mounted above earlier entries (BABW placement rule).
local priority={A=1,B=2,C=3,D=4,E=5,F=6,G=7,H=8,J=9,K=10,L=11}
function M.group(rows)
 local sorted={};for _,r in ipairs(rows)do sorted[#sorted+1]=r end;table.sort(sorted,function(a,b)return a.id<b.id end)
 local groups={}
 for _,r in ipairs(sorted)do
  local chosen
  for _,g in ipairs(groups)do if(r.x-g.x)^2+(r.y-g.y)^2<=.75^2 and #g.rows<4 and (function()local a=math.abs(((r.bearing_deg-g.rows[1].bearing_deg+180)%360)-180);return a<=20 or a>=160 end)() then chosen=g;break end end
  if not chosen then chosen={x=r.x,y=r.y,rows={}};groups[#groups+1]=chosen end
  chosen.rows[#chosen.rows+1]=r
 end
 for _,g in ipairs(groups)do table.sort(g.rows,function(a,b)
  if a.mount_order and b.mount_order and a.mount_order~=b.mount_order then return a.mount_order<b.mount_order end
  local pa,pb=priority[a.asset:sub(1,1)]or 99,priority[b.asset:sub(1,1)]or 99
  local na=tonumber(a.asset:match('^[A-Z]+(%d+)'))or 0;local nb=tonumber(b.asset:match('^[A-Z]+(%d+)'))or 0
  return pa>pb or pa==pb and(na>nb or na==nb and(a.asset<b.asset or a.asset==b.asset and a.id<b.id))
 end)end
 return groups
end
return M
