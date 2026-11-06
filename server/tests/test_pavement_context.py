"""Roadside pavement evidence uses geographic source cells, never a caller's cell membership."""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapcells
import mapdata
import mapgeom
import netfetch
import revisit
import roads
from test_roads import FRAME, features


class Sources:
    def __init__(self, layers=None):
        self.layers = layers or {}
        self.calls = []
        self.failed = set()

    def cell(self, key, layers, kind):
        assert layers == ("roads",) and kind == "demand"
        self.calls.append(key)
        if key in self.failed:
            raise OSError("isolated test source unavailable")
        return SimpleNamespace(layers={"roads": self.layers.get(key, mapgeom.empty("roads"))})


class PavementSourceTest(unittest.TestCase):
    def test_only_cells_near_probes_are_read_once(self):
        first = mapcells.cell_of(40.8, -104.9)
        distant = mapcells.CellKey(first.z, first.x + 20, first.y)
        points = np.array([[lon, lat] for lat, lon in [first.center(), distant.center(), first.center()]])
        md = Sources()
        with mock.patch.object(roads, "road_specs", side_effect=AssertionError("source hook must not classify roads")):
            self.assertEqual(mapdata.pavement_hook(md)(points), [])
        self.assertEqual(set(md.calls), {first, distant})
        self.assertEqual(len(md.calls), 2, "no bounding-box fill or fixed eight-neighbor expansion")

    def test_probe_near_source_edge_reads_both_sides(self):
        key = mapcells.cell_of(40.8, -104.9)
        west, south, east, north = key.bounds()
        lat = (south + north) / 2
        points = np.array([[east - 5. / (111320. * np.cos(np.radians(lat))), lat]])
        md = Sources()
        mapdata.pavement_hook(md)(points)
        self.assertEqual(set(md.calls), {key, mapcells.CellKey(key.z, key.x + 1, key.y)})

    def test_failed_context_is_faulted_and_retried(self):
        key = mapcells.cell_of(40.8, -104.9)
        lat, lon = key.center()
        md = Sources()
        md.failed.add(key)
        hook = mapdata.pavement_hook(md)
        before = revisit.thread_faults()
        with self.assertRaises(OSError):
            hook(np.array([[lon, lat]]))
        self.assertGreater(revisit.thread_faults(), before)
        md.failed.clear()
        before = revisit.thread_faults()
        self.assertEqual(hook(np.array([[lon, lat]])), [])
        self.assertEqual(revisit.thread_faults(), before, "an explicitly empty cell is complete")
        self.assertEqual(md.calls, [key, key])

    def test_missing_road_layer_is_not_complete_empty_context(self):
        md = SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers={}))
        key = mapcells.cell_of(40.8, -104.9)
        lat, lon = key.center()
        before = revisit.thread_faults()
        with self.assertRaises(RuntimeError):
            mapdata.pavement_hook(md)(np.array([[lon, lat]]))
        self.assertGreater(revisit.thread_faults(), before)

    def test_parallel_road_mask_is_same_for_different_source_cell_copies(self):
        lines = [[(0., 0.), (800., 0.)], [(0., 30.), (800., 30.)]]
        complete = features(lines, ["residential", "motorway"])
        first = features(lines, ["residential", "motorway"])
        second = features(lines[:1], ["residential"])
        key = mapcells.cell_of(float(complete.lonlat[0, 1]), float(complete.lonlat[0, 0]))
        md = Sources({key: complete})
        first._bnge_pavement = second._bnge_pavement = mapdata.pavement_hook(md)

        def pavement_cover(lat, lon):
            _, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.)
            return (abs(n) < 6.) | (abs(n - 30.) < 6.)

        a, b = roads._road_specs(first, pavement_cover, False)[0], roads._road_specs(second, pavement_cover, False)[0]
        self.assertFalse(a.urban)
        self.assertEqual((a.urban, a.width), (b.urban, b.width))

    def test_decoded_roads_carry_a_lazy_source_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            md = mapdata.MapData(directory, fetcher=netfetch.Fetcher(offline=True))
            md.rail_tags = md.lane_tags = None
            try:
                arrays = mapgeom.empty("roads").to_arrays("")
                arrays["provenance"] = "{}"
                key = mapcells.cell_of(40.8, -104.9)
                with mock.patch.object(md, "layer_arrays", return_value=(arrays, True)), \
                     mock.patch.object(mapdata, "pavement_hook", return_value="lazy-source-hook") as hook:
                    f = md.cell(key, ("roads",)).layers["roads"]
                self.assertEqual(f._bnge_pavement, "lazy-source-hook")
                hook.assert_called_once_with(md)
            finally:
                md.close()
                md.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
