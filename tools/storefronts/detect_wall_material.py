"""Offline running-bond detector for a manually identified, roughly front-on wall crop."""

import argparse
import hashlib
import itertools
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


def detect(image):
    patch = image.convert("RGB").copy()
    patch.thumbnail((768, 768))
    pixels = np.asarray(patch, dtype=float)
    grey = pixels @ np.array([0.299, 0.587, 0.114])
    unknown = {"material": "unknown"}
    if min(grey.shape) < 48:
        return unknown
    # Bed joints run across the crop. Keep one peak per seam, including thick mortar.
    strength = np.abs(np.diff(grey, axis=0)).mean(axis=1)
    threshold = max(
        4, float(np.median(strength)) * 2.5, float(np.percentile(strength, 97)) * 0.38
    )
    candidates = np.flatnonzero(strength >= threshold)
    if not len(candidates):
        return unknown
    groups = np.split(candidates, np.flatnonzero(np.diff(candidates) > 4) + 1)
    rows = np.array([int(np.average(g, weights=strength[g]).round()) for g in groups])
    if len(rows) < 5:
        return unknown
    gaps = np.diff(rows)
    period = float(np.median(gaps))
    if period < 7 or np.mean(abs(gaps - period) <= max(2, period * 0.20)) < 0.8:
        return unknown
    # Real running bond offsets head joints in adjacent courses, then repeats two courses later.
    profiles = []
    for a, b in itertools.pairwise(rows):
        margin = max(2, round((b - a) * 0.22))
        band = grey[a + margin : b - margin]
        if len(band) < 2:
            return unknown
        edge = np.abs(np.diff(band, axis=1)).mean(axis=0)
        if np.percentile(edge, 99) < max(3, np.median(edge) * 2):
            return unknown
        edge = np.convolve(edge, [0.25, 0.5, 0.25], mode="same")
        edge -= edge.mean()
        profiles.append(edge / max(np.linalg.norm(edge), 1e-8))
    adjacent = np.median([a @ b for a, b in itertools.pairwise(profiles)])
    repeat = np.median([a @ b for a, b in zip(profiles, profiles[2:])])
    if repeat < 0.40 or repeat - adjacent < 0.28:
        return unknown
    rgb = np.median(pixels.reshape(-1, 3), axis=0).round().astype(int)
    result = {
        "material": "brick",
        "courses": len(profiles),
        "joint_repeat": round(float(repeat), 3),
    }
    # A monochrome reference provides masonry evidence but no paint or clay hue.
    if np.any(pixels[:, :, 0] != pixels[:, :, 1]) or np.any(
        pixels[:, :, 1] != pixels[:, :, 2]
    ):
        result["color"] = "#{:02x}{:02x}{:02x}".format(*tuple(rgb))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("photo", type=Path)
    parser.add_argument(
        "--crop",
        nargs=4,
        type=int,
        metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
        required=True,
    )
    parser.add_argument(
        "--osm", type=Path, help="Local OSM XML with footprints around the camera"
    )
    parser.add_argument("--camera", nargs=2, type=float, metavar=("LON", "LAT"))
    parser.add_argument(
        "--bearing",
        type=float,
        help="Bearing from camera to crop centre, clockwise from true north",
    )
    parser.add_argument("--way", help="Matched standalone building OSM way ID")
    parser.add_argument(
        "--wall", nargs=4, type=float, metavar=("LON1", "LAT1", "LON2", "LAT2")
    )
    parser.add_argument(
        "--source", required=True, help="Photo ID, URL or local capture identifier"
    )
    parser.add_argument("--captured", help="Capture date if known")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    auto = any(value is not None for value in (args.osm, args.camera, args.bearing))
    if auto:
        if (
            any(value is None for value in (args.osm, args.camera, args.bearing))
            or args.way
            or args.wall
        ):
            parser.error(
                "Use --osm, --camera and --bearing together, or use --way and --wall"
            )
        from photo_match import match_wall

        try:
            way, wall = match_wall(args.osm, args.camera, args.bearing)
        except ValueError as error:
            parser.error(str(error))
    elif args.way and args.wall:
        way, wall = args.way, [args.wall[:2], args.wall[2:]]
    else:
        parser.error("Specify the camera sight line or explicit wall endpoints")
    with Image.open(args.photo) as original:
        photo = ImageOps.exif_transpose(original)
        l, t, r, b = args.crop
        if not (0 <= l < r <= photo.width and 0 <= t < b <= photo.height):
            parser.error("Crop must lie inside the oriented photo")
        observation = detect(photo.crop((l, t, r, b)))
    if auto:
        observation.update(camera=args.camera, bearing=args.bearing)
    observation.update(
        way=way,
        wall=wall,
        source=args.source,
        captured=args.captured,
        crop=args.crop,
        photo_sha256=hashlib.sha256(args.photo.read_bytes()).hexdigest(),
    )
    args.output.write_text(json.dumps(observation, indent=2), encoding="utf-8")
    print(observation["material"])


if __name__ == "__main__":
    main()
