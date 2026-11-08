-- Client-only Groenpol selection. Reuses both fresh and cached server signal placements.
-- Source: user's confirmed Groenpol.fbx. The graph has no mode-specific crossing phases.
local M = {}
local country = require('beamngEarth/nl_country')
local mounts = require('beamngEarth/nl_signal_mounts')
local lanes = require('beamngEarth/nl_signal_lanes')
local context = require('beamngEarth/nl_signal_context')
local clearance = require('beamngEarth/nl_signal_clearance')
M.SINGLE_LANE_OVERHEAD={trunk=true,primary=true,secondary=true}
M.EXIT_CLASSES={motorway_link=true,motorway=true,trunk_link=true}
M.ROOT = '/levels/beamng_earth/art/shapes/beamngEarth/groenpol/'
M.SHAPE = M.ROOT .. 'head_vehicle_sven_v4.dae'
M.SUPPORT_SHAPE = M.ROOT .. 'mast_straight_sven_v4.dae'
M.STATIC_SHAPE = M.ROOT .. 'static_vehicle_sven_v4.dae'
M.STATIC_SHAPE_300 = M.ROOT .. 'static_vehicle_300_sven_v4.dae'
M.RESOURCES = { 'head_vehicle_sven_v4.dae', 'mast_straight_sven_v4.dae', 'components.json', 'main.materials.json', 'groenpol_nl_BaseColor.png',
  'groenpol_nl_d.png',
  'lens_channel_0_v2.color.png', 'lens_channel_1_v2.color.png', 'lens_channel_2_v2.color.png' }
local loaded = false
local zones=setmetatable({},{__mode='k'})
function M.junctionZone(graph,junction,frame)
  if not(graph and graph.signals and frame and frame.worldToGeodetic)then return end
  local by=zones[graph];if not by then by={};zones[graph]=by end
  if by[junction]~=nil then return by[junction]or nil end
  for _,j in ipairs(graph.signals)do if j.j==junction then
    for _,a in ipairs(j.app or{})do
      local meta=type(a[12])=='table'and a[12]or{}
      local zone=meta.zone
      if not zone and a[2]and a[3]and a[4]then
        local lat,lon=frame:worldToGeodetic(a[2],a[3],a[4]);local la,lo=frame:worldToGeodetic(a[2]+a[5],a[3]+a[6],a[4])
        local x,y=context.mercator(lat,lon);local xx,yy=context.mercator(la,lo)
        zone=context.lookup(lat,lon,xx-x,yy-y,1).zone
      end
      if zone=='bubeko'then by[junction]='bubeko';return'bubeko'end
    end
  end end
  by[junction]=false
end
local function finite(n) return type(n) == 'number' and n == n and math.abs(n) < math.huge end

function M.available(exists)
  if not mounts.catalog then return false, 'component_catalog_unavailable' end
  exists = exists or function(p) return FS and FS:fileExists(p) end
  for _, name in ipairs(M.RESOURCES) do if not exists(M.ROOT .. name) then return false, name end end
  return true
end

local function laneContext(a,frame,graph,junction)
  local preliminary=lanes.describe(graph,junction,a)
  local okProbe,lat,lon=pcall(frame.worldToGeodetic,frame,a[2],a[3],a[4])
  local ctx={zone='unknown',zone_source='context_unavailable'}
  if okProbe then
    local okDir,la,lo=pcall(frame.worldToGeodetic,frame,a[2]+a[5],a[3]+a[6],a[4])
    if okDir then
      local x,y=context.mercator(lat,lon);local xx,yy=context.mercator(la,lo)
      ctx=context.lookup(lat,lon,xx-x,yy-y,preliminary.count)
    end
  end
  local lp=lanes.describe(graph,junction,a,ctx)
  local meta=type(a[12])=='table'and a[12]or{}
  local exit=M.EXIT_CLASSES[lp.road_class]or M.EXIT_CLASSES[ctx.road_class]or M.EXIT_CLASSES[meta.osm_source_road_class]
  if M.junctionZone(graph,junction,frame)=='bubeko'or exit then
    if not exit then ctx.zone='bubeko';ctx.zone_source='junction_bubeko_maximum';lp.zone='bubeko'end
    lp.lens_source=exit and'motorway_exit'or'junction_bubeko_maximum'
    lp.lens_diameter_mm=300
    for _,h in ipairs(lp.heads or{})do h.variant=lanes.variant(h.turn,300)end
  end
  return lp,ctx
end

-- Pure: a is the existing 11-field graph approach, frame the current world anchor.
-- The prop's position determines the country, including mixed tiles and Belgian enclaves.
-- +X is the prepared model's front, which faces back along the incoming travel direction.
function M.plan(a, frame, stock, available, graph, junction, preferAlternative,search)
  local p = { shape = stock, model = 'stock', reason = 'position_unavailable', yaw = a[11] or 0,
    x = a[8], y = a[9], z = a[10], stop = { a[2], a[3], a[4] }, dir = { a[5], a[6] },
    approach_key=a[1],junction_key=junction }
  if not (finite(p.x) and finite(p.y) and finite(p.z)) then return p end
  if not (frame and frame.worldToGeodetic) then p.reason = 'frame_unavailable'; return p end
  local ok, lat, lon = pcall(frame.worldToGeodetic, frame, p.x, p.y, p.z)
  if not ok then p.reason = 'frame_unavailable'; return p end
  p.lat, p.lon = lat, lon
  if not country.contains(lat, lon) then p.reason = 'outside_netherlands'; return p end
  p.nl=true;p.omit=true -- a failed Dutch placement must never resurrect a stock boom
  if not (finite(a[5]) and finite(a[6])) or a[5]^2 + a[6]^2 < .5 then
    p.reason = 'direction_unavailable'; return p
  end
  if not available then p.reason = 'asset_unavailable'; return p end
  p.lane_plan,p.data_context=laneContext(a,frame,graph,junction)
  local options={kind='straight'}
  local fit,fitReason,fitDetails
  if graph and not p.lane_plan.centre_distance_m then p.reason='unverified_lane_geometry';return p end
  if graph and p.lane_plan.centre_distance_m then
    fit,fitReason,fitDetails=clearance.fit(graph,a,p.lane_plan,frame,search)
    if not fit and p.lane_plan.count<=3 and not(search and search.side)then
      local left={side='left',back=search and search.back}
      fit,fitReason,fitDetails=clearance.fit(graph,a,p.lane_plan,frame,left)
    end
    if not fit then p.reason=fitReason;p.placement_failure=fitDetails;return p end
    p.x,p.y,p.z=fit.x,fit.y,fit.z;p.placement=fit
    p.source_foot={a[8],a[9],a[10]}
    options.left_span_m=fit.left_span_m;options.carriageway_width_m=fit.carriageway_width_m
    options.include_left=fit.left_span_m~=nil
    options.left_height_delta_m=(fit.left_z or fit.z)-fit.z
    if fit.side=='left'then options.right_variant=p.lane_plan.heads[1].variant end
    for _,h in ipairs(p.lane_plan.heads or {})do
      h.original_distance_m=h.distance_m
      h.distance_m=fit.side=='left'and fit.mirror_span_m-h.distance_m or h.distance_m+fit.outward_m
    end
  end
  local variant=lanes.variant(nil,p.lane_plan.lens_diameter_mm)
  local first=p.lane_plan.heads and p.lane_plan.heads[1]
  if first then variant=first.variant end
  local layout=mounts.plan({{id='primary',variant=variant,lane_id=first and first.lane_id}},options)
  local overhead=p.lane_plan.count>1 or M.SINGLE_LANE_OVERHEAD[p.lane_plan.road_class]
  if overhead and not p.lane_plan.reason then
    local reach=0
    for _,h in ipairs(p.lane_plan.heads)do
      reach=math.max(reach,h.distance_m)
    end
    options.kind=nil;options.arm_length_m=reach
    local proposed=mounts.plan(p.lane_plan.heads,options)
    if fit and fit.side~='left'and (preferAlternative or not proposed.ready)then
      local feet=clearance.alternativeFeet(graph,a,p.lane_plan,fit,frame)
      local alternatives=mounts.alternatives(p.lane_plan.heads,feet)
      if alternatives[1]then
        proposed=alternatives[1]
        fit.feet={feet.right.foot,feet.left.foot};fit.alternative_feet=feet
      end
    end
    if proposed.ready then layout=proposed
    else
      p.lane_plan.reason=proposed.reason
      if proposed.reason=='no_safe_portal_left_foot'then
        variant=lanes.variant(nil,p.lane_plan.lens_diameter_mm)
        options.kind='straight';options.arm_length_m=0
        layout=mounts.plan({{id='primary',variant=variant}},options)
      end
    end
  end
  if not layout.ready then p.reason = layout.reason; return p end
  if fit and fit.side=='left'then
    if layout.kind=='portal'then p.reason='no_safe_right_portal_foot';return p end
    mounts.mirrorLeft(layout)
  end
  if fit and layout.kind=='portal'then
    local repeaters,reasons=clearance.roadsideRepeaters(graph,a,p.lane_plan,fit,frame)
    fit.roadside_repeaters=repeaters;fit.roadside_repeater_reasons=reasons
    if #repeaters>0 then
      options.roadside_repeaters=repeaters
      layout=mounts.plan(p.lane_plan.heads,options)
      if not layout.ready then p.reason=layout.reason;return p end
      for _,r in ipairs(repeaters)do fit.feet[#fit.feet+1]=r.foot end
    end
  end
  p.yaw = math.deg(math.atan2(-a[6], -a[5]))
  mounts.aimHeads(layout,p,a,p.lane_plan)
  p.parts, p.layout, p.signature = layout.parts, layout, mounts.signature(layout)
  p.shape, p.model, p.reason = mounts.catalog.heads[variant].shape, 'groenpol_vehicle', 'nl_vehicle_approach'
  p.omit=false
  p.yaw = math.deg(math.atan2(-a[6], -a[5]))
  return p
end

-- Only nearly coincident, same-facing stops on different source junctions collapse.
-- Parallel approaches and subsequent junctions retain their own controllers.
function M.duplicates(graph,frame)
  local rows,blocked={},{}
  for _,j in ipairs(graph.signals or {})do if j.kind=='signal' then for i,a in ipairs(j.app or {})do
    if a[8] and frame and frame.worldToGeodetic then
      local ok,lat,lon=pcall(frame.worldToGeodetic,frame,a[8],a[9],a[10])
      if ok and country.contains(lat,lon)then rows[#rows+1]={j=j.j,i=i,a=a,key='bne_'..j.j..'_'..i} end
    end
  end end end
  table.sort(rows,function(a,b)return a.key<b.key end)
  for i=1,#rows do if not blocked[rows[i].key]then for k=i+1,#rows do
    local x,y=rows[i],rows[k];local a,b=x.a,y.a
    local dx,dy=b[2]-a[2],b[3]-a[3]
    if a[5]*b[5]+a[6]*b[6]>.985 and math.abs((b[4] or 0)-(a[4] or 0))<1.5
      and math.abs(dx*a[5]+dy*a[6])<5 and math.abs(-dx*a[6]+dy*a[5])<1.2 then
      blocked[y.key]={reason='coincident_same_facing_stop',kept=x.key}
    end
  end end end
  return blocked
end

function M.ensureMaterials()
  if loaded then return true end
  local ok,result=pcall(loadJsonMaterialsFile,M.ROOT..'main.materials.json')
  if ok and result~=false then loaded=true;return true end
  return false
end

-- Static scenery uses the same authored parts with dark lenses, including outside the AI window.
function M.staticShape(frame,x,y,z)
  if not (frame and frame.worldToGeodetic) then return end
  local ok,lat,lon=pcall(frame.worldToGeodetic,frame,x,y,z)
  if not ok or not country.contains(lat,lon) then return end
  local ctx=context.lookup(lat,lon,0,0,1)
  local shape=ctx.zone=='bubeko' and M.STATIC_SHAPE_300 or M.STATIC_SHAPE
  if M.available() and FS:fileExists(shape) and M.ensureMaterials() then
    return shape:sub(1,-5),1
  end
end

-- A dark row outside the native AI window still gets a lane-correct physical plan.
function M.staticPlan(frame,x,y,z,yaw)
  if not (frame and frame.worldToGeodetic and frame.geodeticToWorld) then return end
  local ok,lat,lon=pcall(frame.worldToGeodetic,frame,x,y,z)
  if not ok or not country.contains(lat,lon) then return end
  local angle=math.rad(yaw+90);local dx,dy=math.cos(angle),math.sin(angle)
  local la,lo=frame:worldToGeodetic(x+dx,y+dy,z)
  local mx,my=context.mercator(lat,lon);local xx,yy=context.mercator(la,lo)
  local ctx=context.lookup(lat,lon,xx-mx,yy-my,nil)
  local n=ctx.road_lane_count or 1
  if n<1 or n>5 or n%1~=0 then n=1 end
  local opposite=ctx.road_opposite_count
  if opposite==nil then opposite=n end
  local radius=(n+opposite)*3.3/2
  local cx,cy=x-dy*(radius+1),y+dx*(radius+1)
  if ctx.road_x then
    local longitude=ctx.road_x*180/20037508.342789244
    local latitude=math.deg(2*math.atan(math.exp(ctx.road_y/20037508.342789244*math.pi))-math.pi/2)
    cx,cy=frame:geodeticToWorld(latitude,longitude,z)
  end
  local a={'source',cx,cy,z,dx,dy,0,x,y,z,yaw,
    {zone=ctx.zone,turns=ctx.turns,road_class=ctx.road_class}}
  local g={nodes={source={cx-dx*50,cy-dy*50,z,radius},junction={cx+dx*30,cy+dy*30,z,radius}},
    edges={{'source','junction',opposite==0 and 1 or 0,n,opposite,13,1,radius,radius}}}
  local p=M.resolve(a,frame,'stock',g,'junction')
  if p.model=='stock' then return end
  p.static_lane_source=ctx.road_lane_count and 'OSM_lane_context' or 'one_lane_fallback_unresolved'
  return p
end

local function keepAccepted(a,frame,graph,junction,h)
  if not(h and h.saved_plan and h.model~='stock'and frame and frame.worldToGeodetic)then return end
  local saved=h.saved_plan
  local oldHeight=h.sourcez or saved.source_foot and saved.source_foot[3]
  if oldHeight and a[10]and math.abs(a[10]-oldHeight)>2 then return end
  if saved.approach_key and(saved.approach_key~=a[1]or saved.junction_key~=junction)then return end
  if not(a[2]and a[3]and a[4]and a[5]and a[6])then return end
  if (a[2]-saved.stop[1])^2+(a[3]-saved.stop[2])^2>4 or math.abs(a[4]-saved.stop[3])>2 then return end
  local len=math.sqrt(a[5]^2+a[6]^2)
  if len<.5 or(a[5]*saved.dir[1]+a[6]*saved.dir[2])/len<.996 then return end
  local lp=laneContext(a,frame,graph,junction)
  if not(saved.placement and saved.layout and saved.lane_plan and saved.layout.head_count==lp.count
    and lp.count==saved.lane_plan.count and lp.lens_diameter_mm==saved.lane_plan.lens_diameter_mm)then return end
  if lp.lane_width_m and saved.lane_plan.lane_width_m and math.abs(lp.lane_width_m-saved.lane_plan.lane_width_m)>.25 then return end
  for i,head in ipairs(lp.heads or{})do
    local old=saved.lane_plan.heads and saved.lane_plan.heads[i]
    if old and old.variant~=head.variant then return end
  end
  local scene=require('beamngEarth/nl_signal_scene');scene.reconcile(h);if not scene.alive(h)then return end
  local probe=require('beamngEarth/nl_signal_surfaces').begin(saved.x,saved.y,saved.z,frame)
  local pending,updates=false,{}
  for index,entry in ipairs(h.parts)do if entry.role=='support'and probe then
    local foot=mounts.worldPose(h,entry.localPose);local ok,z,why=probe(foot.x,foot.y,foot.z)
    if not ok then
      if why=='pending'or why=='ground_unavailable'or why=='terrain_material_unavailable'or why=='walk_ground_mismatch'then pending=true
      else return end
    elseif math.abs(z-foot.z)>.5 then return
    elseif math.abs(z-foot.z)>.001 then updates[#updates+1]={index=index,z=z}end
  end end
  -- Accepted geometry reserves its place. A new proposal must avoid it, rather
  -- than both owners repeatedly moving away from each other on every refresh.
  if not pending and #updates>0 then scene.adjustGround(h,updates)end
  saved.placement.surface_pending=pending;saved.placement.accepted_lock=true
  return saved
end

function M.place(a,frame,stock,available,graph,junction,others,name)
  local existing=others and others[name]
  local kept=keepAccepted(a,frame,graph,junction,existing)
  if kept then return kept end
  local p=M.plan(a,frame,stock,available,graph,junction)
  local space=require('beamngEarth/nl_signal_space')
  local conflict=space.conflict(p,others,name)
  local shared=require('beamngEarth/nl_signal_shared')
  for _,candidate in ipairs(shared.candidates(p,others,name,frame))do
    if not space.conflict(candidate,others,name)then return candidate end
    -- If the shared backboard obstructs another assembly, retain bare housings.
    candidate=shared.withoutShields(candidate,others,name)
    if not space.conflict(candidate,others,name)then return candidate end
  end
  if not conflict then return p end
  local alt=M.plan(a,frame,stock,available,graph,junction,true)
  if not alt.omit and not space.conflict(alt,others,name)then alt.avoided_overlap=conflict;return alt end
  if p.lane_plan and p.lane_plan.count<=3 then
    local left=M.plan(a,frame,stock,available,graph,junction,false,{side='left'})
    if not left.omit and not space.conflict(left,others,name)then left.avoided_overlap=conflict;return left end
  end
  for _,back in ipairs(clearance.searchOffsets(a,p.lane_plan))do
    local q=M.plan(a,frame,stock,available,graph,junction,false,{back=back})
    if not q.omit and not space.conflict(q,others,name)then q.avoided_overlap=conflict;return q end
  end
  p.omit=true;p.reason='nearby_signal_geometry_conflict';return p
end

function M.resolve(a, frame, stock, graph, junction,others,name)
  local available = M.available()
  local p = M.place(a, frame, stock, available, graph, junction,others,name)
  if p.model ~= 'stock' then
    for _,part in ipairs(p.parts)do
      if not (FS and FS:fileExists(part.shape)) then
        p=M.plan(a,frame,stock,false);p.reason='component_asset_unavailable';break
      end
    end
  end
  if p.model ~= 'stock' and not loaded then
    if not M.ensureMaterials() then
      p = M.plan(a, frame, stock, false); p.reason = 'material_load_failed'
    end
  end
  return p
end

-- Read-only audit: no scene objects, material loading, tile writes or cache access.
function M.audit(graph, frame, stock, available)
  if available == nil then available = M.available() end
  local blocked=M.duplicates(graph,frame)
  local occupied={}
  local result = { approaches = {}, groenpol = 0, stock = 0, omitted=0, stops = 0,duplicates=blocked,
    placement_source = 'existing_graph_inferred_approaches',
    deferred_modes = { 'bicycle: no mode-specific graph data', 'pedestrian: no crossing phases/stop lines' } }
  for _, j in ipairs(graph.signals or {}) do
    for i, a in ipairs(j.app or {}) do
      if j.kind == 'signal' then
        local instance='bne_'..j.j..'_'..i
        local p = M.place(a, frame, stock, available, graph, j.j,occupied,instance)
        if blocked[instance]then p.omit=true;p.reason=blocked[instance].reason end
        p.junction, p.approach, p.phase = j.j, a[1], a[7]
        p.instance = 'bne_' .. j.j .. '_' .. i
        if not p.omit and p.model~='stock'then occupied[p.instance]=p end
        result.approaches[#result.approaches + 1] = p
        local key=p.omit and 'omitted' or p.model=='stock' and 'stock' or 'groenpol'
        result[key]=result[key]+1
      else result.stops = result.stops + 1 end
    end
  end
  return result
end

function M.reset() loaded = false;context.reset() end
return M
