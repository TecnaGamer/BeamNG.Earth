"""Material-led terrain base colour, with a restrained trace of the aerial photo.

The terrain shader adds its detail/macro grain to the base map; simply increasing
those strengths leaves the full aerial image underneath. Blend the base towards
the generated surface's colour in linear light instead. Original imagery remains
available for lawn/snow classification. No new map requests or geometry changes.
"""
import os
import struct

import numpy as np
from PIL import Image

import surface

IMAGERY_SHARE = .10
MATERIAL_COLOURS = {
    'bngEarthGroundAsphalt': (86, 85, 81),
    'bngEarthGroundGrass': (72, 86, 57),
    'bngEarthGroundDusty': (145, 128, 100),
    'bngEarthGroundLoose': (145, 140, 127),
    'bngEarthGroundRock': (130, 132, 129),
    'bngEarthGroundSnow': (224, 230, 232),
    'bngEarthGroundMud': (100, 89, 72),
    'bngEarthGroundCobble': (140, 138, 132),
    'bngEarthGroundLawn': (68, 84, 52),
    'beamngEarthDirt': (135, 119, 90),
}


def _linear(rgb):
    x = np.asarray(rgb, np.float64) / 255.
    return np.where(x <= .04045, x / 12.92, ((x + .055) / 1.055) ** 2.4)


def _srgb(rgb):
    x = np.clip(rgb, 0., 1.)
    x = np.where(x <= .0031308, 12.92*x, 1.055*x**(1./2.4)-.055)
    return np.clip(np.rint(x*255.), 0, 255).astype(np.uint8)


_COLOURS = np.array([MATERIAL_COLOURS[name] for name, _ in surface.MATERIALS], np.uint8)
# A small lookup table avoids per-pixel colour conversions for every 1024² block.
_BLEND = _srgb((1.-IMAGERY_SHARE)*_linear(_COLOURS)[:, :, None] +
               IMAGERY_SHARE*_linear(np.arange(256))[None, None, :])


def material_grid(out_dir, ter_name):
    """Read the existing .ter's final surface IDs; return global IDs with row 0 north."""
    with open(os.path.join(out_dir, ter_name), 'rb') as f:
        header = f.read(5)
        n = struct.unpack_from('<I', header, 1)[0]
        if not 2 <= n <= 4096:
            raise ValueError('invalid terrain sample size')
        f.seek(5 + 2*n*n)
        layers = f.read(n*n)
        if len(layers) != n*n:
            raise ValueError('incomplete terrain layer map')
        count = struct.unpack('<I', f.read(4))[0]
        if not 1 <= count <= 256:
            raise ValueError('invalid terrain material count')
        order = {name: i for i, (name, _) in enumerate(surface.MATERIALS)}
        mapping = np.full(256, surface.DIRT, np.uint8)
        for i in range(count):
            length = f.read(1)
            if not length:
                raise ValueError('incomplete terrain material names')
            name = f.read(length[0]).decode('utf-8')
            mapping[i] = order.get(name, surface.DIRT)
    return mapping[np.frombuffer(layers, np.uint8).reshape(n, n)[::-1]]


def blend(rgb, out_dir, ter_name=None):
    """90% generated surface colour + 10% aerial colour; never mutate the source photo."""
    rgb = np.asarray(rgb, np.uint8)
    h, w = rgb.shape[:2]
    ids = material_grid(out_dir, ter_name) if ter_name else np.full((1, 1), surface.DIRT, np.uint8)
    if ids.shape != (h, w):
        ids = np.asarray(Image.fromarray(ids).resize((w, h), Image.Resampling.NEAREST))
    return _BLEND[ids[..., None], np.arange(3), rgb]
