#!/usr/bin/env python3
"""Inspect the generator's road inputs and prepare repeatable real-world comparison views.

    python tools/road_audit.py --bbox 48.975,12.055,49.055,12.465 --class motorway
        --source-record r2925465 --heading 90 --out docs/research/a3-regensburg

Uses cached data by default; --fetch explicitly permits normal map-data downloads. Writes
audit.json (coverage, source versions, geometry hashes, mixed bridge/ground segments),
roads.geojson and set.json for tools/benchmark.py. Links open Street View, Mapillary and
OpenStreetMap at each view, without downloading images or requiring imagery credentials.
The mixed-level flag identifies roads affected by the former whole-feature building-clearance
bug; it is a review priority, not proof of an obstruction. No terrain or meshes are rendered.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from urllib.parse import urlencode

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import mapcells  # noqa: E402
import mapdata  # noqa: E402
import netfetch  # noqa: E402
import roads  # noqa: E402


class AuditMapData(mapdata.MapData):
    """Offline audit reads cached arrays without the pipeline's normal repair-and-store behavior."""

    def layer_arrays(self, key, layer, kind="demand"):
        if not self.fetcher.offline:
            return super().layer_arrays(key, layer, kind)
        arrays = self._load(self.path(key, layer))
        if arrays is None:
            raise FileNotFoundError("missing or unreadable cached road layer")
        return arrays, True


def bbox_arg(value):
    try:
        south, west, north, east = (float(v) for v in value.split(","))
        if not (-85 <= south < north <= 85 and -180 <= west < east <= 180):
            raise ValueError
        return south, west, north, east
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("bbox must be south,west,north,east within -85..85 latitude") from None


def midpoint(line):
    """Position and heading halfway along a lon/lat polyline by local metric arc length."""
    line = np.asarray(line, np.float64)
    scale = np.array([math.cos(math.radians(float(line[:, 1].mean()))), 1.0])
    delta = np.diff(line, axis=0) * scale
    lengths = np.hypot(delta[:, 0], delta[:, 1])
    total = float(lengths.sum())
    if total <= 1e-12:
        return None
    arc = np.cumsum(lengths)
    j = min(int(np.searchsorted(arc, total / 2, side="right")), len(lengths) - 1)
    t = (total / 2 - (arc[j - 1] if j else 0)) / max(lengths[j], 1e-12)
    p = line[j] + t * (line[j + 1] - line[j])
    heading = math.degrees(math.atan2(delta[j, 0], delta[j, 1])) % 360
    return float(p[1]), float(p[0]), heading


def reference_links(lat, lon, heading):
    return {
        "streetview": "https://www.google.com/maps/@?" + urlencode({
            "api": 1, "map_action": "pano", "viewpoint": f"{lat:.7f},{lon:.7f}",
            "heading": f"{heading:.1f}", "pitch": 0, "fov": 70}),
        "mapillary": f"https://www.mapillary.com/app/?lat={lat:.7f}&lng={lon:.7f}&z=17",
        "osm": f"https://www.openstreetmap.org/#map=19/{lat:.7f}/{lon:.7f}",
    }


def inspect_features(feats, bbox, road_class=None, source_record=None):
    """Records and GeoJSON from one layer; global IDs let the caller deduplicate cells."""
    south, west, north, east = bbox
    specs = roads.road_specs(feats)
    bridges = roads.bridge_specs(feats)
    records, geo = [], []
    for i, fid in enumerate(feats.ids):
        cls = str(feats.attrs["class"][i])
        if road_class and cls != road_class:
            continue
        src = str(feats.attrs.get("src_record", [""] * len(feats))[i])
        if source_record and src.split("@")[0] != source_record:
            continue
        parts = [r for part in feats.parts(i) for r in part if len(r) >= 2]
        if not parts:
            continue
        # Select by actual line intersection, rather than its bbox (a diagonal can miss the area).
        if not any(roads.clip_polyline(p, west, south, east, north) for p in parts):
            continue
        ground = roads.ground_parts(feats, i)
        elevated = roads.bridge_parts(feats, i)
        tunnels = roads.tunnel_parts(feats, i)
        mixed = bool(ground and (elevated or tunnels))
        sp = specs[i] or bridges[i]
        # The camera sits on a ground portion: AGL alone cannot locate a bridge's deck height.
        camera_line = max(ground, key=lambda p: len(p)) if ground else None
        cam = midpoint(camera_line) if camera_line is not None else None
        if cam and not (south <= cam[0] <= north and west <= cam[1] <= east):
            cam = None
        if cam and sp is not None and sp.dir < 0:
            cam = (cam[0], cam[1], (cam[2] + 180) % 360)
        record = {
            "id": str(fid), "class": cls, "source_record": src,
            "name": str(feats.attrs.get("name", [""] * len(feats))[i]),
            "width_m": round(float(sp.width), 3) if sp else None,
            "ground_parts": len(ground), "bridge_parts": len(elevated), "tunnel_parts": len(tunnels),
            "mixed_ground_and_structure": mixed,
            "geometry_sha256": hashlib.sha256(b"".join(np.asarray(p, "<f8").tobytes() for p in parts)).hexdigest(),
        }
        if cam:
            lat, lon, heading = cam
            record["camera"] = {"lat": lat, "lon": lon, "heading": round(heading, 2)}
            record["references"] = reference_links(lat, lon, heading)
        records.append(record)
        for kind, pieces in (("ground", ground), ("bridge", elevated), ("tunnel", tunnels)):
            for part in pieces:
                geo.append({"type": "Feature", "properties": {
                    "id": str(fid), "kind": kind, "width_m": record["width_m"], "source_record": src},
                    "geometry": {"type": "LineString", "coordinates": np.asarray(part).tolist()}})
    return records, geo


def benchmark_set(records, max_views, heading=None):
    # Priority first, deterministic ties; cameras use complete original segments, never cell clips.
    candidates = sorted((r for r in records if "camera" in r and (heading is None or
                        abs((r["camera"]["heading"] - heading + 180) % 360 - 180) <= 60)),
                        key=lambda r: (not r["mixed_ground_and_structure"], r["id"]))[:max_views]
    places = [{"id": "road_" + r["id"].replace("-", "_"),
               "name": r["name"] or r["source_record"] or r["id"], **r["camera"],
               "heldout": False, "group": "road_audit", "tags": ["road", r["class"]],
               "why": "Ground portion of a mixed-level road" if r["mixed_ground_and_structure"] else "Road review",
               "references": r["references"]} for r in candidates]
    return {"version": 1, "about": "Frozen road input comparison views from tools/road_audit.py; retain between runs.",
            "defaults": {"date": "2026-09-22", "hour": 10, "weather": "clear", "views": [
                {"name": "driver", "agl": 1.7, "pitch": 0, "fov": 70, "under": True},
                {"name": "overhead", "agl": 100, "pitch": -80, "fov": 70}]}, "places": places}


def audit(md, bbox, road_class=None, source_record=None):
    records, geo, seen, missing, loaded, unavailable = [], [], set(), [], [], {}
    for key in mapcells.cells_for_bbox(*bbox):
        # In offline mode, skip missing layers explicitly. Do not search remote raw parquet for them.
        if md.fetcher.offline and not md.has(key, "roads"):
            missing.append(str(key))
            continue
        try:
            cd = md.cell(key, layers=("roads",))
        except Exception as ex:
            if not md.fetcher.offline:
                raise
            missing.append(str(key))
            unavailable[str(key)] = f"{type(ex).__name__}: {ex}"
            continue
        loaded.append({"cell": str(key), "digest": cd.digest(), "provenance": cd.provenance})
        rows, features = inspect_features(cd.layers["roads"], bbox, road_class, source_record)
        fresh = {r["id"] for r in rows} - seen
        records.extend(r for r in rows if r["id"] in fresh)
        geo.extend(f for f in features if f["properties"]["id"] in fresh)
        seen.update(fresh)
    records.sort(key=lambda r: r["id"])
    report = {"bbox": bbox, "class": road_class, "source_record": source_record, "versions": md.versions(),
              "coverage": {"loaded": len(loaded), "missing": missing, "complete": not missing,
                           "unreadable": unavailable},
              "summary": {"roads": len(records), "mixed_ground_and_structure": sum(
                  r["mixed_ground_and_structure"] for r in records)}, "cells": loaded, "roads": records}
    return report, {"type": "FeatureCollection", "features": geo}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", type=bbox_arg, required=True)
    ap.add_argument("--class", dest="road_class")
    ap.add_argument("--source-record", help="OSM/Overture source ID without @version, e.g. r2925465")
    ap.add_argument("--cache-dir", default=mapdata.DEFAULT_CACHE)
    ap.add_argument("--fetch", action="store_true", help="download missing road layers through the normal fetcher")
    ap.add_argument("--max-views", type=int, default=12)
    ap.add_argument("--heading", type=float, help="benchmark views within 60 degrees of this travel heading")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.max_views < 1:
        ap.error("--max-views must be positive")
    if a.heading is not None and not math.isfinite(a.heading):
        ap.error("--heading must be finite")
    md = AuditMapData(a.cache_dir, netfetch.Fetcher(offline=not a.fetch))
    try:
        report, geo = audit(md, a.bbox, a.road_class, a.source_record)
    finally:
        md.close()
    a.out.mkdir(parents=True, exist_ok=True)
    views = benchmark_set(report["roads"], a.max_views, a.heading)
    report["summary"]["selected_views"] = len(views["places"])
    for name, data in (("audit.json", report), ("roads.geojson", geo),
                       ("set.json", views)):
        (a.out / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"coverage": report["coverage"], "summary": report["summary"], "out": str(a.out)}, indent=2))
    return 0 if report["coverage"]["complete"] and views["places"] else 2


if __name__ == "__main__":
    sys.exit(main())
