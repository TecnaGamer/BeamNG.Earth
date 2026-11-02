"""Rebuild the arid road-cut atlas and approximate PBR maps offline.

Uses the shared overlapping restoration assembler. Relief is inferred from
isotropic narrow fracture contrast; it is not measured photogrammetric depth.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import restore_roadcut_material as restore
from assemble_cliff_scans import gaussian, save_color
from bake_scan_cliff_material import periodic, flat_normal, resize_periodic
import cliffmaterial as cm

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT/'tools/assets/arid_roadcut_generated'
WORK = ROOT/'build/arid-roadcut-restoration'
OUT = ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff'
SIZE = 4096
PREFIX = 'cut_arid_v1'
NORMAL_RELIEF_STRENGTH = .35
MICRO_NORMAL_STRENGTH = .3
ALBEDO_CONTRAST = .65
MINERAL_CHROMA = .5
ALBEDO_FACTOR = (1.4, 1.45, 1.55)  # warm natural-outcrop balance; regional vertex tint stays active


def reconstruct(rgb):
    """Infer angular joint relief without favoring horizontal limestone beds."""
    ppm = len(rgb)/cm.WIDTH_M
    lum = rgb @ np.array((.2126, .7152, .0722), np.float32)
    smooth = gaussian(lum, .045*ppm)
    local = gaussian(smooth, .35*ppm)
    contrast = np.clip((local-smooth)/np.maximum(local, .08), 0, 1)
    joints = np.clip((contrast-.04)/.16, 0, 1)
    mask = (joints > .32).astype(np.uint8)
    # Periodic padding preserves fracture distances at texture boundaries.
    n = len(rgb)
    distance = cv2.distanceTransform(np.tile(1-mask, (3, 3)), cv2.DIST_L2, 5)[n:2*n, n:2*n]/ppm
    plates = 1-np.exp(-distance/.22)
    chips = np.clip((smooth-gaussian(smooth, .12*ppm))/np.maximum(local, .08), -.3, .3)
    height = .22*gaussian(plates, .045*ppm)-.10*gaussian(joints, .04*ppm)+.035*chips
    height = periodic(height, round(ppm*1.5))
    height -= height.min()
    return np.clip(height, 0, cm.ROAD_CUT_RELIEF_M).astype(np.float32), joints, contrast


def bake():
    rgb = restore.read_rgb(ASSETS/'restored_4k.png')
    assert rgb.shape == (SIZE, SIZE, 3)
    height, joints, contrast = reconstruct(rgb)
    apron = round(1.5*SIZE/cm.WIDTH_M)
    # A separate CPU map keeps arid geometry aligned with its own fractures.
    # The existing Austin map and material remain byte-for-byte unchanged.
    cpu = resize_periodic(gaussian(height, .18*SIZE/cm.WIDTH_M), cm.HEIGHT_SIZE)
    Image.fromarray(np.uint16(np.rint(cpu*65535/cm.ROAD_CUT_RELIEF_M))).save(cm.ARID_CUT_HEIGHT_PATH)
    detail = np.zeros((*height.shape, 3), np.float32)
    detail[..., 2] = 1
    restore.save_rgb(OUT/f'{PREFIX}.normal.png', flat_normal(height*NORMAL_RELIEF_STRENGTH, detail)*.5+.5)
    color = np.where(rgb <= .04045, rgb/12.92, ((rgb+.055)/1.055)**2.4)
    color *= (1+.25*contrast)[..., None]
    # Same neutral calibration as other cliff families: regional vertex tint
    # supplies local rock color while mineral variation remains in the atlas.
    target = ((np.array((155, 152, 147), np.float32)/255+.055)/1.055)**2.4
    color *= target/np.median(color[::4, ::4].reshape(-1, 3), axis=0)
    # Keep crisp fracture boundaries while reducing the baked source's rust /
    # grey contrast. Regional vertex tint supplies the broad terrain hue.
    ratio = np.maximum(color/target, 1e-4)
    light = ratio @ np.array((.2126, .7152, .0722), np.float32)
    mineral = light[..., None]+MINERAL_CHROMA*(ratio-light[..., None])
    color = target*np.maximum(mineral, 1e-4)**ALBEDO_CONTRAST
    color *= target/np.median(color[::4, ::4].reshape(-1, 3), axis=0)
    save_color(OUT/f'{PREFIX}_base.color.png', periodic(color, apron))
    for a, label in ((np.clip(.87+.08*joints-.025*gaussian(contrast, 5), .82, .97), 'roughness'),
                     (1-.12*gaussian(joints, 1.2), 'ao')):
        restore.save_rgb(OUT/f'{PREFIX}_{label}.data.png', periodic(a, apron))
    metadata = dict(output_size=SIZE, span_m=cm.WIDTH_M, normal_reference='flat',
                    normal_relief_strength=NORMAL_RELIEF_STRENGTH, micro_normal_strength=MICRO_NORMAL_STRENGTH,
                    albedo_contrast=ALBEDO_CONTRAST, mineral_chroma=MINERAL_CHROMA, albedo_factor=ALBEDO_FACTOR,
                    source_sha256=hashlib.sha256((ASSETS/'restored_4k.png').read_bytes()).hexdigest(),
                    depth='Approximate isotropic joint relief; not measured depth',
                    cpu_height_sha256=hashlib.sha256(cm.ARID_CUT_HEIGHT_PATH.read_bytes()).hexdigest(),
                    selection='Arid Koppen visual biome; no coordinate-specific rule')
    (OUT/f'{PREFIX}_bake.json').write_text(json.dumps(metadata, indent=2)+'\n')
    print(json.dumps(metadata), flush=True)


def install():
    path = OUT.parent/'cliff.materials.json'
    materials = json.loads(path.read_text())
    arid = copy.deepcopy(materials[cm.ROAD_CUT_MATERIAL])
    arid['name'] = arid['mapTo'] = cm.ARID_CUT_MATERIAL
    base = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/'+PREFIX
    arid['Stages'][0].update(baseColorMap=base+'_base.color.png', normalMap=base+'.normal.png',
                            roughnessMap=base+'_roughness.data.png', ambientOcclusionMap=base+'_ao.data.png',
                            detailNormalMapStrength=MICRO_NORMAL_STRENGTH,
                            baseColorFactor=[*ALBEDO_FACTOR, 1])
    materials[cm.ARID_CUT_MATERIAL] = arid
    path.write_text(json.dumps(materials, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assemble', action='store_true')
    parser.add_argument('--bake', action='store_true')
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    cv2.setNumThreads(8)
    WORK.mkdir(parents=True, exist_ok=True)
    if args.assemble:
        restore.ASSETS, restore.WORK = ASSETS, WORK
        restore.assemble()
    if args.bake:
        bake()
    if args.install:
        install()
