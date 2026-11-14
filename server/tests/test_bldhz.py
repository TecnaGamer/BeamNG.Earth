"""The horizon ring's tall buildings (bldhz.py) from a local index (tools/build_tall_index.py's shards)."""
import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import bldhz  # noqa: E402
import geotiles  # noqa: E402
import mapgeom  # noqa: E402

LAT, LON = 40.7484, -73.9857        # (a tall building's place: any)


def _ring(lat, lon, half_m=20.0):
    dlat = half_m / 111_320.0
    dlon = half_m / (111_320.0 * np.cos(np.radians(lat)))
    r = np.array([[lon - dlon, lat - dlat], [lon + dlon, lat - dlat], [lon + dlon, lat + dlat], [lon - dlon, lat + dlat]])
    return np.round(r * 1e7).astype(np.int32)


def _shard(n_b=2, parts=True):
    import build_tall_index as T
    b_ids = [f"b{i:03d}" for i in range(n_b)]
    rings = [_ring(LAT + 0.001 * i, LON) for i in range(n_b)]
    a = {"b_id": mapgeom.text_column(b_ids),
         "b_ring_off": np.r_[0, np.cumsum([len(r) for r in rings])].astype(np.int64),
         "b_ll": np.concatenate(rings)}
    for c in T.B_SPEC["text"]:
        a["b_t_" + c] = mapgeom.text_column(["office" if c == "class" else None] * n_b)
    for c in T.B_SPEC["float"]:
        a["b_f_" + c] = mapgeom.float_column([200.0 if c == "height" else None] * n_b)
    for c in T.B_SPEC["bool"]:
        a["b_b_" + c] = mapgeom.bool_column([c == "has_parts" and parts] * n_b)
    if parts:
        pr = [_ring(LAT, LON, 15.0), _ring(LAT, LON, 8.0)]
        a["p_id"] = mapgeom.text_column(["p0", "p1"])
        a["p_ring_off"] = np.r_[0, np.cumsum([len(r) for r in pr])].astype(np.int64)
        a["p_ll"] = np.concatenate(pr)
        for c in T.P_SPEC["text"]:
            a["p_t_" + c] = mapgeom.text_column(["b000" if c == "building_id" else None] * 2)
        for c in T.P_SPEC["float"]:
            a["p_f_" + c] = mapgeom.float_column([{"height": h, "min_height": m}.get(c) for h, m in
                                                  ((200.0, None), (260.0, 200.0))])
    return a


def _index(d, arrays, sha=None):
    import build_tall_index as T
    gx, gy = geotiles.tile_of(LAT, LON, bldhz.Z)
    os.makedirs(os.path.join(d, "12"), exist_ok=True)
    p = os.path.join(d, "12", f"{gx}_{gy}.npz")
    T.write_npz(p, arrays)
    with open(p, "rb") as f:
        real = hashlib.sha1(f.read()).hexdigest()
    key = geotiles.key(gx, gy, bldhz.Z)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"format": bldhz.FORMAT, "release": "test", "z": bldhz.Z, "min_height": 60.0,
                   "tiles": {key: {"n": 2, "sha1": sha or real}}}, f)
    return gx, gy, real


class BldhzTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        bldhz._local.clear()

    def _with(self, arrays, sha=None):
        gx, gy, real = _index(self.d.name, arrays, sha)
        p = mock.patch.object(bldhz, "DIR", self.d.name)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(bldhz._local.clear)
        return gx, gy, real

    def test_pieces_per_skyline_square(self):
        gx, gy, _ = self._with(_shard())
        tile = geotiles.geobox(gx, gy, bldhz.Z)
        sq = bldhz.squares(tile, lambda la, lo: np.zeros(np.shape(la)))
        sx, sy = geotiles.tile_of(LAT, LON, geotiles.SKY_Z)
        self.assertIn(geotiles.key(sx, sy, geotiles.SKY_Z), sq)
        n = sum(len(v) for v in sq.values())
        self.assertGreaterEqual(n, 2)
        tops = [float(s.min_h or 0) + float(s.height or 0) for v in sq.values() for s, _a, _b in v]
        self.assertGreaterEqual(max(tops), 250.0, "the building drawn from its parts (the 260 m one)")
        again = bldhz.squares(tile, lambda la, lo: np.zeros(np.shape(la)))
        self.assertEqual(sorted(sq), sorted(again))
        for k in sq:
            self.assertTrue(all(np.array_equal(a[0].ring, b[0].ring) for a, b in zip(sq[k], again[k])))

    def test_shard_checked(self):
        gx, gy, _ = self._with(_shard(), sha="0" * 40)
        with self.assertRaises(ValueError):
            bldhz.source().load(gx, gy)

    def test_malformed_shard(self):
        a = _shard(parts=False)
        a["b_ring_off"] = a["b_ring_off"].copy()
        a["b_ring_off"][1] = 99
        gx, gy, _ = self._with(a)
        with self.assertRaises(ValueError):
            bldhz.source().load(gx, gy)

    def test_cache_key_follows_the_shard(self):
        import blockworker
        gx, gy, real = self._with(_shard())
        with mock.patch.object(blockworker, "_s", mock.Mock(md=None)):
            k1 = blockworker._bldhz_key(gx, gy)
            self.assertIn(real, k1)
            bldhz._local.clear()
            _index(self.d.name, _shard(n_b=3))
            k2 = blockworker._bldhz_key(gx, gy)
        self.assertNotEqual(k1, k2)

    def test_far_geotile_pieces(self):
        """blockworker's bldhz kind: a piece a skyline square, each a shape at the tile's corner in its frame."""
        import blockworker
        gx, gy, _ = self._with(_shard())
        tile = geotiles.geobox(gx, gy, bldhz.Z)
        dem = mock.Mock(sample=lambda la, lo, z: np.zeros(np.shape(la)))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(blockworker, "_s", mock.Mock(md=None, dem=dem)), \
                mock.patch.object(blockworker, "ground", lambda _a, la, lo, h: h):
            data, shapes, n = blockworker._far_geo_build("bldhz", tile, 15, d, "/x", None)
            self.assertGreaterEqual(n, 2)
            sx, sy = geotiles.tile_of(LAT, LON, geotiles.SKY_Z)
            sq = geotiles.key(sx, sy, geotiles.SKY_Z)
            self.assertIn(sq, data["parts"])
            self.assertEqual(data["parts"][sq]["pos"], [tile.box[0], tile.box[1], 0.0])
            self.assertTrue(all(os.path.exists(os.path.join(d, f)) for f in shapes.values()))
            self.assertEqual(shapes[sq], blockworker.far_geo_piece(tile.key, sq, "bldhz"))

    def test_reach_by_setting(self):
        """near none, normal 20 km, far 35 km, max to the horizon (or the fixed view distance)."""
        self.assertEqual([bldhz.reach(s) for s in (0.6, 1.0, 1.5, 2.0)], [0.0, 20000.0, 35000.0, bldhz.HORIZON_M])
        self.assertEqual(bldhz.reach(2.0, 50000.0), 50000.0)
        self.assertEqual(bldhz.reach(1.0, 50000.0), 20000.0)

    def test_from_the_data_site(self):
        """The published layout (build_tall_index.write_pages: PMTiles parts in the build's folder, a manifest) read
        through a fake host as osmyears is: pieces as from the local index; the frozen build in the cache key."""
        import build_tall_index as T
        from tests.test_osm_building_years import FakeFetcher
        from types import SimpleNamespace
        gx, gy = geotiles.tile_of(LAT, LON, bldhz.Z)
        out = os.path.join(self.d.name, "pages")
        m = T.write_pages(out, {(gx, gy): T.npz_bytes(_shard())}, "test", 60.0)
        base = "https://tall.test/tallbld"
        files = {f"{base}/manifest.json": Path(out, "manifest.json").read_bytes()}
        for p in m["parts"]:
            files[f"{base}/{p['archive']}"] = Path(out, p["archive"]).read_bytes()
        md = SimpleNamespace(fetcher=FakeFetcher(files), cache_dir=os.path.join(self.d.name, "cache"))
        with mock.patch.object(bldhz, "DIR", None), mock.patch.object(bldhz, "BASE", base), \
                mock.patch.dict(bldhz._pages, clear=True):
            src = bldhz.source(md)
            self.assertIsInstance(src, bldhz.PagesIndex)
            src.version()                   # (pinned: MapData.warm_manifests)
            self.assertTrue(bldhz.on(md))
            tile = geotiles.geobox(gx, gy, bldhz.Z)
            sq = bldhz.squares(tile, lambda la, lo: np.zeros(np.shape(la)), md)
            self.assertTrue(sq and sum(len(v) for v in sq.values()) >= 2)
            self.assertEqual(src.sig(gx, gy)[:2], ("pages", m["built"]))
            empty = geotiles.geobox(gx + 5, gy, bldhz.Z)
            self.assertEqual(bldhz.squares(empty, lambda la, lo: np.zeros(np.shape(la)), md), {})

    def test_builder_columns_are_the_map_cells(self):
        """The standalone builder's column copies are overture.LAYERS' (the server reads its tiles as map cells), its
        tile maths geotiles', its WKB ring mapgeom's."""
        import build_tall_index as T
        import overture
        for mine, theirs in ((T.B_SPEC, overture.LAYERS["buildings"]), (T.P_SPEC, overture.LAYERS["building_parts"])):
            for k in ("cols", "text", "float", "bool"):
                self.assertEqual(mine.get(k, ()), theirs.get(k, ()), k)
        lat = np.array([LAT, -33.86, 51.5, 85.0, -85.0])
        lon = np.array([LON, 151.2, -0.12, 179.99, -179.99])
        x, y = T.tile_of(lat, lon)
        gx, gy = geotiles.tile_of(lat, lon, bldhz.Z)
        self.assertEqual((x.tolist(), y.tolist()), (np.asarray(gx).tolist(), np.asarray(gy).tolist()))
        import struct
        ring = [(1.0, 2.0), (3.0, 2.0), (3.0, 4.0), (1.0, 2.0)]
        poly = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", 4) + b"".join(struct.pack("<dd", *p) for p in ring)
        multi = struct.pack("<BI", 1, 6) + struct.pack("<I", 1) + poly
        for b in (poly, multi):
            self.assertEqual(T.first_ring_wkb(b).tolist(), mapgeom.parse_wkb(b)[1][0][0].tolist())

    def test_off_without_dir(self):
        with mock.patch.object(bldhz, "DIR", None):
            self.assertFalse(bldhz.on())
        importlib.reload(bldhz)


if __name__ == "__main__":
    unittest.main()
