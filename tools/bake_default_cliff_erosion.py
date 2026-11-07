"""Bake distant canyon erosion into the default cliff normal without extra samples.

Retain the scan normal's 8K resolution and micro-normal slot. Sample the erosion
in metres (274.32 x 182.88), rather than stretching it to the square scan repeat.
Cook the generated PNG in BeamNG, then publish its validated BC5 mip chain.
"""
import argparse
import json
import struct
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff'
SOURCE = 'scan_v2_flat.normal.png'
EROSION = 'canyon_far_v8.normal.png'
STEM = 'scan_eroded_v1'
WIDTH_SCALE, HEIGHT_SCALE = 3.0, 2.0
EROSION_GAIN = .45  # same effective slope as the near canyon composition
WRAP_BLEND = .20
ROW_BATCH = 256


def axis_samples(size, source_size, scale, start=0, end=None):
    """Pixel-centred periodic crop, feathered to its previous repeat at the end."""
    uv = (np.arange(start, size if end is None else end, dtype=np.float32) + .5) / size
    coord = (.5 * (1 - 1 / scale) + uv / scale) * source_size - .5
    weight = np.clip((uv - (1 - WRAP_BLEND)) / WRAP_BLEND, 0, 1)
    return coord, weight * weight * (3 - 2 * weight)


def bake():
    with Image.open(OUT / SOURCE) as image:
        base = np.asarray(image.convert('RGB'))
    with Image.open(OUT / EROSION) as image:
        normal = np.asarray(image.convert('RGB'), dtype=np.float32) / 127.5 - 1
    slopes = normal[..., :2] / np.maximum(normal[..., 2:3], .05)
    eh, ew = slopes.shape[:2]
    h, w = base.shape[:2]
    result = np.empty_like(base)
    x, wx = axis_samples(w, ew, WIDTH_SCALE)
    wx = wx[None, :, None]
    for start in range(0, h, ROW_BATCH):
        end = min(h, start + ROW_BATCH)
        y, wy = axis_samples(h, eh, HEIGHT_SCALE, start, end)
        xx, yy = np.meshgrid(x, y)
        def sample(dy):
            a = cv2.remap(slopes, xx, yy + dy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
            b = cv2.remap(slopes, xx - ew / WIDTH_SCALE, yy + dy,
                          cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
            return a * (1 - wx) + b * wx
        wy = wy[:, None, None]
        erosion = sample(0) * (1 - wy) + sample(-eh / HEIGHT_SCALE) * wy
        near = base[start:end].astype(np.float32) / 127.5 - 1
        gradient = near[..., :2] / np.maximum(near[..., 2:3], .05) + EROSION_GAIN * erosion
        combined = np.concatenate((gradient, np.ones((*gradient.shape[:2], 1), np.float32)), axis=-1)
        combined /= np.linalg.norm(combined, axis=-1, keepdims=True)
        result[start:end] = np.rint(np.clip(combined * .5 + .5, 0, 1) * 255).astype('uint8')
    Image.fromarray(result).save(OUT / (STEM + '.normal.png'))
    (OUT / (STEM + '_bake.json')).write_text(json.dumps(dict(
        source_normal=SOURCE, erosion_normal=EROSION, size=[w, h],
        erosion_repeat_metres=[91.44 * WIDTH_SCALE, 91.44 * HEIGHT_SCALE],
        erosion_slope_gain=EROSION_GAIN, wrap_blend=WRAP_BLEND,
        normal_orientation='OpenGL +Y', method='Periodic surface-gradient addition'), indent=2) + '\n')
    print('Baked', STEM, w, h)


def publish(cooked):
    data = Path(cooked).read_bytes()
    with Image.open(OUT / SOURCE) as image:
        w, h = image.size
    mips = max(w, h).bit_length()
    expected = 148 + sum(max(1, (w >> i) // 4) * max(1, (h >> i) // 4) * 16 for i in range(mips))
    if (len(data) != expected or data[:4] != b'DDS ' or data[84:88] != b'DX10'
            or struct.unpack_from('<I', data, 128)[0] != 83
            or struct.unpack_from('<II', data, 12) != (h, w)
            or struct.unpack_from('<I', data, 28)[0] != mips):
        raise ValueError('Expected full-resolution BC5_UNORM DDS with all mip levels')
    name = STEM + '_ready.normal.dds'
    (OUT / name).write_bytes(data)
    path = OUT.parent / 'cliff.materials.json'
    materials = json.loads(path.read_text())
    for key in ('bne_cliff_strata', 'bne_cliff_strata_terrain'):
        stage = materials[key]['Stages'][0]
        stage['normalMap'] = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/' + name
        stage['normalMapUseUV'] = 0
    path.write_text(json.dumps(materials, indent=2) + '\n')
    print('Published', name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', type=Path)
    args = parser.parse_args()
    publish(args.publish) if args.publish else bake()
