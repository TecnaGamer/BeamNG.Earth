"""Bake limestone face relief and cavity shading from the approved albedo.

Uses the canyon/arid edge-preserving relief estimator at the limestone's square
world scale. These are inferred shading channels, not changes to collision or
CPU displacement. Broad pigment alone is not interpreted as cavity depth.
"""
import json
import numpy as np
from PIL import Image
from bake_canyon_near_pbr import relief_fields, blur
import bake_cliff_roughness as roughness

OUT = roughness.OUT
SPAN_M = 91.44


def channels(luminance):
    height, _, _ = relief_fields(luminance)
    h, w = height.shape
    dx = (np.roll(height, -1, 1)-np.roll(height, 1, 1))*w/(2*SPAN_M)
    dy = (np.roll(height, -1, 0)-np.roll(height, 1, 0))*h/(2*SPAN_M)
    normal = np.stack((-dx, dy, np.ones_like(dx)), -1)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    cavity = np.zeros_like(height)
    for radius, weight in ((8, .8), (24, .6), (64, .35)):
        cavity += weight*np.maximum(blur(height, max(.5, radius*w/4096))-height, 0)
    return normal, np.clip(np.exp(-2.4*cavity), .48, 1)


def bake():
    with Image.open(OUT/'limestone_cut_base.color.png') as image:
        luminance = np.asarray(image.convert('L'), np.float32)/255
    normal, ao = channels(luminance)
    Image.fromarray(np.uint8(np.rint((normal*.5+.5)*255))).save(OUT/'limestone_cut.normal.png')
    Image.fromarray(np.uint8(np.rint(ao*255))).convert('RGB').save(OUT/'limestone_cut_ao.data.png')
    path = roughness.ASSETS/'cliff.materials.json'
    materials = json.loads(path.read_text())
    for name in ('bne_cliff_roadcut', 'bne_cliff_roadcut_terrain'):
        stage = materials[name]['Stages'][0]
        stage.update(normalMapStrength=.48, normalMapUseUV=0,
                     ambientOcclusionMapUseUV=0, detailNormalMapStrength=.35)
    path.write_text(json.dumps(materials, indent=2)+'\n')
    roughness.bake()


if __name__ == '__main__':
    bake()
