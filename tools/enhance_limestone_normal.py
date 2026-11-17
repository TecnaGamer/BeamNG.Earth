"""Bake fine limestone relief into an authored normal map (offline, never at runtime).

Pass an unmodified source and a different output path; repeated application would
amplify the previous pass. Broad joint and ledge slopes retain their alignment.
"""
import argparse
from pathlib import Path
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

FINE_RADIUS = 3.0
FINE_GAIN = 0.65
GRAIN_STRENGTH = 0.20
SEED = 1847


def enhance(rgb):
    normal = np.asarray(rgb, np.float32) / 127.5 - 1
    slope = normal[..., :2] / np.maximum(normal[..., 2:3], .25)
    # Strengthen small authored chips without scaling the broad rock relief.
    broad = gaussian_filter(slope, (FINE_RADIUS, FINE_RADIUS, 0), mode='wrap')
    fine = slope - broad
    rng = np.random.default_rng(SEED)
    noise = rng.standard_normal(slope.shape[:2], dtype=np.float32)
    height = gaussian_filter(noise, .8, mode='wrap') - gaussian_filter(noise, 2.4, mode='wrap')
    variation = gaussian_filter(noise, 20, mode='wrap')
    variation /= max(float(variation.std()), 1e-6)
    strength = GRAIN_STRENGTH * (1 + .35 * np.tanh(variation))
    dy = (np.roll(height, -1, 0) - np.roll(height, 1, 0)) * .5
    dx = (np.roll(height, -1, 1) - np.roll(height, 1, 1)) * .5
    grain = np.stack((dx, dy), axis=-1)
    grain /= max(float(grain.std()), 1e-6)
    slope += FINE_GAIN * fine + grain * strength[..., None]
    result = np.concatenate((slope, np.ones((*slope.shape[:2], 1), np.float32)), axis=-1)
    result /= np.linalg.norm(result, axis=-1, keepdims=True)
    return np.clip(np.rint((result + 1) * 127.5), 0, 255).astype(np.uint8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error('source and output must differ; retain the original for reproducibility')
    with Image.open(args.source) as source:
        result = enhance(source.convert('RGB'))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(result).save(args.output, lossless=True, exact=True)


if __name__ == '__main__':
    main()
