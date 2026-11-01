local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir..'/testlib.lua')
local decals = require('beamngEarth/cliffdecals')
local files,objects,removed,created = {},{},0,0
FS={directoryCreate=function() end,fileExists=function(_,p) return files[p] end,
 copyFile=function(_,src,dst) files[dst]=true end,triggerFilesChanged=function() end}
writeFile=function() end
jsonEncode=function() return '{}' end
loadJsonMaterialsFile=function() end
scenetree={findObject=function(n) return objects[n] end}
createObject=function()
 return {setField=function() end,registerObject=function(self,n) objects[n]=self end}
end
vec3=function(x,y,z) return {x=x,y=y,z=z} end
Engine={Render={DecalMgr={addDecal=function(p,n,r,t,s,idx,flags)
 created=created+1; T.check(flags==1,'runtime decal is not saved into level');return {position=p}
end,notifyDecalModified=function() end,removeDecal=function() removed=removed+1 end}}}
local rows={{'talus_c0a080',0,0,0,0,0,1,0,8}}
local tiles={a={cliff=23,cliffDecals=rows}}
local player={x=0,y=0,z=0}
decals.update(tiles,player,false)
decals.update(tiles,player,false)
T.check(created==1 and decals.status().instances==1,'stable tile is created once')
tiles.a.cliff=24
decals.update(tiles,player,false)
T.check(created==2 and removed==1,'replacement receiver is reprojected')
decals.invalidate()
T.check(removed==2 and decals.status().instances==0,'terrain swap drops projection')
decals.update(tiles,player,true)
T.check(created==2,'waits while terrain is swapping')
decals.update(tiles,player,false)
decals.update({},player,false)
T.check(removed==3,'tile unloading removes its decals')
decals.update(tiles,{x=1000,y=0,z=0},false)
T.check(decals.status().instances==0,'distant tiles do not allocate decals')
local m=decals.material('talus_c0a080').bne_cliff_decal_talus_c0a080.Stages[1]
T.check(m.opacityMap and m.normalMap and m.roughnessMap and m.ambientOcclusionMap,'all D0206 PBR maps present')
T.check(not decals.material('bad_path'),'invalid colour groups rejected')
local pl={r={1,0,0,0,1,0,0,0,1},t={10,20,30},c={0,0,0},pz=0}
local x,y,z=decals.placed(rows[1],pl)
T.check(x==10 and y==20 and z==30,'tile frame position follows anchor placement')
decals.LIMIT=1
local farRows={{'talus_c0a080',80,0,0,0,0,1,0,8}}
local both={far={cliff=33,cliffDecals=farRows}}
decals.update(both,player,false)
local before=removed
both.near={cliff=34,cliffDecals=rows}
decals.update(both,player,false)
T.check(removed==before+1 and decals.status().instances==1,'nearer arriving tile takes the limited decal slot')
decals.invalidate()
local stopped={try=function() return false end,charge=function() error('no charge without work') end}
decals.update(tiles,player,false,stopped)
T.check(decals.status().instances==0,'creation respects the frame governor')
local warm=decals.material('talus_c0a080').bne_cliff_decal_talus_c0a080.Stages[1].baseColorFactor
T.check(warm[1]>warm[2] and warm[2]>warm[3],'warm mineral tint does not acquire a blue compensation cast')
decals.update(tiles,player,false)
T.check(decals.status().instances==1,'decal present before toggle')
decals.setEnabled(false)
local createdBefore=created
decals.update(tiles,player,false)
T.check(decals.status().instances==0 and created==createdBefore,'off removes projections and prevents recreation')
decals.reset()
T.check(not decals.status().enabled,'level reset preserves disabled preference')
decals.setEnabled(true)
decals.update(tiles,player,false)
T.check(decals.status().instances==1,'on restores retained tile projections')

decals.invalidate()
decals.LIMIT=400
local function row(x,y,z,nx,ny,nz)
 return {'talus_c0a080',x,y,z,nx or 0,ny or 0,nz or 1,0,8}
end
-- A continuous seam sampled every two metres needs overlapping stamps, not
-- multiple layers over the same surface. Include a duplicate from another tile.
local seam={}
for i=0,10 do seam[#seam+1]=row(i*2,0,0) end
local coverage={a={cliff=1,cliffDecals=seam},b={cliff=2,cliffDecals={row(0,0,0)}}}
local transformed=0
local placed=decals.placed
decals.placed=function(...) transformed=transformed+1;return placed(...) end
local charged={try=function(_,kind) T.check(kind=='cliffDecal','decals use their own budget');return true end,
 charge=function(_,kind) T.check(kind=='cliffDecal','decals charge their own budget') end}
decals.update(coverage,player,false,charged)
T.check(decals.hasWork() and decals.status().instances==2,'remaining stamps request streaming budget and obey per-frame limit')
for i=1,10 do decals.update(coverage,player,false) end
T.check(decals.status().instances==6,'continuous seam keeps half-width overlap without duplicate layers')
T.check(not decals.hasWork(),'completed decal queue lets the governor idle')
T.check(transformed==12 and decals.status().selections==2,'stationary frames reuse positions and selection')
local selections=decals.status().selections
decals.update(coverage,{x=1,y=0,z=0},false)
T.check(decals.status().selections==selections,'small motion preserves selection hysteresis')
decals.update(coverage,{x=3,y=0,z=0},false)
T.check(decals.status().selections==selections+1 and transformed==12,'movement reselects cached positions')
decals.placed=placed

decals.invalidate()
local facets={a={cliff=1,cliffDecals={row(0,0,0),row(0,0,1),row(0,0,0,0,0,-1)}}}
for i=1,3 do decals.update(facets,player,false) end
T.check(decals.status().instances==3,'nearby parallel ledges and opposing faces retain separate decals')
facets.a.rowPl={pl=pl};facets.a.geo=true
decals.update(facets,player,false)
T.check(decals.status().instances==2 and decals.hasWork(),'anchor replacement discards old projections and rebuilds within budget')
decals.setEnabled(false)
T.check(not decals.hasWork() and decals.status().pending==0,'disabled decals do not keep streaming awake')
decals.setEnabled(true)
decals.update(coverage,player,false,stopped)
T.check(decals.hasWork(),'queued decals remain pending when the governor denies budget')
decals.drop('a');decals.drop('b')
decals.update({},player,false)
T.check(not decals.hasWork() and decals.status().instances==0,'unloaded pending tiles cannot create stale projections')
local addDecal=Engine.Render.DecalMgr.addDecal
Engine.Render.DecalMgr.addDecal=function() return nil end
decals.RETRY_MS=0
decals.update(tiles,player,false)
T.check(not decals.hasWork() and decals.status().instances==0,'failed native creation does not keep streaming awake')
Engine.Render.DecalMgr.addDecal=addDecal
decals.update(tiles,player,false)
T.check(decals.status().instances==1,'failed native creation retries while stationary')
T.done('cliffdecals')
