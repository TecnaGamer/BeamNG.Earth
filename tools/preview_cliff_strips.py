#!/usr/bin/env python3
"""Opt-in live prototype: project height-clipped rubble strips across cliff contacts.

Reads the loaded render meshes once, builds offline, then installs temporary
TSStatics. No production streaming or per-frame mesh building is enabled.
Stock textures are extracted only into the game's private temporary directory.
Run from the repository root; --cleanup removes this preview and restores decals.
"""
import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'server'), str(ROOT/'tools')]
import cdae
import cliffstrips as strips
import make_detail_texture
import mcp
from bake_cliff_transition import linear, NAMES

USER = Path('C:/Users/Austin/AppData/Local/BeamNG/BeamNG.drive/current')
GAME = Path('I:/SteamLibrary/steamapps/common/BeamNG.drive')
VROOT = '/temp/beamngEarth/projected-cliff-strips/'
OUT = USER/VROOT.lstrip('/')
REPORT = ROOT/'build/projected-cliff-strips'
RADIUS = 65.0
MAX_RUN_M = 28.0


def cleanup():
    return mcp.lua("for _,id in ipairs(projectedCliffStripIds or {}) do local o=scenetree.findObjectById(id);if o then o:delete() end end;projectedCliffStripIds={};if projectedCliffDecalsEnabled~=nil then require('beamngEarth/roads').setCliffStripsEnabled(projectedCliffDecalsEnabled);projectedCliffDecalsEnabled=nil end;return 'preview removed'")


def world(p, row):
    q = np.array(row['quat'])
    return p+2*np.cross(q[:3],np.cross(q[:3],p)+q[3]*p)+np.array([row['pos'][k] for k in 'xyz'])


def receivers(focus):
    # Box distance includes large terrain tiles whose centers are far away.
    code = "local p=vec3(%f,%f,%f);local a={};" % tuple(focus)
    code += """for _,name in ipairs(scenetree.findClassObjects('TSStatic')) do
local o=scenetree.findObject(name);local s=o:getField('shapeName',0)
if s:find('/temp/beamngEarth/') and not s:find('projected%-cliff%-strips') and o:getField('hidden',0)~='1' then
local b=o:getWorldBox();local c=b:getCenter();local e=b:getExtents()*0.5
if math.max(0,math.abs(c.x-p.x)-e.x)<75 and math.max(0,math.abs(c.y-p.y)-e.y)<75 and math.max(0,math.abs(c.z-p.z)-e.z)<100 then
local q=o:getRotation();a[#a+1]={id=o:getID(),shape=s,pos=o:getPosition(),quat={q.x,q.y,q.z,q.w}}
end end end;return jsonEncode(a)"""
    rows=json.loads(mcp.lua(code));(REPORT/'receivers.json').write_text(json.dumps(rows))
    rock,ground,ground_normals=[],[],[]
    for row in rows:
        path=USER/row['shape'].lstrip('/')
        if not path.exists(): continue
        d=cdae.read(path);names=d['header'].get('objectNames')
        if names not in (['tile'],['cliff']): continue
        # Only the highest LOD: later entries are alternate meshes, not receivers.
        mesh=next((m for m in d['meshes'] if m['type']!=4),None)
        if mesh is None: continue
        a=cdae.mesh_arrays(mesh);p=world(a['verts'],row)
        tri=a['indices'].reshape(-1,3)[:,[0,2,1]]
        verts=p[tri]
        keep=np.all(verts.max(1)>focus-[RADIUS,RADIUS,100],axis=1)&np.all(verts.min(1)<focus+[RADIUS,RADIUS,100],axis=1)
        if not keep.any(): continue
        normal_world=world(a['norms'],row)-np.array([row['pos'][k] for k in 'xyz'])
        if names==['tile']:
            ground.append(verts[keep]);ground_normals.append(normal_world[tri[keep]])
        else:
            colors=np.frombuffer(mesh['colors'][2],np.uint8).reshape(-1,4)
            material_name=d['materials'][0]['name']
            family=material_name.removesuffix('_terrain')
            means=json.loads((ROOT/'server/cliff_transition_means.json').read_text())
            mean=np.array(means[NAMES.index(family)])
            rim=np.clip((a['tverts2'][:,0]*16-.5)/15,0,1) if material_name.endswith('_terrain') else np.zeros(len(p))
            # Match the already blended cliff surface, not its raw texture alone.
            albedo=colors[:,:3]/255*(mean[None,:]*(1-rim[:,None])+rim[:,None])
            rock.append(dict(p=p,normals=normal_world,tri=tri[keep],colors=colors,albedo=albedo,material=material_name,id=row['id']))
    # The near ground is a rendered TerrainBlock; TSStatic ground tiles there
    # are deliberately hidden. Sample that exact block, including each cell's
    # center to recover the engine's triangle diagonal (no ray/collision proxy).
    terrain_code = "local p=vec3(%f,%f,%f);local out={};" % tuple(focus)
    terrain_code += """for _,name in ipairs(scenetree.findClassObjects('TerrainBlock')) do
local o=scenetree.findObject(name);local b=o:getWorldBox();local c=b:getCenter();local e=b:getExtents()*0.5
if o:getField('isRenderEnabled',0)~='false' and math.abs(p.x-c.x)<e.x and math.abs(p.y-c.y)<e.y then
local step=tonumber(o:getField('squareSize',0)) or 2;local base=o:getPosition()
local x0=base.x+math.floor((p.x-75-base.x)/step)*step;local y0=base.y+math.floor((p.y-75-base.y)/step)*step
local n=math.ceil(150/step);local h={};local mid={}
for j=0,n do for i=0,n do h[#h+1]=o:getHeight(vec3(x0+i*step,y0+j*step,0)) end end
for j=0,n-1 do for i=0,n-1 do mid[#mid+1]=o:getHeight(vec3(x0+(i+0.5)*step,y0+(j+0.5)*step,0)) end end
out={x=x0,y=y0,n=n,step=step,h=h,mid=mid};break end end;return jsonEncode(out)"""
    block=json.loads(mcp.lua(terrain_code))
    if block:
        n=block['n'];j,i=np.mgrid[:n+1,:n+1]
        p=np.column_stack((block['x']+i.ravel()*block['step'],block['y']+j.ravel()*block['step'],block['h']))
        j,i=np.mgrid[:n,:n];a=(j*(n+1)+i).ravel();b=a+1;c=a+n+1;d=c+1
        diag=np.abs((p[a,2]+p[d,2])/2-block['mid'])<np.abs((p[b,2]+p[c,2])/2-block['mid'])
        faces=np.concatenate((np.column_stack((a[diag],b[diag],d[diag])),np.column_stack((a[diag],d[diag],c[diag])),np.column_stack((a[~diag],b[~diag],c[~diag])),np.column_stack((b[~diag],d[~diag],c[~diag]))))
        ground.append(p[faces])
        dy,dx=np.gradient(p[:,2].reshape(n+1,n+1),block['step'])
        normals=strips.unit(np.column_stack((-dx.ravel(),-dy.ravel(),np.ones(len(p)))))
        ground_normals.append(normals[faces])
    if not ground:raise RuntimeError('No rendered ground receiver at the view target.')
    return rock,np.concatenate(ground),np.concatenate(ground_normals)


def contacts(rock, terrain):
    """Intersect cliff triangles with the rendered terrain height surface.

    Vertex clearance interpolates the contact inside each cliff triangle;
    projection subsequently conforms the strip to both actual receiver meshes.
    """
    height=strips.Projector(terrain,[0,0,1],[0,0,0],bin_m=4)
    segments=[]
    for mesh in rock:
        p=mesh['p'];ids=np.unique(mesh['tri']);local=(p[ids]-height.origin)@height.axes.T
        hit,_,owner=height.sample(local[:,:2])
        clearance=np.full(len(p),np.nan);clearance[ids]=p[ids,2]-hit[:,2]
        for tri in mesh['tri']:
            s=clearance[tri]
            if not np.isfinite(s).all() or s.min()>=0 or s.max()<=0:continue
            pts=[]
            for i,j in ((0,1),(1,2),(2,0)):
                if (s[i]<0)!=(s[j]<0):pts.append(p[tri[i]]+(p[tri[j]]-p[tri[i]])*s[i]/(s[i]-s[j]))
            if len(pts)==2 and np.linalg.norm(pts[1]-pts[0])>.01:
                segments.append((np.array(pts),mesh['material'],mesh['colors'][tri,:3].mean(0)))
    # Weld contour endpoints across triangle and chunk boundaries.
    nodes={};edges=[];points=[];adj={}
    for pts,mat,color in segments:
        pair=[]
        for p in pts:
            key=tuple(np.rint(p/.03).astype(int))
            if key not in nodes:nodes[key]=len(points);points.append(p)
            pair.append(nodes[key])
        if pair[0]==pair[1]:continue
        ei=len(edges);edges.append((pair,mat,color))
        for v in pair:adj.setdefault(v,[]).append(ei)
    used=set();runs=[]
    starts=sorted(adj,key=lambda v:len(adj[v])==2)
    for start in starts:
        while any(e not in used for e in adj[start]):
            v=start;run=[points[v]];colors=[];materials=[]
            while True:
                choices=[e for e in adj[v] if e not in used]
                if not choices:break
                e=choices[0];used.add(e);pair,mat,color=edges[e]
                v=pair[1] if pair[0]==v else pair[0]
                run.append(points[v]);colors.append(color);materials.append(mat)
                if v==start or len(adj[v])!=2:break
            if len(run)>2:runs.append((np.array(run),materials[0],np.mean(colors,0)))
    return runs


def assets():
    prefix='assets/materials/terrain/rock/t_dirt_rocky/t_dirt_rocky'
    with zipfile.ZipFile(GAME/'content/assets/materials/terrain.zip') as z:
        for suffix in ('_b.color.dds','_b.png','_h.png','_nm.normal.dds','_ao.data.dds','_r.data.dds'):
            (OUT/('rock'+suffix)).write_bytes(z.read(prefix+suffix))
    h=np.asarray(Image.open(OUT/'rock_h.png'),float)
    if h.ndim==3:h=h[:,:,0]
    h=(h-h.min())/max(1,h.max()-h.min())
    # Linear map encoding: alpha-test at .5 becomes height >= threshold.
    # A small floor margin avoids quantization holes in the dense center.
    encoded=.51+.49*h
    (OUT/'height.data.dds').write_bytes(make_detail_texture.encode([encoded]))
    t=np.linspace(0,1,strips.LOOKUP_WIDTH)
    lookup=(128/255)*.5/(.505+.52*t)
    (OUT/'threshold.data.dds').write_bytes(make_detail_texture.encode([np.tile(lookup,(8,1))]))
    rgb=np.asarray(Image.open(OUT/'rock_b.png').convert('RGB'),float)/255
    return np.mean(linear(rgb),axis=(0,1))


def material(name, family, rubble_mean):
    # Factors are authored in sRGB; vertex colors carry the linear target albedo.
    normalized=1/np.maximum(rubble_mean,.001)
    factor=1.055*normalized**(1/2.4)-.055
    return dict(name=name,mapTo=name,**{'class':'Material'},version=1.5,alphaTest=True,alphaRef=128,
        translucent=False,doubleSided=True,castShadows=False,Stages=[dict(
        baseColorMap=VROOT+'rock_b.color.dds',baseColorFactor=[*factor,1],vertColor=True,
        normalMap=VROOT+'rock_nm.normal.dds',normalMapStrength=.65,
        roughnessMap=VROOT+'rock_r.data.dds',roughnessFactor=1,metallicFactor=0,
        ambientOcclusionMap=VROOT+'rock_ao.data.dds',ambientOcclusionFactor=.35,
        opacityMap=VROOT+'height.data.dds',opacityFactor=1,opacityMapUseUV=0,
        opacityDetailMap=VROOT+'threshold.data.dds',opacityDetailMapUseUV=1,
        detailOpacityMapStrength=1,detailScale=[1,1]),{},{},{}])


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--cleanup',action='store_true');ap.add_argument('--max-strips',type=int,default=8)
    ap.add_argument('--width',type=float,default=4);ap.add_argument('--focus',type=float,nargs=3);args=ap.parse_args()
    if args.cleanup:print(cleanup());return
    OUT.mkdir(parents=True,exist_ok=True);REPORT.mkdir(parents=True,exist_ok=True)
    snapshot=json.loads(mcp.lua("local p=getCameraPosition();local q=getCameraQuat();local h=Engine.castRay(p,p+q*vec3(0,1,0)*500,true,false);return jsonEncode({pos=p,rot=q,hit=h and h.pt})"))
    (REPORT/'camera.json').write_text(json.dumps(snapshot))
    if not args.focus and not snapshot.get('hit'):raise SystemExit('Aim the camera at a cliff contact first.')
    focus=np.array(args.focus if args.focus else [snapshot['hit'][k] for k in 'xyz']);start=time.perf_counter()
    rock,terrain,terrain_normals=receivers(focus);runs=contacts(rock,terrain)
    print('receivers',len(rock),len(terrain),'contact runs',len(runs),flush=True)
    rubble_mean=assets();materials={};batches={};stats=[]
    allrock=np.concatenate([m['p'][m['tri']] for m in rock])
    alltri=np.concatenate((terrain,allrock))
    allnormals=np.concatenate((terrain_normals,np.concatenate([m['normals'][m['tri']] for m in rock])))
    rock_albedo=np.concatenate([m['albedo'][m['tri']] for m in rock])
    color_points=np.concatenate([m['p'][np.unique(m['tri'])] for m in rock])
    color_values=np.concatenate([m['albedo'][np.unique(m['tri'])] for m in rock])

    candidates=[]
    for run,mat,color in runs:
        length=np.linalg.norm(np.diff(run,axis=0),axis=1).sum()
        if length<5:continue
        for i in range(0,len(run)-1,max(1,int(len(run)*MAX_RUN_M/length))):
            n=max(3,int(len(run)*MAX_RUN_M/length)+1);part=run[i:i+n]
            if len(part)>2:candidates.append((np.linalg.norm(part.mean(0)-focus),part,mat,color))
    for _,run,mat,color in sorted(candidates,key=lambda x:x[0]):
        if len(stats)>=args.max_strips:break
        center=run.mean(0)
        lo,hi=run.min(0)-8,run.max(0)+8
        nearby=np.all(alltri.max(1)>=lo,axis=1)&np.all(alltri.min(1)<=hi,axis=1)
        receivers_local=alltri[nearby]
        # Averaged visible rock/terrain normals project onto both sides of contact.
        normal=np.cross(receivers_local[:,1]-receivers_local[:,0],receivers_local[:,2]-receivers_local[:,0])
        normal=strips.unit(normal);normal[normal[:,2]<0]*=-1
        normal=strips.unit(normal.mean(0))
        if normal[2]<.2:normal=strips.unit(normal+[0,0,.3])
        projector=strips.Projector(receivers_local,normal,center,vertex_normals=allnormals[nearby])
        strip=strips.build(run,projector,half_width=args.width/2)
        if strip is None:continue
        fam=mat.removesuffix('_terrain');name='bne_projected_strip_'+fam
        if name not in materials:materials[name]=material(name,fam,rubble_mean)
        ids=np.flatnonzero(np.all(color_points>=lo-5,axis=1)&np.all(color_points<=hi+5,axis=1))
        distances=[];indices=[]
        for chunk in np.array_split(strip.positions,max(1,int(np.ceil(len(strip.positions)/64)))):
            d=np.linalg.norm(chunk[:,None,:]-color_points[ids][None,:,:],axis=2)
            nearest=np.argpartition(d,min(2,len(ids)-1),axis=1)[:,:min(3,len(ids))]
            distances.append(np.take_along_axis(d,nearest,axis=1));indices.append(ids[nearest])
        dist,idx=np.concatenate(distances),np.concatenate(indices)
        weight=1/np.maximum(dist,.05)**2;weight/=weight.sum(1,keepdims=True)
        tint=np.clip((color_values[idx]*weight[:,:,None]).sum(1),0,1)
        # On the rock side, interpolate the actual receiving face's tint.
        owners=np.flatnonzero(nearby)[strip.receiver]
        for owner in np.unique(owners[owners>=len(terrain)]):
            take=owners==owner
            face=alltri[owner]
            edge=np.column_stack((face[1]-face[0],face[2]-face[0]))
            point=strip.positions[take]-projector.axes[2]*strips.SURFACE_OFFSET_M
            weights=np.linalg.lstsq(edge,(point-face[0]).T,rcond=None)[0].T
            bary=np.column_stack((1-weights.sum(1),weights))
            tint[take]=np.clip(bary@rock_albedo[owner-len(terrain)],0,1)
        batches.setdefault(name,[]).append((strip,np.column_stack((np.rint(tint*255),np.full(len(tint),255))).astype(np.uint8)))
        owners=np.flatnonzero(nearby)[strip.receiver]
        stats.append(dict(center=center.tolist(),vertices=len(strip.positions),triangles=len(strip.tris),
            terrain_vertices=int((owners<len(terrain)).sum()),rock_vertices=int((owners>=len(terrain)).sum()),family=fam))
    if not stats:raise SystemExit('No usable contacts near the view target; aim at a cliff/terrain boundary.')
    (OUT/'main.materials.json').write_text(json.dumps(materials,indent=2))
    code="for _,id in ipairs(projectedCliffStripIds or {}) do local o=scenetree.findObjectById(id);if o then o:delete() end end;projectedCliffStripIds={};loadJsonMaterialsFile('"+VROOT+"main.materials.json');"
    for i,(name,batch) in enumerate(batches.items()):
        positions=[];tris=[];uv=[];uv2=[];normals=[];colors=[];offset=0
        for strip,color in batch:
            positions.append(strip.positions-focus);tris.append(strip.tris+offset);offset+=len(strip.positions)
            uv.append(strip.uvs);uv2.append(strip.uvs2);normals.append(strip.normals)
            colors.append(color)
        filename='preview_%d_%d.cdae'%(i,time.time_ns())
        cdae.write(OUT/filename,np.concatenate(positions),np.concatenate(tris),uvs=np.concatenate(uv),uvs2=np.concatenate(uv2),normals=np.concatenate(normals),colors=np.concatenate(colors),material=name)
        code+="local o=createObject('TSStatic');o:setField('shapeName',0,'%s%s');o:setField('collisionType',0,'None');o:setField('decalType',0,'None');o.canSave=false;o:setPosition(vec3(%f,%f,%f));o:registerObject('');projectedCliffStripIds[#projectedCliffStripIds+1]=o:getID();"%((VROOT,filename)+tuple(focus))
    # Remember the prior setting, so cleanup does not change the user's preference.
    code+="for name,_ in pairs(jsonReadFile('"+VROOT+"main.materials.json')) do local m=scenetree.findObject(name);if m then m:flush();m:reload() end end;if projectedCliffDecalsEnabled==nil then projectedCliffDecalsEnabled=require('beamngEarth/settings').get('cliffDecals')~=false end;require('beamngEarth/roads').setCliffStripsEnabled(false);return jsonEncode(projectedCliffStripIds)"
    result=mcp.lua(code)
    report=dict(objects=result,strips=stats,seconds=time.perf_counter()-start,focus=focus.tolist())
    (REPORT/'result.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))


if __name__=='__main__':main()
