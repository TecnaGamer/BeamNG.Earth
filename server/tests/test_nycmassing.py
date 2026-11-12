"""New York's measured massing (server/nycmassing.py, tools/build_nyc_massing.py): the shard encoding, the bands of a
building's roof levels, the join to the map's footprints, the map's own parts winning, nothing outside New York, and a
shard that could not be had not kept."""
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "tools"))

import buildings as bld  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import mapgeom  # noqa: E402
import nycmassing as nm  # noqa: E402
import build_nyc_massing as builder  # noqa: E402

NATIVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= nm.NATIVE
SHAPELY = importlib.util.find_spec("shapely") is not None
LAT, LON = 40.7549, -73.9840            # Midtown
KEY = mapcells.cell_of(LAT, LON)
CELL = geodesy.EnuFrame(*KEY.center(), 0.0)


def sq(x, y, w, d):
    """An open rectangle in metres."""
    return np.array([(x, y), (x + w, y), (x + w, y + d), (x, y + d)], np.float64)


def dm(r):
    return np.round(np.asarray(r) * 10).astype(np.int64)


def record(bin_=1, x=10.0, y=10.0, w=40.0, bands=((0.0, 20.0, 1.0), (20.0, 80.0, 0.5))):
    """A record of a w x w building at (x, y) in the cell's frame: per band (bottom, top, its outline's side share)."""
    c = (x + w / 2, y + w / 2)
    parts = []
    for b, t, s in bands:
        ww = w * s
        parts.append((b, t, dm(sq(c[0] - ww / 2, c[1] - ww / 2, ww, ww))))
    return {"bin": bin_, "pxy": dm(c), "area": w * w, "top": bands[-1][1], "parts": parts}


def shard_bytes(recs):
    buf = io.BytesIO()
    np.savez_compressed(buf, **builder.encode(recs))
    return buf.getvalue()


def lonlat(e, n):
    la, lo, _ = CELL.enu_to_geodetic(np.asarray(e, np.float64), np.asarray(n, np.float64), 0.0)
    return np.stack([lo, la], 1)


def layer(rings, **attrs):
    """A buildings layer (mapgeom.Features) of closed rings in the cell's frame, ids B0, B1, ..."""
    ids = np.array([f"B{i}" for i in range(len(rings))])
    geoms = []
    for r in rings:
        r = np.vstack([r, r[:1]])
        geoms.append((mapgeom.POLYGON, [[lonlat(r[:, 0], r[:, 1])]]))
    f = mapgeom.build("buildings", ids, geoms)
    n = len(rings)
    base = {"class": ["commercial"] * n, "height": [np.nan] * n, "min_height": [np.nan] * n}
    base.update(attrs)
    for k, v in base.items():
        f.attrs[k] = np.array(v, object if isinstance(v[0], str) or v[0] is None else np.float64)
    return f


class Stub:
    def __init__(self, recs):
        self.recs = recs

    def index(self):
        return self.recs


def records(recs, frame=CELL, box=(-500, -500, 500, 500)):
    return nm.Records(frame, [(KEY, nm.decode(shard_bytes(recs)))], box)


class EncodingTest(unittest.TestCase):
    def test_round_trip(self):
        recs = [record(7), record(8, x=-300.5, y=420.3, w=12.0, bands=((0.0, 9.0, 1.0), (9.0, 15.5, 0.6)))]
        s = nm.decode(shard_bytes(recs))
        self.assertEqual(s["bin"].tolist(), [7, 8])
        self.assertEqual(s["first"].tolist(), [0, 2, 4])
        for b, r in enumerate(recs):
            np.testing.assert_allclose(s["pt"][b], np.asarray(r["pxy"]) / 10.0)
            for k, (bot, top, ring) in enumerate(r["parts"]):
                p = s["first"][b] + k
                got = s["xy"][s["vfirst"][p]:s["vfirst"][p + 1]]
                np.testing.assert_allclose(got, ring / 10.0)
                self.assertAlmostEqual(s["bot"][p], bot, places=3)
                self.assertAlmostEqual(s["ptop"][p], top, places=3)

    def test_bad_shards(self):
        self.assertEqual(nm.decode(b""), {})
        self.assertEqual(nm.decode(b"not a zip"), {})
        a = builder.encode([record()])
        a["pnv"] = a["pnv"] + 1                 # (the corner counts do not add up)
        buf = io.BytesIO()
        np.savez_compressed(buf, **a)
        self.assertEqual(nm.decode(buf.getvalue()), {})


class BandsTest(unittest.TestCase):
    def test_snap_levels(self):
        self.assertEqual(builder.snap_levels([30.0, 10.0, 10.3, 55.0]), [10.3, 30.0, 55.0])

    def test_merge_plan(self):
        # (990 sets back 10 m2 from 1000: one band; 480 sets back 20 from 500: one band; the rest stay)
        self.assertEqual(builder.merge_plan([1000.0, 990.0, 500.0, 480.0, 100.0]), [0, 2, 4])
        self.assertEqual(builder.merge_plan([1000.0, 1000.0]), [0])
        self.assertEqual(len(builder.merge_plan([1000.0 - 100 * k for k in range(10)], max_bands=4)), 4)

    @unittest.skipUnless(SHAPELY, "shapely (the builder's) not installed")
    def test_stacked_bands(self):
        import shapely
        ft = 1 / builder.FT
        faces = [(10.0, shapely.box(0, 0, 50 * ft, 100 * ft)), (30.0, shapely.box(50 * ft, 0, 100 * ft, 100 * ft)),
                 (35.0, shapely.box(60 * ft, 10 * ft, 62 * ft, 12 * ft))]     # (a 4 m2 vent: left out)
        bands = builder.bands_of(faces)
        self.assertEqual([(round(b, 1), round(t, 1)) for b, t, _p in bands], [(0.0, 10.0), (10.0, 30.0)])
        a = [sum(p.area for p in polys) * builder.FT2 for _b, _t, polys in bands]
        self.assertAlmostEqual(a[0], 10000.0, delta=20.0)          # (the whole footprint up to the lower roof)
        self.assertAlmostEqual(a[1], 5000.0, delta=20.0)           # (the tall half above it)

    @unittest.skipUnless(SHAPELY, "shapely (the builder's) not installed")
    def test_courtyard_cut_open_and_no_setback_dropped(self):
        import shapely
        ft = 1 / builder.FT
        ring = shapely.box(0, 0, 60 * ft, 60 * ft).difference(shapely.box(20 * ft, 20 * ft, 40 * ft, 40 * ft))
        bands = builder.bands_of([(12.0, ring), (25.0, shapely.box(0, 0, 20 * ft, 60 * ft))])
        self.assertEqual(len(bands), 2)
        self.assertTrue(all(not p.interiors for _b, _t, polys in bands for p in polys))
        self.assertAlmostEqual(sum(p.area for p in bands[0][2]) * builder.FT2, 3200.0, delta=20.0)
        self.assertEqual(builder.bands_of([(12.0, shapely.box(0, 0, 60 * ft, 60 * ft))]), [])     # (one level)


@unittest.skipUnless(NATIVE, "earthfast with nyc_massing_match")
class MatchTest(unittest.TestCase):
    def test_match_by_point_and_area(self):
        recs = records([record(1, x=10.0), record(2, x=100.0, w=20.0)])
        f = layer([sq(10, 10, 40, 40), sq(100, 10, 40, 40), sq(300, 300, 10, 10)])
        got = recs.match_layer(f, CELL, [0, 1, 2])
        # (the first: its own; the second holds record 2's point but is four times its area; the third: nothing)
        self.assertEqual(got, {0: 0})

    def test_one_building_a_record(self):
        recs = records([record(1)])
        f = layer([sq(10, 10, 40, 40), sq(11, 11, 38, 38)])
        self.assertEqual(recs.match_layer(f, CELL, [0, 1]), {0: 0})
        # (the same id in a second cell's layer takes it again; another id does not)
        self.assertEqual(recs.match_layer(f, CELL, [0]), {0: 0})
        g = layer([sq(10, 10, 40, 40)])
        g.ids = np.array(["Z"])
        self.assertEqual(recs.match_layer(g, CELL, [0]), {})

    def test_parts_in_the_anchor_frame(self):
        frame = geodesy.EnuFrame(LAT + 0.001, LON - 0.002, 0.0)
        recs = nm.Records(frame, [(KEY, nm.decode(shard_bytes([record(1)])))], (-5000, -5000, 5000, 5000))
        e, n, _ = frame.geodetic_to_enu(*CELL.enu_to_geodetic(np.array([30.0]), np.array([30.0]), 0.0)[:2], 0.0)
        np.testing.assert_allclose(recs.pt[0], [float(e[0]), float(n[0])], atol=0.02)
        parts = recs.parts(0)
        self.assertEqual([p[1] for p in parts], [(20.0, 0.0), (60.0, 20.0)])
        ring = parts[1][0]
        self.assertTrue(np.allclose(ring[0], ring[-1]))
        self.assertAlmostEqual(abs(bld._area2(ring[:-1])) / 2, 400.0, delta=0.5)


@unittest.skipUnless(NATIVE, "earthfast with nyc_massing_match")
class TileTest(unittest.TestCase):
    def _items(self, f, nyc, part_layers=()):
        return bld.tile_buildings(CELL, -256, -256, 256, 256, [f], lambda la, lo: np.zeros(np.shape(la)),
                                  part_layers=part_layers, nyc=nyc)

    def test_setbacks_drawn_as_parts(self):
        f = layer([sq(10, 10, 40, 40)])
        plain = self._items(f, None)
        self.assertEqual(len(plain), 1)
        items = self._items(f, Stub(records([record(1)])))
        self.assertEqual(sorted((round(s.min_h or 0.0), round(s.height + (s.min_h or 0.0))) for s, _, _ in items),
                         [(0, 20), (20, 80)])
        self.assertEqual(len({s.seed for s, _, _ in items}), 1, "one look")

    def test_water_tank_on_the_topmost_part_only(self):
        # (the setbacks took New York's tanks away: the model's own are smaller than its pieces kept; a building drawn
        # from its parts gets one on its topmost part, as many of these roofs have)
        f = layer([sq(10, 10, 40, 40)])
        with mock.patch.object(bld, "water_tank", lambda *a, **k: True):
            self.assertTrue(all(s.tank for s, _, _ in self._items(f, None)))
            items = self._items(f, Stub(records([record(1)])))
            self.assertGreater(len(items), 1)
            tops = [float(s.min_h or 0) + float(s.height or 0) for s, _, _ in items]
            self.assertEqual([s.tank for s, _, _ in items], [max(tops) - t < bld.WATER_TANK_TOP_M for t in tops])

    def test_map_parts_win(self):
        f = layer([sq(10, 10, 40, 40)])
        f.attrs["has_parts"] = np.array([1.0])
        ring = sq(10, 10, 40, 20)
        r = np.vstack([ring, ring[:1]])
        parts = mapgeom.build("building_parts", np.array(["p1"]), [(mapgeom.POLYGON, [[lonlat(r[:, 0], r[:, 1])]])])
        for k, v in {"building_id": np.array(["B0"], object), "height": np.array([33.0]),
                     "min_height": np.array([np.nan]), "num_floors": np.array([np.nan]),
                     "min_floor": np.array([np.nan])}.items():
            parts.attrs[k] = v
        items = self._items(f, Stub(records([record(1)])), part_layers=[parts])
        tops = sorted(round(s.height + (s.min_h or 0.0)) for s, _, _ in items)
        self.assertIn(33, tops, "the map's part")
        self.assertNotIn(80, tops, "not the record's")

    def test_pitched_roof_and_raised_buildings_keep_the_map(self):
        f = layer([sq(10, 10, 40, 40)], roof_shape=["gabled"])
        self.assertEqual(len(self._items(f, Stub(records([record(1)])))), 1)
        g = layer([sq(10, 10, 40, 40)], min_height=[6.0], height=[12.0])
        self.assertEqual(len(self._items(g, Stub(records([record(1)])))), 1)


def manifest_bytes(keys):
    import json
    return json.dumps({"format": 1, "cells": {f"{k.z}/{k.x}/{k.y}": {"buildings": 1} for k in keys}}).encode()


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_local_folder(self):
        d = os.path.join(self.tmp.name, str(KEY.z), str(KEY.x))
        os.makedirs(d)
        with open(os.path.join(d, f"{KEY.y}.npz"), "wb") as fh:
            fh.write(shard_bytes([record(5)]))
        src = nm.Source(None, local=self.tmp.name)
        self.assertEqual(src.shard(KEY)["bin"].tolist(), [5])
        self.assertEqual(src.shard(mapcells.CellKey(14, KEY.x + 1, KEY.y)), {})     # (none there)

    def test_failed_fetch_not_kept(self):
        calls = []

        class Down:
            def get(self, url, kind="demand"):
                calls.append(url)
                raise OSError("offline")
        src = nm.Source(self.tmp.name, fetcher=Down(), base="https://example.invalid/nyc")
        self.assertIsNone(src.shard(KEY))
        self.assertIsNone(src.shard(KEY))
        self.assertEqual(len(calls), 1, "not asked again by every tile meanwhile")
        self.assertFalse(os.path.exists(src._path(KEY)))
        with mock.patch.object(nm.time, "time", lambda: 1e12):
            src.shard(KEY)
        self.assertEqual(len(calls), 2, "asked again after SHARD_RETRY_S")

    def test_missing_shard_kept_empty_and_fetched_kept(self):
        import netfetch

        asked = []

        class Site:
            def get(self, url, kind="demand"):
                asked.append(url)
                if url.endswith("/manifest.json"):
                    return mock.Mock(body=manifest_bytes([KEY]))
                if url.endswith(f"/{KEY.y}.npz"):
                    return mock.Mock(body=shard_bytes([record(9)]))
                raise netfetch.NotFound(url)
        src = nm.Source(self.tmp.name, fetcher=Site(), base="https://example.invalid/nyc")
        self.assertEqual(src.shard(KEY)["bin"].tolist(), [9])
        self.assertGreater(os.path.getsize(src._path(KEY)), 0)
        other = mapcells.CellKey(14, KEY.x, KEY.y + 1)
        self.assertEqual(src.shard(other), {})
        self.assertFalse(os.path.exists(src._path(other)), "a cell the manifest does not list: nothing asked or kept")
        self.assertEqual(len(asked), 2, "the manifest and the one listed shard")
        self.assertEqual(nm.Source(self.tmp.name, fetcher=None).shard(KEY)["bin"].tolist(), [9], "from disk")

    def test_shards_from_the_host_the_manifest_names(self):
        import json
        import netfetch
        asked = []

        class Site:
            def get(self, url, kind="demand"):
                asked.append(url)
                if url.endswith("/manifest.json"):
                    m = json.loads(manifest_bytes([KEY]))
                    m["data"] = "https://tiles.test/data/nycmassing/v1/k1"
                    return mock.Mock(body=json.dumps(m).encode())
                if url.startswith("https://tiles.test/") and url.endswith(f"/{KEY.y}.npz"):
                    return mock.Mock(body=shard_bytes([record(9)]))
                raise netfetch.NotFound(url)
        src = nm.Source(self.tmp.name, fetcher=Site(), base="https://example.invalid/nyc")
        self.assertEqual(src.shard(KEY)["bin"].tolist(), [9])
        self.assertEqual(asked[1:], [f"https://tiles.test/data/nycmassing/v1/k1/{KEY.z}/{KEY.x}/{KEY.y}.npz"])

    def test_nothing_asked_or_kept_before_publishing(self):
        import netfetch
        asked = []

        class Unpublished:
            def get(self, url, kind="demand"):
                asked.append(url)
                raise netfetch.NotFound(url)
        src = nm.Source(self.tmp.name, fetcher=Unpublished(), base="https://example.invalid/nyc")
        self.assertEqual(src.shard(KEY), {})
        self.assertEqual(src.shard(mapcells.CellKey(14, KEY.x + 1, KEY.y)), {})
        self.assertEqual(asked, ["https://example.invalid/nyc/manifest.json"], "the manifest only, once")
        self.assertFalse(os.path.exists(src._path(KEY)))

    def test_fault_when_not_had(self):
        import revisit
        before = revisit.faults()
        src = nm.Source(self.tmp.name, fetcher=mock.Mock(get=mock.Mock(side_effect=OSError("down"))),
                        base="https://example.invalid/nyc")
        with mock.patch.object(nm, "source", lambda cache_dir: src):
            got = nm.Fetch(CELL, -100, -100, 100, 100, self.tmp.name).index()
        self.assertIsNone(got)
        self.assertGreater(revisit.faults(), before)

    def test_outside_new_york_asks_nothing(self):
        balt = geodesy.EnuFrame(39.28, -76.59, 0.0)
        with mock.patch.object(nm, "source", side_effect=AssertionError("asked")):
            f = nm.Fetch(balt, -500, -500, 500, 500, self.tmp.name)
            self.assertIsNone(f.fut)
            self.assertIsNone(f.index())
        self.assertFalse(nm.credited_near(self.tmp.name, 39.28, -76.59, 2000.0))

    def test_credited_near_a_shard_on_disk(self):
        def get(url, kind="demand"):
            return mock.Mock(body=manifest_bytes([KEY]) if url.endswith("/manifest.json") else shard_bytes([record(3)]))
        src = nm.Source(self.tmp.name, fetcher=mock.Mock(get=get), base="https://example.invalid/nyc")
        src.shard(KEY)
        self.assertTrue(nm.credited_near(self.tmp.name, LAT, LON, 2000.0))


class CreditTest(unittest.TestCase):
    def test_wording_in_credits_md(self):
        import credits
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "CREDITS.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(f'"Buildings: {credits.full_label("bld:nyc-3d")}"', text)
        self.assertTrue(credits.SHORT["bld:nyc-3d"].isascii())


if __name__ == "__main__":
    unittest.main()
