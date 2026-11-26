"""The scanner's inputs (native/roadscan pack.rs): each build folder of tools/widecorpus build.py (one anchor frame, its
geotiles' emitted meshes, physics terrain blocks and source lines) written as one pack, and the index of a state's
packs with each tile's lat/lon fit and the placements between the frames of neighbouring packs (geotiles.placement,
the mod's own). Format marshalling only: the meshes are decoded by the server's cdae reader and placed exactly as the
wide corpus's checks.measure places them (the same collision rule), every check is in Rust.

    python3 tools/roadscan/export.py INDEX.bin SOURCES.json [--jobs N]

SOURCES.json: [{"dir": BUILD_DIR, "lat": .., "lon": ..} ...] (the frame each folder was built in: build.py's place)."""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1]
sys.path[:0] = [str(TREE / "server"), str(TREE / "tools" / "widecorpus")]
import numpy as np  # noqa: E402

import checks  # noqa: E402
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import roadmesh  # noqa: E402
import surface  # noqa: E402

LEVELS = {"ground": 0, "bridge": 1, "tunnel": 2}
PAVED = (surface.ASPHALT, surface.COBBLE, surface.PAVERS, surface.BRICK)
KINDS = ("road", "walk", "walkcol", "deck", "tunnel", "barrier", "buscol")


def _str(s):
    b = str(s).encode("utf-8")[:65535]
    return struct.pack("<H", len(b)) + b


def tile_entries(directory, frame, key, tile, layers):
    """[(kind, collides, [(flags, triangles (k, 3, 3))])] of one tile: its road geotile (rg_, in the geotile's own
    frame, placed) and road surface (rs_1_, in the build's frame) meshes, as checks.measure reads them."""
    src = geotiles.frame(*geotiles.parse(key)[1:])
    entries = []
    for file, src_frame in ((f"rg_{key}.json", src), (f"rs_1_{key}.json", None)):
        path = directory / file
        if not path.exists():
            continue
        data = checks.read(path)
        for what, entry in data.items():
            if not isinstance(entry, dict) or "shape" not in entry:
                continue
            if what not in KINDS and not what.startswith("rail"):
                continue
            collision = what != "road" and not (what == "walk" and "walkcol" in data) and not (
                what.startswith("rail") and not what.startswith("railcol") and "railcol" in data)
            groups = []
            for mat, tri in checks.groups(directory, entry, frame, src_frame, tile["exact"].get(what)):
                flags = ((1 if mat == roadmesh.DECK_TOP else 0) | (2 if mat == roadmesh.DECK_SIDE else 0) |
                         (4 if mat == "curbs" else 0) |
                         (8 if layers.get(mat) == "mark" or mat.startswith(("line_", "crossing")) else 0))
                groups.append((flags, np.asarray(tri, np.float64).reshape(-1, 3, 3)))
            entries.append((checks.audit_kind(what), collision, groups))
    return entries


def write_pack(directory, lat, lon, out):
    """The pack of one build folder; returns [(key, quad centre (x, y), median terrain z)] of its tiles."""
    directory = Path(directory)
    frame = geodesy.EnuFrame(lat, lon)
    tiles = checks.read(directory / "tiles.json")
    parts = [b"RSCANPK1", struct.pack("<ddI", lat, lon, len(tiles))]
    info = []
    for tile in tiles:
        key = tile["key"]
        ctx = checks.read(directory / (key + ".source.json"))
        terrain = checks.Terrain(directory / (key + ".ter"), ctx["block"])
        quad = np.asarray(ctx["quad"], np.float64).reshape(4, 2)
        n = len(terrain.z)
        parts += [_str(key), quad.astype("<f8").tobytes(),
                  struct.pack("<Iqq", n, int(terrain.origin[0]), int(terrain.origin[1])),
                  np.ascontiguousarray(terrain.z, "<f4").tobytes(),
                  np.ascontiguousarray(terrain.mat == 255, "u1").tobytes(),
                  np.ascontiguousarray(np.isin(terrain.mat, PAVED), "u1").tobytes()]
        entries = tile_entries(directory, frame, key, tile, ctx.get("layers", {}))
        z = terrain.z[np.isfinite(terrain.z)]
        org = np.array([round(float(quad[:, 0].mean())), round(float(quad[:, 1].mean())),
                        round(float(np.median(z))) if len(z) else 0.0])
        parts += [org.astype("<f8").tobytes(), struct.pack("<I", len(entries))]
        for kind, collides, groups in entries:
            total = sum(len(t) for _f, t in groups)
            parts.append(struct.pack("<BBI", kind, 1 if collides else 0, total))
            for flags, tri in groups:
                rows = np.zeros(len(tri), dtype=[("f", "u1"), ("p", "<f4", (9,))])
                rows["f"] = flags
                rows["p"] = (tri - org).reshape(-1, 9)
                parts.append(rows.tobytes())
        lines = ctx["lines"]
        parts.append(struct.pack("<I", len(lines)))
        for ln in lines:
            xy = np.asarray(ln["xy"], "<f8").reshape(-1, 2)
            parts += [_str(ln["id"]), struct.pack("<Bdd", LEVELS.get(ln["level"], 0), float(ln["width"]),
                                                  float(ln.get("drive") or 0.0)),
                      struct.pack("<BBB", min(255, int(ln.get("lanes") or 1)), 1 if ln.get("oneway") else 0,
                                  1 if ln.get("paved") else 0),
                      _str(ln.get("cls", "")), struct.pack("<I", len(xy)), xy.tobytes()]
        info.append((key, quad.mean(0).tolist(), float(np.median(z)) if len(z) else 0.0))
    tmp = Path(str(out) + ".tmp")
    tmp.write_bytes(b"".join(parts))
    os.replace(tmp, out)
    return info


def affine(frame, c):
    """lat = a0 + a1 x + a2 y, lon = a3 + a4 x + a5 y fitted over 100 m round c (the server's world_to_geodetic)."""
    pts = np.array([[c[0], c[1], 0.0], [c[0] + 100.0, c[1], 0.0], [c[0], c[1] + 100.0, 0.0]])
    la, lo, _ = geotiles.world_to_geodetic(frame, pts)
    a1, a2 = (la[1] - la[0]) / 100.0, (la[2] - la[0]) / 100.0
    a4, a5 = (lo[1] - lo[0]) / 100.0, (lo[2] - lo[0]) / 100.0
    return [la[0] - a1 * c[0] - a2 * c[1], a1, a2, lo[0] - a4 * c[0] - a5 * c[1], a4, a5]


def _one(job):
    try:
        return job["i"], write_pack(job["dir"], job["lat"], job["lon"], job["out"]), None
    except Exception as e:      # (one folder's failure is reported, the others are written)
        return job["i"], None, f"{type(e).__name__}: {e}"


def export(index, sources, jobs=2, reuse=True):
    """Every source folder's pack beside the index (packs/NNNNN.pack) and the index; returns the failures."""
    index = Path(index)
    pdir = index.parent / "packs"
    pdir.mkdir(parents=True, exist_ok=True)
    work = []
    for i, s in enumerate(sources):
        out = pdir / f"{i:05d}.pack"
        meta = pdir / f"{i:05d}.json"
        if reuse and out.exists() and meta.exists() and json.loads(meta.read_text()).get("dir") == str(s["dir"]):
            continue
        work.append(dict(i=i, dir=str(s["dir"]), lat=float(s["lat"]), lon=float(s["lon"]), out=str(out)))
    fails = []
    with multiprocessing.get_context("spawn").Pool(max(1, jobs)) as pool:
        for i, info, err in pool.imap_unordered(_one, work):
            if err:
                fails.append((sources[i]["dir"], err))
                print("export failed", sources[i]["dir"], err, flush=True)
                continue
            (pdir / f"{i:05d}.json").write_text(json.dumps(dict(dir=str(sources[i]["dir"]), tiles=info)))
    # the index: every pack, every tile once (the first pack holding it), the placements between neighbours' frames
    packs, tiles, seen = [], [], {}
    for i, s in enumerate(sources):
        meta = pdir / f"{i:05d}.json"
        if not meta.exists() or not (pdir / f"{i:05d}.pack").exists():
            continue
        info = json.loads(meta.read_text())["tiles"]
        p = len(packs)
        frame = geodesy.EnuFrame(float(s["lat"]), float(s["lon"]))
        packs.append((f"packs/{i:05d}.pack", float(s["lat"]), float(s["lon"]), frame, info))
        for t, (key, c, zmed) in enumerate(info):
            if key in seen:
                continue
            seen[key] = len(tiles)
            tiles.append((key, p, t, affine(frame, c), c, zmed))
    pairs = {}
    for key, p, _t, _a, c, zmed in tiles:
        _z, gx, gy = geotiles.parse(key)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                q = seen.get(geotiles.key(gx + dx, gy + dy))
                if q is None or tiles[q][1] == p or (tiles[q][1], p) in pairs:
                    continue
                qp = tiles[q][1]
                qc, qz = tiles[q][4], tiles[q][5]
                r, tt, cc = geotiles.placement(packs[qp][3], packs[p][3], (qc[0], qc[1], qz))
                pairs[(qp, p)] = (r.ravel().tolist(), tt.tolist(), cc.tolist(), qz)
    out = [b"RSCANIX1", struct.pack("<I", len(packs))]
    for path, la, lo, _f, _i in packs:
        out += [_str(path), struct.pack("<dd", la, lo)]
    out.append(struct.pack("<I", len(tiles)))
    for key, p, t, a, _c, _z in tiles:
        out += [_str(key), struct.pack("<II", p, t), struct.pack("<6d", *a)]
    out.append(struct.pack("<I", len(pairs)))
    for (s, d), (r, tt, cc, pz) in sorted(pairs.items()):
        out += [struct.pack("<II", s, d), struct.pack("<16d", *r, *tt, *cc, pz)]
    tmp = Path(str(index) + ".tmp")
    tmp.write_bytes(b"".join(out))
    os.replace(tmp, index)
    return fails


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("index", type=Path)
    ap.add_argument("sources", type=Path)
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args()
    bad = export(a.index, json.loads(a.sources.read_text()), a.jobs)
    sys.exit(1 if bad else 0)
