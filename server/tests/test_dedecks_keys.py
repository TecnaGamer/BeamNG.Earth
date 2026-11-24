"""Germany's measured decks (dedom.py) and the caches: the build they are measured with (dedom.identity) names the German
answers' folder (roadz.RoadZ._path), is in MapData.versions() (plans, facts, blocks, tiles, packs, the BeamMP share)
and, left out of the archives' vkey, in the German cells' decision slices (decisions.slice_digest)."""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import dedom  # noqa: E402
import decisions  # noqa: E402
import mapcells  # noqa: E402
import mpshare  # noqa: E402
import netfetch  # noqa: E402
import roadz  # noqa: E402

PAGES = "https://pages.test/data"
COLOGNE = mapcells.cell_of(50.94, 6.96)       # (inside the NRW service's box)
PARIS = mapcells.cell_of(48.8566, 2.3522)
DENVER = mapcells.cell_of(39.74, -104.99)


class Pages:
    """A fetcher (.get(url, kind).body) serving files, 404 for the rest."""

    def __init__(self, files):
        self.files = files

    def get(self, url, kind="demand", **_kw):
        got = self.files.get(url)
        if got is None:
            raise netfetch.NotFound(url)
        return types.SimpleNamespace(body=got)


class Run(unittest.TestCase):
    """Each test its own server run: dedom's manifest and pagesdata's decisions forgotten."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dedecks_keys_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(self.reset)
        self.reset()
        for p in (mock.patch.object(dedom.pagesdata, "PAGES", PAGES),
                  mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop(dedom.LOCAL_ENV, None)

    @staticmethod
    def reset():
        dedom._state.update(cache=None, fetcher=None, pages=None)
        dedom._shards.clear()
        dedom.pagesdata.session_reset()

    def next_run(self):
        """Another server run after the pin's refresh: what Pages serves then is the run's."""
        self.reset()
        shutil.rmtree(os.path.join(self.dir, "pagesdata"), ignore_errors=True)

    def publish(self, built):
        files = {}
        if built is not None:
            files[f"{PAGES}/dedecks/manifest.json"] = json.dumps({"built": built, "shards": 1}).encode()
        dedom.configure(self.dir, Pages(files))


class IdentityTest(Run):
    def test_published_none_and_local(self):
        self.publish("2026-11-23")
        self.assertIsNone(dedom.identity(network=False), "not decided yet, no network: unknown")
        got = dedom.identity()
        self.assertTrue(got.startswith("b-"), got)
        self.assertEqual(dedom.identity(network=False), got, "decided: the same without the network")
        self.next_run()
        self.publish(None)
        self.assertEqual(dedom.identity(), "none", "not published")
        with mock.patch.dict(os.environ, {dedom.LOCAL_ENV: self.dir}):
            self.assertTrue(dedom.identity().startswith("dir-"))

    def test_two_builds_two_names(self):
        self.publish("2026-11-23")
        a = dedom.identity()
        self.next_run()
        self.publish("2026-12-01")
        self.assertNotEqual(dedom.identity(), a)


class CellTest(Run):
    def test_german_cells(self):
        self.assertTrue(roadz.de_cell(COLOGNE))
        self.assertFalse(roadz.de_cell(PARIS))
        self.assertFalse(roadz.de_cell(DENVER))
        with mock.patch.object(roadz, "DE_ON", False):
            self.assertFalse(roadz.de_cell(COLOGNE))
        # (one cell past a service's box: a road part of the cell can reach a cell measured there)
        box = (50.0, 7.0, 51.0, 8.0)
        with mock.patch.object(dedom, "SERVICES", [{"name": "t", "box": box}]):
            k = mapcells.cell_of(box[0] - 1e-6, 7.5)       # (just south of the box)
            self.assertTrue(roadz.de_cell(mapcells.CellKey(k.z, k.x, k.y + 1)))
            self.assertFalse(roadz.de_cell(mapcells.CellKey(k.z, k.x, k.y + 3)))

    def test_versions_hold_the_build(self):
        md = types.SimpleNamespace()
        self.publish("2026-11-23")
        self.assertIsNone(dedom.identity(network=False))
        dedom.manifest().current()              # (freeze_manifests: decided at the start)
        a = dedom.identity(network=False)
        self.assertTrue(a.startswith("b-"))
        # (left out of the archives' vkey: their German items carry it in their slices)
        self.assertIn("dedecks", decisions.WEEKLY)
        self.next_run()
        self.publish(None)
        dedom.manifest().current()
        self.assertEqual(dedom.identity(network=False), "none")
        del md

    def test_none_for_the_run_reads_no_samples(self):
        self.publish(None)
        dedom.manifest().current()
        pages = dedom.manifest()
        pages.missing = False                   # (a worker that took the run's "none" from the session)
        self.assertEqual(dedom._remote(lambda u: b"", 1, 2), b"", "no samples, no fault: the build is none")

    def test_answers_kept_under_their_build(self):
        self.publish("2026-11-23")
        rz = roadz.RoadZ(self.dir, None)
        self.assertIsNone(rz._path("lidar", COLOGNE, network=False), "not decided: not on disk as far as known")
        p = rz._path("lidar", COLOGNE)
        self.assertIn(os.sep + "dedecks-b-", p)
        self.assertEqual(rz._path("lidar", COLOGNE, network=False), p)
        self.assertIn(os.sep + "lidar" + os.sep, rz._path("lidar", DENVER))
        self.next_run()
        self.publish(None)
        self.assertNotEqual(roadz.RoadZ(self.dir, None)._path("lidar", COLOGNE), p,
                            "an answer made while nothing was published is not read once it is")

    def test_slices_and_share(self):
        off = {"rails": None, "lanes": None}
        self.publish("2026-11-23")
        a, pa = decisions.slice_digest(off, tuple(COLOGNE), []), decisions.slice_digest(off, tuple(PARIS), [])
        self.next_run()
        self.publish("2026-12-01")
        self.assertNotEqual(decisions.slice_digest(off, tuple(COLOGNE), []), a, "an item of one build: not another's")
        self.assertEqual(decisions.slice_digest(off, tuple(PARIS), []), pa, "outside Germany: the digests as before")
        self.next_run()
        self.publish("2026-12-01")
        self.assertIsNone(decisions.slice_digest(off, tuple(COLOGNE), [], fetch=False), "not decided: no item")
        self.assertFalse(mpshare.local_only(tuple(COLOGNE)), "shared: the share's key holds the versions")


if __name__ == "__main__":
    unittest.main()
