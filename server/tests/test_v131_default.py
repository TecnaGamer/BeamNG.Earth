"""The default flags are exactly v1.3.1 (docs/V1_4_SPEC.md §3.0/§3.1: everything new sits behind a flag
until its in-game gate passes): the selection (tile keys and levels, refined nodes, hysteresis and
HOLD_S), the switch distances, the DEM zooms, the tile geometry and the written .cdae / .dae bytes, the
swap-group deltas and the heightmap of a default `earthd.Server`, compared with fixtures/v131_golden.json.

The fixture was written by the v1.3.1 code (the pre-v1.4 tree) through compute() below, which calls
both APIs (v1.4 adds `layout=`); regenerate it from a tree with

    python3 server/tests/test_v131_default.py --write <server dir> server/tests/fixtures/v131_golden.json

Only do that from v1.3.1 code: the point of the file is that the current code reproduces it. Floats are
rounded before hashing (1e-4 m for geometry, 1e-6 for err / skirt / deltas), so a last-bit change in
numpy cannot fail the test, but any real change does. (The message level: test_server and
test_protocol_agreement check that a 32-cell hello and far_tile carry no v1.4 field; status.backlog
is the one additive field of M0.)
"""
import hashlib
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "v131_golden.json"
LAT0, LON0 = 36.5785, -118.2923
POSES = [(0.0, 0.0), (37.3, 20.1), (180.0, 60.0), (420.0, 130.0), (1555.0, -800.0), (1500.0, -790.0),
         (-12345.6, 6789.1), (70000.0, -3.0)]
TILES = [((-2, 3, -5), 1 / 64), ((-1, 1, -3), 1 / 64), ((0, 1, -2), 1 / 64), ((1, 0, -1), None),
         ((4, -1, 2), None), ((8, 0, 0), None)]


class RoughDem:
    def sample(self, lat, lon, zoom, progress=None):
        lat, lon = np.asarray(lat), np.asarray(lon)
        return (1500 + 300 * np.sin(lat * 90) * np.cos(lon * 70) + zoom * 7.0
                + 4.0 * np.sin(lat * 61000) * np.cos(lon * 47000))


def _h(data):
    return hashlib.sha256(data).hexdigest()


def _hr(a, nd):
    """Digest of an array rounded to nd decimals (-0.0 folded into 0.0)."""
    return _h(np.ascontiguousarray(np.round(np.asarray(a, np.float64), nd) + 0.0).tobytes())


def _r(x, nd=6):
    return None if x is None else round(float(x), nd) + 0.0


def compute(server_dir):
    """Outputs of a default earthd.Server of the tree at server_dir (v1.3.1 or later)."""
    sys.path.insert(0, str(server_dir))
    import earthd
    import farfield as ff
    import geodesy as g
    import heightmap
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        s = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=None)       # every other argument default
        try:
            lay = {"layout": s.layout} if hasattr(s, "layout") else {}
            out["server"] = {"min_level": s.min_level, "near_radii": sorted(s.near_radii.items()),
                             "near_visual": s.near_visual, "far_format": s.far_format,
                             "tex": [s.tex_size(lvl) for lvl in range(-2, 10)],
                             "format": getattr(earthd, "FORMAT", None) or s.layout.format}
            # selection as Connection.update_far calls it, along a short drive with hysteresis and hold
            sel, prev = {}, frozenset()
            for i, (px, py) in enumerate(POSES):
                tiles, refined = ff.select(px, py, (), prev, s.kfun, s.hysteresis, s.radius,
                                           min_level=s.min_level, radii=s.near_radii)
                held, _ = ff.select(px + 90.0, py, (), frozenset(refined), s.kfun, s.hysteresis, s.radius,
                                    hold=set(sorted(refined)[:20]), min_level=s.min_level, radii=s.near_radii)
                sel[str(i)] = [len(tiles), _h(",".join(sorted(tiles)).encode()),
                               _h(",".join(sorted(map(str, refined))).encode()), _h(",".join(sorted(held)).encode())]
                prev = frozenset(refined)
            for i, (px, py, cx, cy) in enumerate([(0.0, 0.0, 0.0, 0.0), (600.0, 300.0, 512.0, 256.0)]):
                tiles, _ = ff.select(px, py, [ff.Block(cx, cy).bbox, ff.Block(cx + 256, cy).bbox])
                sel[f"terrain{i}"] = [len(tiles), _h(",".join(sorted(tiles)).encode())]
            out["select"] = sel
            out["switch"] = [ff.switch_distance(lvl, s.kfun, s.near_radii, s.min_level) for lvl in range(-2, 10)]
            a = s.new_anchor(LAT0, LON0)
            out["zooms"] = [a.zooms[lvl] for lvl in range(0, 10)]
            # geometry and bytes
            frame, dem = g.EnuFrame(LAT0, LON0), RoughDem()
            geo, built = {}, {}
            for (lvl, i, j), q in TILES:
                t = ff.Tile(lvl, i, j)
                zoom = 15 if lvl <= 0 else a.zooms[lvl]
                b = ff.build_tile(frame, dem, t, zoom, q, None if lvl <= 0 else a.zooms[lvl - 1],
                                  with_sink=s.near_visual == "terrain", **lay)
                built[t.key] = (t, b)
                rec = {"pos": [_r(v, 4) for v in b.pos], "err": _r(b.err), "skirt": _r(b.skirt), "sink": _r(b.sink),
                       "tris": _h(np.ascontiguousarray(b.tris, np.int64).tobytes()),
                       "positions": _hr(b.positions, 4), "normals": _hr(b.normals, 4), "uvs": _hr(b.uvs, 6),
                       "planar": _hr(b.planar, 6), "grid": _hr(b.grid, 4)}
                for name, kw in (("cdae_tex", dict(material="bngEarthSat_1_x", textured=True, tex_px=256)),
                                 ("cdae", {}), ("dae", {})):
                    p = os.path.join(tmp, "t." + name.split("_")[0])
                    ff.write_built(p, b, name.split("_")[0], **kw)
                    rec[name + "_bytes"] = os.path.getsize(p)
                geo[t.key] = rec
            out["tiles"] = geo
            par, kids = ff.Tile(2, 0, 0), [ff.Tile(1, i, j) for i in (0, 1) for j in (0, 1)]
            bp = ff.build_tile(frame, dem, par, a.zooms[2], **lay)
            bk = [ff.build_tile(frame, dem, k, a.zooms[1], **lay) for k in kids]
            out["deltas"] = [_r(v) for v in ff.group_deltas(([(k, b.grid) for k, b in zip(kids, bk)], [(par, bp.grid)]))]
            (t0, b0), (t1, b1) = built["-2_3_-5"], built["-1_1_-3"]
            out["deltas_near"] = [_r(v) for v in ff.group_deltas(([(t0, b0.grid)], [(t1, b1.grid)]))]
            p = os.path.join(tmp, "hm.png")
            # The v1.3.1 block: 1024² at 2 m. Since 2026-11-01 the block lattice is 1 m (heightmap.SQUARE): the same
            # 2 km is the 2048² block, and its every second sample (odd rows from the north, even columns) is the
            # v1.3.1 sample, bit for bit.
            sub = int(round(2.0 / getattr(heightmap, "SQUARE", 2.0)))
            info = heightmap.generate(frame, lambda la, lo: dem.sample(la, lo, 15), p, 256.0, -512.0, (300.0, -400.0),
                                      1 / 64, **({"size": 1024 * sub} if sub > 1 else {}))
            out["heightmap"] = {k: _r(v) for k, v in sorted(info.items()) if k != "seconds"}
            from PIL import Image
            with Image.open(p) as im:
                px = np.asarray(im)[sub - 1::sub, ::sub]
                out["heightmap_png"] = [px.shape[1], im.mode, _h(px.tobytes())]
        finally:
            s.hm_pool.shutdown(wait=True)
            s.tile_pool.shutdown(wait=True)
    return out


class V131DefaultTest(unittest.TestCase):
    maxDiff = None

    def test_default_server_reproduces_v131(self):
        want = json.loads(FIXTURE.read_text())
        got = json.loads(json.dumps(compute(Path(__file__).resolve().parents[1])))
        self.assertEqual(sorted(got), sorted(want))
        for k in want:
            self.assertEqual(got[k], want[k], k)

    def test_default_flags(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import earthd
        import farfield as ff
        self.assertEqual(earthd.CELLS_DEFAULT, 32)                         # Server(): the v1.3.1 layout
        # the command line: 64 cells in mesh mode (P8 passed), terrain mode frozen at 32
        self.assertEqual((earthd.resolve_cells(None, "mesh"), earthd.resolve_cells(None, "terrain"),
                          earthd.resolve_cells(32, "mesh")), (64, 32, 32))
        self.assertEqual(earthd.STATUS_INTERVAL, 0.0)                      # status on events only
        self.assertEqual(ff.Layout(earthd.CELLS_DEFAULT).format, 3)
        p = inspect.signature(earthd.Server).parameters
        self.assertEqual((p["cells"].default, p["status_interval"].default, p["tex"].default, p["kfun"].default,
                          p["near_radii"].default, p["near_visual"].default), (32, 0.0, None, None, None, "mesh"))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--write":
        res = compute(Path(sys.argv[2]).resolve())
        Path(sys.argv[3]).write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
        print("wrote", sys.argv[3])
    else:
        unittest.main()
