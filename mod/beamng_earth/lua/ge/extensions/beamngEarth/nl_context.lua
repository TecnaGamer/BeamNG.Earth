-- Inferred road context, nominal plate sizes and conservative orientation audit.
-- Never infer a road speed from the number printed on a sign.
local M={}
local function finite(n)return type(n)=='number' and n==n and math.abs(n)<math.huge end
local function clamp(n)return math.max(-1,math.min(1,n))end
function M.sourceFront(frame,x,y,bearing,convention)
  local nx,ny=frame:northAt(x,y)
  local beta=math.rad(bearing+(convention=='travel_bearing' and 180 or 0))
  return nx*math.cos(beta)+ny*math.sin(beta),ny*math.cos(beta)-nx*math.sin(beta)
end
function M.yawForFront(x,y)return math.deg(math.atan2(x,y))+90 end
function M.match(x,y,edges)
  local choices={}
  for _,e in ipairs(edges)do
    local dx,dy=e.bx-e.ax,e.by-e.ay;local l2=dx*dx+dy*dy
    if l2>.01 then
      local t=math.max(0,math.min(1,((x-e.ax)*dx+(y-e.ay)*dy)/l2))
      local qx,qy=e.ax+t*dx,e.ay+t*dy;local d=math.sqrt((x-qx)^2+(y-qy)^2)
      if d<=30 then local l=math.sqrt(l2);choices[#choices+1]={edge=e,distance_m=d,ax=dx/l,ay=dy/l}end
    end
  end
  table.sort(choices,function(a,b)if math.abs(a.distance_m-b.distance_m)>1e-6 then return a.distance_m<b.distance_m end;return tostring(a.edge.key)<tostring(b.edge.key)end)
  local best=choices[1];if not best then return{status='road_context_unavailable'}end
  local meta=best.edge.context or {}
  local speed=meta.max_speed_kmh
  local source=meta.speed_source
  if not finite(speed) and finite(best.edge.ai_speed_mps) and best.edge.ai_speed_mps>0 then speed=best.edge.ai_speed_mps*3.6;source='legacy_ai_hint_not_legal_limit' end
  if not finite(speed) or speed<=0 or speed>180 then speed=nil;source=nil end
  local result={status='spatial_road_match_not_nwb_id',edge_key=best.edge.key,distance_m=best.distance_m,
    ax=best.ax,ay=best.ay,road_class=meta.road_class,speed_kmh=speed,speed_source=source,flow_x=best.edge.flow_x,flow_y=best.edge.flow_y}
  for i=2,#choices do local other=choices[i]
    if other.distance_m>best.distance_m+2.5 then break end
    if math.abs(best.ax*other.ax+best.ay*other.ay)<math.cos(math.rad(25)) then result.ambiguous=true;result.status='junction_or_crossing_ambiguous';break end
    local om=other.edge.context or {}
    if meta.road_class and om.road_class and (meta.road_class=='motorway')~=(om.road_class=='motorway') then result.ambiguous=true;result.status='parallel_road_class_ambiguous';break end
  end
  return result
end
function M.rotation(fx,fy,road,sourceBearing)
  local out={fx=fx,fy=fy,status='source_preserved',delta_deg=0}
  if not road.ax then out.status='road_context_unavailable';return out end
  if road.ambiguous then out.status='ambiguous_road_source_preserved';return out end
  if road.flow_x then out.possible_reverse_facing=fx*road.flow_x+fy*road.flow_y>math.cos(math.rad(30))end
  local dot=fx*road.ax+fy*road.ay
  local error=math.deg(math.acos(clamp(math.abs(dot))))
  out.axis_error_deg=error
  -- Cardinal source headings often encode a quadrant rather than a surveyed
  -- angle. Resolve that quadrant against a certain local road tangent. The
  -- 45-degree tie and perpendicular/reversed ambiguities remain unmodified.
  local quadrant=finite(sourceBearing) and math.abs(sourceBearing/90-math.floor(sourceBearing/90+.5))<1e-8
  local limit=quadrant and 44.9 or 20
  if error>limit then out.status='large_axis_mismatch_review';return out end
  if error<.01 then out.status='source_axis_consistent';return out end
  local sign=dot>=0 and 1 or -1
  out.fx,out.fy=road.ax*sign,road.ay*sign
  out.delta_deg=math.deg(math.atan2(fx*out.fy-fy*out.fx,fx*out.fx+fy*out.fy))
  out.status=error>20 and 'coarse_bearing_axis_alignment_inferred' or 'small_axis_alignment_inferred'
  return out
end
function M.dimensions(sign,shape,road)
  shape=shape or sign.shape
  local circle=shape=='circle'
  local triangle=shape=='triangle' or shape=='rounded_triangle' or shape=='inverted_rounded_triangle'
  local out={width_m=sign.width_m,height_m=sign.height_m,scale=1,status='catalogue_fallback_not_surveyed',shape=shape}
  if not circle and not triangle then out.status='special_shape_size_unresolved';return out end
  if road.ambiguous or (road.road_class~='motorway' and not road.speed_kmh)then return out end
  local kind=road.road_class=='motorway' and 3 or road.speed_kmh>80.05 and 3 or road.speed_kmh>50.05 and 2 or 1
  local widths=circle and {.6,.8,1.0} or {.7,.9,1.1}
  out.width_m=widths[kind];out.scale=out.width_m/sign.width_m;out.height_m=sign.height_m*out.scale
  out.type=kind;out.status='road_context_nominal_inferred';out.source=road.road_class=='motorway' and 'motorway_class' or road.speed_source
  return out
end
function M.supplementary(sign,parentScale)
  return{width_m=sign.width_m*parentScale,height_m=sign.height_m*parentScale,scale=parentScale,status='scaled_with_parent_not_surveyed'}
end
return M
