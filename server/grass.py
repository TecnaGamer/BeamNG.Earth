"""Pure, image-scale grass palette selection, independent of vegetation quantity.

Only already-vegetated surface samples may use these palettes. Brown colour is not
evidence that a surface supports plants: land cover, slope and the existing surface
exclusions remain the caller's decision. No image fetches or object work live here.
"""
import numpy as np

DRY, LIGHT, LUSH = range(3)
EXCLUDED = np.uint8(255)
PALETTES = ("dry", "light", "lush")
SMOOTH_M = 12.0           # radius of the local colour neighbourhood, in ground metres


def _ramp(x, lo, hi):
    t = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def color_weights(rgb, fallback=(0.0, 0.0, 1.0)):
    """Dry / light-green / lush probabilities at RGB colours (..., 3), in 0..255.

    Green relative to red distinguishes gold/tan from green; green relative to blue
    and saturation reject neutral grey evidence. Brightness separates pale grass
    from dark vegetation. Near-black shadows, near-white imagery and low-chroma
    colours gradually take ``fallback`` (one prior or an array of priors). The
    caller should supply locally averaged colours, rather than classify texels.
    """
    c = np.clip(np.asarray(rgb, np.float32)[..., :3], 0.0, 255.0)
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    high, low = c.max(-1), c.min(-1)
    saturation = (high - low) / np.maximum(high, 1.0)
    green_red = (g - r) / np.maximum(g + r, 1.0)
    green_blue = (g - b) / np.maximum(g + b, 1.0)
    green = _ramp(green_red, -0.025, 0.075) * _ramp(green_blue, 0.0, 0.12)
    dark = 1.0 - _ramp(high, 80.0, 145.0)
    rich = _ramp(green_red, 0.08, 0.23)
    lush = green * dark * (0.7 + 0.3 * rich)
    observed = np.stack((1.0 - green, green - lush, lush), axis=-1)
    confidence = (_ramp(saturation, 0.055, 0.18) * _ramp(high, 16.0, 42.0)
                  * (1.0 - _ramp(low, 205.0, 245.0)))
    prior = np.maximum(np.asarray(fallback, np.float32), 0.0)
    prior = np.broadcast_to(prior, observed.shape)
    total = prior.sum(-1, keepdims=True)
    prior = np.where(total > 0, prior / np.maximum(total, 1e-8),
                     np.array((0.0, 0.0, 1.0), np.float32))
    out = observed * confidence[..., None] + prior * (1.0 - confidence[..., None])
    return out.astype(np.float32)


def smooth_colors(rgb, radius_px):
    """O(pixels) box mean, (..., RGB) with two spatial dimensions, float32 output.

    Supply a halo at least ``radius_px`` wide and crop it *after* this operation
    when comparing neighbouring tiles. Edge replication is only for the image's
    outermost boundary; it cannot supply missing geographic neighbours.
    """
    a = np.asarray(rgb, np.float32)[..., :3]
    if a.ndim != 3:
        raise ValueError("smooth_colors expects an H x W x 3 image")
    radius = max(0, int(radius_px))
    if radius == 0:
        return a.copy()
    width = 2 * radius + 1
    # Separable running sums use only an image-sized temporary, rather than a
    # sliding-window view or a pass for every neighbour. Float64 sums make the
    # same halo independent of where a tile's prefix sum starts.
    out = a
    for axis in (0, 1):
        pads = [(0, 0)] * out.ndim
        pads[axis] = (radius, radius)
        padded = np.pad(out, pads, mode="edge")
        zero_shape = list(padded.shape)
        zero_shape[axis] = 1
        sums = np.concatenate((np.zeros(zero_shape, np.float64),
                               np.cumsum(padded, axis=axis, dtype=np.float64)), axis=axis)
        first = [slice(None)] * out.ndim
        last = [slice(None)] * out.ndim
        first[axis], last[axis] = slice(None, -width), slice(width, None)
        out = (sums[tuple(last)] - sums[tuple(first)]) / width
    return out.astype(np.float32)


def world_random(lat, lon):
    """Uniform [0,1) keyed by geographic position, independent of array/tile order.

    Seven decimal places (~1 cm) absorb floating point frame conversion noise
    while keeping neighbouring 2 m terrain samples independent. No Python hash
    seed or mutable RNG state participates.
    """
    lat, lon = np.broadcast_arrays(np.asarray(lat, np.float64), np.asarray(lon, np.float64))
    iy = np.rint(lat * 1e7).astype(np.int64).astype(np.uint64)
    ix = np.rint(lon * 1e7).astype(np.int64).astype(np.uint64)
    with np.errstate(over="ignore"):
        value = ix * np.uint64(0x9E3779B185EBCA87) ^ iy * np.uint64(0xC2B2AE3D27D4EB4F)
        value ^= np.uint64(0x6752415353)
        value ^= value >> np.uint64(30)
        value *= np.uint64(0xBF58476D1CE4E5B9)
        value ^= value >> np.uint64(27)
        value *= np.uint64(0x94D049BB133111EB)
        value ^= value >> np.uint64(31)
    return (value >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def choose(weights, lat, lon, eligible=None):
    """Palette index per world sample from blended weights; 255 where excluded.

    This chooses an asset category, never thins samples or changes density. A
    gradual weight change gives a gradual change in the asset distribution,
    while reloading or batching the same world samples preserves their choices.
    """
    w = np.maximum(np.asarray(weights, np.float32), 0.0)
    if w.shape[-1] != 3:
        raise ValueError("grass weights need dry, light and lush components")
    total = w.sum(-1)
    sample = world_random(lat, lon) * total
    selected = np.where(sample < w[..., DRY], DRY,
                        np.where(sample < w[..., DRY] + w[..., LIGHT], LIGHT, LUSH)).astype(np.uint8)
    if eligible is not None:
        selected = np.where(np.asarray(eligible, bool), selected, EXCLUDED).astype(np.uint8)
    return selected
