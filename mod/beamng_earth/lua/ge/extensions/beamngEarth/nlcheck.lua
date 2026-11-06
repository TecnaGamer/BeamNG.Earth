-- Read-only scene audit: independent of the fixture's internal object-ID list.
local M={}
local base='/levels/beamng_earth/art/shapes/beamngEarth/nl_testset/'
local catalog='/levels/beamng_earth/art/shapes/beamngEarth/nl_catalog/'
local function finite(x)return type(x)=='number' and x==x and math.abs(x)<math.huge end
function M.snapshot()
  local fixture=assert(extensions.beamngEarth_nltestset,'Load the testset extension first').report()
  local manifest=assert(jsonReadFile(base..'manifest.json'))
  local core=extensions.beamngEarth_core
  local current=core and core._state and core._state()
  local frame=current and current.anchor
  local terrain=scenetree.findObject('bngEarthTerrain')
  local expected={}
  local specs={}
  for _,a in ipairs(manifest.assemblies)do specs[a.id]=a end
  if frame then
    for _,loc in ipairs(fixture.outcome.locations or {})do
      local x,y,z=frame:geodeticToWorld(loc.lat,loc.lon,loc.height_m)
      expected[#expected+1]={x=x,y=y,z=z,id=loc.id,pole_offset_m=loc.pole_offset_m}
    end
  end
  local result={internal_objects=fixture.objects,actual_objects=0,posts=0,plates=0,clamp_pairs=0,
    fixture_status=fixture.outcome.status,anchorId=current and current.anchorId,
    anchor_lat=current and current.anchor and current.anchor.lat,anchor_lon=current and current.anchor and current.anchor.lon,
    terrain_available=terrain~=nil,ground_checks={},max_placement_error_m=0,max_surface_drift_m=0,max_terrain_gap_m=0}
  local poleNames={}
  for _,p in ipairs(manifest.poles)do poleNames[p.model]=true end
  for _,name in ipairs(scenetree.findClassObjects('TSStatic') or {})do
    local o=scenetree.findObject(name)
    local shape=o and o:getField('shapeName',0) or ''
    if shape:sub(1,#base)==base or shape:sub(1,#catalog)==catalog then
      result.actual_objects=result.actual_objects+1
      local model=shape:sub(#base+1)
      if poleNames[model]then
        result.posts=result.posts+1
        local pos=o:getPosition()
        local nearest,distance
        for _,p in ipairs(expected)do local d=(p.x-pos.x)^2+(p.y-pos.y)^2;if not distance or d<distance then nearest,distance=p,d end end
        if nearest and distance<.01 then
          local a=specs[nearest.id] or {pole_offset_m=assert(nearest.pole_offset_m,'Missing pole placement metadata')}
          local surface=be:getSurfaceHeightBelow(vec3(pos.x,pos.y,nearest.z+50))
          local ground=terrain and terrain:getHeight(vec3(pos.x,pos.y,nearest.z))
          local row={id=nearest.id,placement_error_m=pos.z-(nearest.z+a.pole_offset_m)}
          result.max_placement_error_m=math.max(result.max_placement_error_m,math.abs(row.placement_error_m))
          if finite(surface)then row.surface_drift_m=surface-nearest.z;result.max_surface_drift_m=math.max(result.max_surface_drift_m,math.abs(row.surface_drift_m))end
          if finite(ground)then row.terrain_gap_m=nearest.z-ground;result.max_terrain_gap_m=math.max(result.max_terrain_gap_m,math.abs(row.terrain_gap_m))end
          result.ground_checks[#result.ground_checks+1]=row
        end
      elseif model=='clamps_28cm.dae' or model=='clamps_10cm.dae' or model=='clamps_3cm.dae'then result.clamp_pairs=result.clamp_pairs+1
      else result.plates=result.plates+1 end
    end
  end
  result.counts_agree=result.actual_objects==result.internal_objects
  result.ground_note='Terrain/surface differences are diagnostics; buildings and road decks need explicit placement policy.'
  return result
end
function M.save()
  local filename='/settings/beamngEarth-nl-scene-audit.json'
  assert(jsonWriteFile(filename,M.snapshot(),true)~=false,'Could not save scene audit')
  return filename
end
return M
