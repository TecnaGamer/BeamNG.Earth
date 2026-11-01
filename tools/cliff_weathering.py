"""Deterministic metre-scale shaping and vertical runoff for scan assemblies.

This is an artistic shallow runoff model for an upright cliff, not a geological
time simulation. Gravity sends water down the image; recesses route it sideways.
Depth is measured out from the face, so erosion subtracts material. All sampling
coordinates are shared by the PBR maps, and normals are rebuilt after shaping.
"""
import cv2
import numpy as np


def blur(field, sigma):
    return cv2.GaussianBlur(field, (0, 0), max(.01, sigma), borderType=cv2.BORDER_REFLECT_101)


def noise(shape, cell_pixels, rng):
    """Smooth, non-periodic value noise with a specified physical wavelength."""
    ny, nx = (max(3, int(np.ceil(n / cell_pixels)) + 2) for n in shape)
    values = rng.normal(0, 1, (ny, nx)).astype(np.float32)
    field = cv2.resize(values, shape[::-1], interpolation=cv2.INTER_CUBIC)
    return (field-field.mean()) / max(float(field.std()), .001)


def runoff(height, cell_m, rain, iterations=10):
    """Route rainfall to lower rows, erode channels, and deposit trapped sediment.

    Each row conserves incoming water before a small infiltration loss. Side
    boundaries are closed; the bottom drains. Only a fraction of loose material
    settles in sheltered depressions. Erosion has a fixed metre-scale budget.
    """
    surface = height.copy()
    total_cut = np.zeros_like(height)
    total_deposit = np.zeros_like(height)
    last_water = np.zeros_like(height)
    for _ in range(iterations):
        water = np.maximum(rain, 0).copy()
        sediment = np.zeros_like(height)
        for y in range(height.shape[0]-1):
            lower = np.pad(surface[y+1], 1, mode='edge')
            candidates = np.stack((lower[:-2], lower[1:-1], lower[2:]))
            # Prefer recessed rock, with a gravity bias toward straight down.
            preference = np.exp(np.clip((surface[y][None, :]-candidates) / .28, -4, 4))
            preference *= np.array((.22, 1, .22), np.float32)[:, None]
            preference[0, 0] = preference[2, -1] = 0
            weights = preference / preference.sum(axis=0, keepdims=True)
            rate = np.clip(np.log1p(water[y]) / 5, 0, 1.5)
            cut = np.minimum(rate * .32 / max(iterations, 1), .65-total_cut[y])
            total_cut[y] += cut
            surface[y] -= cut
            sediment[y] += cut
            pocket = np.clip((np.mean(candidates, axis=0)-surface[y]) / .5, 0, 1)
            deposit = np.minimum(sediment[y] * pocket * .12, .12-total_deposit[y])
            surface[y] += deposit
            total_deposit[y] += deposit
            sediment[y] -= deposit
            for k, dx in enumerate((-1, 0, 1)):
                start, stop = max(0, -dx), min(height.shape[1], height.shape[1]-dx)
                target = slice(start+dx, stop+dx)
                part = slice(start, stop)
                water[y+1, target] += water[y, part] * weights[k, part] * .985
                sediment[y+1, target] += sediment[y, part] * weights[k, part]
        # Mild rounding of exposed crests; retain the scan's small sharp chips.
        convex = np.maximum(surface-blur(surface, .32/cell_m), 0)
        weather = np.minimum(convex*.012, .65-total_cut)
        surface -= weather
        total_cut += weather
        last_water = water
    return surface, total_cut, total_deposit, last_water


def weather_maps(size, span_m, seed=6081):
    """Create broad buttresses, warped bedding coordinates, and drainage masks."""
    rng = np.random.default_rng(seed)
    shape = (size, size)
    ppm = size/span_m
    # Anisotropic rock masses: broad buttresses separated by vertical recesses.
    broad = noise(shape, 18*ppm, rng)
    medium = noise(shape, 6*ppm, rng)
    ridges = 1-np.abs(noise((max(8, size//3), size), 9*ppm, rng))
    ridges = cv2.resize(ridges, (size, size), interpolation=cv2.INTER_CUBIC)
    relief = broad*3.5 + medium*.6 + ridges
    dx = noise(shape, 19*ppm, rng)*2.0 + noise(shape, 7*ppm, rng)*.25
    dy = noise(shape, 24*ppm, rng)*2.8 + noise(shape, 8*ppm, rng)*.35
    return relief, dx, dy


def shape_and_erode(canvas, ppm, seed=6081):
    """Warp every channel together, then weather the combined continuous surface."""
    size = canvas['height'].shape[0]
    sim_size = min(640, size)
    span_m = size/ppm
    relief, dx, dy = weather_maps(sim_size, span_m, seed)
    # Avoid folding the scan projection: bound the displacement Jacobian.
    for field in (dx, dy):
        gy, gx = np.gradient(field, span_m/sim_size)
        field *= min(1, .24/max(float(np.max(np.abs(gx))), float(np.max(np.abs(gy))), .001))
    dx = cv2.resize(dx, (size, size))*ppm
    dy = cv2.resize(dy, (size, size))*ppm
    yy, xx = np.mgrid[:size, :size].astype(np.float32)
    mapx, mapy = xx+dx, yy+dy
    for key in ('color', 'height', 'detail', 'roughness', 'ao', 'owner'):
        interpolation = cv2.INTER_NEAREST if key == 'owner' else cv2.INTER_LINEAR
        canvas[key] = cv2.remap(canvas[key], mapx, mapy, interpolation, borderMode=cv2.BORDER_REFLECT_101)
    # Transform tangent normal covectors with the same sampling Jacobian.
    dx_y, dx_x = np.gradient(dx)
    dy_y, dy_x = np.gradient(dy)
    nx, ny = canvas['detail'][..., 0].copy(), canvas['detail'][..., 1].copy()
    canvas['detail'][..., 0] = (1+dx_x)*nx-dy_x*ny
    canvas['detail'][..., 1] = -dx_y*nx+(1+dy_y)*ny
    del mapx, mapy, xx, yy, dx, dy, dx_x, dx_y, dy_x, dy_y, nx, ny
    small = cv2.resize(canvas['height'], (sim_size, sim_size), interpolation=cv2.INTER_AREA)
    # Remove accumulated quilt offsets, not the scan's local relief.
    trend = blur(small, 10*sim_size/span_m)
    canvas['height'] -= cv2.resize(trend, (size, size))
    # A wall projection cannot represent the underside of a scanned overhang.
    # Compress its local depth rather than turning the visibility jump into a
    # metre-deep stretched sheet. Broad formations remain independently scaled.
    scan_relief_gain = .45
    canvas['height'] *= scan_relief_gain
    relief = np.tanh(relief/4.5)*4.5
    local = (small-trend)*scan_relief_gain
    base = local+relief
    rng = np.random.default_rng(seed+1)
    rain = np.clip(1 + noise(base.shape, 8*sim_size/span_m, rng)*.3, .2, 1.8)
    eroded, cut, deposit, water = runoff(base, span_m/sim_size, rain)
    # Add only simulated macro changes, preserving high frequency scanned relief.
    delta = eroded-local
    canvas['height'] += cv2.resize(delta, (size, size))
    canvas['height'] = blur(canvas['height'], ppm*.085)
    wear = cv2.resize(np.clip(cut/.2, 0, 1), (size, size))
    silt = cv2.resize(np.clip(deposit/.1, 0, 1), (size, size))
    drainage = cv2.resize(np.clip(np.log1p(water)/7, 0, 1), (size, size))
    # Weathering affects material at the same positions as the shape changes.
    canvas['color'] *= (1 + wear*.035 - drainage*.045)[..., None]
    canvas['color'] = canvas['color']*(1-silt[..., None]*.12) + np.array((.32,.29,.25), np.float32)*silt[..., None]*.12
    canvas['roughness'] = np.clip(canvas['roughness']+wear*.03+silt*.04, .5, .98)
    cavity = cv2.resize(np.maximum(blur(eroded, .6*sim_size/span_m)-eroded, 0), (size, size))
    canvas['ao'] = np.minimum(canvas['ao'], np.clip(np.exp(-cavity*.3), .65, 1))
    canvas['height'] -= canvas['height'].min()
    return dict(erosion=wear, sediment=silt, drainage=drainage,
                macro_relief=cv2.resize(relief, (size, size)),
                max_erosion_m=float(cut.max()), max_deposit_m=float(deposit.max()),
                scan_relief_gain=scan_relief_gain, runoff_grid_size=sim_size, seed=seed)
