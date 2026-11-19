"""OSM building and part colours (tools/build_osm_building_colours.py -> PMTiles parts; server/osmcolours.py + earthfast
buildcolours.rs read a tile; buildings.tile_buildings fills facade_color / roof_color where Overture's is empty): the
builder's colour parse on the values OSM has most, the blob decoded and damaged blobs refused, a small build read back
through a fake host (a building across a tile edge, relations' keys, the margin round a tile), the Plaza's roofs green
from the data, a colour Overture kept winning, the building tiles' keys naming the build, and nothing changed (no
faults) while it is not published.

No network: the builds are made here from rows (build_rows) and served by a fake fetcher; the planet passes
(read_planet) are tested on a small OSM XML file when pyosmium is installed."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[2] / "tools"), str(HERE.parent)]
import build_osm_building_colours as bc  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import netfetch  # noqa: E402
import osmcolours  # noqa: E402
import revisit  # noqa: E402
from test_osm_building_years import FakeFetcher  # noqa: E402

try:
    import osmium
except ImportError:
    osmium = None

BASE = "https://colours.test/osmcolours"
LAT0, LON0 = 40.76456, -73.97381        # the Plaza


def box(lon, lat, d=0.0003):
    return (lon - d, lat - d, lon + d, lat + d)


def z12_edge_lon(lon=LON0):
    n = 1 << bc.SHARD_ZOOM
    return int((lon + 180.0) / 360.0 * n) / n * 360.0 - 180.0


EDGE = z12_edge_lon()
GREEN, BEIGE, LIGHTGRAY, GOLD = 0x006400, 0xf5f5dc, 0xd3d3d3, 0xffd700
ROWS = [  # (key, facade, roof, bbox)
    (2 * 703143170, -1, GREEN, box(LON0, LAT0)),                       # a Plaza part
    (2 * 9771573 + 1, BEIGE, -1, box(LON0, LAT0, 0.001)),              # a relation (the Plaza's outline)
    (2 * 9771573, -1, GOLD, box(LON0 + 0.002, LAT0)),                  # the same number as a way
    (2 * 5, LIGHTGRAY, LIGHTGRAY, (EDGE - 0.001, LAT0, EDGE + 0.001, LAT0 + 0.001)),   # across a tile edge
    (2 * 6, 0x123456, -1, box(EDGE - 0.004, LAT0 + 0.002)),            # west of the edge, 340 m from it
    (2 * 9, 0xabcdef, 0x000000, box(LON0 + 0.4, LAT0 + 0.3)),          # far off, another tile
] + [(2 * (100 + k), 0x010101 * k, -1 if k % 3 else 0x808080, box(LON0 + 0.001 * k, LAT0 - 0.003)) for k in range(30)]
WANT = {k: (f, r) for k, f, r, _b in ROWS}


def build(out, rows=ROWS, **kw):
    rows = sorted(rows)
    return bc.build_rows([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows],
                         out, source="test", log=lambda s: None, **kw)


class ColourParseTest(unittest.TestCase):
    def test_values_osm_has_most(self):
        cases = {"DarkGreen": GREEN, "darkgreen": GREEN, "dark_green": GREEN, "Dark Green": GREEN,
                 "light_gray": LIGHTGRAY, "lightgrey": LIGHTGRAY, "light grey": LIGHTGRAY, "Light-Gray": LIGHTGRAY,
                 "dark-grey": 0xa9a9a9, "darkgray": 0xa9a9a9, "beige": BEIGE, "Gold": GOLD, "lightblue": 0xadd8e6,
                 "grey": 0x808080, "White": 0xffffff, "lightsteelblue": 0xb0c4de, "rebeccapurple": 0x663399,
                 "#FFF": 0xffffff, "#abc": 0xaabbcc, "#abc8": 0xaabbcc, "#9ccbe3": 0x9ccbe3, "#D1BBAE": 0xd1bbae,
                 "#11223344": 0x112233, "8c9293": 0x8c9293, " #999999 ": 0x999999, "white;blue": 0xffffff,
                 "beige;brown": BEIGE, "cream": 0xfffdd0, "terracotta": 0xe2725b, "Brick": 0xb22222,
                 "anthracite": 0x383b40,
                 "#70c2dcb": None, "brick red": None, "black_with_white_lines": None, "1": None, "": None,
                 "#": None, "#ggg": None, "#12345": None, "facades": None, "rgb(1,2,3)": None, None: None}
        self.assertEqual({k: bc.parse_colour(k) for k in cases}, cases)

    def test_css_names_complete(self):
        self.assertEqual(len(bc.CSS_COLOURS), 148)
        try:
            import matplotlib.colors as mc
        except ImportError:
            self.skipTest("matplotlib not installed")
        self.assertEqual(bc.CSS_COLOURS, {k: int(v[1:], 16) for k, v in mc.CSS4_COLORS.items()})

    def test_names_draw_as_the_server_draws_the_name(self):
        """A filled colour is the name's hex: the server draws it as it draws the name (buildings.parse_color): the
        names Overture dropped in Manhattan, and every other name the server knows that CSS does not have."""
        import buildings as bld
        css = set(bc.CSS_COLOURS)
        extra = {k for k in bld.COLOR_NAMES if k not in css and k.replace("_", "") not in css}
        self.assertEqual(set(bc.EXTRA_COLOURS), extra, "the builder's extra names are the server's")
        for name in sorted(extra | {"beige", "lightgray", "darkgray", "darkgreen", "gold", "lightblue", "lightgrey",
                                    "white", "grey", "brown", "red", "maroon", "silver", "tan", "pink"}):
            want = bld.parse_color(name)
            got = bld.parse_color(osmcolours.hex_of(bc.parse_colour(name)))
            self.assertIsNotNone(want, name)
            np.testing.assert_allclose(got, want, atol=0.5 / 255 + 1e-9, err_msg=name)

    def test_only_buildings_and_parts_with_a_colour(self):
        unread = Counter()
        self.assertEqual(bc.colours_of({"building": "yes", "roof:colour": "DarkGreen"}, unread), (-1, GREEN))
        self.assertEqual(bc.colours_of({"building:part": "yes", "building:colour": "beige", "roof:colour": "x"},
                                       unread), (BEIGE, -1))
        self.assertIsNone(bc.colours_of({"building": "no", "roof:colour": "red"}, unread))
        self.assertIsNone(bc.colours_of({"amenity": "bench", "roof:colour": "red"}, unread))
        self.assertIsNone(bc.colours_of({"building": "yes", "roof:colour": "brick red"}, unread))
        self.assertIsNone(bc.colours_of({"building": "yes", "name": "x"}, unread))
        self.assertEqual(unread, Counter({"x": 1, "brick red": 1}))

    def test_keys(self):
        self.assertEqual(bc.key_of("way", 703143170), osmcolours.key_of("w703143170@1"))
        self.assertEqual(bc.key_of("relation", 9771573), osmcolours.key_of("r9771573@2"))


class DecodeTest(unittest.TestCase):
    def setUp(self):
        fast.require(osmcolours.NATIVE, "obc_decode")

    def test_round_trip_raw_and_deflated(self):
        keys = np.array([4, 10, 11, 2 ** 40], np.uint64)
        fac = np.array([0x123456, -1, 0x000000, 0xffffff], np.int64)
        roof = np.array([-1, 0x654321, 0x123456, 0xffffff], np.int64)
        blob = bc.encode_tile(keys, fac, roof)
        for b in (blob, bc.ots.deflate(blob)):
            k, f, r = fast._ef.obc_decode(b)
            self.assertEqual((k.dtype, f.dtype, r.dtype), (np.uint64, np.int32, np.int32))
            self.assertEqual((k.tolist(), f.tolist(), r.tolist()), (keys.tolist(), fac.tolist(), roof.tolist()))
        self.assertEqual(blob.count(bytes([0x12, 0x34, 0x56])), 1, "a colour once in the palette")

    def test_empty(self):
        k, f, r = fast._ef.obc_decode(bc.encode_tile([], [], []))
        self.assertEqual((len(k), len(f), len(r)), (0, 0, 0))

    def test_allocation_bombs_raise_before_allocating(self):
        """A tiny deflated tile claiming tens of millions of rows (Codex review of 9f1bcf84: 62 KB for 32 M rows of
        one-byte deltas and no colours, 512 MB asked for) is refused before its rows are allocated; so is a count past
        MAX_ROWS (2 M), and a row with neither colour."""
        import resource
        import zlib
        n = (32 << 20) - 8      # (inside the old 64 MiB cap: 9f1bcf84's module decoded these 65 KB, +608 MB peak)
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        z = [c.compress(b"OBC1" + bc.ots.uv1(n))]
        one, zero = b"\x01" * (1 << 20), bytes(1 << 20)
        z += [c.compress(one) for _ in range(31)] + [c.compress(one[:n - (31 << 20)])]
        z += [c.compress(zero) for _ in range(31)] + [c.compress(zero[:n - (31 << 20)])]
        z = b"".join(z) + c.compress(b"\x00") + c.flush()
        self.assertLess(len(z), 200_000)
        rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        with self.assertRaisesRegex(ValueError, "inflates past"):
            fast._ef.obc_decode(z)
        n = 2_000_001
        tile = b"OBC1" + bc.ots.uv1(n) + b"\x01" * n + bytes([bc.FACADE]) * n + b"\x01\x01\x02\x03" + bytes(n)
        with self.assertRaisesRegex(ValueError, "rows, past"):
            fast._ef.obc_decode(bc.ots.deflate(tile))
        self.assertLess((resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0) / 1024, 64, "no rows allocated")
        with self.assertRaisesRegex(ValueError, "no colour"):
            fast._ef.obc_decode(b"OBC1" + bc.ots.uv1(2) + b"\x01\x01" + bytes([bc.FACADE, 0]) + b"\x01\x01\x02\x03\x00")
        with self.assertRaises(ValueError):
            bc.encode_tile([4], [-1], [-1])

    def test_damaged_tiles_raise(self):
        blob = bc.encode_tile(np.array([4, 10], np.uint64), np.array([0x123456, -1]), np.array([-1, 0x654321]))
        bad_flag = bytearray(blob)
        bad_flag[7] |= 4
        for bad in (b"", b"OBC1", blob[:-1], b"OBY1" + blob[4:], bc.ots.deflate(blob)[:-3],
                    b"OBC1" + bc.ots.uv1(1 << 40), bytes(bad_flag), blob[:-1] + b"\x07",
                    b"OBC1" + bytes((16 << 20) + 1)):
            with self.assertRaises(ValueError):
                fast._ef.obc_decode(bad)


class BuildAndReadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fast.require(osmcolours.NATIVE, "obc_decode")
        cls.tmp = tempfile.mkdtemp(prefix="obc-test-")
        cls.out = os.path.join(cls.tmp, "out")
        with mock.patch.object(bc.ots, "PART_SLACK", 400):      # (parts of a tile or two: several here)
            cls.man = build(cls.out, part_bytes=600)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files(self, out=None, man=None):
        out, man = out or self.out, man or self.man
        got = {}
        for p in man["parts"]:
            with open(os.path.join(out, p["archive"]), "rb") as f:
                got[f"{BASE}/{p['archive']}"] = f.read()
        with open(os.path.join(out, "manifest.json"), "rb") as f:
            got[f"{BASE}/manifest.json"] = f.read()
        return got

    def md(self, files=None):
        """A map data whose colour manifest is pinned on disk (MapData.warm_manifests' fetch), not yet frozen."""
        md = SimpleNamespace(fetcher=FakeFetcher(self.files() if files is None else files),
                             cache_dir=tempfile.mkdtemp(dir=self.tmp))
        osmcolours.shards(md).version()
        return md

    def test_manifest(self):
        m = self.man
        self.assertEqual((m["format"], m["zoom"], m["facade"], m["roof"]),
                         (1, 12, sum(f >= 0 for f, _ in WANT.values()), sum(r >= 0 for _, r in WANT.values())))
        self.assertGreaterEqual(len(m["parts"]), 2)
        self.assertRegex(m["key"], r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
        self.assertEqual([p["archive"] for p in m["parts"]],
                         [f"{m['key']}/osmcolours-{i:03d}.pmtiles" for i in range(len(m["parts"]))])
        self.assertIsNotNone(osmcolours.osmyears.pinned_parts(m))

    def test_every_row_read_back(self):
        with mock.patch.object(osmcolours, "BASE", BASE):
            sh = osmcolours.shards(self.md())
            got = osmcolours.osmyears.tiles_of(sh, LAT0 - 0.01, LON0 - 0.04, LAT0 + 0.31, LON0 + 0.41,
                                               osmcolours._EMPTY)
        self.assertEqual({k: (f, r) for k, f, r in zip(*(c.tolist() for c in got))}, WANT)
        self.assertEqual(got[0].tolist(), sorted(got[0].tolist()))

    def test_the_tile_margin_reaches_the_next_tile(self):
        """A tile 100 m east of a z12 edge takes the rows of the tile west of it within MARGIN_M (a building across
        the edge has its parts there)."""
        frame = geodesy.EnuFrame(LAT0, EDGE + 0.0012 + 0.0015)     # (its west side ~100 m east of the edge)
        with mock.patch.object(osmcolours, "BASE", BASE):
            md = self.md()
            got = osmcolours.Fetch(frame, -128, -128, 128, 128, md).colours()
            with mock.patch.object(osmcolours.Fetch, "MARGIN_M", 0.0):
                bare = osmcolours.Fetch(frame, -128, -128, 128, 128, md).colours()
        self.assertIn(2 * 6, got[0].tolist(), "the row west of the edge")
        self.assertNotIn(2 * 6, bare[0].tolist(), "(not without the margin)")

    def test_fetch_and_index(self):
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmcolours, "BASE", BASE):
            md = self.md()
            n0 = revisit.faults()
            idx = osmcolours.Index(osmcolours.Fetch(frame, -300, -300, 300, 300, md).colours())
            self.assertEqual(revisit.faults(), n0)
            self.assertEqual(osmcolours.identity(md), self.man["built"])
        self.assertEqual(idx.lookup(["w703143170@1", "r9771573@2", "w9771573@3", "w999@1", None, "n1@1", ""]),
                         [(None, "#006400"), ("#f5f5dc", None), (None, "#ffd700"), (None, None), (None, None),
                          (None, None), (None, None)])
        self.assertEqual(idx.fill({"roof_color": "", "facade_color": None}, "w703143170@1"),
                         {"roof_color": "#006400", "facade_color": None})
        kept = {"roof_color": "#ff0000"}
        self.assertIs(idx.fill(kept, "w703143170@1"), kept, "a colour Overture kept wins")
        self.assertEqual(kept, {"roof_color": "#ff0000"})

    def test_not_published_no_fault(self):
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmcolours, "BASE", BASE):
            md = self.md(files={})
            n0 = revisit.faults()
            self.assertIsNone(osmcolours.Fetch(frame, -300, -300, 300, 300, md).colours())
            self.assertEqual(revisit.faults(), n0, "nothing published: the buildings as before, kept")
            self.assertEqual(osmcolours.identity(md), "none")

    def test_unreachable_tiles_fault(self):
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmcolours, "BASE", BASE):
            md = self.md()
            md.fetcher.fail = netfetch.FetchFailed("offline")
            n0 = revisit.faults()
            self.assertIsNone(osmcolours.Fetch(frame, -300, -300, 300, 300, md).colours())
            self.assertEqual(revisit.faults(), n0 + 1, "built again later, with the colours")

    def test_a_frozen_build_never_reads_another_under_its_name(self):
        rows = [r if r[0] != 2 * 703143170 else (r[0], -1, 0xff0000, r[3]) for r in ROWS]
        import time as _t
        _t.sleep(1.1)                               # (another build time: another key)
        out_b = os.path.join(self.tmp, "out_b")
        with mock.patch.object(bc.ots, "PART_SLACK", 400):
            man_b = build(out_b, rows, part_bytes=600)
        fb = self.files(out_b, man_b)
        frame = geodesy.EnuFrame(LAT0, LON0)
        files = self.files()
        with mock.patch.object(osmcolours, "BASE", BASE):
            md = self.md(files)
            self.assertEqual(osmcolours.identity(md), self.man["built"])
            for a, b in zip([p["archive"] for p in self.man["parts"]], [p["archive"] for p in man_b["parts"]]):
                files[f"{BASE}/{a}"] = fb[f"{BASE}/{b}"]
            n0 = revisit.faults()
            self.assertIsNone(osmcolours.Fetch(frame, -300, -300, 300, 300, md).colours())
            self.assertEqual(revisit.faults(), n0 + 1)

    def test_off(self):
        with mock.patch.object(osmcolours, "ON", False):
            md = SimpleNamespace(fetcher=FakeFetcher(self.files()), cache_dir=self.tmp)
            self.assertIsNone(osmcolours.shards(md))
            self.assertEqual(osmcolours.identity(md), "off")
            self.assertIsNone(osmcolours.Fetch(geodesy.EnuFrame(LAT0, LON0), -300, -300, 300, 300, md).colours())

    def test_building_tile_keys_name_the_colours(self):
        """The tiles that draw buildings are keyed on the colours' build (blockworker._md_versions(True),
        MapData.versions(buildings=True)); the others, plans and cell facts not; decisions leave it out of the vkey
        (a weekly dataset)."""
        import blockworker
        import decisions
        import mapdata
        with mock.patch.object(osmcolours, "BASE", BASE):
            md = self.md()
            md.versions = lambda: {"pipeline": 1}
            with mock.patch.object(blockworker, "_s", SimpleNamespace(md=md)):
                self.assertEqual(blockworker._md_versions(), {"pipeline": 1})
                self.assertEqual(blockworker._md_versions(True)["osmcolours"], self.man["built"])
            v = mapdata.MapData.versions(SimpleNamespace(versions=lambda: {"pipeline": 1}, fetcher=md.fetcher,
                                                         cache_dir=md.cache_dir), buildings=True)
            self.assertEqual((v["osmcolours"], v["pipeline"]), (self.man["built"], 1))
            self.assertIn("osmyears", v)
        off = SimpleNamespace(fetcher=None, cache_dir=self.tmp, versions=lambda: {"pipeline": 1})
        with mock.patch.object(blockworker, "_s", SimpleNamespace(md=off)):
            self.assertEqual(blockworker._md_versions(True)["osmcolours"], "off")
        self.assertIn("osmcolours", decisions.WEEKLY)


def colours_of(*rows):
    """An osmcolours.Fetch stand-in: [(src_record, facade, roof)] (-1: none)."""
    kv = sorted((osmcolours.key_of(r), f, c) for r, f, c in rows)
    return SimpleNamespace(colours=lambda: (np.array([k for k, _, _ in kv], np.uint64),
                                            np.array([f for _, f, _ in kv], np.int32),
                                            np.array([c for _, _, c in kv], np.int32)))


class PlazaTest(unittest.TestCase):
    """The Plaza from its real Overture rows (fixtures/landmark_parts.json: roof_color empty on every part, Overture
    dropped OSM's roof:colour=DarkGreen on 22 of them) with OSM's colours as the dataset has them (way 703143170 ...,
    the Manhattan build from Geofabrik's New York extract, 2026-11-14)."""

    @classmethod
    def setUpClass(cls):
        from test_building_parts_rules import FIXTURE
        cls.data = json.loads(FIXTURE.read_text())

    def _items(self, name, rec=None, **kw):
        import buildings as bld
        from test_building_parts_rules import layers_of
        rec = rec or self.data[name]
        f = geodesy.EnuFrame(rec["lat"], rec["lon"])
        with mock.patch.object(bld, "TOWN_COVERAGE", -1.0):
            return bld.tile_buildings(f, -256, -256, 256, 256, [layers_of(rec)[0]],
                                      lambda la, lo: np.zeros(np.shape(la)), part_layers=[layers_of(rec)[1]], **kw)

    def _green_parts(self, rec):
        return [p for p in rec["parts"] if (p["roof_shape"] == "skillion" and p["roof_material"] == "roof_tiles")
                or (p["roof_shape"] == "gabled" and p["roof_height"] == 10.0) or p["roof_shape"] == "dome"]

    def _osm(self, rec):
        """OSM's colours of the Plaza's 22 green-roofed parts (the build's rows: roof DarkGreen, no facade)."""
        return [(p["src_record"], -1, GREEN) for p in self._green_parts(rec)]

    def test_roofs_green_from_the_data(self):
        import buildings as bld
        from test_building_parts_rules import base
        rec = self.data["plaza"]
        self.assertEqual(sorted(int(p["src_record"][1:].split("@")[0]) for p in self._green_parts(rec))[0], 703143170)
        self.assertFalse([p for p in rec["parts"] if p.get("roof_color")], "Overture: none")
        items = self._items("plaza", colours=colours_of(*self._osm(rec)))
        is_green = [(lambda c: c[1] > c[0] + 10 and c[1] > c[2])(bld._sky_colour(s.roof_mat, 1.0)) for s, _, _ in items]
        green = [s for (s, _, _), g in zip(items, is_green) if g]
        self.assertEqual(sorted(s.roof for s in green), ["dome"] * 2 + ["gabled"] * 2 + ["skillion"] * 18)
        self.assertFalse([s.roof_mat for (s, _, _), g in zip(items, is_green) if base(s.roof_mat) == bld.ROOF_TILES
                          and not g], "no terracotta left")
        # (the same as Overture's own colour on them would draw: the filled hex is the name's)
        direct = json.loads(json.dumps(rec))
        for p in self._green_parts(direct):
            p["roof_color"] = "DarkGreen"
        self.assertEqual(bld.build(items)[2].tobytes(), bld.build(self._items("plaza", rec=direct))[2].tobytes())

    def test_a_colour_overture_kept_wins(self):
        import buildings as bld
        rec = json.loads(json.dumps(self.data["plaza"]))
        for p in self._green_parts(rec):
            p["roof_color"] = "#FF0000"
        items = self._items("plaza", rec=rec, colours=colours_of(*self._osm(rec)))
        self.assertFalse([s for s, _, _ in items if (lambda c: c[1] > c[0] + 10 and c[1] > c[2])(
            bld._sky_colour(s.roof_mat, 1.0))], "Overture's red, not OSM's green")

    def test_the_buildings_colour_reaches_its_bare_parts(self):
        """A colour filled on the building (its relation's facade: none of its parts has one) is every part's, as a
        mapped one is (p_fcol: the part's own, else the building's); one part's own colour is its alone."""
        import buildings as bld
        rec = self.data["plaza"]
        blue = lambda s: (lambda c: c[2] > c[0] + 0.15 and c[2] > c[1] + 0.15)(bld._sky_colour(s.facade, 1.0))  # noqa
        plain = self._items("plaza")
        self.assertFalse([s for s, _, _ in plain if blue(s)])
        dyed = self._items("plaza", colours=colours_of((rec["building"]["src_record"], 0x0000cd, -1)))
        self.assertEqual(len(dyed), len(plain))
        self.assertGreaterEqual(sum(blue(s) for s, _, _ in dyed), 50, "its 50 parts drawn")
        self.assertFalse([(a.facade, b.facade) for (a, _, _), (b, _, _) in zip(plain, dyed)
                          if not blue(b) and a.facade != b.facade],
                         "the rest (its 1 m roof deck part, a plain slab by its own rule) as before")
        one = self._items("plaza", colours=colours_of((rec["parts"][0]["src_record"], 0x0000cd, -1)))
        self.assertEqual(sum(blue(s) for s, _, _ in one), 1)

    def test_none_changes_nothing(self):
        """No data (not published: Fetch.colours() None) or rows of other buildings: the same tiles as without."""
        import buildings as bld
        for name in ("plaza", "empire_state", "capitol"):
            a = bld.build(self._items(name))
            for c in (SimpleNamespace(colours=lambda: None), colours_of(("w1@1", 0x123456, 0x654321))):
                b = bld.build(self._items(name, colours=c))
                self.assertEqual((a[0].tobytes(), a[2].tobytes()), (b[0].tobytes(), b[2].tobytes()), name)


@unittest.skipIf(osmium is None, "pyosmium not installed (the data repo's job has it)")
class PlanetReadTest(unittest.TestCase):
    """read_planet's three passes over a small OSM file: ways, a relation placed by its member ways (an untagged outer
    and inner), nodes only some ways use, objects without a colour or no building left out."""

    XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
  <node id="1" lat="40.7640" lon="-73.9740" version="1"/>
  <node id="2" lat="40.7640" lon="-73.9730" version="1"/>
  <node id="3" lat="40.7650" lon="-73.9730" version="1"/>
  <node id="4" lat="40.7650" lon="-73.9740" version="1"/>
  <node id="5" lat="40.7700" lon="-73.9800" version="1"/>
  <node id="6" lat="40.7700" lon="-73.9790" version="1"/>
  <node id="7" lat="40.7710" lon="-73.9790" version="1"/>
  <node id="8" lat="40.7702" lon="-73.9798" version="1"/>
  <node id="9" lat="40.7702" lon="-73.9795" version="1"/>
  <node id="10" lat="40.7705" lon="-73.9795" version="1"/>
  <way id="703143170" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building:part" v="yes"/><tag k="roof:colour" v="DarkGreen"/></way>
  <way id="11" version="1"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="5"/></way>
  <way id="12" version="1"><nd ref="8"/><nd ref="9"/><nd ref="10"/><nd ref="8"/></way>
  <way id="13" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="1"/>
    <tag k="building" v="yes"/><tag k="roof:colour" v="no such colour"/></way>
  <way id="14" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="1"/>
    <tag k="shelter" v="yes"/><tag k="roof:colour" v="red"/></way>
  <relation id="9771573" version="1">
    <member type="way" ref="11" role="outer"/><member type="way" ref="12" role="inner"/>
    <tag k="type" v="multipolygon"/><tag k="building" v="hotel"/><tag k="building:colour" v="beige"/>
    <tag k="roof:colour" v="light_gray"/></relation>
</osm>
"""

    def test_three_passes(self):
        tmp = tempfile.mkdtemp(prefix="obc-planet-")
        try:
            p = os.path.join(tmp, "x.osm")
            with open(p, "w") as f:
                f.write(self.XML)
            keys, fac, roof, bb, unread = bc.read_planet(p, log=lambda s: None)
            self.assertEqual(keys.tolist(), [2 * 9771573 + 1, 2 * 703143170])
            self.assertEqual((fac.tolist(), roof.tolist()), ([BEIGE, -1], [LIGHTGRAY, GREEN]))
            np.testing.assert_allclose(bb[0], (-73.980, 40.770, -73.979, 40.771), err_msg="its outer way's box")
            np.testing.assert_allclose(bb[1], (-73.974, 40.764, -73.973, 40.765))
            self.assertEqual(unread, Counter({"no such colour": 1}))
            man = bc.build(p, os.path.join(tmp, "out"), log=lambda s: None)
            self.assertEqual((man["rows"], man["facade"], man["roof"], man["unread"]), (2, 1, 2, 1))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
