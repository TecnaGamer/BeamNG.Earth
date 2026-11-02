"""Bake scanned cliff PBR maps and full-relief normals against a flat plane."""
import argparse
import json
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'))
import cliffmaterial as cm
from assemble_cliff_scans import detail_from_normals, geometric_normal, unit, save_color

SOURCE = ROOT/'build/cliff-assembly'
OUT = ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff'
PREFIX = 'scan_v2'


def periodic(a, width):
    """Shared smooth opposing-edge apron, applied before computing normals."""
    result = a.copy()
    for axis in (0, 1):
        v = np.swapaxes(result, axis, 0)
        first, last = v[:width].copy(), v[-width:][::-1].copy()
        average = (first+last)*.5
        t = (.5+.5*np.cos(np.linspace(0, np.pi, width))).astype(np.float32)
        t = t.reshape((width,)+(1,)*(v.ndim-1))
        v[:width] = first*(1-t)+average*t
        v[-width:] = (last*(1-t)+average*t)[::-1]
    return result


def resize_periodic(a, size):
    """Bilinear interpolation at pixel centres, identical to the CPU sampler."""
    ny, nx = a.shape[:2]
    w, h = size
    x = (np.arange(w, dtype=np.float32)+.5)*nx/w-.5
    y = (np.arange(h, dtype=np.float32)+.5)*ny/h-.5
    xx, yy = np.meshgrid(x, y)
    return cv2.remap(a, xx, yy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def runtime_frame(height, width=cm.WIDTH_M, span=cm.HEIGHT_M, slope=cm.REFERENCE_SLOPE):
    """UV tangent frame of P(u,v)=(u,v/slope,v)+Nterrain*height.

    Rows run downwards in the PNG, whereas UV V points up the wall. This is the
    same normal displacement used by cliffrocks.mesh; normals include displacement.
    """
    ny, nx = height.shape
    hu = (np.roll(height,-1,1)-np.roll(height,1,1))/(2*width/nx)
    hv = (np.roll(height,1,0)-np.roll(height,-1,0))/(2*span/ny)
    terrain_normal = np.array((0, -slope, 1), np.float32)/np.sqrt(1+slope*slope)
    tangent = np.array((1, 0, 0), np.float32) + hu[..., None]*terrain_normal
    vertical = np.array((0, 1/slope, 1), np.float32) + hv[..., None]*terrain_normal
    normal = unit(np.cross(tangent, vertical))
    tangent = unit(tangent)
    return tangent, unit(np.cross(normal, tangent)), normal


def residual_normal(high_height, source_detail, low_height, size):
    """Encode high surface shading relative to the low mesh, not a flat plane."""
    high_frame = runtime_frame(high_height)
    target = unit(sum(axis*source_detail[..., i:i+1] for i, axis in enumerate(high_frame)))
    del high_frame
    low_t, _, low_n = runtime_frame(low_height)
    normal = unit(resize_periodic(low_n, size))
    tangent = resize_periodic(low_t, size)
    tangent = unit(tangent-normal*np.sum(tangent*normal, axis=-1, keepdims=True))
    bitangent = np.cross(normal, tangent)
    return unit(np.stack([np.sum(target*axis, axis=-1) for axis in (tangent,bitangent,normal)], axis=-1))


def source_detail(height, normal_rgb, ppm, strip_rows=128):
    """Recover scan detail in strips, preserving full-image gradient boundaries."""
    ny, nx = height.shape
    result = np.empty((ny,nx,3),np.float32)
    for start in range(0,ny,strip_rows):
        end = min(start+strip_rows,ny)
        lo, hi = max(0,start-1),min(ny,end+1)
        geometry = geometric_normal(height[lo:hi],ppm)[start-lo:end-lo]
        full = unit(normal_rgb[start:end].astype(np.float32)/127.5-1)
        result[start:end] = detail_from_normals(full,geometry)
    return result


def flat_normal(height, detail, strip_rows=128):
    """Project the complete high-resolution shading onto an undisplaced plane.

    Periodic halo rows keep derivative/normal continuity between strips. The
    reference plane retains the existing UV physical scale and 3:1 slope; only
    relief is removed from the receiving frame. No low-mesh normals are subtracted.
    """
    ny,nx = height.shape
    flat = runtime_frame(np.zeros((2,2),np.float32))
    axes = [axis[0,0] for axis in flat]
    result = np.empty((ny,nx,3),np.float32)
    for start in range(0,ny,strip_rows):
        end = min(start+strip_rows,ny)
        indices = np.arange(start-1,end+1)%ny
        high = runtime_frame(height[indices],span=cm.HEIGHT_M*len(indices)/ny)
        target = unit(sum(axis[1:-1]*detail[start:end,...,i:i+1] for i,axis in enumerate(high)))
        result[start:end] = unit(np.stack([np.sum(target*axis,axis=-1) for axis in axes],axis=-1))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--normals-only', action='store_true', help='Keep existing CPU height and other PBR maps byte-for-byte.')
    args = parser.parse_args(argv)
    cv2.setNumThreads(8)
    OUT.mkdir(parents=True, exist_ok=True)
    height = np.load(SOURCE/'height_metres.npy')
    size = height.shape[::-1]
    ppm = size[0]/cm.WIDTH_M
    # Recover source micro-normal detail before replacing the source's geometry.
    with Image.open(SOURCE/'normal.png') as im:
        detail = source_detail(height,np.asarray(im.convert('RGB')),ppm)
    apron = round(3*ppm)
    height = periodic(height, apron)
    height -= height.min()
    height *= cm.RELIEF_M/max(float(height.max()), 1e-6)
    detail = unit(periodic(detail, apron))
    if not args.normals_only:
        low = cv2.resize(height, cm.HEIGHT_SIZE, interpolation=cv2.INTER_AREA)
        Image.fromarray(np.rint(low/cm.RELIEF_M*65535).astype(np.uint16)).save(cm.HEIGHT_PATH)
    print('Baking normals against a flat reference', flush=True)
    normal = flat_normal(height, detail)
    normal_suffix = 'flat.normal.png'
    # Encode in strips too, avoiding multiple full-resolution float temporaries.
    encoded = np.empty(normal.shape,np.uint8)
    for start in range(0,normal.shape[0],128):
        encoded[start:start+128] = np.rint(np.clip(normal[start:start+128]*.5+.5,0,1)*255).astype(np.uint8)
    Image.fromarray(encoded).save(OUT/f'{PREFIX}_{normal_suffix}')
    del encoded
    del normal, detail, height
    for source, suffix, channel in [('basecolor.png','base.color.png','RGB'),
                                     ('roughness.png','roughness.data.png','L'),
                                     ('ao.png','ao.data.png','L')]:
        if args.normals_only:
            continue
        a = np.asarray(Image.open(SOURCE/source).convert(channel), np.float32)/255
        if channel == 'RGB':
            a = np.where(a<=.04045,a/12.92,((a+.055)/1.055)**2.4)
            save_color(OUT/f'{PREFIX}_{suffix}', periodic(a, apron))
        else:
            Image.fromarray(np.rint(periodic(a, apron)*255).astype(np.uint8)).save(OUT/f'{PREFIX}_{suffix}')
    material_path = OUT.parent/'cliff.materials.json'
    material = json.loads(material_path.read_text())
    stage = material['bne_cliff_strata']['Stages'][0]
    base = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/'
    for key, suffix in [('baseColorMap','base.color.png'),('normalMap',normal_suffix),
                        ('roughnessMap','roughness.data.png'),('ambientOcclusionMap','ao.data.png')]:
        if not args.normals_only or key == 'normalMap':
            stage[key] = base+PREFIX+'_'+suffix
    # A normal-only comparison preserves the material's current detail settings.
    detail_root = '/temp/beamngEarth/shapes/cliff_talus_v2/'
    if not args.normals_only:
        # Match road-cut brightness and temper the scans' warm cast; vertex tint supplies local color.
        stage['baseColorFactor'] = [1.3, 1.4, 1.5, 1]
        for key in list(stage):
            if 'detail' in key.lower():
                del stage[key]
        stage.update(detailMap=detail_root+'detail.data.dds', detailNormalMap=base+'cut_restored_micro_v1.normal.png',
                     detailNormalMapStrength=.3, detailScale=[cm.WIDTH_M/1.2, cm.HEIGHT_M/1.2])
    material_path.write_text(json.dumps(material,indent=2)+'\n')
    meta = dict(source='build/cliff-assembly', size=size, span_m=[cm.WIDTH_M,cm.HEIGHT_M],
                height_grid=cm.HEIGHT_SIZE, relief_m=cm.RELIEF_M, reference_slope=cm.REFERENCE_SLOPE,
                normal_reference='flat',
                normal='OpenGL +Y tangent-space full relief against undisplaced plane',
                limitations='Full relief can reinforce slopes already represented by geometry; the flat bake favors visible detail.')
    (OUT/'scan_bake.json').write_text(json.dumps(meta,indent=2))
    (OUT/'SCAN_ATTRIBUTION.txt').write_text((SOURCE/'ATTRIBUTION.txt').read_text()+f'\nRuntime changes: periodic edge apron, relief scaling, flat-reference normal bake.\n')
    print('Normal map and binding updated' if args.normals_only else 'Runtime scan maps and CPU displacement baked', flush=True)


if __name__ == '__main__':
    main()
