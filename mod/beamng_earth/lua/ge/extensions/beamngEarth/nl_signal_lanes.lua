-- Read incoming lanes from the existing graph; never guess turn arrows from road angles.
local M={}
local function number(n)return type(n)=='number' and n==n and math.abs(n)<math.huge end
local function count(n)return number(n) and n>=1 and n<=16 and n%1==0 end

function M.variant(turn,diameter)
  local base=diameter==300 and 'vehicle_300' or 'vehicle'
  local arrow=({left='left',right='right',slight_left='slight_left',slight_right='slight_right'})[turn]
  return arrow and base..'_arrow_'..arrow or base
end

function M.describe(graph,junction,a,context)
  context=context or {}
  local metadata=type(a[12])=='table' and a[12] or {}
  local zone=metadata.zone or context.zone or 'unknown'
  local diameter=zone=='bubeko' and 300 or 200
  local edge,n,opposite,radius
  for _,e in ipairs(graph and graph.edges or {})do
    local k,back,r
    if e[1]==a[1] and e[2]==junction then k,back,r=e[4],e[5],e[9] or e[8]
    elseif e[2]==a[1] and e[1]==junction and e[3]~=1 then k,back,r=e[5],e[4],e[8] end
    if k and count(k) then
      if edge then return {count=1,source='ambiguous_graph_edge',synchronized=true,zone=zone,lens_diameter_mm=diameter} end
      edge,n,opposite,radius=e,k,back,r
    end
  end
  if not edge then return {count=1,source='incoming_edge_unavailable',synchronized=true,zone=zone,lens_diameter_mm=diameter} end
  local p={count=n,source='graph_incoming_lanes',synchronized=true,heads={},zone=zone,lens_diameter_mm=diameter,zone_source=metadata.zone and 'explicit_approach_zone' or context.zone_source,turns_source=context.turns_source}
  if not number(radius) or radius<=0 or not number(opposite) or opposite<0 then p.reason='lane_width_unavailable';return p end
  local width=2*radius/(n+opposite);p.lane_width_m=width;p.carriageway_half_width_m=radius
  p.road_class=metadata.road_class or type(edge[10])=='table' and edge[10].road_class or context.road_class
  local node=graph.nodes and graph.nodes[a[1]]
  if not node then p.reason='lane_position_unavailable';return p end
  p.incoming_node=node;p.junction_node=graph.nodes[junction]
  local dx,dy=a[5],a[6];local len=math.sqrt(dx*dx+dy*dy)
  if len<.5 then p.reason='direction_unavailable';return p end
  dx,dy=dx/len,dy/len
  local leftX,leftY=-dy,dx
  local centreDistance=(node[1]-a[8])*leftX+(node[2]-a[9])*leftY
  p.centre_distance_m=centreDistance
  local turns=type(metadata.turns)=='table' and #metadata.turns==n and metadata.turns or type(context.turns)=='table' and #context.turns==n and context.turns or {}
  if turns==metadata.turns then p.turns_source='explicit_approach_turns' end
  for k=1,n do
    local rightOffset=-radius+(opposite+k-.5)*width
    p.heads[k]={id='lane_'..k,lane_id=tostring(junction)..':'..a[1]..':'..k,
      variant=M.variant(turns[k],diameter),turn=turns[k] or 'unknown',distance_m=centreDistance-rightOffset}
  end
  return p
end
return M
