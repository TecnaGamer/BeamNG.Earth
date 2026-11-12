-- Roadside feet checked against the graph's carriageways, never a fixed portal span.
local M={FOOT_CLEARANCE_M=.6,MAX_SPAN_M=40,REMOTE_PORTAL_FOOT_M=3,
  MAX_BEFORE_STOP_M=20,MAX_AFTER_STOP_M=3}
local cache=setmetatable({},{__mode='k'})
local CELL=32
local function segmentDistance(x,y,a,b)
  local dx,dy=b[1]-a[1],b[2]-a[2];local l2=dx*dx+dy*dy
  local t=l2>1e-6 and math.max(0,math.min(1,((x-a[1])*dx+(y-a[2])*dy)/l2)) or 0
  return math.sqrt((x-a[1]-t*dx)^2+(y-a[2]-t*dy)^2),t
end
local function index(graph)
  if cache[graph] then return cache[graph] end
  local grid={}
  for _,e in ipairs(graph.edges or {})do
    local a,b=graph.nodes[e[1]],graph.nodes[e[2]]
    if a and b then
      local radius=math.max(e[8] or 0,e[9] or e[8] or 0)
      local pad=radius+M.FOOT_CLEARANCE_M
      local row={a=a,b=b,radius=radius}
      for x=math.floor((math.min(a[1],b[1])-pad)/CELL),math.floor((math.max(a[1],b[1])+pad)/CELL)do
        for y=math.floor((math.min(a[2],b[2])-pad)/CELL),math.floor((math.max(a[2],b[2])+pad)/CELL)do
          local key=x..','..y;grid[key]=grid[key] or {};grid[key][#grid[key]+1]=row
        end
      end
    end
  end
  cache[graph]=grid;return grid
end
function M.clear(graph,x,y,z)
  if not graph then return false end
  local rows=index(graph)[math.floor(x/CELL)..','..math.floor(y/CELL)] or {}
  for _,row in ipairs(rows)do
    local distance,t=segmentDistance(x,y,row.a,row.b)
    local roadz=(row.a[3] or 0)*(1-t)+(row.b[3] or 0)*t
    if (not z or math.abs(z-roadz)<2.5) and distance<row.radius+M.FOOT_CLEARANCE_M-1e-5 then return false end
  end
  return true
end
function M.stopRange(a,lane)
  local len=math.sqrt(a[5]^2+a[6]^2);local dx,dy=a[5]/len,a[6]/len
  local before,after=M.MAX_BEFORE_STOP_M,M.MAX_AFTER_STOP_M
  local node,j=lane.incoming_node,lane.junction_node
  if node then before=math.min(before,math.max(0,(a[2]-node[1])*dx+(a[3]-node[2])*dy-2))end
  if j then after=math.min(after,math.max(0,(j[1]-a[2])*dx+(j[2]-a[3])*dy-2))end
  local source=(a[8]-a[2])*dx+(a[9]-a[3])*dy
  return source-after,source+before
end
function M.searchOffsets(a,lane)
  local lo,hi=M.stopRange(a,lane);local out={}
  for _,d in ipairs({0,1,-1,2,-2,3,-3,4,-4,6,-6,8,10,12,16,20})do
    if d>=lo-1e-6 and d<=hi+1e-6 then out[#out+1]=d end
  end
  return out,lo,hi
end
function M.fit(graph,a,lane,frame,search)
  if not lane.carriageway_half_width_m or not lane.centre_distance_m then return nil,'road_width_unavailable' end
  local dx,dy=a[5],a[6];local l=math.sqrt(dx*dx+dy*dy);dx,dy=dx/l,dy/l
  -- The original prop foot includes the drawn road's parking/edge strips, which
  -- may be wider than the AI lane radii. Carry that verge width to the left too.
  local roadHalf=math.max(lane.carriageway_half_width_m,lane.centre_distance_m-1)
  -- Front X points towards the approaching driver; local +Y is the roadside right.
  local fx,fy,rx,ry=-dx,-dy,dy,-dx
  search=search or {};local mirrored=search.side=='left'
  local baseSpan=lane.heads[1].distance_m+(lane.lane_width_m or 3.3)/2+.35
  local probe=require('beamngEarth/nl_signal_surfaces').begin(a[8],a[9],a[10],frame)
  local rejected={}
  local function foot(x,y)
    if probe then
      local ok,z,kind=probe(x,y,a[10])
      -- A fully decoded road tile plus a measured ground height is stronger than
      -- the AI graph's broad lane strip, which can cover a real grass island.
      if ok and (kind=='walk'or kind=='verge')then return z,kind end
      kind=ok and 'graph_carriageway' or kind or 'unknown'
      rejected[kind]=(rejected[kind]or 0)+1
    elseif M.clear(graph,x,y,a[10])then return a[10],'graph_only' end
  end
  local offsets={};for d=0,8,.5 do offsets[#offsets+1]=d end
  local last=lane.heads and lane.heads[#lane.heads]
  local inward=last and math.max(0,last.distance_m-(lane.lane_width_m or 3.3)/2-.35)or 0
  if not mirrored then for d=.5,math.min(inward,8),.5 do offsets[#offsets+1]=-d end end
  local backs,minBack,maxBack=M.searchOffsets(a,lane)
  if search.back~=nil then
    backs={};if search.back>=minBack and search.back<=maxBack then backs[1]=search.back end
  end
  local candidates={};local reference={}
  local bgt=nil -- BGT adapter is deliberately outside this integration.
  local bgtCandidates=bgt and bgt.candidates(a[8],a[9],45)or{}
  if frame and frame.worldToGeodetic and frame.geodeticToWorld then
    local lat,lon=frame:worldToGeodetic(a[2],a[3],a[4])
    local data=require('beamngEarth/nl_signal_reference')
    for _,p in ipairs(data.poles)do if math.abs(p[1]-lat)<.0005 and math.abs(p[2]-lon)<.0008 then
      local px,py=frame:geodeticToWorld(p[1],p[2],a[10]);reference[#reference+1]={px,py}
      local back=(px-a[8])*fx+(py-a[9])*fy;local outward=(px-a[8])*rx+(py-a[9])*ry
      if mirrored then outward=-outward-baseSpan end
      if back>=minBack and back<=maxBack and (search.back==nil or math.abs(back-search.back)<.01)
        and outward>=(mirrored and 0 or -inward) and outward<=8 then
        candidates[#candidates+1]={back,outward,'PDOK_BGT_paal'}
      end
    end end
  end
  for _,p in ipairs(bgtCandidates)do
    reference[#reference+1]=p
    local back=(p[1]-a[8])*fx+(p[2]-a[9])*fy;local outward=(p[1]-a[8])*rx+(p[2]-a[9])*ry
    if mirrored then outward=-outward-baseSpan end
    if back>=minBack and back<=maxBack and(search.back==nil or math.abs(back-search.back)<.01)
      and outward>=(mirrored and 0 or -inward)and outward<=8 then
      candidates[#candidates+1]={back,outward,p.source}
    end
  end
  table.sort(candidates,function(p,q)return p[1]^2+p[2]^2<q[1]^2+q[2]^2 end)
  for _,back in ipairs(backs)do for _,outward in ipairs(offsets)do candidates[#candidates+1]={back,outward}end end
  for _,candidate in ipairs(candidates)do
      local back,outward=candidate[1],candidate[2]
      local lateral=mirrored and -baseSpan-outward or outward
      local x,y=a[8]+fx*back+rx*lateral,a[9]+fy*back+ry*lateral
      local rightZ,rightKind=foot(x,y)
      if rightZ then
        local centre=lane.centre_distance_m+outward
        local first=lane.heads and lane.heads[1]
        -- Search immediately left of the INCOMING lanes first: a median is closer
        -- than the opposite verge. A naked road centreline never counts as an island.
        local span=first and first.distance_m+outward+(lane.lane_width_m or 3.3)/2+.35 or centre+roadHalf+.6
        local fit={x=x,y=y,z=rightZ,outward_m=outward,back_m=back,side=mirrored and'left'or'right',
          centre_distance_m=centre,carriageway_width_m=2*roadHalf,
          feet={{x,y,rightZ}},checked=probe and 'drawn_road_and_walk_meshes' or 'graph_carriageways',
          right_surface=rightKind,left_reason='no_safe_left_foot'}
        fit.reference_candidates=reference;fit.foot_source=candidate[3]or'drawn_surface_search'
        fit.stop_reference={a[2],a[3],a[4]};fit.stop_offset_m=(x-a[2])*dx+(y-a[3])*dy
        if mirrored then fit.mirror_span_m=baseSpan+outward;return fit end
        for extra=0,math.min(16,roadHalf+6),.25 do
          local reach=span+extra
          if reach>M.MAX_SPAN_M then break end
          local lx,ly=x-rx*reach,y-ry*reach
          local leftZ,leftKind=foot(lx,ly)
          if leftZ and (not probe or leftKind=='walk'or leftKind=='verge')then
            fit.left_span_m=reach;fit.left_z=leftZ;fit.left_surface=leftKind;fit.left_reason=nil
            fit.feet[2]={lx,ly,leftZ};break
          end
        end
        return fit
      end
  end
  return nil,probe and 'no_verified_right_ground' or 'no_clear_right_foot',{surface_rejections=rejected}
end

function M.alternativeFeet(graph,a,lane,fit,frame)
  local probe=require('beamngEarth/nl_signal_surfaces').begin(fit.x,fit.y,fit.z,frame)
  if not probe then return end
  local len=math.sqrt(a[5]^2+a[6]^2);local fx,fy,rx,ry=-a[5]/len,-a[6]/len,a[6]/len,-a[5]/len
  local width=lane.lane_width_m or 3.3
  local feet={right={position={0,0,0},foot={fit.x,fit.y,fit.z},edge_distance_m=lane.heads[#lane.heads].distance_m-width/2}}
  local edge=lane.heads[1].distance_m+width/2
  local backs,minBack,maxBack=M.searchOffsets(a,lane)
  for _,p in ipairs(fit.reference_candidates or{})do
    local back=(p[1]-fit.x)*fx+(p[2]-fit.y)*fy;local span=-((p[1]-fit.x)*rx+(p[2]-fit.y)*ry)
    if back+fit.back_m>=minBack and back+fit.back_m<=maxBack and span>=edge+.22 and span<=edge+3 then
      local ok,z,kind=probe(p[1],p[2],fit.z)
      if ok and (kind=='walk'or kind=='verge')then
        feet.left={position={back,-span,z-fit.z},foot={p[1],p[2],z},edge_distance_m=span-edge,source=p.source or'PDOK_BGT_paal'};return feet
      end
    end
  end
  -- Independent left feet may sit on an island a little before the right pole.
  for _,globalBack in ipairs(backs)do
    local back=globalBack-fit.back_m
    for gap=.35,3,.25 do
      local span=edge+gap;local x,y=fit.x+fx*back-rx*span,fit.y+fy*back-ry*span
      local ok,z,kind=probe(x,y,fit.z)
      if ok and (kind=='walk'or kind=='verge')then
        feet.left={position={back,-span,z-fit.z},foot={x,y,z},edge_distance_m=gap};return feet
      end
    end
  end
  if fit.left_span_m then
    feet.left={position={0,-fit.left_span_m,fit.left_z-fit.z},foot=fit.feet[2],edge_distance_m=fit.left_span_m-edge}
    return feet
  end
end

-- A wide portal may need its structural leg beyond another carriageway. Its low
-- lamp does not then replace a readable lamp beside the incoming lanes.
function M.roadsideRepeaters(graph,a,lane,fit,frame)
  local result,reasons={},{}
  local first,last=lane.heads and lane.heads[1],lane.heads and lane.heads[#lane.heads]
  if not first or not last or not fit.left_span_m then return result,reasons end
  local probe=require('beamngEarth/nl_signal_surfaces').begin(fit.x,fit.y,fit.z,frame)
  if not probe then return result,{reason='surface_provider_unavailable'}end
  local length=math.sqrt(a[5]^2+a[6]^2)
  local fx,fy,rx,ry=-a[5]/length,-a[6]/length,a[6]/length,-a[5]/length
  local width=lane.lane_width_m or 3.3
  local backs=M.searchOffsets(a,lane)
  local sides={{side='left',edge=first.distance_m+width/2,foot=fit.left_span_m,sign=1},
    {side='right',edge=last.distance_m-width/2,foot=0,sign=-1}}
  for _,s in ipairs(sides)do
    local gap=(s.foot-s.edge)*s.sign
    if gap>M.REMOTE_PORTAL_FOOT_M then
      reasons[s.side]='no_safe_nearby_foot'
      for _,globalBack in ipairs(backs)do
        local back=globalBack-fit.back_m
        if reasons[s.side]=='placed'then break end
        for outward=M.FOOT_CLEARANCE_M,M.REMOTE_PORTAL_FOOT_M,.25 do
          if gap-outward>=1 then
            local distance=s.edge+s.sign*outward
            local x,y=fit.x+fx*back-rx*distance,fit.y+fy*back-ry*distance
            local ok,z,kind=probe(x,y,fit.z)
            if ok and (kind=='walk'or kind=='verge')then
              local separate=true
              for _,foot in ipairs(fit.feet)do
                if (foot[1]-x)^2+(foot[2]-y)^2<1.5^2 then separate=false end
              end
              for _,p in ipairs(result)do
                if (p.foot[1]-x)^2+(p.foot[2]-y)^2<1.5^2 then separate=false end
              end
              if separate then
                result[#result+1]={side=s.side,position={back,-distance,z-fit.z},foot={x,y,z},
                  surface=kind,edge_distance_m=outward,portal_foot_distance_m=gap}
                reasons[s.side]='placed';break
              end
            end
          end
        end
      end
    else reasons[s.side]='portal_foot_near_lanes'end
  end
  return result,reasons
end
return M
