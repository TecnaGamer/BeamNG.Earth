"""Bake aligned near-cliff PBR from the accepted diffuse, plus pinned microdetail.

The albedo alone cannot identify measured height. Locally normalized, multi-scale
relief emphasizes stone faces and joints instead of treating each bright grain as
a protrusion. All broad channels share diffuse UV0 at full 4096 resolution.
"""
import json
from pathlib import Path
import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff'
WIDTH_M, HEIGHT_M = 91.44, 182.88
DETAIL_REPEAT_M = 3.0
DETAIL_COLOR_STRENGTH = .18
DETAIL_NORMAL_STRENGTH = .22
DETAIL_AO_STRENGTH = .22
DETAIL_ROUGHNESS_STRENGTH = .35
MAIN_NORMAL_STRENGTH = .30


def blur(a, sigma):
    pad = max(1,int(np.ceil(4*sigma)))
    wrapped = np.pad(a,pad,mode='wrap')
    return cv2.GaussianBlur(wrapped,(0,0),sigma)[pad:-pad,pad:-pad]


def preserve_face_edges(relative, scale):
    """Denoise within stone faces without averaging across their crack boundaries."""
    radius = max(1, int(np.ceil(4*scale)))
    wrapped = np.pad(relative, radius, mode='wrap')
    return cv2.bilateralFilter(wrapped, 2*radius+1, .035, max(.5, 2.5*scale))[radius:-radius,radius:-radius]


def broad_faces(lum, scale):
    """Remove small isolated bumps before retaining the larger fractured faces."""
    stride = max(1, int(round(2*scale)))
    h,w = lum.shape
    coarse = cv2.resize(lum, (max(1,w//stride),max(1,h//stride)), interpolation=cv2.INTER_AREA)
    # Median rejects isolated grain without rounding a coherent stone boundary.
    wrapped = np.pad(coarse,2,mode='wrap')
    coarse = cv2.medianBlur(wrapped,5)[2:-2,2:-2]
    return cv2.resize(coarse,(w,h),interpolation=cv2.INTER_LINEAR)


def flatten_deep_recesses(facets):
    """Give deep local joints a floor instead of continuing a rounded bowl.

    Keep a small residual slope so dark strata do not become identical plateaus.
    This changes the relief profile without amplifying its height or fine grain.
    """
    floor = -.20
    return np.maximum(facets, floor) + .15*np.minimum(facets-floor, 0)


def relief_fields(lum):
    """Infer metre-scale relative relief and pores without elevating entire pale beds."""
    scale = lum.shape[1]/4096
    def filtered(radius):
        return blur(lum,max(.5,radius*scale))
    grain = filtered(1.2)
    joints = filtered(16)
    blocks = filtered(48)
    beds = filtered(112)
    # Relative local contrast prevents pale sandstone from getting stronger bumps
    # than dark rock with the same surface pattern. Broad bed pigment is removed.
    # A Gaussian across the face rounded every fractured edge. Filter relative
    # relief within similar-valued faces instead, retaining abrupt joint breaks.
    relative = (broad_faces(filtered(2.5),scale)-blocks)/(blocks+.15)
    facets = flatten_deep_recesses(np.clip(preserve_face_edges(relative,scale),-.40,.40))
    face = blocks+facets*(blocks+.15)
    ledges = np.clip((filtered(12)-beds)/(beds+.15),-.35,.35)
    recess = np.clip((joints-face)/(joints+.15),0,.25)
    pores = (grain-face)/(face+.15)
    macro = np.clip((filtered(32)-filtered(128))/(beds+.15),-.35,.35)
    # Main normal carries broad blocks/bedding; the separate detail normal owns
    # fine grain. Do not reintroduce pores as relief in this map.
    height = 1.4*facets+1.2*ledges+1.0*macro-.25*recess
    micro = blur(np.abs(pores),max(.5,3*scale))
    return height,micro,facets


def channels(lum):
    """OpenGL tangent normals, varying dry-rock roughness, and multi-scale cavity AO."""
    height,micro,facets = relief_fields(lum)
    h,w = height.shape
    dx = (np.roll(height,-1,1)-np.roll(height,1,1))*w/(2*WIDTH_M)
    dy = (np.roll(height,-1,0)-np.roll(height,1,0))*h/(2*HEIGHT_M)
    normal = np.stack((-dx,dy,np.ones_like(dx)),-1)
    normal /= np.linalg.norm(normal,axis=-1,keepdims=True)
    scale = w/4096
    # Occlusion belongs to recessed cracks and ledges, not every dark pigment.
    cavity = np.zeros_like(height)
    for radius,weight in ((8,.8),(24,.6),(64,.35)):
        cavity += weight*np.maximum(blur(height,max(.5,radius*scale))-height,0)
    ao = np.clip(np.exp(-1.1*cavity),.76,1)
    # Rough granular/recessed areas; slightly smoother coherent exposed facets.
    roughness = np.clip(.86+.7*micro+.18*cavity-.18*np.maximum(facets,0),.72,.98)
    return normal,roughness,ao


def bake(albedo_name=None, prefix_name=None):
    from bake_canyon_detail import bake as bake_detail
    bake_detail()
    path=OUT.parent/'cliff.materials.json'
    mats=json.loads(path.read_text())
    stage=mats['bne_cliff_canyon_sat']['Stages'][0]
    albedo_name = albedo_name or Path(stage['baseColorMap']).name
    prefix_name = prefix_name or albedo_name.split('_base.')[0]+'_faceted_near'
    with Image.open(OUT/albedo_name) as im:
        lum = np.asarray(im.convert('L'),np.float32)/255
    normal,roughness,ao = channels(lum)
    for suffix,a in (('.normal.png',normal*.5+.5),('_roughness.data.png',roughness),('_ao.data.png',ao)):
        Image.fromarray(np.uint8(np.rint(np.clip(a,0,1)*255))).save(OUT/(prefix_name+suffix))
    prefix='/levels/beamng_earth/art/shapes/beamngEarth/cliff/'
    stage.update(normalMap=prefix+prefix_name+'.normal.png',normalMapUseUV=0,
                 normalMapStrength=MAIN_NORMAL_STRENGTH,
                 roughnessMap=prefix+prefix_name+'_roughness.data.png',roughnessMapUseUV=0,roughnessFactor=1,
                 ambientOcclusionMap=prefix+prefix_name+'_ao.data.png',ambientOcclusionMapUseUV=0,metallicFactor=0)
    stage.pop('detailNormalMapUseUV',None)  # Not an engine field; use normalDetailMapUseUV.
    stage.update(detailMap=prefix+'canyon_detail_dirt_rocky_v1.data.png',
                 detailMapUseUV=0, detailBaseColorMapStrength=DETAIL_COLOR_STRENGTH,
                 detailNormalMap=prefix+'canyon_detail_dirt_rocky_v1.normal.dds',
                 normalDetailMapUseUV=0, detailNormalMapStrength=DETAIL_NORMAL_STRENGTH,
                 detailScale=[WIDTH_M/DETAIL_REPEAT_M,HEIGHT_M/DETAIL_REPEAT_M],
                 ambientOcclusionDetailMap=prefix+'canyon_detail_dirt_rocky_v1_ao.data.png',
                 detailAoMapStrength=DETAIL_AO_STRENGTH,
                 roughnessDetailMap=prefix+'canyon_detail_dirt_rocky_v1_roughness.data.png',
                 detailRoughnessMapStrength=DETAIL_ROUGHNESS_STRENGTH,
                 useAnisotropic=True)
    path.write_text(json.dumps(mats,indent=2)+'\n')
    (OUT/(prefix_name+'_bake.json')).write_text(json.dumps(dict(source=albedo_name,
        size=list(lum.shape),uv=0,method='Median-filtered fractured faces, narrow joint edges and flattened deep recess floors; diffuse-derived approximate PBR',
        normal_orientation='OpenGL +Y', detail_repeat_metres=DETAIL_REPEAT_M,
        roughness_percentiles=np.percentile(roughness,[0,5,50,95,100]).tolist(),
        ao_percentiles=np.percentile(ao,[0,5,50,95,100]).tolist(),
        limitation='Approximate PBR inferred from albedo, not measured surface channels.'),indent=2)+'\n')
    print('Baked near canyon normal, roughness and cavity AO:',lum.shape)


if __name__=='__main__':
    bake()
    from bake_cliff_transition import bake as bake_transition
    bake_transition()
