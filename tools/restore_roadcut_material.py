"""Assemble overlapping AI restorations and bake the approved road-cut material.

The model reconstructs plausible detail, not measured geology. Registration and
low-frequency color matching retain the source composition. CPU displacement,
UV scale and mesh density remain unchanged. No network or model calls here.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from assemble_cliff_scans import gaussian, save_color
from bake_scan_cliff_material import periodic, flat_normal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'))
import cliffmaterial as cm
ASSETS = ROOT/'tools/assets/roadcut_generated'
WORK = ROOT/'build/roadcut-restoration'
OUT = ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff'
SIZE = 4096
PREFIX = 'cut_restored_v1'
COLOR_SIGMA = 40
OVERLAP = 28
MICRO_SPAN_M = 1.2
MICRO_FILTER_M = .035
MICRO_STRENGTH = .7
MICRO_NAME = 'cut_restored_micro_v1.normal.png'


def read_rgb(path):
    with Image.open(path) as im:
        return np.asarray(im.convert('RGB'), np.float32)/255


def save_rgb(path, rgb):
    Image.fromarray(np.rint(np.clip(rgb, 0, 1)*255).astype(np.uint8)).save(path)


def register(detail, reference):
    """Align low-frequency features with smooth, bounded optical flow.

    Flow runs at source resolution so invented pores cannot pull the large
    structure around. Only the restored crop is warped, never the reference.
    """
    h, w = reference.shape[:2]
    small = cv2.resize(detail, (w, h), interpolation=cv2.INTER_AREA)
    def guide(a):
        lum = a.mean(axis=2)
        lum = (lum-gaussian(lum, 12))/np.maximum(gaussian(lum, 12), .1)
        return np.uint8(np.clip(lum*160+128, 0, 255))
    flow = cv2.calcOpticalFlowFarneback(guide(reference), guide(small), None,
                                       .5, 4, 31, 5, 7, 1.5, 0)
    flow = gaussian(np.clip(flow, -8, 8), 3)
    dh, dw = detail.shape[:2]
    flow = cv2.resize(flow, (dw, dh), interpolation=cv2.INTER_LINEAR)
    flow *= np.array((dw/w, dh/h), np.float32)
    xx, yy = np.meshgrid(np.arange(dw, dtype=np.float32), np.arange(dh, dtype=np.float32))
    return cv2.remap(detail, xx+flow[..., 0], yy+flow[..., 1], cv2.INTER_CUBIC,
                     borderMode=cv2.BORDER_REFLECT_101)


def assemble():
    source = read_rgb(ASSETS/'source.png')
    n = source.shape[0]
    total = np.zeros((SIZE, SIZE, 3), np.float32)
    weights = np.zeros((SIZE, SIZE), np.float32)
    records = json.loads((WORK/'tiles.json').read_text())
    for record in records:
        name = record['name']
        x0, y0, x1, y1 = record['bounds']
        detail = read_rgb(WORK/(name+'-output.png'))
        record['generated_size'] = list(detail.shape[1::-1])
        record['sha256'] = hashlib.sha256((WORK/(name+'-output.png')).read_bytes()).hexdigest()
        ref = source[np.arange(y0, y1)%n][:, np.arange(x0, x1)%n]
        detail = register(detail, ref)
        px0, py0, px1, py1 = [round(v*SIZE/n) for v in (x0, y0, x1, y1)]
        width, height = px1-px0, py1-py0
        detail = cv2.resize(detail, (width, height), interpolation=cv2.INTER_AREA)
        ref = cv2.resize(ref, (width, height), interpolation=cv2.INTER_CUBIC)
        # Preserve broad mineral color while retaining restored fracture detail.
        detail *= np.clip(gaussian(ref, COLOR_SIGMA)/np.maximum(gaussian(detail, COLOR_SIGMA), .03), .5, 2)
        feather = round(OVERLAP*SIZE/n*2)
        def ramp(length):
            distance = np.minimum(np.arange(length)+.5, length-np.arange(length)-.5)
            return .5-.5*np.cos(np.pi*np.clip(distance/feather, 0, 1))
        weight = ramp(height)[:, None]*ramp(width)[None, :]
        ys, xs = np.arange(py0, py1)%SIZE, np.arange(px0, px1)%SIZE
        total[np.ix_(ys, xs)] += detail*weight[..., None]
        weights[np.ix_(ys, xs)] += weight
        print('Registered '+name, flush=True)
    assert np.all(weights > 0), 'Uncovered atlas pixels'
    result = total/weights[..., None]
    save_rgb(ASSETS/'restored_4k.png', result)
    (ASSETS/'restoration_tiles.json').write_text(json.dumps(records, indent=2)+'\n')
    save_rgb(WORK/'atlas-preview.png', cv2.resize(result, (1400, 1400), interpolation=cv2.INTER_AREA))
    before = cv2.resize(source, (SIZE, SIZE), interpolation=cv2.INTER_CUBIC)
    box = (slice(1250, 1950), slice(1250, 1950))
    comparison = Image.new('RGB', (1400, 740), (28, 28, 28))
    for i, (a, title) in enumerate(((before, 'Original source enlarged'), (result, 'AI-restored 4K, same area'))):
        comparison.paste(Image.fromarray(np.uint8(np.clip(a[box], 0, 1)*255)), (i*700, 40))
        ImageDraw.Draw(comparison).text((i*700+15, 13), title, fill='white')
    comparison.save(WORK/'restoration-comparison.png')


def reconstruct(rgb):
    """Plausible relief from narrow joints, not grayscale-as-height.

    Remove metre-scale illumination/mineral changes before extracting joints.
    Horizontal morphological support favours bedding over isolated stains.
    Distance from these joints makes convex plates with recessed boundaries.
    Broad dark weathering has no direct influence on surface depth.
    """
    size = rgb.shape[0]
    assert rgb.shape == (size, size, 3)
    ppm = size/cm.WIDTH_M
    lum = rgb @ np.array((.2126, .7152, .0722), np.float32)
    smooth = gaussian(lum, .045*ppm)
    local = gaussian(smooth, .35*ppm)
    contrast = np.clip((local-smooth)/np.maximum(local, .08), 0, 1)
    narrow = np.clip((contrast-.035)/.14, 0, 1)
    kernel = np.ones((1, max(3, round(.25*ppm))), np.uint8)
    beds = cv2.morphologyEx(narrow, cv2.MORPH_OPEN, kernel)
    # Stronger short fractures survive too, without interpreting wide stains
    # as cavities. Both channels use the generated image's own feature layout.
    joints = np.maximum(beds, .4*narrow)
    mask = (joints > .32).astype(np.uint8)
    # Wrap before distance transform so opposite tile borders are neighbours.
    distance = cv2.distanceTransform(np.tile(1-mask, (3, 3)), cv2.DIST_L2, 5)
    distance = distance[size:2*size, size:2*size]/ppm
    plates = 1-np.exp(-distance/.18)
    height = .12*gaussian(plates, .045*ppm)-.10*gaussian(joints, .04*ppm)
    # Limited microcontrast is an approximation, not a claim of recovered pores.
    chips = np.clip((smooth-gaussian(smooth, .12*ppm))/np.maximum(local, .08), -.3, .3)
    height += .035*chips
    height = periodic(height, round(ppm*1.5))
    height -= height.min()
    height = np.clip(height, 0, cm.ROAD_CUT_RELIEF_M)
    return height.astype(np.float32), joints, contrast


def bake():
    rgb = read_rgb(ASSETS/'restored_4k.png')
    assert rgb.shape == (SIZE, SIZE, 3)
    # Reconstruct the approved baseline in memory; never overwrite CPU height.
    old_rgb = cv2.resize(read_rgb(ASSETS/'source.png'), (2048, 2048), interpolation=cv2.INTER_LANCZOS4)
    old_height, _, _ = reconstruct(old_rgb)
    old_height = cv2.resize(old_height, (SIZE, SIZE), interpolation=cv2.INTER_CUBIC)
    new_height, joints, contrast = reconstruct(rgb)
    sigma = .35*SIZE/cm.WIDTH_M
    # Keep broad relief; replace fuzzy joint shading with registered fine relief.
    shading = gaussian(old_height, sigma)+.8*(new_height-gaussian(new_height, sigma))
    apron = round(1.5*SIZE/cm.WIDTH_M)
    shading = periodic(shading, apron)
    detail = np.zeros((*shading.shape, 3), np.float32)
    detail[..., 2] = 1
    normal = flat_normal(shading, detail)
    save_rgb(OUT/f'{PREFIX}.normal.png', normal*.5+.5)
    color = np.where(rgb <= .04045, rgb/12.92, ((rgb+.055)/1.055)**2.4)
    color *= (1+.25*contrast)[..., None]
    target = ((np.array((155, 152, 147), np.float32)/255+.055)/1.055)**2.4
    color *= target/np.median(color[::4, ::4].reshape(-1, 3), axis=0)
    color = periodic(color, apron)
    save_color(OUT/f'{PREFIX}_base.color.png', color)
    save_color(WORK/'basecolor.png', color)
    for a, label in ((np.clip(.87+.08*joints-.025*gaussian(contrast, 5), .82, .97), 'roughness'),
                     (1-.12*gaussian(joints, 1.2), 'ao')):
        a = periodic(a, apron)
        save_rgb(OUT/f'{PREFIX}_{label}.data.png', a)
        save_rgb(WORK/(label+'.png'), a)
    metadata = dict(output_size=SIZE, span_m=cm.WIDTH_M, normal_reference='flat',
                    source='16 overlapping generative restorations of the approved limestone image',
                    depth='Approximate joint-guided shading; not measured depth',
                    geometry='Existing CPU height, UV scale and mesh density preserved',
                    cpu_height_sha256=hashlib.sha256(cm.ROAD_CUT_HEIGHT_PATH.read_bytes()).hexdigest())
    (OUT/f'{PREFIX}_bake.json').write_text(json.dumps(metadata, indent=2)+'\n')
    print(json.dumps(metadata), flush=True)


def bake_micro():
    """Keep scanned millimetre/centimetre relief without the source rock form.

    Filter tangent slopes, not encoded RGB. Limit sharp scan outliers and
    feather periodic edges before renormalizing. The engine layers this over
    the existing macro normal at its own physical scale, including flat beds.
    """
    source = ROOT/'tools/assets/cliff/rock_06_nor_gl_2k.png'
    n = read_rgb(source)*2-1
    slopes = n[..., :2]/np.maximum(n[..., 2:], .2)
    sigma = MICRO_FILTER_M*n.shape[1]/MICRO_SPAN_M
    slopes -= gaussian(slopes, sigma)
    slopes = .65*np.tanh(slopes/.65)
    slopes = periodic(slopes, 32)
    normal = np.concatenate((slopes, np.ones_like(slopes[..., :1])), axis=-1)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    save_rgb(OUT/MICRO_NAME, normal*.5+.5)
    metadata = dict(source='Rock 06 / Rob Tuytel / Poly Haven, CC0',
                    source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    span_m=MICRO_SPAN_M, filter_m=MICRO_FILTER_M,
                    strength=MICRO_STRENGTH, size=n.shape[0],
                    relief='High-pass scanned tangent slopes; no albedo-derived height')
    (OUT/'cut_restored_micro_v1.json').write_text(json.dumps(metadata, indent=2)+'\n')
    print(json.dumps(metadata), flush=True)


def install():
    path = OUT.parent/'cliff.materials.json'
    materials = json.loads(path.read_text())
    stage = materials[cm.ROAD_CUT_MATERIAL]['Stages'][0]
    base = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/'+PREFIX
    stage.update(baseColorMap=base+'_base.color.png', normalMap=base+'.normal.png',
                 roughnessMap=base+'_roughness.data.png', ambientOcclusionMap=base+'_ao.data.png')
    # Retain the current 1.2 m detail-color scale; replace only normal relief.
    stage.update(detailNormalMap='/levels/beamng_earth/art/shapes/beamngEarth/cliff/'+MICRO_NAME,
                 detailScale=[cm.WIDTH_M/MICRO_SPAN_M, cm.HEIGHT_M/MICRO_SPAN_M],
                 detailNormalMapStrength=MICRO_STRENGTH)
    path.write_text(json.dumps(materials, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assemble', action='store_true')
    parser.add_argument('--bake', action='store_true')
    parser.add_argument('--detail', action='store_true', help='Rebuild scanned fine bump normal only')
    parser.add_argument('--install', action='store_true', help='Select restored maps in repository material')
    args = parser.parse_args()
    cv2.setNumThreads(8)
    WORK.mkdir(parents=True, exist_ok=True)
    if args.assemble:
        assemble()
    if args.bake:
        bake()
    if args.bake or args.detail:
        bake_micro()
    if args.install:
        install()
