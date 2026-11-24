"""quality.py / earthfast quality.rs: the per-cell data-quality record (the adaptive plan's J2). Shares by road class
and tag come from the cell's own length (clipped at its edges); the 3 x 3 block sums the neighbours; a building far from
any drivable road is found over the neighbours' grids; the record is deterministic and the CIP builder's section
(tools/build_cip.quality_cells) is the same bytes as the server's own making (quality.Source); a stored section of
another version is made again; an empty neighbourhood gives the same bytes everywhere (an archive keeps them once);
the source block reads this server's tables."""
import json
import os
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import fast  # noqa: E402
import mapcells  # noqa: E402
import mapgeom as mg  # noqa: E402
import quality  # noqa: E402

HAVE = getattr(fast._ef, "VERSION", 0) >= quality.NATIVE
KEY = mapcells.CellKey(14, 4705, 6244)


def features(kind, geoms, attrs, own=None):
    """A cell file's arrays: geoms a list of features, each a list of parts, each a list of rings ((k, 2) lon/lat)."""
    n = len(geoms)
    part_off, ring_off, coord_off, ll = [0], [0], [0], []
    for f in geoms:
        for p in f:
            for r in p:
                ll.extend(r)
                coord_off.append(len(ll))
            ring_off.append(len(coord_off) - 1)
        part_off.append(len(ring_off) - 1)
    out = {"ids": mg.text_column([f"{i:08d}-0000-4000-8000-000000000000" for i in range(n)]),
           "kind": np.full(n, kind, np.uint8), "part_off": np.array(part_off, np.int64),
           "ring_off": np.array(ring_off, np.int64), "coord_off": np.array(coord_off, np.int64),
           "lonlat": np.array(ll, np.float64).reshape(-1, 2),
           "own": np.ones(n, bool) if own is None else np.array(own, bool), "clipped": np.zeros(n, bool)}
    for k, v in attrs.items():
        out[f"attr.{k}"] = mg.text_column(v) if isinstance(v[0], str) else np.array(v)
    out["provenance"] = np.array(json.dumps({"id": "test"}))
    return out


def empty(kind=2):
    return features(kind, [], {})


def cell_layers(key, roads=True, buildings=(), lanes=True):
    """Layers of cell key: an east-west primary road across its middle (lanes and name tagged), its buildings at the
    given (fx, fy) fractions of the cell (from the south-west), a residential area over its west half."""
    lon0, lat0, lon1, lat1 = key.bounds()
    mid = 0.5 * (lat0 + lat1)
    out = {ly: empty() for ly in quality.LAYERS}
    if roads:
        out["roads"] = features(2, [[[[(lon0 - 0.001, mid), (lon1 + 0.001, mid)]]],
                                    [[[(lon0 + 0.3 * (lon1 - lon0), mid), (lon0 + 0.3 * (lon1 - lon0), lat1)]]]],
                                {"subtype": ["road", "road"], "class": ["primary", "residential"],
                                 "subclass": ["", ""], "name": ["Main Street", ""],
                                 "src_record": ["w42@1", "w43@1"], "osm_lanes": [2.0, np.nan],
                                 "osm_width": [np.nan, np.nan], "osm_maxspeed": [50.0, np.nan],
                                 "osm_surface": ["asphalt", ""], "osm_sidewalk": ["", ""], "osm_lit": ["", ""],
                                 "osm_smoothness": ["", ""], "osm_oneway": ["", ""]})
    if buildings:
        bs, cx, cy = [], [], []
        for fx, fy in buildings:
            x, y = lon0 + fx * (lon1 - lon0), lat0 + fy * (lat1 - lat0)
            d = 0.0001
            bs.append([[[(x, y), (x + d, y), (x + d, y + d), (x, y + d), (x, y)]]])
            cx.append(x + d / 2)
            cy.append(y + d / 2)
        nb = len(bs)
        out["buildings"] = features(3, bs, {"cx": cx, "cy": cy, "src_dataset": ["OpenStreetMap"] * (nb - 1) + [
            "Microsoft ML Buildings"], "height": np.array([10.0] + [np.nan] * (nb - 1), np.float32),
            "num_floors": np.full(nb, np.nan, np.float32), "roof_shape": ["flat"] + [""] * (nb - 1),
            "roof_color": [""] * nb, "roof_material": [""] * nb, "facade_material": [""] * nb,
            "facade_color": [""] * nb, "has_parts": np.zeros(nb, bool)})
    lu = [[[(lon0, lat0), (0.5 * (lon0 + lon1), lat0), (0.5 * (lon0 + lon1), lat1), (lon0, lat1), (lon0, lat0)]]]
    out["land_use"] = features(3, [lu], {"subtype": ["residential"], "class": ["residential"]})
    return out, (json.dumps({"kind": "Ways", "ways": [["42", {"turn:lanes": "left|through"}]]}).encode()
                 if lanes else None)


class FakeMd:
    """MapData's reads the quality Source makes (layer_arrays, the roads' OSM tag join), from a dict of cells (their
    roads hold their OSM tags already: the join gives them as they are)."""

    def __init__(self, cells, osm="o1"):
        self.cells = cells
        self.cip = None
        self.lane_tags = None
        self.osm = osm

    def layer_arrays(self, key, layer, kind="demand"):
        return self.cells[key][0][layer], True

    def roads_joined_arrays(self, key, arrays, kind="demand"):
        return arrays, True

    def osm_tags_build(self, network=True):
        return self.osm


@unittest.skipUnless(HAVE, "needs earthfast with quality")
class Record(unittest.TestCase):
    def counts_of(self, key, **kw):
        layers, lanes = cell_layers(key, **kw)
        return quality.counts(key, layers, lanes)

    def test_shares_and_fields(self):
        nine = [self.counts_of(k) if k == KEY else None for k in quality.ring(KEY)]
        rec = quality.record(KEY, nine)
        self.assertEqual(len(rec), fast._ef.QUALITY_DATA_LEN)
        f = quality.fields(rec)
        self.assertEqual(f["own.primary.lanes"], 200)           # tagged on all of it
        self.assertEqual(f["own.primary.maxspeed"], 200)
        self.assertEqual(f["own.primary.name"], 200)
        self.assertEqual(f["own.primary.lane_detail"], 200)     # turn:lanes on way 42
        self.assertEqual(f["own.local.lanes"], 0)
        self.assertEqual(f["own.local.lane_detail"], 0)
        self.assertEqual(f["own.primary.parking"], 254)         # not in this release's inputs
        self.assertEqual(f["own.motorway.lanes"], 255)          # no motorway
        lon0, lat0, lon1, lat1 = KEY.bounds()
        width = (lon1 - lon0) * 111194.93 * np.cos(np.radians(0.5 * (lat0 + lat1)))
        self.assertLess(abs(f["own.primary.m"] - width), 2)     # clipped at the cell's edges
        self.assertEqual(f["cells"], 1)
        self.assertEqual(f["flags"] & 1, 0)
        self.assertEqual(f["flags"] & 2, 2)
        self.assertGreater(f["own.sq.builtup"], 1900)           # half of 4096
        self.assertLess(f["own.sq.builtup"], 2200)

    def test_far_buildings_and_neighbours(self):
        # one building by the road, one in the far north-east corner: far from every road unless the neighbour to the
        # east has a road running along its west edge
        bl = [(0.5, 0.52), (0.98, 0.98)]
        nine = [self.counts_of(k, buildings=bl) if k == KEY else None for k in quality.ring(KEY)]
        f = quality.fields(quality.record(KEY, nine))
        self.assertEqual(f["own.buildings"], 2)
        self.assertEqual(f["own.far_buildings"], 1)
        self.assertEqual(f["own.bld.height"], 100)
        self.assertEqual(f["own.bld.ml"], 100)
        east = mapcells.CellKey(14, KEY.x + 1, KEY.y)
        lon0, lat0, lon1, lat1 = east.bounds()
        layers = {ly: empty() for ly in quality.LAYERS}
        x = lon0 + 0.002 * (lon1 - lon0)
        layers["roads"] = features(2, [[[[(x, lat0), (x, lat1)]]]], {
            "subtype": ["road"], "class": ["service"], "subclass": [""], "name": [""], "src_record": [""]})
        nine[5] = quality.counts(east, layers, None)
        f2 = quality.fields(quality.record(KEY, nine))
        self.assertEqual(f2["own.far_buildings"], 0)
        self.assertEqual(f2["cells"], 2)
        self.assertGreater(f2["nb.service.m"], 1000)            # the neighbour's road in the 3 x 3 sums
        self.assertEqual(f2["nb.service.lanes"], 254)           # its OSM tags not joined
        self.assertEqual(f2["own.service.m"], 0)

    def test_deterministic_and_empty_cells_alike(self):
        a = self.counts_of(KEY, buildings=[(0.2, 0.3)])
        self.assertEqual(a, self.counts_of(KEY, buildings=[(0.2, 0.3)]))
        far = mapcells.CellKey(14, 100, 9000)
        recs = []
        for k in (KEY, far):
            e = {ly: empty() for ly in quality.LAYERS}
            nine = [None if nk is None else quality.counts(nk, e, None) for nk in quality.ring(k)]
            recs.append(quality.record(k, nine))
        self.assertEqual(recs[0], recs[1])                      # (a CIP archive keeps such cells once)

    def test_ring_wraps_and_stops_at_the_poles(self):
        r = quality.ring(mapcells.CellKey(14, 0, 0))
        self.assertIsNone(r[0])
        self.assertEqual(r[3], mapcells.CellKey(14, (1 << 14) - 1, 0))

    def test_builder_and_server_agree(self):
        import build_cip
        keys = [mapcells.CellKey(14, KEY.x + dx, KEY.y + dy) for dy in (-1, 0, 1, 2) for dx in (-2, -1, 0, 1, 2)]
        cells = {}
        for i, k in enumerate(keys):
            cells[k] = cell_layers(k, roads=i % 3 != 1, buildings=[(0.1 * (i % 7), 0.9), (0.5, 0.5)], lanes=i % 2 == 0)
        md = FakeMd(cells)

        class LaneTags:     # (a known lane build; slices per cell from the fake cells)
            def known(self):
                return "w1"

        b = build_cip.Builder.__new__(build_cip.Builder)
        b.md, b.layers, b.only, b.quality = md, tuple(quality.LAYERS), None, True
        md.layer_name = lambda ly: ly
        b.layer = lambda key, ly: (ly, cells[key][0][ly])
        built = [k for k in keys if k.y in (KEY.y, KEY.y + 1) and abs(k.x - KEY.x) <= 1]     # (the others: neighbours
                                                                                            # read as layers)

        def made(key):
            return dict({f"m:{ly}": a for ly, a in cells[key][0].items()},
                        **({"t:lanes": cells[key][1]} if cells[key][1] is not None else {})), []
        orig = quality.lanes_slice
        quality.lanes_slice = lambda md_, key, roads: cells[key][1]
        try:
            md.lane_tags = None     # (the builder's own sections say which cells have a slice)
            got = {k: s.get("q") for k, s, _l, why in build_cip.quality_cells(b, built, made)}
            src = quality.Source(md)
            mine = {k: src.data(k) for k in built}
        finally:
            quality.lanes_slice = orig
        self.assertEqual(set(got), set(built))
        for k in built:
            self.assertIsNotNone(got[k], k)
            self.assertEqual(got[k], mine[k], k)
            self.assertEqual(quality.fields(got[k])["cells"], 9)

    def test_stored_section_of_another_version_is_made_again(self):
        nine = [self.counts_of(k) if k == KEY else None for k in quality.ring(KEY)]
        rec = quality.record(KEY, nine)
        self.assertTrue(quality.stored_ok(rec))
        other = bytes([rec[0], rec[1] + 1]) + rec[2:]
        self.assertFalse(quality.stored_ok(other))
        self.assertFalse(quality.stored_ok(rec[:-1]))

        class Reader:
            def one(self, key, name, kind="demand"):
                return other if name == "q" else None

            def data_builds(self):
                return {"osmtags": "o1"}
        cells = {k: cell_layers(k) for k in quality.ring(KEY)}
        md = FakeMd(cells)
        md.cip = Reader()
        orig = quality.lanes_slice
        quality.lanes_slice = lambda md_, key, roads: cells[key][1]
        try:
            src = quality.Source(md)
            got = src.data(KEY)
        finally:
            quality.lanes_slice = orig
        self.assertEqual(src.stats["made"], 1)
        self.assertEqual(quality.fields(got)["cells"], 9)

    def test_stored_section_of_another_osm_tags_build_is_made_again(self):
        """A CIP build's "q" counted with other OSM road tags than the run's (the roads' tags are joined at the
        decode, from the run's build: mapdata roads 10) is not read: the record is made from the run's layers."""
        nine = [self.counts_of(k) if k == KEY else None for k in quality.ring(KEY)]
        rec = quality.record(KEY, nine)
        asked = []

        class Reader:
            def one(self, key, name, kind="demand"):
                asked.append(name)
                return rec if name == "q" else None

            def data_builds(self):
                return {"osmtags": "o-older"}
        cells = {k: cell_layers(k) for k in quality.ring(KEY)}
        md = FakeMd(cells)
        md.cip = Reader()
        orig = quality.lanes_slice
        quality.lanes_slice = lambda md_, key, roads: cells[key][1]
        try:
            src = quality.Source(md)
            got = src.data(KEY)
            self.assertEqual((src.stats["stored"], src.stats["made"]), (0, 1))
            self.assertNotIn("q", asked)
            md.osm = "o-older"          # (the run's build the one it was counted with: read)
            self.assertEqual(quality.Source(md).data(KEY), rec)
        finally:
            quality.lanes_slice = orig
        self.assertEqual(quality.fields(got)["cells"], 9, "made from the run's 3 x 3 cells")

    def test_cip_puts_the_record_first(self):
        import cip
        nine = [self.counts_of(k) if k == KEY else None for k in quality.ring(KEY)]
        rec = quality.record(KEY, nine)
        cell = cip.encode_cell(KEY, {"m:roads.9": cell_layers(KEY)[0]["roads"], "q": rec})
        self.assertEqual([n for n, _c, _l in cip.cell_sections(cell)], ["q", "m:roads.9"])
        self.assertEqual(cip.decode_cell(KEY, cell, ["q"])["q"], rec)

    def test_handed_over_columns_cover_the_kernels_reads(self):
        import re
        rs = (Path(__file__).resolve().parents[2] / "native" / "earthfast" / "src" / "quality.rs").read_text()
        body = rs.split("mod tests")[0]
        named = set(re.findall(r'"((?:attr|rule)\.[a-z_.]+)"', body))
        named |= {f"rule.{n}.feature" for n in re.findall(r'rule_rows\("([a-z_]+)"', body)}
        named |= set(re.findall(r'\.(?:ints|floats|strs|nonempty)\("([a-z_]+)"\)', body))
        named.discard("ids")
        self.assertEqual(named - quality.KEEP, set())
        layers, lanes = cell_layers(KEY, buildings=[(0.2, 0.3)])
        full = fast._ef.quality_counts(KEY.z, KEY.x, KEY.y, {ly: __import__("cip").arrays_section(
            {k: v for k, v in a.items() if k != "provenance"}) for ly, a in layers.items()}, lanes)
        self.assertEqual(full, quality.counts(KEY, layers, lanes))

    def test_source_block(self):
        m = quality.meta(KEY)                   # Fells Point: Maryland's six-inch photos, NAIP, USGS, EOX
        self.assertEqual(len(m), fast._ef.QUALITY_META_LEN)
        nine = [self.counts_of(k) if k == KEY else None for k in quality.ring(KEY)]
        f = quality.fields(quality.record(KEY, nine) + m)
        self.assertEqual(f["img.zoom"], 17)
        self.assertEqual(f["img.flags"] & 2, 0)                 # photos at 1 m: no generated ground
        self.assertEqual(f["roadz.bits"] & 1, 1)                # the US lidar's bridges
        self.assertIn(f["dem.src"], (1, 2))
        sea = quality.meta(mapcells.CellKey(14, 2000, 9000))    # the South Atlantic: Sentinel-2 only
        f = quality.fields(quality.record(KEY, nine) + sea)
        self.assertEqual(f["img.zoom"], 14)
        self.assertEqual(f["img.flags"] & 2, 2)


if __name__ == "__main__":
    unittest.main()
