"""Reconstruct a 300-foot square fractured arid cliff and aligned near/far PBR.

Uses the canyon patch assembler and edge-preserving diffuse-derived PBR baker.
No single-image upscale; see the patch manifest and assembly audit for provenance.
Run with Python containing numpy, Pillow and OpenCV; cook the resulting normals
in BeamNG and retain the verified BC5 ready DDS alongside the authoring PNGs.
"""
import copy
import json
from pathlib import Path
import cv2
import numpy as np
from PIL import Image
import bake_canyon_tiled as tiled
import bake_canyon_near_pbr as pbr
from bake_cliff_transition import bake as transitions, linear

ROOT = Path(__file__).resolve().parents[1]
OUT = pbr.OUT
PREFIX = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/'
REPEAT_M = 91.44
NEAR = 'arid_near'
FAR = 'arid_far'


def save(name, array):
    Image.fromarray(np.uint8(np.rint(np.clip(array,0,1)*255))).save(OUT/name)


def bake():
    tiled.SOURCES = ROOT/'tools/assets/arid_generated/v1'
    tiled.ALBEDO = NEAR+'_base.color.png'
    tiled.PREVIEW = ROOT/'build/arid-fractured-v1'
    tiled.assemble()
    albedo = tiled.image_array(OUT/tiled.ALBEDO)
    lum = albedo @ np.array([.299,.587,.114],np.float32)
    pbr.WIDTH_M = pbr.HEIGHT_M = REPEAT_M
    normal,rough,ao = pbr.channels(lum)
    for name,a in (('.normal.png',normal*.5+.5),('_roughness.data.png',rough),('_ao.data.png',ao)):
        save(NEAR+name,a)
    height,_,_ = pbr.relief_fields(lum)
    # Far relief follows the same slabs, but suppresses grain and broadens joints.
    # Keep rounded exposed faces and narrow recessed cracks from the canyon baker.
    broad = pbr.blur(height,7)
    far_height = .65*broad + .22*pbr.blur(height,2)
    dx = (np.roll(far_height,-1,1)-np.roll(far_height,1,1))*4096/(2*REPEAT_M)
    dy = (np.roll(far_height,-1,0)-np.roll(far_height,1,0))*4096/(2*REPEAT_M)
    far_normal = np.stack((-dx,dy,np.ones_like(dx)),-1)
    far_normal /= np.linalg.norm(far_normal,axis=-1,keepdims=True)
    save(FAR+'.normal.png',far_normal*.5+.5)
    save(FAR+'_roughness.data.png',pbr.blur(rough,7))
    save(FAR+'_ao.data.png',1-.45*(1-pbr.blur(ao,7)))
    # A dedicated distance albedo keeps large fracture patterns without grain shimmer.
    far_albedo = np.stack([pbr.blur(albedo[:,:,i],2) for i in range(3)],-1)
    save(FAR+'_base.color.png',far_albedo)
    lo,hi = np.percentile(height,[1,99])
    cpu = cv2.resize(np.clip((height-lo)/(hi-lo),0,1),(256,64),interpolation=cv2.INTER_AREA)
    Image.fromarray(np.uint16(np.rint(cpu*65535))).save(ROOT/'server/cliff_arid_cut_height.png')
    path = OUT.parent/'cliff.materials.json'
    mats = json.loads(path.read_text())
    near = copy.deepcopy(mats['bne_cliff_canyon_sat'])
    near['name'] = near['mapTo'] = 'bne_cliff_roadcut_arid'
    stage = near['Stages'][0]
    stage.update(baseColorMap=PREFIX+NEAR+'_base.color.png',baseColorFactor=[1,1,1,1],
        normalMap=PREFIX+NEAR+'.normal.dds',normalMapUseUV=0,normalMapStrength=.32,
        roughnessMap=PREFIX+NEAR+'_roughness.data.png',roughnessMapUseUV=0,roughnessFactor=1,
        ambientOcclusionMap=PREFIX+NEAR+'_ao.data.png',ambientOcclusionMapUseUV=0,
        detailScale=[REPEAT_M/3,REPEAT_M/3],metallicFactor=0)
    mats[near['name']] = near
    far = copy.deepcopy(mats['bne_far_cliff_canyon_blend'])
    far['name'] = far['mapTo'] = 'bne_far_cliff_arid_blend'
    strength = .65
    mean_gain = np.mean(np.clip(1+(2*linear(far_albedo)-1)*strength,0,1),axis=(0,1),dtype=np.float64)
    far['Stages'][0].update(detailMap=PREFIX+FAR+'_base.color.png',detailMapUseUV=0,
        detailBaseColorMapStrength=strength,detailScale=[1,1],
        baseColorFactor=[*tiled.srgb(4/mean_gain).tolist(),1],
        normalMap=PREFIX+FAR+'.normal.dds',normalMapUseUV=1,normalMapStrength=.70,
        roughnessMap=PREFIX+FAR+'_roughness.data.png',roughnessMapUseUV=0,roughnessFactor=1,
        ambientOcclusionMap=PREFIX+FAR+'_ao.data.png',ambientOcclusionMapUseUV=0)
    mats[far['name']] = far
    path.write_text(json.dumps(mats,indent=2)+'\n')
    transitions()
    audit = dict(size=[4096,4096],repeat_metres=[REPEAT_M,REPEAT_M],
        method='Canyon median/bilateral face filtering and diffuse-aligned approximate relief, cavity AO and roughness',
        near_normal_strength=.32,far_normal_strength=.70,far_relief_weights=[.65,.22],
        normal_orientation='OpenGL +Y',detail_repeat_metres=3,
        limitation='Inferred from diffuse texture, not measured physical channels.',
        near_roughness_range=[float(rough.min()),float(rough.max())],near_ao_range=[float(ao.min()),float(ao.max())])
    (OUT/(NEAR+'_bake.json')).write_text(json.dumps(audit,indent=2)+'\n')
    print(json.dumps(audit))

if __name__ == '__main__':
    bake()
