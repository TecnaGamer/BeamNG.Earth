"""Add the approved distant erosion to the near canyon's diffuse-aligned normal.

Bake offline into the existing normal slot: no extra shader samples or geometry.
The erosion is enlarged horizontally to match the far cliff width multiplier.
Only erosion is resampled; diffuse-aligned joints and microdetail keep their scale.
Cook the PNG in BeamNG before --publish accepts the BC5 DDS for runtime use.
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
BASE_NORMAL = 'canyon_v5_faceted_near.normal.png'
EROSION_NORMAL = 'canyon_far_v8.normal.png'
OUTPUT_STEM = 'canyon_v5_eroded_near_v2'
EROSION_WIDTH_SCALE = 3.0  # match FAR_CANYON_NORMAL_WIDTH_SCALE without scaling rock joints
EROSION_WRAP_BLEND = .20  # close the enlarged crop smoothly inside the shared UV0 tile
# The near material applies 0.30 strength to the combined map. This gives the
# added erosion about 0.45 effective slope, quieter than the distant 0.70.
EROSION_SLOPE_GAIN = 1.5
MATERIALS = ('bne_cliff_canyon_sat', 'bne_cliff_canyon_sat_terrain')


def combine_normals(base, erosion, gain=EROSION_SLOPE_GAIN, width_scale=EROSION_WIDTH_SCALE):
    """Add surface gradients, preserving a flat input and periodic tile edges.

    Interpolate gradients rather than encoded RGB, then reconstruct unit normals.
    Both sources use the same OpenGL tangent convention. Height-gradient addition
    preserves the authored near joints while introducing the distant erosion.
    """
    if width_scale < 1:
        raise ValueError("Erosion width scale must be at least one")
    def slopes(normal):
        return normal[..., :2] / np.maximum(normal[..., 2:3], .05)
    broad = slopes(erosion).astype(np.float32)
    h, w = base.shape[:2]
    eh, ew = broad.shape[:2]
    # Pixel-centred periodic sampling avoids clamped borders in ordinary resize.
    u = (np.arange(w, dtype=np.float32) + .5) / w
    x = (.5 * (1 - 1 / width_scale) + u / width_scale) * ew - .5
    y = (np.arange(h, dtype=np.float32) + .5) * eh / h - .5
    xx, yy = np.meshgrid(x, y)
    enlarged = cv2.remap(broad, xx, yy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
    # A fractional source repeat would otherwise create a seam at every UV0 wrap.
    # Crossfade to the preceding crop at the closing edge, with zero blend slope
    # at both endpoints. This leaves the inner 80% at the exact far width scale.
    if width_scale != 1:
        previous = cv2.remap(broad, xx - ew / width_scale, yy,
                             cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
        weight = np.clip((u - (1 - EROSION_WRAP_BLEND)) / EROSION_WRAP_BLEND, 0, 1)
        weight = (weight * weight * (3 - 2 * weight))[None, :, None]
        enlarged = enlarged * (1 - weight) + previous * weight
    broad = enlarged
    combined = np.concatenate((slopes(base) + gain * broad,
                               np.ones((h, w, 1), dtype=np.float32)), axis=-1)
    return combined / np.linalg.norm(combined, axis=-1, keepdims=True)


def bake():
    def read(name):
        with Image.open(OUT / name) as image:
            return np.asarray(image.convert('RGB'), dtype=np.float32) / 127.5 - 1
    base = read(BASE_NORMAL)
    result = combine_normals(base, read(EROSION_NORMAL))
    Image.fromarray(np.rint(np.clip(result * .5 + .5, 0, 1) * 255).astype('uint8')).save(
        OUT / (OUTPUT_STEM + '.normal.png'))
    (OUT / (OUTPUT_STEM + '_bake.json')).write_text(json.dumps(dict(
        source_normal=BASE_NORMAL, erosion_normal=EROSION_NORMAL,
        size=list(base.shape[:2]), erosion_slope_gain=EROSION_SLOPE_GAIN,
        uv=0, repeat_metres=[91.44, 182.88], erosion_width_scale=EROSION_WIDTH_SCALE,
        erosion_wrap_blend=EROSION_WRAP_BLEND, normal_orientation='OpenGL +Y',
        method='Periodic height-gradient addition; existing material normal strength and microdetail retained'), indent=2) + '\n')
    print('Baked', OUTPUT_STEM, base.shape[:2])


def publish(cooked):
    data = Path(cooked).read_bytes()
    if (data[:4] != b'DDS ' or data[84:88] != b'DX10'
            or struct.unpack_from('<I', data, 128)[0] != 83
            or struct.unpack_from('<II', data, 12) != (4096, 4096)
            or struct.unpack_from('<I', data, 28)[0] != 13
            or len(data) != 22369796):
        raise ValueError('Expected a fully cooked 4096-square BC5_UNORM DDS with 13 mip levels')
    name = OUTPUT_STEM + '_ready.normal.dds'
    (OUT / name).write_bytes(data)
    path = OUT.parent / 'cliff.materials.json'
    mats = json.loads(path.read_text())
    for key in MATERIALS:
        stage = mats[key]['Stages'][0]
        stage['normalMap'] = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/' + name
        stage['normalMapUseUV'] = 0
    path.write_text(json.dumps(mats, indent=2) + '\n')
    print('Published cooked near erosion normal:', name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', type=Path, help='Publish a fully cooked BC5 DDS after baking')
    args = parser.parse_args()
    if args.publish:
        publish(args.publish)
    else:
        bake()
