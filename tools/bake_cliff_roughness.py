"""Bake conservative cliff roughness from the relief actually used for shading.

Run after changing cliff normals/materials. Only roughness maps and their material
bindings change; albedo, normals, geometry and AO are preserved. These are artistic
curvature-derived roughness estimates, not measured material properties.
Requires NumPy, Pillow and SciPy (offline authoring dependencies).
"""
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / 'mod/beamng_earth'
ASSETS = MOD / 'levels/beamng_earth/art/shapes/beamngEarth'
OUT = ASSETS / 'cliff'
PREFIX = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/'
VERSION = 'relief_roughness_v1'
# Most exposed faces remain dry stone. Small smoother patches are a separate pass.
PROFILES = {'near': (.86, .18, .10), 'far': (.88, .13, .09), 'contact': (.88, .13, .09)}


def curvature_exposure(normal):
    """Positive for convex ridges, negative for concave joints, zero on planes.

    OpenGL tangent normals store (-dh/dx, +dh/dy, 1), with image rows downward.
    Smooth gradients at several scales before differentiating, so roughness
    follows coherent rock faces rather than amplified pixel grain. Periodic
    filtering and derivatives preserve the source texture's wrapping behavior.
    """
    slopes = np.clip(normal[..., :2] / np.maximum(normal[..., 2:3], .15), -4, 4)
    exposure = np.zeros(normal.shape[:2], np.float32)
    scale = normal.shape[1] / 4096
    for radius, weight in ((8, .20), (32, .45), (96, .35)):
        smooth = gaussian_filter(slopes, (max(.5, radius*scale), max(.5, radius*scale), 0), mode='wrap')
        convexity = (np.roll(smooth[..., 0], -1, 1) - np.roll(smooth[..., 0], 1, 1)
                     - np.roll(smooth[..., 1], -1, 0) + np.roll(smooth[..., 1], 1, 0)) * .5
        amplitude = float(np.percentile(np.abs(convexity), 95))
        if amplitude > 1e-6:
            exposure += weight * np.tanh(convexity / amplitude)
    return exposure


def roughness(normal, profile='near'):
    base, polished, recessed = PROFILES[profile]
    exposure = curvature_exposure(normal)
    result = base - polished*np.maximum(exposure, 0) + recessed*np.maximum(-exposure, 0)
    return result.astype(np.float32), exposure


# Small coherent polish patches with fine variation, rather than isolated dots.
# Reduce the previous polish strength by 15%: 0.40 * 0.85 = 0.34.
# Half-width cells break up broad highlights into more, smaller regions.
PATCH_REDUCTION = .34
PATCH_STRENGTH_MIN = .55
PATCH_VARIATION_CELL_TEXELS = 48
BREAKUP_VARIATION_CELL_TEXELS = 20
BREAKUP_MIN = .35
BREAKUP_MAX = .95
PATCH_CELL_TEXELS = 32
SMALL_PATCH_CELL_TEXELS = 12
DETAIL_CELL_TEXELS = 6
MICRO_CELL_TEXELS = 3


def periodic_perlin(shape, cell_texels, seed):
    """Sample tileable 2D gradient Perlin noise, in bounded-memory row batches."""
    height, width = shape
    rows = max(1, round(height / cell_texels))
    cols = max(1, round(width / cell_texels))
    rng = np.random.default_rng(seed)
    angle = rng.uniform(0, 2*np.pi, (rows, cols))
    gradients = np.stack((np.cos(angle), np.sin(angle)), -1).astype(np.float32)
    x = np.arange(width, dtype=np.float32) * cols / width
    ix = np.floor(x).astype(int)
    fx = x - ix
    fade_x = fx**3 * (fx*(fx*6-15)+10)
    out = np.empty(shape, np.float32)
    for start in range(0, height, 128):
        y = np.arange(start, min(start+128, height), dtype=np.float32) * rows / height
        iy = np.floor(y).astype(int)[:,None]
        fy = (y - np.floor(y))[:,None]
        fade_y = fy**3 * (fy*(fy*6-15)+10)
        dots = []
        for dy, dx in ((0,0), (0,1), (1,0), (1,1)):
            g = gradients[(iy+dy)%rows, (ix[None,:]+dx)%cols]
            dots.append(g[...,0]*(fx[None,:]-dx) + g[...,1]*(fy-dy))
        top = dots[0] + fade_x*(dots[1]-dots[0])
        bottom = dots[2] + fade_x*(dots[3]-dots[2])
        out[start:start+len(y)] = top + fade_y*(bottom-top)
    return out


def patch_reduction(shape, seed):
    """Vary polish strength and erode it with two bands of fine, tileable grain.

    Grain only restores roughness toward the dry base; it never creates glossier
    specks. Its amplitude and frequency mix vary spatially, so patches do not
    share a uniform finish or a repeating single-scale noise pattern.
    """
    strength_noise = np.clip(.5 + 1.5*periodic_perlin(shape, PATCH_VARIATION_CELL_TEXELS, seed+3), 0, 1)
    strength = PATCH_STRENGTH_MIN + (1-PATCH_STRENGTH_MIN)*strength_noise
    variation = np.clip(.5 + 2*periodic_perlin(shape, BREAKUP_VARIATION_CELL_TEXELS, seed+4), 0, 1)
    mix = .25 + .5*variation
    grain = mix*periodic_perlin(shape, DETAIL_CELL_TEXELS, seed+1)
    grain += (1-mix)*periodic_perlin(shape, MICRO_CELL_TEXELS, seed+5)
    grain = np.clip(.5 + 2.5*grain, 0, 1)
    grain = grain*grain*(3-2*grain)
    breakup = BREAKUP_MIN + (BREAKUP_MAX-BREAKUP_MIN)*variation
    return PATCH_REDUCTION*strength*(1-breakup*grain)


def polished_patches(values, exposure, seed):
    """Apply uneven polish islands with fine rough interruptions throughout.

    The previous 34% reduction is a ceiling. Soft boundaries and concave joints
    keep their dry finish; finer grain breaks up each island's interior.
    """
    coarse = periodic_perlin(values.shape, PATCH_CELL_TEXELS, seed)
    mask = np.clip((coarse-.28)/.14, 0, 1)
    mask = mask*mask*(3-2*mask)
    # Scatter a second population of smaller patches between the original ones.
    small = periodic_perlin(values.shape, SMALL_PATCH_CELL_TEXELS, seed+2)
    small_mask = np.clip((small-.32)/.24, 0, 1)
    small_mask = small_mask*small_mask*(3-2*small_mask)
    mask = np.maximum(mask, small_mask)
    exposed = np.clip((exposure+.15)/.2, 0, 1)
    mask *= exposed*exposed*(3-2*exposed)
    result = values * (1-mask*patch_reduction(values.shape, seed))
    return result.astype(np.float32), mask


def read_normal(path, max_side):
    with Image.open(path) as im:
        im = im.convert('RGB')
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        encoded = np.asarray(im, dtype=np.float32) / 127.5 - 1
    # BC5 stock normals contain XY only. PNG authoring normals contain full XYZ.
    if path.suffix == '.dds' and np.max(encoded[..., 2]) < -.99:
        encoded[..., 2] = np.sqrt(np.maximum(1-np.sum(encoded[..., :2]**2, axis=-1), .001))
    return encoded


def source_for(name, stage):
    source = stage.get('normalMap')
    uv = stage.get('normalMapUseUV', 0)
    if not source:  # Legacy distant materials have no normal sample, but share its rock chart.
        source = PREFIX + ('canyon_v3.normal.png' if 'canyon' in name else 'scan_v2_flat.normal.png')
        uv = 0
    path = MOD / source.lstrip('/')
    if path.name.endswith('_ready.normal.dds'):
        path = path.with_name(path.name.replace('_ready.normal.dds', '.normal.png'))
    return path, uv


def bake():
    records, generated = [], {}
    for file in ('cliff.materials.json', 'cliff_contact.materials.json'):
        path = ASSETS / file
        mats = json.loads(path.read_text(encoding='utf8'))
        for name, mat in mats.items():
            stage = mat['Stages'][0]  # Additional terrain-rim layer is soil, not rock.
            profile = 'contact' if 'contact' in name else 'far' if name.startswith('bne_far_') else 'near'
            source, uv = source_for(name, stage)
            # Keep near 4K PBR alignment; distant roughness needs only broad features.
            max_side = 1024 if profile == 'far' else 4096
            key = (source.name, profile)
            if key not in generated:
                normal = read_normal(source, max_side)
                values, exposure = roughness(normal, profile)
                patch_mask = np.zeros_like(values)
                if profile != 'far':
                    seed = int.from_bytes(hashlib.sha256(source.name.encode()).digest()[:8], 'little')
                    values, patch_mask = polished_patches(values, exposure, seed)
                filename = source.name.split('.normal')[0] + '_' + profile + '_' + VERSION + '.data.png'
                Image.fromarray(np.uint8(np.rint(values*255))).convert('RGB').save(OUT / filename)
                generated[key] = filename
                records.append(dict(file=filename, source=source.name,
                    source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    sha256=hashlib.sha256((OUT/filename).read_bytes()).hexdigest(),
                    size=list(values.shape[::-1]), profile=profile,
                    patch_coverage=float(np.mean(patch_mask > .1)),
                    patch_core_coverage=float(np.mean(patch_mask > .95)),
                    percentiles=np.percentile(values, [0,5,50,95,100]).tolist(),
                    convex_mean=float(values[exposure>.2].mean()), concave_mean=float(values[exposure<-.2].mean())))
                print(filename, records[-1]['percentiles'], flush=True)
            stage.update(roughnessMap=PREFIX+generated[key], roughnessMapUseUV=uv, roughnessFactor=1)
            if 'roughnessDetailMap' in stage:
                # Existing granular detail is additive. Keep its darkest texels
                # from overpowering the conservative face-scale roughness.
                stage['detailRoughnessMapStrength'] = .06
        path.write_text(json.dumps(mats, indent=2)+'\n', encoding='utf8')
    (OUT/(VERSION+'.json')).write_text(json.dumps(dict(method=__doc__.strip(), profiles=PROFILES,
        polished_patches=dict(reduction=PATCH_REDUCTION, strength_min=PATCH_STRENGTH_MIN,
            strength_cell_texels=PATCH_VARIATION_CELL_TEXELS,
            breakup_range=[BREAKUP_MIN, BREAKUP_MAX], breakup_cell_texels=BREAKUP_VARIATION_CELL_TEXELS,
            micro_cell_texels=MICRO_CELL_TEXELS,
            patch_cell_texels=PATCH_CELL_TEXELS, small_patch_cell_texels=SMALL_PATCH_CELL_TEXELS,
            detail_cell_texels=DETAIL_CELL_TEXELS,
            profiles=['near', 'contact']), files=records), indent=2)+'\n', encoding='utf8')


if __name__ == '__main__':
    bake()
