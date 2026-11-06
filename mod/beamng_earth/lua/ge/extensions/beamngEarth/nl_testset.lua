-- Opt-in 40-sign synthetic acceptance fixture. Existing geodesy/geotile and core read-only state only.
local M = {}
local geotile -- Loaded only for a live geographic session; standalone Gridmap needs no Earth modules.
local base = '/levels/beamng_earth/art/shapes/beamngEarth/nl_testset/'
local ids, prepared, anchorId, geographic, bench = {}, nil, nil, false, nil
local outcome = {status='not_started', measured=false}
local assemblyObjects={}
local lastRenderDelta={created=0,reused=0,deleted=0}
local sizeProfiles
local function finite(x) return type(x)=='number' and x==x and math.abs(x)<math.huge end
local function state()
  local core = extensions and extensions.beamngEarth_core
  return core and core._state and core._state() or nil
end
local function removeObjects()
  for _,id in ipairs(ids) do local o=scenetree.findObjectById(id); if o then o:delete() end end
  for _,list in pairs(assemblyObjects)do for _,id in ipairs(list)do local o=scenetree.findObjectById(id);if o then o:delete()end end end
  ids={};assemblyObjects={}
end
function M.clear()
  local trees=package.loaded['beamngEarth/trees'];if trees and trees.setSourceSignReplacements then trees.setSourceSignReplacements({})end
  removeObjects(); prepared=nil; anchorId=nil; geographic=false; bench=nil
  outcome.status='cleared'
end
local function make(shape,x,y,z,yaw,plateScale)
  local o=createObject('TSStatic')
  o:setField('shapeName',0,shape:sub(1,1)=='/' and shape or base..shape)
  o:setField('collisionType',0,'None'); o:setField('decalType',0,'None')
  o.canSave=false; o:setScale(vec3(plateScale or 1,1,plateScale or 1))
  local h=math.rad(yaw+90)/2
  -- BeamNG rotates vectors with the conjugate of a Hamilton quaternion.
  o:setPosRot(x,y,z,0,0,math.sin(h),math.cos(h))
  o:registerObject(''); ids[#ids+1]=o:getID(); scenetree.MissionGroup:addObject(o)
end
local function show()
  loadJsonMaterialsFile(base..'main.materials.json')
  for _,a in ipairs(prepared.assemblies)do for _,b in ipairs(a.boards)do local s=prepared.manifest.signs[b.code];if s.material_file and not scenetree.findObject(s.material)then loadJsonMaterialsFile(s.material_file)end end end
  for _,name in ipairs({'bne_nl_test_A1_50','bne_nl_test_B6','bne_nl_test_C1','bne_nl_test_J16','bne_nl_test_metal'}) do
    assert(scenetree.findObject(name),'Test material did not register: '..name)
  end
  local previous,nextObjects=assemblyObjects,{}
  ids={};local delta={created=0,reused=0,deleted=0}
  local function drop(list)for _,id in ipairs(list or {})do local o=scenetree.findObjectById(id);if o then o:delete();delta.deleted=delta.deleted+1 end end end
  local ok,err=pcall(function()
    for _,a in ipairs(prepared.assemblies)do
      local parts={a.id or '',a.pole,string.format('%.6f|%.6f|%.6f|%.6f|%.7f',a.x,a.y,a.z,a.pole_offset_m,a.yaw or prepared.yaw)}
      for _,b in ipairs(a.boards)do parts[#parts+1]=table.concat({b.code,b.source_id or '',string.format('%.6f|%.7f|%.7f',b.centre_m,b.yaw or a.yaw or prepared.yaw,b.scale or 1),b.clamp_model or 'clamps_28cm.dae'},'|')end
      local key=table.concat(parts,';');local old=previous[key];local live=old~=nil
      if old then for _,id in ipairs(old)do if not scenetree.findObjectById(id)then live=false;break end end end
      if live then
        nextObjects[key]=old;for _,id in ipairs(old)do ids[#ids+1]=id;delta.reused=delta.reused+1 end
      else
        drop(old);local first=#ids+1
        make(a.pole,a.x,a.y,a.z+a.pole_offset_m,a.yaw or prepared.yaw)
        for _,b in ipairs(a.boards)do
          make(prepared.manifest.signs[b.code].model,a.x,a.y,a.z+b.centre_m,b.yaw or a.yaw or prepared.yaw,b.scale)
          make(b.clamp_model or 'clamps_28cm.dae',a.x,a.y,a.z+b.centre_m,b.yaw or a.yaw or prepared.yaw)
        end
        local list={};for i=first,#ids do list[#list+1]=ids[i];delta.created=delta.created+1 end;nextObjects[key]=list
      end
      previous[key]=nil
    end
    for _,list in pairs(previous)do drop(list)end
  end)
  if not ok then removeObjects();error(err)end
  assemblyObjects=nextObjects;lastRenderDelta=delta
end
-- Choose a clear, nearly flat area; all 39 ground points are sampled before objects are replaced.
function M.spawn(x,y,referenceZ,faceYawDeg)
  assert(finite(x) and finite(y) and finite(referenceZ) and finite(faceYawDeg),'Four finite placement arguments required')
  local manifest=assert(jsonReadFile(base..'manifest.json'),'Install the testset assets first')
  local current=state()
  assert(not (current and current.pending),'Wait until the current teleport/rebase has completed')
  local frame=current and current.active and current.anchor or nil
  if frame then geotile=require('beamngEarth/geotile') end
  local terrain=frame and assert(scenetree.findObject('bngEarthTerrain'),'Earth terrain is not ready')
  local candidate={manifest=manifest,assemblies={},yaw=faceYawDeg,tiles={},locations={}}
  for _,a in ipairs(manifest.assemblies) do
    local px,py=x+a.x_m,y+a.y_m
    -- Earth fixtures use terrain, never roofs or other collision surfaces.
    local z
    if terrain then z=terrain:getHeight(vec3(px,py,referenceZ))
    else z=be:getSurfaceHeightBelow(vec3(px,py,referenceZ+50)) end
    assert(finite(z) and math.abs(z-referenceZ)<30,'No reliable nearby surface: '..a.id)
    local item={id=a.id,x=px,y=py,z=z,pole=a.pole,pole_offset_m=a.pole_offset_m,boards=a.boards}
    candidate.assemblies[#candidate.assemblies+1]=item
    if frame then
      local lat,lon,h=frame:worldToGeodetic(px,py,z)
      local tx,ty=geotile.tileOf(lat,lon)
      local key=geotile.key(tx,ty)
      candidate.tiles[key]=(candidate.tiles[key] or 0)+1
      candidate.locations[#candidate.locations+1]={id=a.id,lat=lat,lon=lon,height_m=h,tile=key,synthetic=true}
    end
  end
  M.clear(); prepared=candidate; geographic=frame~=nil; anchorId=geographic and current.anchorId or nil
  local ok,err=pcall(show)
  if not ok then M.clear(); error(err) end
  outcome={status='spawned',measured=false,geographic=geographic,metrics=manifest.metrics,tiles=candidate.tiles,locations=candidate.locations,
    height_policy=geographic and 'Earth TerrainBlock height; no roof/road-deck snapping' or 'Generic collision surface (non-geographic)',
    heading_note='Explicit demonstration yaw in current world frame; not an NDW surveyed bearing',memory_note='GPU and total process memory not measured'}
  return #ids
end
-- Explicit preview only: source positions, assumed mounting; never infer shared posts.
function M.previewNDW(index,convention)
  assert(type(index)=='table' and index.kind=='ndw_small_area_preview' and index.schema_version==1,'Expected bounded NDW import')
  assert(convention=='travel_bearing' or convention=='front_bearing','Confirm source bearing convention explicitly')
  assert(type(index.records)=='table' and #index.records<=40,'Maximum 40 records')
  local current=assert(state(),'Start Earth first')
  assert(current.active and current.anchor and not current.pending,'Wait for an active stable Earth anchor')
  local frame=current.anchor
  local terrain=assert(scenetree.findObject('bngEarthTerrain'),'Earth terrain not ready')
  local car=assert(getPlayerVehicle(0),'Place a vehicle first'):getPosition()
  local manifest=assert(jsonReadFile(base..'manifest.json'))
  local planner=require('beamngEarth/nl_placement')
  local context=require('beamngEarth/nl_context')
  if not sizeProfiles then local p=jsonReadFile(base..'size_profiles.json');sizeProfiles=p and p.kind=='nl_plate_size_profiles' and p.shapes or {} end
  local gm=map and map.getMap and map.getMap()
  local edges=planner.fromMap(gm and gm.nodes,car.x,car.y,180)
  local candidate={manifest=manifest,assemblies={},yaw=0,tiles={},locations={}}
  local seen,skipped,rows={},{},{}
  geotile=require('beamngEarth/geotile')
  for _,r in ipairs(index.records)do
    assert(type(r.id)=='string' and not seen[r.id],'Duplicate/missing NDW id');seen[r.id]=true
    assert(finite(r.lat) and finite(r.lon) and r.lat>=50 and r.lat<=54 and r.lon>=3 and r.lon<=8,'Invalid WGS84')
    assert(finite(r.bearing_deg) and r.bearing_deg>=0 and r.bearing_deg<360,'Invalid bearing')
    assert(manifest.signs[r.asset],'Missing exact sign asset')
    local px,py=frame:geodeticToWorld(r.lat,r.lon,0)
    if(px-car.x)^2+(py-car.y)^2<=120^2 then
      local fx,fy=context.sourceFront(frame,px,py,r.bearing_deg,convention)
      local road=context.match(px,py,edges)
      local dimensions=context.dimensions(manifest.signs[r.asset],sizeProfiles[r.asset],road)
      rows[#rows+1]={id=r.id,x=px,y=py,asset=r.asset,bearing_deg=r.bearing_deg,mount_order=r.mount_order,source=r,
        road=road,dimensions=dimensions,rotation=context.rotation(fx,fy,road,r.bearing_deg)}
    else skipped[#skipped+1]={id=r.id,reason='outside_120m_preview_radius'}end
  end
  for _,group in ipairs(planner.group(rows))do
    local spot,why=planner.roadside(group.x,group.y,edges,group.rows[1].bearing_deg)
    if not spot then for _,row in ipairs(group.rows)do skipped[#skipped+1]={id=row.id,reason=why}end
    else
      local slots,sourceIds={{},{}},{}
      for _,row in ipairs(group.rows)do
        sourceIds[#sourceIds+1]=row.id
        local diff=math.abs(((row.bearing_deg-group.rows[1].bearing_deg+180)%360)-180)
        local slot=slots[diff<=90 and 1 or 2]
        slot[#slot+1]={code=row.asset,bearing=row.bearing_deg,source_id=row.id,size=row.dimensions,rotation=row.rotation,road=row.road}
        assert(not row.source.supplementary_assets or #row.source.supplementary_assets<=2,'Maximum two supplementary assets')
        for _,code in ipairs(row.source.supplementary_assets or {})do
          assert(manifest.signs[code] and manifest.signs[code].role=='supplementary','Unknown supplementary asset')
          slot[#slot+1]={code=code,bearing=row.bearing_deg,source_id=row.id,size=context.supplementary(manifest.signs[code],row.dimensions.scale),rotation=row.rotation,road=row.road}
        end
      end
      local needed,originalNeeded=0,0
      for _,slot in ipairs(slots)do local h,old=0,0;for i,b in ipairs(slot)do h=h+b.size.height_m+(i>1 and .06 or 0);old=old+manifest.signs[b.code].height_m+(i>1 and .06 or 0)end;needed=math.max(needed,h);originalNeeded=math.max(originalNeeded,old)end
      local top,pole=math.max(2.35+math.max(0,needed-originalNeeded),.6+needed)
      for _,p in ipairs(manifest.poles)do local buried=p.total_m-(top-.05);if buried>=.6-1e-6 and buried<=1.1+1e-6 then pole=p;break end end
      if not pole then for _,row in ipairs(group.rows)do skipped[#skipped+1]={id=row.id,reason='stack_exceeds_available_pole_length'}end
      else
        local z=terrain:getHeight(vec3(spot.x,spot.y,car.z))
        assert(finite(z) and math.abs(z-car.z)<30,'Unreliable terrain height')
        local boards,details={},{}
        for _,slot in ipairs(slots)do local cursor=top
          for _,b in ipairs(slot)do
            local yaw=context.yawForFront(b.rotation.fx,b.rotation.fy)
            local clamp=b.size.height_m<.15 and 'clamps_3cm.dae' or b.size.height_m<.3 and 'clamps_10cm.dae' or 'clamps_28cm.dae'
            boards[#boards+1]={code=b.code,centre_m=cursor-b.size.height_m/2,yaw=yaw,clamp_model=clamp,source_id=b.source_id,scale=b.size.scale}
            details[#details+1]={code=b.code,source_id=b.source_id,width_m=b.size.width_m,height_m=b.size.height_m,scale=b.size.scale,
              sizing_status=b.size.status,sizing_type=b.size.type,sizing_source=b.size.source,road=b.road,source_bearing_deg=b.bearing,
              rotation_status=b.rotation.status,axis_error_deg=b.rotation.axis_error_deg,alignment_delta_deg=b.rotation.delta_deg,possible_reverse_facing=b.rotation.possible_reverse_facing,face_yaw_deg=yaw}
            cursor=cursor-b.size.height_m-.06
          end
        end
        local offset=top-.05-pole.source_above_m
        candidate.assemblies[#candidate.assemblies+1]={id=group.rows[1].id,x=spot.x,y=spot.y,z=z,yaw=boards[1].yaw,pole=pole.model,pole_offset_m=offset,boards=boards}
        local lat,lon,h=frame:worldToGeodetic(spot.x,spot.y,z)
        local tx,ty=geotile.tileOf(lat,lon);local key=geotile.key(tx,ty)
        candidate.tiles[key]=(candidate.tiles[key] or 0)+1
        candidate.locations[#candidate.locations+1]={id=group.rows[1].id,source_ids=sourceIds,lat=lat,lon=lon,height_m=h,tile=key,synthetic=false,pole_offset_m=offset,face_yaw_deg=boards[1].yaw,source_x=group.x,source_y=group.y,roadside_shift_m=spot.shift_m,board_count=#boards,boards=details,board_top_m=top,pole_top_m=top-.05,mounting='proximity grouping and generator order; source order used where supplied'}
      end
    end
  end
  if not(geographic and anchorId==current.anchorId)then M.clear()end
  prepared=candidate;geographic=true;anchorId=current.anchorId;bench=nil
  local ok,err=pcall(show);if not ok then M.clear();error(err)end
  local points={}
  for _,a in ipairs(candidate.assemblies)do for _,b in ipairs(a.boards)do
    local kind=(b.code=='B6'or b.code=='B7')and'control'or b.code:match('^A1%-')and'speed'
    if kind then local h=math.rad(b.yaw+90);local dx,dy=require('beamngEarth/geodesy').beamngRotate(0,0,math.sin(h/2),math.cos(h/2),0,-1,0);points[#points+1]={x=a.x,y=a.y,fx=dx,fy=dy,kind=kind}end
  end end
  local trees=package.loaded['beamngEarth/trees'];if trees and trees.setSourceSignReplacements then trees.setSourceSignReplacements(points)end
  outcome={status=#ids>0 and'ndw_preview_spawned'or'placement_blocked',measured=false,geographic=true,source='NDW',bearing_convention=convention,locations=candidate.locations,tiles=candidate.tiles,skipped=skipped,height_policy='TerrainBlock, conservative navigation-road exclusion; absent geometry is rejected',mounting_note='Nearby source records grouped; source mounting order preferred, then explicit generator priority'}
  return #ids
end
function M.nearCar(faceYawDeg)
  local car=assert(getPlayerVehicle(0),'Place a vehicle first')
  local p=car:getPosition()
  return M.spawn(p.x+10,p.y+10,p.z,faceYawDeg or -90)
end
local function stats(samples)
  local sorted,total={},0
  for _,dt in ipairs(samples) do sorted[#sorted+1]=dt; total=total+dt end
  table.sort(sorted)
  if #sorted==0 then return {frames=0} end
  return {frames=#sorted,seconds=total,average_fps=#sorted/total,median_frame_ms=sorted[math.ceil(#sorted*.5)]*1000,p95_frame_ms=sorted[math.ceil(#sorted*.95)]*1000}
end
-- Keep the camera and all graphics settings fixed. 3 s warmup then 10 s samples per phase.
function M.benchmark()
  assert(prepared and not bench,'Spawn the fixture first; wait for any current benchmark')
  removeObjects()
  outcome.measured=false
  bench={phase='baseline',warmup=3,elapsed=0,samples={},results={}}
  return 'baseline: 3 s warmup + 10 s; then fixture: 3 s warmup + 10 s'
end
function M.onUpdate(dtReal)
  if not prepared then return end
  local current=state()
  if geographic and (not current or current.anchorId~=anchorId or current.pending or not current.active) then
    M.clear(); outcome.status='cleared_on_anchor_change'; return
  end
  local car=getPlayerVehicle and getPlayerVehicle(0)
  if car then
    local p=car:getPosition(); local a=prepared.assemblies[1]
    if a and (p.x-a.x)^2+(p.y-a.y)^2>150^2 then M.clear(); outcome.status='cleared_outside_test_area'; return end
  end
  if not bench or not finite(dtReal) or dtReal<=0 then return end
  if bench.warmup>0 then bench.warmup=bench.warmup-dtReal; return end
  bench.samples[#bench.samples+1]=dtReal; bench.elapsed=bench.elapsed+dtReal
  if bench.elapsed>=10 then
    bench.results[bench.phase]=stats(bench.samples)
    if bench.phase=='baseline' then
      local ok,err=pcall(show)
      if not ok then bench=nil; outcome.status='benchmark_spawn_failed'; outcome.error=tostring(err); return end
      bench.phase='fixture'; bench.warmup=3; bench.elapsed=0; bench.samples={}
    else
      outcome.benchmark=bench.results; outcome.measured=true; outcome.status='benchmark_complete'; bench=nil
      log('I','nl_testset','Benchmark complete; call beamngEarth_nltestset.saveReport()')
    end
  end
end
function M.report()
  return {outcome=outcome,objects=#ids,render_delta=lastRenderDelta,benchmark_phase=bench and bench.phase or nil,limitations={'TSStatic only; Forest comparison pending','Lua/game frame timings; no isolated GPU timings','Synthetic fixture or explicit NDW preview; see outcome source and mounting assumptions','Automatic cleanup on rebase, not production tile streaming'}}
end
function M.saveReport()
  local filename='/settings/beamngEarth-nl-testset-report.json'
  assert(jsonWriteFile(filename,M.report(),true)~=false,'Could not save test report')
  return filename
end
M.onExtensionUnloaded=M.clear
M.onClientEndMission=M.clear
return M
