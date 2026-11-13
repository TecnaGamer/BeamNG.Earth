-- Read the same native road/sidewalk shapes the client draws, without changing map files.
local M={FOOT_RADIUS_M=.22}
local bit=require('bit')
local CELL=8
local cache=setmetatable({},{__mode='k'})
local frameSpent
M.stats={loaded=0,deferred=0,unreadable=0,decode_failed=0}
function M.newFrame()frameSpent=0 end
local function le(s,i,n)local v=0;for k=n-1,0,-1 do v=v*256+s:byte(i+k)end;return v end
local function be(s,i,n)local v=0;for k=0,n-1 do v=v*256+s:byte(i+k)end;return v end
local function float(s,i,big)
  -- BeamNG's embedded FFI does not expose these scalar pointer reads like stock
  -- LuaJIT. Decode IEEE754 explicitly so the game and the offline reader agree.
  local word=big and be(s,i,4)or le(s,i,4)
  local sign=word>=2147483648 and -1 or 1
  local exponent=math.floor(word/8388608)%256;local mantissa=word%8388608
  if exponent==0 then return sign*mantissa*2^-149 end
  assert(exponent~=255,'non-finite native mesh coordinate')
  return sign*(1+mantissa/8388608)*2^(exponent-127)
end
-- The small, uncompressed msgpack subset written by server/cdae.py (TSShape v31).
function M.decode(s)
  assert(#s>=8 and le(s,1,4)%256==31,'unsupported native shape')
  local cursor=9
  local function value()
    local t=s:byte(cursor);assert(t,'truncated native shape');cursor=cursor+1
    if t<128 then return t elseif t>=224 then return t-256 end
    if t==192 then return nil elseif t==194 or t==195 then return t==195 end
    if t==202 then local v=float(s,cursor,true);cursor=cursor+4;return v end
    local sizes={[204]=1,[205]=2,[206]=4,[208]=1,[209]=2,[210]=4}
    if sizes[t]then local n=sizes[t];local v=be(s,cursor,n);cursor=cursor+n
      if t>=208 and v>=2^(n*8-1)then v=v-2^(n*8)end;return v end
    local n,kind
    if t>=160 and t<=191 then n=t-160;kind='string'
    elseif t>=144 and t<=159 then n=t-144;kind='array'
    elseif t>=128 and t<=143 then n=t-128;kind='map'
    else
      local widths={[196]=1,[197]=2,[198]=4,[217]=1,[218]=2,[219]=4,[220]=2,[221]=4,[222]=2,[223]=4}
      local w=assert(widths[t],'unsupported native shape encoding');n=be(s,cursor,w);cursor=cursor+w
      kind=(t<=198 or t<=219)and 'string' or t<=221 and 'array' or 'map'
    end
    assert(n<=#s,'invalid native shape length')
    if kind=='string'then local v=s:sub(cursor,cursor+n-1);assert(#v==n,'truncated native shape');cursor=cursor+n;return v end
    local a={};for i=1,n do if kind=='map'then local k=value();a[k]=value()else a[i]=value()end end;return a
  end
  local header=value();assert(not header.compression and cursor==9+le(s,5,4),'unsupported shape body')
  local function vector()local n,size,data=value(),value(),value();assert(type(data)=='string'and #data==n*size,'invalid shape vector');return data,n,size end
  for _=1,6 do value()end
  for _=1,19 do vector()end
  for _=1,value()do value()end
  local meshes={}
  for _=1,value()do
    local kind=value()
    if kind~=4 then
      assert(kind==0,'unsupported mesh type')
      for _=1,6 do value()end
      local m={};m.verts=vector();vector();vector();vector();vector();vector();m.primitives=vector();m.indices=vector();vector()
      value();value();meshes[#meshes+1]=m
    end
  end
  assert(value()==0,'animated road shape')
  local materials={}
  for i=1,value()do materials[i]=value();for _=1,6 do value()end end
  local grid={};local triangles=0
  for _,m in ipairs(meshes)do
    local vertices={}
    for at=1,#m.verts,4 do vertices[#vertices+1]=float(m.verts,at)end
    for p=1,#m.primitives,12 do
      local start,n,flags=le(m.primitives,p,4),le(m.primitives,p+4,4),le(m.primitives,p+8,4)
      assert(bit.band(flags,0xC0000000)==0,'nontriangle road primitive')
      local material=materials[bit.band(flags,0x0FFFFFFF)+1]
      for k=start,start+n-3,3 do
        assert(k>=0 and k+2<#m.indices/4,'native shape index outside buffer')
        local row={material=material}
        for j=0,2 do
          local ix=le(m.indices,(k+j)*4+1,4)*3
          assert(ix>=0 and ix+2<#m.verts/4,'native shape vertex outside buffer')
          row[j*3+1],row[j*3+2],row[j*3+3]=vertices[ix+1],vertices[ix+2],vertices[ix+3]
        end
        local ax,ay,az,bx,by,bz=row[4]-row[1],row[5]-row[2],row[6]-row[3],row[7]-row[1],row[8]-row[2],row[9]-row[3]
        local nx,ny,nz=ay*bz-az*by,az*bx-ax*bz,ax*by-ay*bx
        -- Ignore walls, kerb faces and steep embankment sides; only broad upward surfaces support a pole.
        if nz*nz>.8*(nx*nx+ny*ny+nz*nz) and math.abs(nz)>1e-8 then
          triangles=triangles+1
          for x=math.floor(math.min(row[1],row[4],row[7])/CELL),math.floor(math.max(row[1],row[4],row[7])/CELL)do
            for y=math.floor(math.min(row[2],row[5],row[8])/CELL),math.floor(math.max(row[2],row[5],row[8])/CELL)do
              local key=x..','..y;grid[key]=grid[key]or{};grid[key][#grid[key]+1]=row
            end
          end
        end
      end
    end
  end
  return {grid=grid,triangles=triangles}
end
function M.height(mesh,x,y,reference)
  local best
  for _,r in ipairs(mesh.grid[math.floor(x/CELL)..','..math.floor(y/CELL)]or{})do
    local ax,ay,bx,by=x-r[1],y-r[2],r[4]-r[1],r[5]-r[2]
    local cx,cy=r[7]-r[1],r[8]-r[2];local det=bx*cy-by*cx
    local u,v=(ax*cy-ay*cx)/det,(bx*ay-by*ax)/det
    if u>=-1e-6 and v>=-1e-6 and u+v<=1.000001 then
      local z=r[3]+u*(r[6]-r[3])+v*(r[9]-r[3])
      if math.abs(z-reference)<4 and(not best or z>best)then best=z end
    end
  end
  return best
end
local groundMaterials={bngEarthGroundGrass=true,bngEarthGroundDusty=true,bngEarthGroundLoose=true,
  bngEarthGroundRock=true,bngEarthGroundSnow=true,bngEarthGroundMud=true,bngEarthGroundLawn=true,
  bngEarthGroundGolfRough=true,bngEarthGroundGolfFairway=true,bngEarthGroundGolfGreen=true,
  bngEarthGroundGolfBunker=true,beamngEarthDirt=true}
local roadMaterials={bngEarthGroundAsphalt=true,bngEarthGroundCobble=true}
function M.groundKind(x,y,z)
  -- Since 1.8.2 the carriageway can be painted into the TerrainBlock, leaving
  -- only markings in its road mesh. A missing triangle is not a free verge.
  local terrain=rawget(_G,'core_terrain')
  if not(terrain and terrain.getTerrain)then return end
  local ok,kind=pcall(function()
    local block=terrain.getTerrain();if not block then return end
    local idx=block:getMaterialIdxWs(vec3(x,y,z))
    if type(idx)~='number'or idx<0 or idx>=255 then return end
    local own=package.loaded['beamngEarth/terrain'];local slots=own and own.slots
    local name=slots and slots[idx+1]or block:getMaterialName(idx)
    if roadMaterials[name]then return 'road' end
    -- A single default dirt layer has no road mask and proves nothing about
    -- asphalt. Require an active material set that actually distinguishes roads.
    local masked=false
    if slots then for _,nm in ipairs(slots)do if roadMaterials[nm]then masked=true;break end end
    else for i=0,block:getMaterialCount()-1 do if roadMaterials[block:getMaterialName(i)]then masked=true;break end end end
    if masked and groundMaterials[name]then return 'verge' end
  end)
  if ok then return kind end
end
local function load(part)
  local owner=part.owner or part;local by=cache[owner]
  if not by then by={};cache[owner]=by end
  local key=part.kind..':'..part.shape
  if by[key]~=nil then return by[key]end
  -- Only this reader's own work consumes its allowance. Context lookup, OSM
  -- enrichment and unrelated work earlier in the frame must not starve it forever.
  if frameSpent and frameSpent>=.002 then M.stats.deferred=M.stats.deferred+1;return nil end
  local started=os.clock()
  local file=io.open(part.shape,'rb');if not file then M.stats.unreadable=M.stats.unreadable+1;return end
  local bytes=file:read('*all');file:close()
  local ok,result=pcall(M.decode,bytes);by[key]=ok and result or false
  if not ok then M.stats.last_error=tostring(result);M.stats.last_shape=part.shape end
  if frameSpent then frameSpent=frameSpent+os.clock()-started end
  local counter=ok and 'loaded' or 'decode_failed';M.stats[counter]=M.stats[counter]+1
  return by[key]
end
function M.begin(x,y,z,frame)
  local provider=M.provider
  if not provider then
    local roads=package.loaded['beamngEarth/roads'];provider=roads and roads.signalSurfaceParts
  end
  if not provider then return nil end -- pure/offline callers can explicitly omit a surface provider
  local bgt=nil -- Use the actual rendered road/walk meshes; no BGT dataset dependency.
  local parts,covered=provider(x,y,65);local ready={};local roadReady=false;local pending={}
  for _,p in ipairs(parts or {})do local mesh=not p.pending and(p.mesh or load(p))or nil
    if mesh then
      local ids={};for _,id in ipairs(p.bgt and p.bgt.object_ids or{})do ids[id]=true end
      ready[#ready+1]={part=p,mesh=mesh,bgt_ids=ids};if p.kind=='road'then roadReady=true end
    end
    if mesh==nil or mesh==false then pending[#pending+1]=p end
  end
  if not roadReady then covered=false end
  local function atGround(px,py,pz)
    local terrain=rawget(_G,'core_terrain')
    if terrain and terrain.getTerrainHeight then
      local ok,h=pcall(terrain.getTerrainHeight,vec3(px,py,pz))
      if ok and type(h)=='number'then return math.abs(h-pz)<.75 end
    end
    -- A BGT polygon has no deck height. Without a ground measurement keep the
    -- native checks; never apply a ground-level polygon to a bridge above it.
    return false
  end
  local function semantic(px,py,pz,radius)
    if bgt and atGround(px,py,pz)then return bgt.classify(px,py,radius)end
  end
  local function sample(px,py,pz)
    local walk,road
    local allowed,role,id
    if bgt then allowed,role,id=bgt.classify(px,py,0)end
    for _,entry in ipairs(ready)do
      local p=entry.part;local r=p.r or {1,0,0,0,1,0,0,0,1};local pos=p.pos or{0,0,0}
      local dx,dy,dz=px-pos[1],py-pos[2],pz-pos[3]
      local lx,ly,lz=r[1]*dx+r[4]*dy+r[7]*dz,r[2]*dx+r[5]*dy+r[8]*dz,r[3]*dx+r[6]*dy+r[9]*dz
      local h=M.height(entry.mesh,lx,ly,lz)
      if h then
        local worldz=pos[3]+r[7]*lx+r[8]*ly+r[9]*h
        -- The BGT adapter batches sidewalks into the road shape too. Only
        -- reclassify a polygon whose actual object ID is present in this skin.
        if p.kind=='walk'or(allowed and entry.bgt_ids[id]and atGround(px,py,worldz))then walk=math.max(walk or -math.huge,worldz)
        else road=math.max(road or -math.huge,worldz)end
      end
    end
    if walk and (not road or walk>=road-.025)then return 'walk',walk end
    if road then return 'road',road end
    local known=type(covered)=='function' and covered(px,py) or covered==true
    return known and 'verge' or 'pending'
  end
  local function vergeFoot(px,py,ground)
    local allowed,role=semantic(px,py,ground,M.FOOT_RADIUS_M)
    if allowed==false then return false,'bgt_'..role end
    -- A free centre does not prove the whole pole foot is outside the road,
    -- especially beside a rounded island tip. Include diagonal directions.
    local function check(x,y)
      local surface=sample(x,y,ground)
      if surface~='verge'and surface~='pending'then return false,'road_at_ground_level' end
      local material=M.groundKind(x,y,ground)
      if material~='verge'then return false,material=='road'and'terrain_road'or'terrain_material_unavailable'end
      return true
    end
    local ok,why=check(px,py);if not ok then return false,why end
    for i=0,7 do
      local angle=i*math.pi/4
      ok,why=check(px+M.FOOT_RADIUS_M*math.cos(angle),py+M.FOOT_RADIUS_M*math.sin(angle))
      if not ok then return false,why end
    end
    return true
  end
  return function(px,py,pz)
    for _,p in ipairs(pending)do
      if not p.footprint or require('beamngEarth/geotile').quadDist(p.footprint,px,py)<=M.FOOT_RADIUS_M then
        return false,nil,'pending'
      end
    end
    local kind,height=sample(px,py,pz)
    local allowed,role=semantic(px,py,height or pz,M.FOOT_RADIUS_M)
    if allowed==false then return false,nil,'bgt_'..role end
    if kind=='walk'then
      -- BGT gaps and holes cannot by themselves authorize anything; the
      -- measured drawn sidewalk below must still support the complete foot.
      for i=0,7 do
        local angle=i*math.pi/4
        local k,h=sample(px+M.FOOT_RADIUS_M*math.cos(angle),py+M.FOOT_RADIUS_M*math.sin(angle),pz)
        if k~='walk'or math.abs(h-height)>.12 then return false,nil,'walk_edge' end
      end
      -- Check below the pole cap, so an existing signal cannot hit its own head.
      -- A not-yet-shown skin or a surface at the wrong level must not float a pole.
      local ray=rawget(_G,'castRayStatic')
      if ray then
        local d=ray(vec3(px,py,height+.3),vec3(0,0,-1),1)
        if not d or d>=1 or d>.65 then return false,nil,'walk_ground_mismatch'end
      end
      return true,height-.015,'walk'
    end
    if kind=='road'then return false,nil,kind end
    -- A painted road tile may have no road mesh at all. An in-bounds native
    -- grass material plus measured terrain height can still verify its verge.
    -- Pending actual shapes above were already rejected by their footprints.
    -- A verge requires actual ground, not the old junction's elevation at a new XY.
    local terrain=rawget(_G,'core_terrain')
    if terrain and terrain.getTerrainHeight then
      local ok,ground=pcall(terrain.getTerrainHeight,vec3(px,py,pz))
      if ok and type(ground)=='number'and math.abs(ground-pz)<3 then
        local supported,why=vergeFoot(px,py,ground)
        if supported then return true,ground-.015,'verge'end
        return false,nil,why
      end
    end
    local ray=rawget(_G,'castRayStatic')
    if ray then
      local d=ray(vec3(px,py,pz+2),vec3(0,0,-1),8)
      if d and d<8 then
        local ground=pz+2-d
        -- Recheck at the measured level: a lower carriageway is not a verge.
        local atGround=sample(px,py,ground)
        if (atGround=='verge'or atGround=='pending')and vergeFoot(px,py,ground)then return true,ground-.015,'verge' end
      end
    end
    return false,nil,'ground_unavailable'
  end
end
function M.reset()cache=setmetatable({},{__mode='k'})end
return M
