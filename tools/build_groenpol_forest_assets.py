#!/usr/bin/env python3
"""Compile shared fixed-material vehicle heads and steel for the server renderer.

The source/audit manifest stays outside the runtime mod. Runtime files contain
only compiled meshes, shared materials and the model asset licence notice.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

import build_groenpol_forest_probe as mesh

ROOT=mesh.ROOT
VIRTUAL='/levels/beamng_earth/art/shapes/beamngEarth/groenpol/forest_v1/'
PREFIX='bne_groenpol_forest_v1_'


def build(target):
    target=Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError('use an empty asset output directory')
    catalog=json.loads((ROOT/'docs/assets/groenpol/components.json').read_text())
    materials=json.loads((mesh.ASSETS/'main.materials.json').read_text())
    runtime=copy.deepcopy(catalog)
    compiled,output_materials,sources={}, {}, {}
    target.mkdir(parents=True,exist_ok=True)

    def source(shape):
        path=ROOT/'mod/beamng_earth'/shape.lstrip('/')
        sources[shape]=hashlib.sha256(path.read_bytes()).hexdigest()
        return mesh.prepared_geometry(path)

    def material(name,lit=False):
        key=PREFIX+name
        output_materials[key]=mesh.material(materials[name],key,lit=lit)
        return key

    for variant,h in runtime['heads'].items():
        if not variant.startswith('vehicle'):
            continue
        dark=[];optics={}
        for p,t,n,uv,mat in source(h['shape']):
            if '_lens_' in mat:
                channel=int(mat.rsplit('_lens_',1)[1].split('_')[0])
                dark_mat=mat.rsplit('_lens_',1)[0]+'_dark_v4'
                dark.append((p,t,n,uv,material(dark_mat)))
                face=p.copy();face[:,0]+=.001
                optics[channel]=(face,t,n,uv,material(mat,lit=True))
            else:
                dark.append((p,t,n,uv,material(mat)))
        if set(optics)!={0,1,2}:
            raise ValueError(f'missing authored optical channels: {variant}')
        files={'body':(f'{variant}_body',dark)}
        for channel,color in enumerate(('red','amber','green')):
            files[color]=(f'{variant}_{color}',[optics[channel]])
        h['forest']={}
        for role,(name,pieces) in files.items():
            compiled[name+'.cdae']=mesh.write_mesh(target/(name+'.cdae'),pieces)
            h['forest'][role]=VIRTUAL+name+'.dae'

    # Steel and feet use the same fixed body material. Compile each source only
    # once even when several support templates share it.
    shapes=set()
    def gather(value):
        if isinstance(value,dict):
            for k,v in value.items():
                if k.endswith('shape') and isinstance(v,str) and v.endswith('.dae'):
                    shapes.add(v)
                else:gather(v)
        elif isinstance(value,list):
            for v in value:gather(v)
    for key in ('supports','hardware','low_repeat','double_shields','double_mount'):
        gather(runtime[key])
    replacements={}
    for shape in sorted(shapes):
        name=Path(shape).stem
        pieces=[(p,t,n,uv,material(mat)) for p,t,n,uv,mat in source(shape)]
        filename=name+'.cdae'
        compiled[filename]=mesh.write_mesh(target/filename,pieces)
        replacements[shape]=VIRTUAL+name+'.dae'
    def replace(value):
        if isinstance(value,dict):return {k:replace(v) for k,v in value.items()}
        if isinstance(value,list):return [replace(v) for v in value]
        return replacements.get(value,value) if isinstance(value,str) else value
    for key in ('supports','hardware','low_repeat','double_shields','double_mount'):
        runtime[key]=replace(runtime[key])
    (target/'main.materials.json').write_text(json.dumps(output_materials,sort_keys=True,indent=2)+'\n')
    (target/'LICENSE.md').write_bytes((mesh.ASSETS/'LICENSE.md').read_bytes())
    audit=dict(format=1,source_sha256=catalog['source_sha256'],source_shapes=sources,
               files=compiled,license='CC-BY-SA-4.0',textures='reuse original Groenpol DDS assets',
               vehicle_variants=sum(k.startswith('vehicle') for k in runtime['heads']))
    return runtime,audit


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('output')
    ap.add_argument('--catalog',required=True,help='server component catalog output, outside the mod')
    ap.add_argument('--audit',required=True,help='source manifest output, outside the mod')
    args=ap.parse_args()
    catalog,audit=build(args.output)
    for path,value in ((args.catalog,catalog),(args.audit,audit)):
        Path(path).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
    print(json.dumps(dict(variants=audit['vehicle_variants'],meshes=len(audit['files']),
                         mesh_bytes=sum(v['bytes'] for v in audit['files'].values()))))
