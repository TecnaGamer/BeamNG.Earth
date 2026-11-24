"""Switzerland's road heights (roadz.py source "tlm": swisstopo's swissTLM3D, cut into cells by
tools/build_swisstlm.py; server/tlmcodes.py): its kinds of structure as levels, where it is offered, how a cell is
read (from BNE_ROADZ_TLM_DIR, from BNGE_ROADZ_TLM_URL, a 404, neither), the plans' tag, and the tool's cell file."""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import build_swisstlm  # noqa: E402
import mapcells  # noqa: E402
import netfetch  # noqa: E402
import roadz  # noqa: E402
import tlmcodes  # noqa: E402

# the Bahnhofquai underpass at Zurich main station (swissTLM3D 2026-02, UUID 1A8D8FA6...): a road, KUNSTBAUTE 1100
UNDERPASS = (np.array([[8.54153770, 47.37725904], [8.54156363, 47.37708721], [8.54162892, 47.37678955]]),
             np.array([403.993, 403.632, 404.045]))
ZURICH = (47.3772, 8.5418)


class Body:
    def __init__(self, body):
        self.body = body


class FakeFetcher:
    """netfetch's get: a dict url -> bytes; any other url a 404."""

    def __init__(self, files):
        self.files, self.asked = files, []

    def get(self, url, kind=None, headers=None, lane=None):
        self.asked.append(url)
        if url in self.files:
            return Body(self.files[url])
        raise netfetch.NotFound("404", url=url, status=404)


def cell_bytes(rows):
    d = tempfile.mkdtemp(prefix="roadz_tlm_cell_")
    try:
        p = os.path.join(d, "c.npz")
        build_swisstlm.write_cell(p, rows)
        with open(p, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


class Saved:
    """roadz's URL, folder and provider put back after a test."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="roadz_tlm_")
        self.cells = os.path.join(self.dir, "tlmcells")      # (the tool's --out: BNE_ROADZ_TLM_DIR)
        self.saved = roadz.TLM_URL, roadz.TLM_DIR, roadz.PROVIDER
        roadz.TLM_DIR = ""

    def tearDown(self):
        roadz.TLM_URL, roadz.TLM_DIR, roadz.PROVIDER = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def keep(self, key, rows):
        """A cell in the tool's folder."""
        p = os.path.join(self.cells, str(key.z), str(key.x), f"{key.y}.npz")
        build_swisstlm.write_cell(p, rows)
        return p


class CodesTest(unittest.TestCase):
    def test_road_structures(self):
        # (swissTLM3D 2026-02's own domain: TLM_STRASSE_KUNSTBAUTE_CD)
        self.assertEqual(tlmcodes.tlm_level(1000), -1, "a tunnel")
        self.assertEqual(tlmcodes.tlm_level(1100), -1, "an underpass")
        self.assertEqual(tlmcodes.tlm_level(800), 1, "on a dam wall")
        self.assertEqual(tlmcodes.tlm_level(200), 1)
        self.assertEqual(tlmcodes.tlm_level(700), 0, "a gallery: on the ground under a roof")
        self.assertEqual(tlmcodes.tlm_level(100), 0)
        self.assertIsNone(tlmcodes.tlm_level(1400), "in or on a building: not cut")
        for odd in (None, "", "x", 999998, 12345):
            self.assertEqual(tlmcodes.tlm_level(odd), 0)


class WhereTest(Saved, unittest.TestCase):
    def test_offered_only_where_a_cell_can_be_had(self):
        roadz.TLM_URL, roadz.PROVIDER = "", None
        self.assertNotIn("tlm", roadz.sources_at(*ZURICH), "no URL, nothing kept: not asked at all")
        roadz.TLM_URL = "https://cells.example/tlm"
        self.assertIn("tlm", roadz.sources_at(*ZURICH))
        self.assertIn("tlm", roadz.sources_at(46.0037, 8.9511))      # Lugano
        self.assertIn("tlm", roadz.sources_at(47.1410, 9.5209))      # Vaduz
        self.assertNotIn("tlm", roadz.sources_at(48.8566, 2.3522))   # Paris
        self.assertNotIn("tlm", roadz.sources_at(59.91, 10.75))      # Oslo
        got = roadz.sources_at(*ZURICH)
        self.assertLess(got.index("tlm"), got.index("ign"), "before BD TOPO's box, which reaches over Zurich")

    def test_a_folder_of_cells(self):
        roadz.TLM_URL = ""
        roadz.PROVIDER = roadz.RoadZ(self.dir, FakeFetcher({}))
        self.keep(mapcells.cell_of(*ZURICH), [(UNDERPASS[0], UNDERPASS[1], -1, "", "Bahnhofquai")])
        self.assertNotIn("tlm", roadz.sources_at(*ZURICH), "a cell on disk alone is not the setting")
        roadz.TLM_DIR = self.cells
        self.assertIn("tlm", roadz.sources_at(*ZURICH))
        self.assertIn("tlm", roadz.sources_at(46.0037, 8.9511), "Lugano: offered too (no cell there: no roads)")
        self.assertNotIn("tlm", roadz.sources_at(48.8566, 2.3522))   # Paris


class CellTest(Saved, unittest.TestCase):
    key = mapcells.CellKey(14, 8580, 5737)

    def test_from_the_url_then_kept_on_disk(self):
        roadz.TLM_URL = "https://cells.example/tlm"
        rows = [(UNDERPASS[0], UNDERPASS[1], -1, "", "Bahnhofquai")]
        f = FakeFetcher({f"https://cells.example/tlm/14/{self.key.x}/{self.key.y}.npz": cell_bytes(rows)})
        ln = roadz.RoadZ(self.dir, f).lines("tlm", self.key)
        self.assertEqual(len(ln), 1)
        self.assertEqual(int(ln.level[0]), -1)
        self.assertAlmostEqual(float(ln.z[1]), 403.632)
        self.assertEqual(str(ln.name[0]), "Bahnhofquai")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "roadz", f"v{roadz.VERSION}", roadz.TLM_CELLS, "14",
                                                    str(self.key.x), f"{self.key.y}.npz")), "kept on disk")
        f2 = FakeFetcher({})
        self.assertEqual(len(roadz.RoadZ(self.dir, f2).lines("tlm", self.key)), 1)
        self.assertEqual(f2.asked, [], "a new server reads it from disk")

    def test_a_404_is_a_cell_without_roads(self):
        roadz.TLM_URL = "https://cells.example/tlm"
        ln = roadz.RoadZ(self.dir, FakeFetcher({})).lines("tlm", self.key)
        self.assertIsNotNone(ln)
        self.assertEqual(len(ln), 0)

    def test_from_the_folder(self):
        roadz.TLM_DIR = self.cells
        self.keep(self.key, [(UNDERPASS[0], UNDERPASS[1], -1, "", "Bahnhofquai")])
        f = FakeFetcher({})
        ln = roadz.RoadZ(self.dir, f).lines("tlm", self.key)
        self.assertEqual(len(ln), 1)
        self.assertAlmostEqual(float(ln.z[1]), 403.632)
        self.assertEqual(f.asked, [], "nothing asked")
        other = mapcells.CellKey(14, self.key.x + 1, self.key.y)
        self.assertEqual(len(roadz.RoadZ(self.dir, f).lines("tlm", other)), 0, "no file: a cell without roads")

    def test_a_folder_s_cells_are_not_kept_in_the_cache(self):
        roadz.TLM_DIR = self.cells
        rows = [(UNDERPASS[0], UNDERPASS[1], -1, "", "Bahnhofquai")]
        self.assertEqual(len(roadz.RoadZ(self.dir, FakeFetcher({})).lines("tlm", self.key)), 0, "not cut yet")
        self.keep(self.key, rows)
        self.assertEqual(len(roadz.RoadZ(self.dir, FakeFetcher({})).lines("tlm", self.key)), 1,
                         "the finished cut read by the next server, not the empty answer kept before")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "roadz", f"v{roadz.VERSION}", roadz.TLM_CELLS)))

    def test_no_url_leaves_the_inference(self):
        roadz.TLM_URL = ""
        f = FakeFetcher({})
        self.assertIsNone(roadz.RoadZ(self.dir, f).lines("tlm", self.key))
        self.assertEqual(f.asked, [], "nothing asked")


class TagTest(Saved, unittest.TestCase):
    """A Swiss cell's plans with the measured heights are keyed apart (plancache: not the plans made before, nor the
    baked packs' or the shared archives', which are made without them)."""
    zurich = mapcells.CellKey(14, 8580, 5737)

    def test_served_in_a_folder_or_neither(self):
        roadz.TLM_URL, roadz.PROVIDER = "", None
        self.assertEqual(roadz.tlm_tags(self.zurich), (), "no cells anywhere: the plans as before")
        roadz.TLM_URL = "https://cells.example/tlm"
        self.assertEqual(roadz.tlm_tags(self.zurich), (roadz.TLM_CELLS,))
        self.assertEqual(roadz.tlm_tags(mapcells.CellKey(14, 8301, 5636)), (), "Paris")
        roadz.TLM_URL = ""
        roadz.PROVIDER = roadz.RoadZ(self.dir, FakeFetcher({}))
        self.keep(self.zurich, [(UNDERPASS[0], UNDERPASS[1], -1, "", "Bahnhofquai")])
        self.assertEqual(roadz.tlm_tags(self.zurich), (), "a cell on disk alone: untagged, as it is not read")
        roadz.TLM_DIR = self.cells
        self.assertEqual(roadz.tlm_tags(self.zurich), (roadz.TLM_CELLS,), "the folder set")
        roadz.PROVIDER = None
        self.assertEqual(roadz.tlm_tags(self.zurich), (roadz.TLM_CELLS,), "the same in a process without a RoadZ")
        nb = mapcells.CellKey(14, self.zurich.x + 1, self.zurich.y)
        self.assertEqual(roadz.tlm_tags(nb), (roadz.TLM_CELLS,), "its neighbour too, cut or not")

    def test_cells_just_outside_the_box_tagged(self):
        roadz.TLM_URL = "https://cells.example/tlm"
        pad = roadz.CLIP_PAD_M / 111320.0
        k = mapcells.cell_of(47.0, 8.5)
        lon0, lat0, lon1, lat1 = k.bounds()
        with mock.patch.object(roadz, "CH_BOUNDS", (45.80, 5.94, lat0 - pad / 2, 10.50)):
            # (the box's edge just south of the cell: a road part of the cell can have its middle inside the box)
            self.assertEqual(roadz.tlm_tags(k), (roadz.TLM_CELLS,))
        with mock.patch.object(roadz, "CH_BOUNDS", (45.80, 5.94, lat0 - 2 * pad, 10.50)):
            self.assertEqual(roadz.tlm_tags(k), ())


class ToolTest(unittest.TestCase):
    def test_the_cell_file_is_roadz_s(self):
        d = tempfile.mkdtemp(prefix="roadz_tlm_tool_")
        try:
            p = os.path.join(d, "14", "8580", "5737.npz")
            rows = [(UNDERPASS[0], UNDERPASS[1], -1, "", "Bahnhofquai"),
                    (UNDERPASS[0] + 0.001, UNDERPASS[1] + 2.0, 0, "", "")]
            build_swisstlm.write_cell(p, rows)
            ln = roadz.Lines.load(p)
            self.assertEqual(len(ln), 2)
            self.assertEqual([int(v) for v in ln.level], [-1, 0])
            np.testing.assert_allclose(ln.z[:3], UNDERPASS[1])
            self.assertFalse(os.path.exists(p + ".tmp"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_names_and_cells(self):
        self.assertEqual(build_swisstlm.name_of("Milchbucktunnel", "Hofwiesenstrasse"),
                         "Milchbucktunnel / Hofwiesenstrasse")
        self.assertEqual(build_swisstlm.name_of(None, "Bahnhofquai", "Bahnhofquai"), "Bahnhofquai")
        self.assertTrue(roadz.name_ok("Hofwiesenstrasse", "Milchbucktunnel / Hofwiesenstrasse", ""))
        self.assertTrue(roadz.name_ok("Milchbucktunnel", "Milchbucktunnel / Hofwiesenstrasse", ""))
        # (a 120 m piece clipping a cell's corner is found in both cells)
        k = mapcells.cell_of(*ZURICH)
        lon0, lat0, lon1, lat1 = k.bounds()
        line = np.array([[lon1 - 0.0008, lat0 - 0.0002], [lon1 + 0.0002, lat0 + 0.0008]])
        self.assertIn((k.x, k.y), build_swisstlm.cells_of_line(line))
        self.assertIn((k.x + 1, k.y), build_swisstlm.cells_of_line(line))
        # (a piece clipping the cell's corner by ~1 m: found exactly, where samples every ~20 m missed it)
        e = 1e-5
        line = np.array([[lon1 - 0.003, lat1 + 0.003 - e], [lon1 + 0.003 - e, lat1 - 0.003]])
        got = build_swisstlm.cells_of_line(line)
        self.assertIn((k.x, k.y), got)
        self.assertEqual(got, {(k.x, k.y - 1), (k.x, k.y), (k.x + 1, k.y)})


if __name__ == "__main__":
    unittest.main()
