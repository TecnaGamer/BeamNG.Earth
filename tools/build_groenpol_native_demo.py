#!/usr/bin/env python3
"""Prepare an isolated Gridmap demo of native mounts and the actual far consumer.

Synthetic flat road inputs exercise the renderer, not geographic correctness.
Nothing is installed or published by this tool. Stage its output in game temp.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import numpy as np

import build_groenpol_forest_assets as assets
ROOT=assets.ROOT
sys.path.insert(0,str(ROOT/'server'))
import nlsignalassembly

TEMP='/temp/nl_native_assembly_assets_20261114/'
ORIGINAL='/levels/beamng_earth/art/shapes/beamngEarth/groenpol/'


def build(target):
    target=Path(target)
    if target.exists() and any(target.iterdir()):raise ValueError('use an empty demo output directory')
    target.mkdir(parents=True,exist_ok=True)
    folder=target/'forest_v1'
    catalog,audit=assets.build(folder)
    road=dict(kind='road',positions=np.array([[-4.95,-50.,0.],[4.95,-50.,0.],[4.95,50.,0.],[-4.95,50.,0.]]),
              triangles=np.array([[0,1,2],[0,2,3]]))
    scene=nlsignalassembly.make_scene([road],np.zeros((3,3),np.uint16),0.,100.,(50.,50.),
                                      [[-100.,-100.,100.,100.]],square=100.)
    request=dict(stop=[0.,0.,0.],travel=[0.,1.],lanes=3,opposite=0,radius=4.95,
                 overhead=True,before=20.,after=3.)
    result=nlsignalassembly.assemblies(scene,[request],catalog,[['left','through','right']],[[1,0]],[False])[0]
    if not result['ready']:raise ValueError(result)
    # Relocate resource names only. All geometry and poses remain native output.
    (target/'assembly.json').write_text(json.dumps(result['assembly']).replace(ORIGINAL,TEMP)+'\n')
    materials=json.loads((folder/'main.materials.json').read_text())
    textures=set()
    for mat in materials.values():
        for stage in mat.get('Stages',[]):
            for key,value in list(stage.items()):
                if isinstance(value,str) and value.startswith(ORIGINAL) and value.endswith('.dds'):
                    relative=value[len(ORIGINAL):]
                    if '/' in relative or '..' in relative:raise ValueError('unexpected texture path')
                    textures.add(relative);stage[key]=TEMP+relative
    for texture in sorted(textures):shutil.copyfile(assets.mesh.ASSETS/texture,target/texture)
    (folder/'main.materials.json').write_text(json.dumps(materials,sort_keys=True,indent=2)+'\n')
    client=ROOT/'mod/beamng_earth/lua/ge/extensions/beamngEarth'
    for name in ('nl_signal_assemblies','nl_signal_forest'):
        raw=(client/(name+'.lua')).read_text()
        if raw.count(ORIGINAL)!=1:raise ValueError('unexpected consumer resource-root declaration')
        (target/(name+'.lua')).write_text(raw.replace(ORIGINAL,TEMP))
    (target/'native_audit.json').write_text(json.dumps(audit,sort_keys=True,indent=2)+'\n')
    (target/'nl_native_assembly.lua').write_text(DEMO)
    return dict(lanes=3,lens_mm=result['assembly']['lens_mm'],native_parts=len(result['assembly']['parts']),
                variants=audit['vehicle_variants'],pose_source='earthfast.NlSignalScene.assemblies')


DEMO="""-- Isolated renderer check: no Earth streaming or map placement.
assert(getCurrentLevelIdentifier()~='beamng_earth','Open Gridmap first')
local root='/temp/nl_native_assembly_assets_20261114/'
local state=_G.bneNlNativeAssemblyDemo
if not state then
  local consumer=dofile(root..'nl_signal_assemblies.lua')
  local factory=dofile(root..'nl_signal_forest.lua')
  state={consumer=consumer,reader=consumer.new(factory.new())}
  _G.bneNlNativeAssemblyDemo=state
end
state.reader:setEnabled(false)
function state.clear()state.reader:setEnabled(false)end
loadJsonMaterialsFile(root..'forest_v1/main.materials.json')
local assembly=assert(jsonReadFile(root..'assembly.json'))
local vehicle=assert(be:getPlayerVehicle(0),'Spawn a car first')
local p,d=vehicle:getPosition(),vehicle:getDirectionVector()
local length=math.sqrt(d.x*d.x+d.y*d.y)
local direction=vec3(d.x/length,d.y/length,0)
local q=quatFromDir(direction,vec3(0,0,1))
local z=core_terrain.getTerrainHeight(p) or p.z
state.reader:setEnabled(true)
assert(state.reader:setFrame({p.x+direction.x*15,p.y+direction.y*15,z,q.x,q.y,q.z,q.w}))
local rows=assert(state.consumer.farParts(assembly,{'red','amber','green'}))
assert(state.reader:enqueue({format=1,revision=0,parts=rows}))
assert(state.reader:tick(64),'Assembly did not complete in one diagnostic tick')
log('I','nlNativeAssembly','Native three-lane 300mm assembly displayed; clear with bneNlNativeAssemblyDemo.clear()')
"""


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    args=parser.parse_args()
    print(json.dumps(build(args.output)))
