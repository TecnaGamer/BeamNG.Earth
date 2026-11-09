"""Germany's surveyed roads (germanroads.py): the official axes' width, lanes and surface taken where the map says
nothing, matched by place and direction; the map's own values win; nothing outside the shards; and the shard format
tools/build_de_roads.py writes."""
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools"))
import germanroads  # noqa: E402
import mapgeom  # noqa: E402
import roads  # noqa: E402
import build_de_roads  # noqa: E402

LAT, LON = 49.30, 7.05          # Saarland
M_LAT = 110540.0
M_LON = 111320.0 * np.cos(np.radians(LAT))


def ll(e, n):
    return LON + e / M_LON, LAT + n / M_LAT


def rules(entries, kind="text"):
    if not entries:
        return mapgeom.RuleTable(np.zeros(0, np.int32), np.zeros(0, "<U8"), np.zeros(0, np.float32),
                                 np.zeros(0, np.float32))
    return mapgeom.RuleTable(np.array([e[0] for e in entries], np.int32),
                             np.array([e[1] for e in entries], np.float32 if kind == "float" else "<U32"),
                             np.zeros(len(entries), np.float32), np.ones(len(entries), np.float32))


def features(lines, classes, oneway=(), widths=(), osm=None):
    coords, part_off, ring_off, coord_off = [], [0], [0], [0]
    for ln in lines:
        coords += [ll(e, n) for e, n in ln]
        coord_off.append(len(coords))
        ring_off.append(len(coord_off) - 1)
        part_off.append(len(ring_off) - 1)
    n = len(lines)
    attrs = {"class": np.array(classes), "subtype": np.array(["road"] * n)}
    for k, v in (osm or {}).items():
        attrs[k] = np.array(v, dtype=object)
    return mapgeom.Features(
        layer="roads", ids=np.array([f"id{i:03d}" for i in range(n)]), kind=np.full(n, mapgeom.LINE, np.uint8),
        part_off=np.array(part_off, np.int64), ring_off=np.array(ring_off, np.int64),
        coord_off=np.array(coord_off, np.int64), lonlat=np.array(coords, np.float64), attrs=attrs,
        rules={"surface": rules([]), "width": rules(list(widths), "float"), "level": rules([], "float"),
               "flags": rules([]), "oneway": rules([(i, "1") for i in oneway])})


def write_shard(folder, axes):
    """axes: [(points [(e, n)], width m, lanes, surf, cls, flags)] -> folder/<x>/<y>.npz as build_de_roads writes."""
    off, lat, lon, attrs = [], [], [], []
    for pts, width, lanes, surf, cls, flags in axes:
        off.append(len(lat))
        for e, n in pts:
            x, y = ll(e, n)
            lat.append(round(y * 1e5))
            lon.append(round(x * 1e5))
        attrs.append((int(width * 10), lanes, surf, cls, flags, 0))
    a = np.array(attrs, np.int32)
    x, y = germanroads.tile_of(LAT, LON)
    os.makedirs(os.path.join(folder, str(x)), exist_ok=True)
    np.savez_compressed(os.path.join(folder, str(x), f"{y}.npz"), off=np.array(off + [len(lat)], np.int32),
                        dlat=np.diff(np.array(lat, np.int64), prepend=0).astype(np.int32),
                        dlon=np.diff(np.array(lon, np.int64), prepend=0).astype(np.int32),
                        width=a[:, 0].astype(np.uint16), lanes=a[:, 1].astype(np.uint8), surf=a[:, 2].astype(np.uint8),
                        cls=a[:, 3].astype(np.uint8), flags=a[:, 4].astype(np.uint8), level=a[:, 5].astype(np.int8))


EAST = [(-400.0, 0.0), (400.0, 0.0)]


class GermanRoadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        germanroads._state["shards"].clear()
        p = mock.patch.object(germanroads, "LOCAL", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(germanroads._state["shards"].clear)

    def specs(self, feats):
        return roads._road_specs(feats, None, False)

    def test_surveyed_width_and_lanes_replace_the_class_guess(self):
        write_shard(self.tmp.name, [(EAST, 6.5, 2, 1, 4, 0)])          # a Kreisstraße, 6.5 m, two lanes
        before = roads.carriageway("secondary", "paved", False)
        sp = self.specs(features([[(-300.0, 2.0), (300.0, 2.5)]], ["secondary"]))[0]
        self.assertNotEqual(before, 6.5)
        self.assertEqual(sp.width, 6.5)
        self.assertEqual(sp.lanes, 2)

    def test_axes_of_many_segments(self):
        bent = [(-400.0, 0.0), (-200.0, 1.0), (0.0, 0.0), (200.0, 1.0), (400.0, 0.0)]
        write_shard(self.tmp.name, [(bent, 8.0, 2, 1, 3, 0),
                                    ([(-400.0, 60.0), (-200.0, 61.0), (0.0, 60.0), (400.0, 60.0)], 5.0, 2, 1, 5, 0),
                                    ([(-400.0, -60.0), (0.0, -60.0), (400.0, -60.0)], 0.0, 4, 1, 2,
                                     germanroads.DIVIDED_AXIS)])
        f = features([[(-300.0, 0.5), (300.0, 0.5)], [(-300.0, 60.5), (300.0, 60.5)]], ["primary", "residential"])
        a, b = self.specs(f)
        self.assertEqual((a.width, a.lanes), (8.0, 2))
        self.assertEqual((b.width, b.lanes), (5.0, None))     # (a town street: its width, not the survey's lanes)

    def test_driveways_and_ramps(self):
        write_shard(self.tmp.name, [(EAST, 4.0, 1, 6, 1, 0)])
        f = features([[(-300.0, 0.0), (300.0, 0.0)]], ["service"])
        self.assertEqual(self.specs(f)[0].width, roads.carriageway("service", "paved", False))

    def test_a_mapped_width_and_osm_lanes_win(self):
        write_shard(self.tmp.name, [(EAST, 6.5, 2, 1, 4, 0)])
        f = features([[(-300.0, 0.0), (300.0, 0.0)]], ["secondary"], widths=[(0, 9.0)],
                     osm={"osm_lanes": ["3"]})
        sp = self.specs(f)[0]
        self.assertEqual(sp.width, 9.0)
        self.assertEqual(sp.lanes, 3)

    def test_a_crossing_or_distant_road_takes_nothing(self):
        write_shard(self.tmp.name, [(EAST, 6.5, 2, 1, 4, 0)])
        cross = features([[(0.0, -300.0), (0.0, 300.0)]], ["secondary"])
        far = features([[(-300.0, 20.0), (300.0, 20.0)]], ["secondary"])
        plain = roads.carriageway("secondary", "paved", False)
        self.assertEqual(self.specs(cross)[0].width, plain)
        self.assertEqual(self.specs(far)[0].width, plain)

    def test_a_carriageway_takes_its_own_axis_not_the_centre(self):
        write_shard(self.tmp.name, [(EAST, 0.0, 4, 1, 1, germanroads.DIVIDED_AXIS),
                                    ([(-400.0, 7.0), (400.0, 7.0)], 11.5, 2, 1, 1, germanroads.CARRIAGEWAY),
                                    ([(400.0, -7.0), (-400.0, -7.0)], 11.5, 2, 1, 1, germanroads.CARRIAGEWAY)])
        f = features([[(-300.0, 7.5), (300.0, 7.5)], [(300.0, -7.5), (-300.0, -7.5)]], ["motorway", "motorway"],
                     oneway=[0, 1])
        for sp in self.specs(f):
            self.assertEqual(sp.lanes, 2)
            self.assertLessEqual(sp.width, 11.5)
            self.assertGreater(sp.width, 7.0)

    def test_a_one_way_road_on_a_divided_centre_axis_takes_nothing(self):
        write_shard(self.tmp.name, [(EAST, 14.0, 4, 1, 2, germanroads.DIVIDED_AXIS)])
        f = features([[(-300.0, 1.0), (300.0, 1.0)]], ["trunk"], oneway=[0])
        self.assertEqual(germanroads.surveyed(germanroads.axes_for(f), f.lonlat, True)[:2], (None, None))

    def test_one_half_of_a_split_road_does_not_take_both_halves_width(self):
        write_shard(self.tmp.name, [(EAST, 20.0, 2, 1, 3, 0)])       # (Mainzer Straße: 20 m, two lanes)
        f = features([[(-300.0, 4.0), (300.0, 4.0)]], ["primary"], oneway=[0])
        before = roads._road_specs(f, None, False)
        with mock.patch.object(germanroads, "ON", False):
            plain = roads._road_specs(f, None, False)[0]
        self.assertEqual((before[0].width, before[0].lanes), (plain.width, plain.lanes))

    def test_track_surfaces(self):
        for surf, kind in ((6, "dirt"), (5, "gravel"), (1, "paved")):
            germanroads._state["shards"].clear()
            write_shard(self.tmp.name, [(EAST, 0.0, 0, surf, 9, 0)])
            sp = self.specs(features([[(-300.0, 0.0), (300.0, 0.0)]], ["track"]))[0]
            self.assertEqual(sp.kind, kind, surf)

    def test_a_main_road_never_turns_to_earth(self):
        write_shard(self.tmp.name, [(EAST, 0.0, 0, 6, 3, 0)])
        self.assertEqual(self.specs(features([[(-300.0, 0.0), (300.0, 0.0)]], ["primary"]))[0].kind, "paved")

    def test_a_mapped_surface_wins(self):
        write_shard(self.tmp.name, [(EAST, 0.0, 0, 6, 9, 0)])
        f = features([[(-300.0, 0.0), (300.0, 0.0)]], ["track"], osm={"osm_surface": ["asphalt"]})
        self.assertEqual(self.specs(f)[0].kind, "paved")

    def test_no_shard_changes_nothing(self):
        f = features([[(-300.0, 0.0), (300.0, 0.0)]], ["secondary"])
        self.assertEqual(self.specs(f)[0].width, roads.carriageway("secondary", "paved", False))

    def test_unreachable_shard_is_a_fault(self):
        import revisit
        f = features([[(-300.0, 0.0), (300.0, 0.0)]], ["secondary"])
        with mock.patch.object(germanroads, "shard", return_value=None):
            n = revisit.thread_faults()
            out = germanroads.adjust_specs(f, [roads.Spec(6.0, "paved", False, "secondary", False)])
        self.assertEqual(out[0].width, 6.0)
        self.assertEqual(revisit.thread_faults(), n + 1)

    def test_off(self):
        write_shard(self.tmp.name, [(EAST, 6.5, 2, 1, 4, 0)])
        f = features([[(-300.0, 0.0), (300.0, 0.0)]], ["secondary"])
        with mock.patch.object(germanroads, "ON", False):
            self.assertNotEqual(self.specs(f)[0].width, 6.5)


class BuilderTest(unittest.TestCase):
    def test_geopackage_lines(self):
        wkb = struct.pack("<BII", 1, 5, 2)
        for pts in ([(7.0, 49.0), (7.1, 49.1)], [(7.2, 49.2), (7.3, 49.3), (7.4, 49.4)]):
            wkb += struct.pack("<BII", 1, 2, len(pts)) + b"".join(struct.pack("<dd", *p) for p in pts)
        blob = b"GP\x00\x03" + struct.pack("<i", 4326) + b"\x00" * 32 + wkb      # (flags 3: little endian, xy envelope)
        got = build_de_roads.lines(blob)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1][2], (7.4, 49.4))

    def test_simplify_keeps_bends(self):
        line = [(7.0 + i * 1e-4, 49.0) for i in range(11)] + [(7.001, 49.001)]
        got = build_de_roads.simplify(line)
        self.assertEqual(got[0], line[0])
        self.assertEqual(got[-1], line[-1])
        self.assertLess(len(got), len(line))
        self.assertIn(line[10], got)


if __name__ == "__main__":
    unittest.main()
