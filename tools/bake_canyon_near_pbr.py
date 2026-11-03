"""Derive conservative near-cliff PBR channels from the approved runtime diffuse.

Deterministic signal processing, not a new visual source. Albedo cannot identify
true material height: remove broad lighting/pigment changes and keep relief and
cavity response restrained. All maps share the diffuse UV0 and 4096 resolution.
"""
import json
from pathlib import Path
import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff'
PREFIX = 'canyon_v4_near'
WIDTH_M, HEIGHT_M = 91.44, 182.88


def blur(a, sigma):
    pad = int(np.ceil(4*sigma))
    wrapped = np.pad(a,pad,mode='wrap')
    return cv2.GaussianBlur(wrapped,(0,0),sigma)[pad:-pad,pad:-pad]


def channels(lum):
    """Band-limited relief, roughness and subtle cavities, in source pixel space."""
    scale = lum.shape[1]/4096
    fine = blur(lum,max(.65,1.2*scale))
    medium = blur(lum,8*scale)
    broad = blur(lum,40*scale)
    # Pale geological beds and broad cast shadows are not protruding rock.
    relief = .50*(fine-medium)+.20*(medium-broad)
    h,w = relief.shape
    dx = (np.roll(relief,-1,1)-np.roll(relief,1,1))*w/(2*WIDTH_M)
    dy = (np.roll(relief,-1,0)-np.roll(relief,1,0))*h/(2*HEIGHT_M)
    normal = np.stack((-dx,dy,np.ones_like(dx)),-1)
    normal /= np.linalg.norm(normal,axis=-1,keepdims=True)
    cavity = np.clip((medium-fine)*2.0,0,.12)
    ao = 1-cavity
    roughness = np.clip(.89+.35*np.abs(fine-medium)+.12*(broad-fine),.82,.98)
    return normal,roughness,ao


def bake():
    with Image.open(OUT/'canyon_v4_base.color.png') as im:
        lum = np.asarray(im.convert('L'),np.float32)/255
    normal,roughness,ao = channels(lum)
    for suffix,a in (('.normal.png',normal*.5+.5),('_roughness.data.png',roughness),('_ao.data.png',ao)):
        Image.fromarray(np.uint8(np.rint(np.clip(a,0,1)*255))).save(OUT/(PREFIX+suffix))
    path=OUT.parent/'cliff.materials.json'
    mats=json.loads(path.read_text())
    stage=mats['bne_cliff_canyon_sat']['Stages'][0]
    prefix='/levels/beamng_earth/art/shapes/beamngEarth/cliff/'+PREFIX
    stage.update(normalMap=prefix+'.normal.png',normalMapUseUV=0,
                 roughnessMap=prefix+'_roughness.data.png',roughnessMapUseUV=0,roughnessFactor=1,
                 ambientOcclusionMap=prefix+'_ao.data.png',ambientOcclusionMapUseUV=0,metallicFactor=0)
    path.write_text(json.dumps(mats,indent=2)+'\n')
    (OUT/(PREFIX+'_bake.json')).write_text(json.dumps(dict(source='canyon_v4_base.color.png',
        size=list(lum.shape),uv=0,method='band-limited diffuse-derived approximate relief; nonmetallic rough rock',
        limitation='Approximate PBR inferred from albedo, not measured surface channels.'),indent=2)+'\n')
    print('Baked aligned near canyon normal, roughness and cavity AO:',lum.shape)

if __name__=='__main__': bake()
