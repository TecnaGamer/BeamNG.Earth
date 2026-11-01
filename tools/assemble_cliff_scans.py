"""Quilt registered scan projections into a 300 ft square rock material.

Local relief originates in scanned geometry, with authored macro weathering.
Minimum-error cuts share one owner
mask across PBR channels; a wider height blend joins the surface continuously.
Normal detail is expressed relative to the source geometry before transferring
it onto the joined surface, so geometry is not counted twice in the shader.

Requires numpy, opencv-python, Pillow. Run project_cliff_scans.py first.
"""
import argparse
import json
import os
from pathlib import Path

os.environ.setdefault('OPENCV_IO_ENABLE_OPENEXR', '1')
import cv2
import numpy as np
from PIL import Image
from cliff_weathering import shape_and_erode

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/cliff-scans'
OUT = ROOT / 'build/cliff-assembly'
PANEL_M = 91.44
TARGET_COLOR = np.array((.34, .305, .255), np.float32)
CHANNELS = ('color', 'height', 'detail', 'roughness', 'ao')


def unit(v):
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-6)


def frame(normal):
    """Projection-X tangent frame, consistently oriented with upward UV V."""
    normal = unit(normal)
    tangent = np.zeros_like(normal)
    tangent[..., 0] = 1
    tangent = unit(tangent - normal * normal[..., :1])
    return tangent, np.cross(normal, tangent), normal


def detail_from_normals(normal, geometry):
    return np.stack([np.sum(normal * axis, axis=-1) for axis in frame(geometry)], axis=-1)


def normal_from_detail(detail, geometry):
    return unit(sum(axis * detail[..., i:i+1] for i, axis in enumerate(frame(geometry))))


def gaussian(a, sigma):
    return cv2.GaussianBlur(a, (0, 0), max(sigma, .01), borderType=cv2.BORDER_REFLECT_101)


def srgb(linear):
    linear = np.clip(linear, 0, 1)
    return np.where(linear <= .0031308, linear * 12.92, 1.055 * linear ** (1/2.4) - .055)


def save_color(path, a):
    Image.fromarray(np.rint(srgb(a) * 255).astype(np.uint8)).save(path)


def read_projection(name, ppm, crop=None, scale=1):
    """Load registered channels and harmonize broad exposure/color only."""
    path = WORK / name / 'projection'
    meta = json.loads((path / 'projection.json').read_text())
    width, height = meta['width_m'], meta['height_m']
    native_w, native_h = meta['pixels']
    if crop is None:
        crop = (.015, .22, .985, .88) if name.startswith('coastal') else (.1, .2, .9, .85)
    x0, y0, x1, y1 = crop
    width, height = width * (x1-x0) * scale, height * (y1-y0) * scale
    size = (max(8, round(width * ppm)), max(8, round(height * ppm)))

    def read(channel, scalar=False):
        a = cv2.imread(str(path / f'{channel}_0001.exr'), cv2.IMREAD_UNCHANGED)
        if a is None:
            raise FileNotFoundError(path / f'{channel}_0001.exr')
        a = a[round(y0*native_h):round(y1*native_h), round(x0*native_w):round(x1*native_w)]
        a = a[..., 0] if scalar else a[..., ::-1]
        return cv2.resize(a, size, interpolation=cv2.INTER_AREA)

    valid = read('Alpha', True) > .995
    color, depth = read('albedo'), read('depth', True) * scale
    geometry, normal = read('geonormal') * 2-1, read('normal') * 2-1
    rough, ao = read('roughness', True), read('ao', True)
    # Small holes at scan silhouettes get nearest valid sample extension. Crops
    # exclude most vegetation/sky; these padded pixels are not chosen as detail.
    if not valid.all():
        if not valid.any():
            raise ValueError(f'No valid surface in {name}')
        _, labels = cv2.distanceTransformWithLabels((~valid).astype(np.uint8), cv2.DIST_L2, 5,
                                                    labelType=cv2.DIST_LABEL_PIXEL)
        yy, xx = np.where(valid)
        invalid = ~valid
        near = labels[invalid] - 1
        for a in (color, depth, geometry, normal, rough, ao):
            a[invalid] = a[yy[near], xx[near]]

    # Remove acquisition-scale color differences; keep pores, chips and mineral
    # variation. This is bounded exposure normalization, not invented de-lighting.
    color /= np.maximum(ao[..., None], .4) ** .5
    luminance = color @ np.array((.2126, .7152, .0722), np.float32)
    broad = gaussian(luminance, ppm * .75)
    lift = np.clip(np.median(broad[valid]) / np.maximum(broad, .015), .55, 2.3) ** .45
    color *= lift[..., None]
    median = np.median(color[valid], axis=0)
    color *= TARGET_COLOR / np.maximum(median, .015)
    gray = np.mean(color, axis=-1, keepdims=True)
    color = gray + (color-gray) * .35
    color = np.clip(color, .005, .85)
    # Remove only the overall scan's orientation/curvature; local ledges remain.
    depth -= gaussian(depth, ppm * 5)
    depth = gaussian(np.clip(depth, -3, 3), ppm*.08)
    result = dict(color=color, height=depth, detail=detail_from_normals(unit(normal), unit(geometry)),
                  roughness=np.clip(rough, .5, .98), ao=np.clip(ao, .35, 1), valid=valid)
    result['meta'] = dict(asset=name, width_m=width, height_m=height, crop=crop, scale=scale,
                          color_gain=(TARGET_COLOR / np.maximum(median, .015)).tolist())
    return result


def minimum_path(cost):
    """Top-to-bottom minimum-error seam with bounded lateral movement."""
    h, w = cost.shape
    total = cost[0].copy()
    parents = np.empty((h, w), np.int8)
    for y in range(1, h):
        choices = np.stack((np.r_[np.inf, total[:-1]], total, np.r_[total[1:], np.inf]))
        step = np.argmin(choices, axis=0)
        parents[y] = step - 1
        total = cost[y] + choices[step, np.arange(w)]
    result = np.empty(h, np.int32)
    result[-1] = np.argmin(total)
    for y in range(h-1, 0, -1):
        result[y-1] = result[y] + parents[y, result[y]]
    return result


def cut_mask(error, overlap_x, overlap_y):
    """Find natural joins rather than a rectangular crossfade boundary."""
    h, w = error.shape
    mask = np.ones((h, w), np.uint8)
    reduction = max(1, round(max(h, w) / 450))
    if overlap_x > 5:
        costs = cv2.resize(error[:, :overlap_x], (max(3, overlap_x//reduction), max(3, h//reduction)),
                           interpolation=cv2.INTER_AREA)
        path = minimum_path(costs)
        path = np.interp(np.linspace(0, len(path)-1, h), np.arange(len(path)), path) * reduction
        mask &= np.arange(w)[None, :] >= path[:, None]
    if overlap_y > 5:
        costs = cv2.resize(error[:overlap_y], (max(3, w//reduction), max(3, overlap_y//reduction)),
                           interpolation=cv2.INTER_AREA)
        path = minimum_path(costs.T)
        path = np.interp(np.linspace(0, len(path)-1, w), np.arange(len(path)), path) * reduction
        mask &= np.arange(h)[:, None] >= path[None, :]
    return mask


def feather(mask, radius):
    padded = np.pad(mask.astype(np.uint8), 1, constant_values=0)
    distance = cv2.distanceTransform(padded, cv2.DIST_L2, 5)[1:-1, 1:-1]
    outside = cv2.distanceTransform(1-padded, cv2.DIST_L2, 5)[1:-1, 1:-1]
    t = np.clip(.5 + (distance-outside) / max(2*radius, 1), 0, 1)
    return t*t*(3-2*t)


def paste(canvas, patch, x, y, overlap_x, overlap_y, ppm, identifier, organic=False):
    h, w = patch['height'].shape
    x1, y1 = min(x+w, canvas['height'].shape[1]), min(y+h, canvas['height'].shape[0])
    if min(x1-x, y1-y) <= 0:
        return
    h, w = y1-y, x1-x
    region = np.s_[y:y1, x:x1]
    occupied = canvas['occupied'][region]
    old_height = canvas['height'][region]
    p = {key: patch[key][:h, :w].copy() for key in CHANNELS}
    if occupied.any():
        offset = np.clip(np.median((old_height-p['height'])[occupied]), -.65, .65)
        p['height'] += offset
    error = np.mean((canvas['color'][region]-p['color'])**2, axis=-1)*12
    error += np.minimum(np.abs(old_height-p['height']), 3)*.04
    mask = cut_mask(error, min(overlap_x, w), min(overlap_y, h))
    if organic:
        yy, xx = np.mgrid[:h, :w]
        ellipse = ((xx-w/2)/(w*.47))**2 + ((yy-h/2)/(h*.47))**2
        mask = (ellipse < 1).astype(np.uint8)
    mask[~occupied] = 1
    color_weight = feather(mask, ppm*.38)
    depth_weight = feather(mask, ppm*1.1)
    color_weight[~occupied] = depth_weight[~occupied] = 1
    for key in CHANNELS:
        weight = depth_weight if key == 'height' else color_weight
        if p[key].ndim == 3:
            weight = weight[..., None]
        canvas[key][region] *= 1-weight
        canvas[key][region] += p[key]*weight
    canvas['owner'][region][color_weight > .5] = identifier
    canvas['occupied'][region] = True


def patch_from(source, width, height, rng, flip=False, scale=1, angle=0):
    """Transform all maps together, keeping rotations within the actual scan."""
    sh, sw = source['height'].shape
    height, width = min(height, int(sh*scale*.98)), min(width, int(sw*scale*.98))
    radians = np.deg2rad(angle)
    for _ in range(40):
        c, s = np.cos(radians), np.sin(radians)
        extent_x = (abs(c)*width + abs(s)*height)/scale
        extent_y = (abs(s)*width + abs(c)*height)/scale
        if extent_x <= sw-1 and extent_y <= sh-1:
            break
        radians *= .8
    x = rng.uniform(extent_x/2, max(extent_x/2, sw-1-extent_x/2))
    y = rng.uniform(extent_y/2, max(extent_y/2, sh-1-extent_y/2))
    yy, xx = np.mgrid[:height, :width].astype(np.float32)
    xx, yy = (xx-(width-1)/2)/scale, (yy-(height-1)/2)/scale
    mapx = (c*xx-s*yy+x).astype(np.float32)
    mapy = (s*xx+c*yy+y).astype(np.float32)
    patch = {key: cv2.remap(source[key], mapx, mapy, cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REFLECT_101) for key in CHANNELS}
    patch['height'] *= scale
    nx, ny = patch['detail'][..., 0].copy(), patch['detail'][..., 1].copy()
    patch['detail'][..., 0] = c*nx-s*ny
    patch['detail'][..., 1] = s*nx+c*ny
    if flip:
        patch = {key: value[:, ::-1].copy() for key, value in patch.items()}
        patch['detail'][..., 0] *= -1
    return patch, dict(center_pixel=(x, y), scale=scale, rotation_degrees=float(np.rad2deg(radians)), flip=flip)


def geometric_normal(height, ppm):
    gy, gx = np.gradient(height, 1/ppm)
    return unit(np.stack((-gx, gy, np.ones_like(gx)), axis=-1))


def write_attribution(output, manifest):
    """Keep license/source notices beside exported maps and review scenes."""
    records = json.loads((ROOT/'tools/assets/cliff/scans.json').read_text(encoding='utf-8'))
    sources = {record['id']: record for record in records}
    lines = ['Derived scanned rock asset. Code license does not replace asset licenses.', '']
    for entry in manifest:
        name = entry['asset']
        source = sources[name]
        lines.extend([f"{name} by {', '.join(source['authors'])}", source['source'],
                      f"{source['license']} {source['license_url']}",
                      'Changes: projection, color correction, quilting, deformation and weathering.', ''])
    (output/'ATTRIBUTION.txt').write_text('\n'.join(lines), encoding='utf-8')


def save_outputs(canvas, ppm, manifest, output, width_m=PANEL_M, height_m=PANEL_M):
    output.mkdir(parents=True, exist_ok=True)
    if not canvas['occupied'].all():
        raise ValueError('Unfilled pixels in cliff assembly')
    height = canvas['height']
    height -= height.min()
    # Keep the reference height signed and uncompressed. Runtime can use a
    # separately bounded displacement envelope without changing scan provenance.
    np.save(output / 'height_metres.npy', height)
    depth_range = max(float(height.max()), 1e-6)
    Image.fromarray(np.rint(height/depth_range*65535).astype(np.uint16)).save(output/'height.png')
    full_normal = normal_from_detail(unit(canvas['detail']), geometric_normal(height, ppm))
    save_color(output / 'basecolor.png', canvas['color'])
    Image.fromarray(np.rint((full_normal*.5+.5)*255).astype(np.uint8)).save(output / 'normal.png')
    for key in ('roughness', 'ao'):
        Image.fromarray(np.rint(canvas[key]*255).astype(np.uint8)).save(output / f'{key}.png')
    Image.fromarray(canvas['owner']).save(output / 'ownership.png')
    # Reference mesh at ~9 cm spacing; the shader receives only the remaining
    # detail relative to that mesh, not a second copy of the large-scale normal.
    nx = min(1024, height.shape[1])
    ny = max(2, round(nx * height_m / width_m))
    mesh_height = cv2.resize(height, (nx, ny), interpolation=cv2.INTER_AREA)
    np.save(output / 'mesh_height.npy', mesh_height)
    mesh_normal = cv2.resize(geometric_normal(mesh_height, nx/width_m), height.shape[::-1])
    residual = detail_from_normals(full_normal, unit(mesh_normal))
    Image.fromarray(np.rint(np.clip(residual*.5+.5, 0, 1)*255).astype(np.uint8)).save(output / 'mesh_detail_normal.png')
    metadata = dict(width_m=width_m, height_m=height_m, texture_size=list(height.shape[::-1]),
                    height_min_m=float(height.min()), height_max_m=float(height.max()),
                    height_png_metres_per_code=depth_range/65535,
                    sources=manifest, method='registered scan projection + varied minimum-error quilting',
                    normal_convention='OpenGL tangent +Y; V points up the face')
    (output / 'assembly.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    write_attribution(output, manifest)
    thumb = Image.open(output / 'basecolor.png')
    thumb.thumbnail((1600, 1600))
    thumb.save(output / 'albedo-overview.jpg', quality=94)


def make_canvas(width, height):
    canvas = {key: np.zeros((height, width, 3) if key in ('color', 'detail') else (height, width), np.float32)
              for key in CHANNELS}
    canvas.update(occupied=np.zeros((height, width), bool), owner=np.zeros((height, width), np.uint8))
    return canvas



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size', type=int, default=4096)
    options = parser.parse_args()
    size, rng = options.size, np.random.default_rng(4317)
    ppm = size / PANEL_M
    names = ('coastal_cliff_01', 'coastal_cliff_02', 'coastal_cliff_04', 'rock_face_01', 'rock_face_02')
    sources = [read_projection(name, ppm) for name in names]
    canvas = make_canvas(size, size)
    manifest = [s['meta'] for s in sources]
    placements = []
    y, row = 0, 0
    overlap_x, overlap_y = round(3*ppm), round(1.7*ppm)
    while y < size:
        band_height = round(min(s['height'].shape[0] for s in sources[:3])*rng.uniform(.52, .77))
        x, column = 0, 0
        previous = -1
        while x < size:
            index = int(rng.choice([i for i in range(3) if i != previous]))
            previous = index
            source = sources[index]
            band_height = min(band_height, source['height'].shape[0])
            patch_width = round(rng.uniform(9, 27)*ppm)
            patch, origin = patch_from(source, patch_width, band_height, rng, bool(rng.integers(2)),
                                       scale=rng.uniform(.85, 1.65), angle=rng.uniform(-14, 14))
            paste(canvas, patch, x, y, overlap_x if x else 0, overlap_y if y else 0, ppm, index+1)
            placements.append(dict(asset=names[index], x=x/ppm, y=y/ppm, transform=origin))
            x += patch['height'].shape[1] - overlap_x
            column += 1
        y += band_height - overlap_y
        row += 1
        print(f'Assembled cliff band {row}: {min(y/ppm,PANEL_M):.1f} / {PANEL_M} m', flush=True)
    # Sparse small outcrops interrupt joins without stretching their rock grain.
    for index in range(76):
        source = sources[3 + index % 2]
        scale = rng.uniform(.85, 1.8)
        patch, origin = patch_from(source, int(source['height'].shape[1]*scale*.8),
                                   int(source['height'].shape[0]*scale*.8), rng, bool(index%2),
                                   scale=scale, angle=rng.uniform(-25, 25))
        h, w = patch['height'].shape
        x, y = int(rng.integers(0, size-w)), int(rng.integers(0, size-h))
        paste(canvas, patch, x, y, 0, 0, ppm, 4+index%2, organic=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'placements.json').write_text(json.dumps(placements, indent=2))
    del sources, source, patch
    print('Shaping broad rock masses and simulating runoff', flush=True)
    weather = shape_and_erode(canvas, ppm)
    for key in ('erosion', 'sediment', 'drainage'):
        Image.fromarray(np.rint(weather.pop(key)*255).astype(np.uint8)).save(OUT/f'{key}.png')
    np.save(OUT/'macro_relief.npy', weather.pop('macro_relief'))
    (OUT/'weathering.json').write_text(json.dumps(weather, indent=2))
    save_outputs(canvas, ppm, manifest, OUT)
    print('Saved assembly', OUT, flush=True)


if __name__ == '__main__':
    main()
