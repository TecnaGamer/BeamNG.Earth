"""Assemble sixteen independently generated detail patches into a 4K near material.

Each 1254px source covers 1152 output pixels: new detail is downsampled, never
enlarged. The approved image controls broad colour/strata; overlapping generated
patches supply new fine content. PBR is derived only after periodic assembly.
Run this after the legacy canyon baker when updating the complete material set.
"""
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from bake_canyon_near_pbr import OUT, bake as bake_pbr
from bake_cliff_transition import bake as bake_transition, linear
from bake_scan_cliff_material import periodic

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT/'tools/assets/canyon_generated/tiled-v5'
ALBEDO = 'canyon_v5_base.color.png'
PBR_PREFIX = 'canyon_v5_faceted_near'
# Keep large beds and the previously approved terrain palette; add resolved
# detail at shorter wavelengths. This corrects generator exposure/palette drift.
PALETTE_SIGMA_PX = 24.0
PERIODIC_APRON_PX = 8


def image_array(path):
    with Image.open(path) as im:
        return np.asarray(im.convert('RGB'), np.float32)/255


def srgb(v):
    return np.where(v <= .0031308, 12.92*v, 1.055*np.maximum(v, 0)**(1/2.4)-.055)


def assemble():
    manifest = json.loads((SOURCES/'manifest.json').read_text())
    size, core, halo, grid = (manifest[k] for k in ('size', 'core', 'halo', 'grid'))
    assert size == core*grid == 4096
    assert {(p['row'],p['column']) for p in manifest['patches']} == {
        (y,x) for y in range(grid) for x in range(grid)}
    guide = image_array(ROOT/manifest['guide'])
    assert guide.shape == (size,size,3)
    footprint = core+2*halo
    overlap = 2*halo
    ramp = np.sin(np.linspace(0,np.pi/2,overlap,dtype=np.float32))**2
    weight = np.ones(footprint,np.float32)
    weight[:overlap], weight[-overlap:] = ramp, ramp[::-1]
    weight = weight[:,None]*weight[None,:]
    total = np.zeros_like(guide)
    coverage = np.zeros((size,size),np.float32)
    hashes = {}
    for patch in manifest['patches']:
        path = SOURCES/patch['file']
        source = image_array(path)
        assert min(source.shape[:2]) >= footprint, 'Never upscale a generated patch'
        assert list(source.shape[1::-1]) == patch['native_size']
        generated = cv2.resize(source,(footprint,footprint),interpolation=cv2.INTER_AREA)
        ys = (np.arange(footprint)+patch['row']*core-halo)%size
        xs = (np.arange(footprint)+patch['column']*core-halo)%size
        reference = guide[np.ix_(ys,xs)]
        correction = cv2.GaussianBlur(reference-generated,(0,0),PALETTE_SIGMA_PX)
        generated = np.clip(generated+correction,0,1)
        total[np.ix_(ys,xs)] += generated*weight[...,None]
        coverage[np.ix_(ys,xs)] += weight
        hashes[patch['file']] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert coverage.min() > .99
    result = periodic(total/coverage[...,None], PERIODIC_APRON_PX)
    # Preserve mean reflected colour, not just mean sRGB. This avoids a brightness
    # jump in the satellite blend from the new high-frequency texture variance.
    target_mean = linear(guide).mean(axis=(0,1),dtype=np.float64)
    actual_mean = linear(result).mean(axis=(0,1),dtype=np.float64)
    result = np.clip(srgb(linear(result)*(target_mean/actual_mean)),0,1)
    encoded = np.uint8(np.rint(result*255))
    Image.fromarray(encoded).save(OUT/ALBEDO)
    result = encoded.astype(np.float32)/255
    actual_mean = linear(result).mean(axis=(0,1),dtype=np.float64)
    audit = {
        'method': manifest['method'],
        'size': [size,size], 'native_patch_sizes': [p['native_size'] for p in manifest['patches']],
        'patch_output_size': footprint, 'patch_core_size': core, 'overlap_pixels': overlap,
        'palette_sigma_pixels': PALETTE_SIGMA_PX, 'periodic_apron_pixels': PERIODIC_APRON_PX,
        'source_hashes': hashes, 'albedo_sha256': hashlib.sha256((OUT/ALBEDO).read_bytes()).hexdigest(),
        'provenance': '4K detail reconstructed from 16 independent generated patches; not a single native-4K generation or a whole-image upscale.',
        'pbr': 'Full-resolution albedo-derived approximate normal, roughness and cavity AO; not measured material channels.',
        'guide_linear_mean': target_mean.tolist(), 'output_linear_mean': actual_mean.tolist(),
    }
    for name, a in [('guide',guide),('assembled',result)]:
        lum = a.mean(axis=2)
        audit[name+'_fine_rms'] = float(np.std(lum-cv2.GaussianBlur(lum,(0,0),1.0)))
    audit['opposing_edge_max_byte_difference'] = int(max(
        np.abs(encoded[0].astype(int)-encoded[-1]).max(),
        np.abs(encoded[:,0].astype(int)-encoded[:,-1]).max()))
    (SOURCES/'assembly.json').write_text(json.dumps(audit,indent=2)+'\n')
    preview = ROOT/'build/canyon-tiled-v5'
    preview.mkdir(parents=True,exist_ok=True)
    Image.fromarray(encoded).resize((1024,1024),Image.Resampling.LANCZOS).save(preview/'assembled.png')
    repeat = np.tile(np.asarray(Image.fromarray(encoded).resize((512,512))),(2,2,1))
    Image.fromarray(repeat).save(preview/'repeat.png')
    # A one-to-one central crop straddles both internal patch boundaries.
    old = np.uint8(np.rint(guide[1792:2304,1792:2304]*255))
    new = encoded[1792:2304,1792:2304]
    Image.fromarray(np.concatenate((old,new),axis=1)).save(preview/'detail-before-after.png')
    return target_mean, actual_mean, audit


def install_material(target_mean, actual_mean):
    path = OUT.parent/'cliff.materials.json'
    mats = json.loads(path.read_text())
    stage = mats['bne_cliff_canyon_sat']['Stages'][0]
    # Only compensate when migrating from the approved v4 material. Subsequent
    # runs are idempotent; a fresh legacy bake restores the v4 factor first.
    if Path(stage['baseColorMap']).name != ALBEDO:
        stage['baseColorFactor'][:3] = srgb(
            linear(np.array(stage['baseColorFactor'][:3]))*target_mean/actual_mean).tolist()
    stage['baseColorMap'] = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/'+ALBEDO
    path.write_text(json.dumps(mats,indent=2)+'\n')
    bake_pbr(ALBEDO,PBR_PREFIX)
    bake_transition()


def bake():
    target_mean, actual_mean, audit = assemble()
    install_material(target_mean, actual_mean)
    print(json.dumps({k:audit[k] for k in (
        'size','guide_fine_rms','assembled_fine_rms','opposing_edge_max_byte_difference')},indent=2))


if __name__ == '__main__':
    bake()
