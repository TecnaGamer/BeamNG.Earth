"""Test a real NIBIO SR16 WMS tile before driving; no BeamNG startup needed."""
import argparse
import math
from pathlib import Path
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))
import norway_sr16
import norway_outline


def cell_of(lat, lon, zoom=14):
    n = 1 << zoom
    x = math.floor((lon + 180.0) / 360.0 * n)
    rad = math.radians(lat)
    y = math.floor((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n)
    return x, y


def bounds_of(x, y, zoom=14):
    n = 1 << zoom

    def latitude(row):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))

    return 360.0 * x / n - 180.0, latitude(y + 1), 360.0 * (x + 1) / n - 180.0, latitude(y)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lat", type=float, default=62.89, help="test latitude (default: the E39 test area)")
    p.add_argument("--lon", type=float, default=7.67, help="test longitude (default: the E39 test area)")
    p.add_argument("--cache-dir", type=Path, default=Path(tempfile.gettempdir()) / "beamng-earth-sr16-probe")
    p.add_argument("--preview", type=Path, help="optional path to save the actual NIBIO colour map PNG")
    args = p.parse_args()
    x, y = cell_of(args.lat, args.lon)
    bounds = bounds_of(x, y)
    print(f"NIBIO SR16 probe: {args.lat:.5f}, {args.lon:.5f} (z14 cell {x}/{y})")
    print("Inside Norway boundary:", bool(norway_outline.contains(args.lat,args.lon)))
    p16 = norway_sr16.SR16(str(args.cache_dir))
    tile = p16.cell(x, y, bounds)
    if tile is None:
        print("No SR16 tile. Reason:", p16.last_error or "outside Norway / BNE_SR16 disabled")
        print("The vegetation generator will preserve the original canopy-based trees at this location.")
        return 1
    values, counts = np.unique(tile.classes, return_counts=True)
    print("Source:", p16.last_source)
    for k, n in zip(values, counts):
        print(f"  {norway_sr16.NAMES[int(k)]:<14}: {n:>6} pixels ({n / tile.classes.size * 100:5.1f}%)")
    middle = tile.sample(args.lon, args.lat)
    print("Class at coordinate:", norway_sr16.NAMES[int(middle)])
    predicted={norway_sr16.SPRUCE:"norway_spruce",norway_sr16.PINE:"norway_pine",
               norway_sr16.BROADLEAF:"norway_birch",norway_sr16.CONIFER_MIX:"spruce/pine mixture",
               norway_sr16.MIXED:"mixed foliage"}
    print("Expected Norway stock-tree species group:",predicted.get(int(middle),"CHM fallback"))
    raw = p16.dir / str(x) / f"{y}.png"
    if raw.is_file():
        with Image.open(raw) as im:
            rgba = np.asarray(im.convert("RGBA"), np.uint8)
        opaque = rgba[rgba[:, :, 3] > 200, :3]
        if len(opaque):
            colors, frequencies = np.unique(opaque, axis=0, return_counts=True)
            common = np.argsort(-frequencies)[:8]
            print("Most common WMS RGB colours (useful if the published palette changes):")
            for i in common:
                print(f"  RGB{tuple(int(c) for c in colors[i])}  {frequencies[i]} pixels")
        if args.preview:
            args.preview.parent.mkdir(parents=True, exist_ok=True)
            args.preview.write_bytes(raw.read_bytes())
            print("Raw NIBIO map preview:", args.preview)
    mapped = int(np.count_nonzero((tile.classes >= norway_sr16.SPRUCE)
                                   & (tile.classes <= norway_sr16.MIXED)))
    if mapped == 0:
        print("WARNING: no classified forest pixels. Try a clearly forested area, or share this output for palette review.")
    elif mapped < 100:
        print("Low forest coverage: this tile may be rocky, urban, or sparsely forested.")
    else:
        print("SR16 forest classes were decoded. This is a data probe, not proof the game has loaded v0.6.")
        print("Use apply.py --check to verify the installed server and Lua files; restart both processes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
