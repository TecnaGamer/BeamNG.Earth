"""mapdata.MapData.layer_provenance off the event loop (GitHub #1054: a first read of a cell file on a busy hard disk
held the loop 5.2 s): the first ask returns None and reads on a thread; the next has it. wait=True reads at once."""
import json
import os
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mapcells  # noqa: E402
import mapdata  # noqa: E402


class ProvenanceTest(unittest.TestCase):
    def test_read_on_a_thread(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        md = mapdata.MapData(d)
        key = mapcells.cells_for_bbox(39.28, -76.60, 39.2801, -76.5999)[0]
        path = md.path(key, "buildings")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        np.savez(path, provenance=np.array(json.dumps({"id": "overture:test"})))
        self.assertIsNone(md.layer_provenance(key, "buildings"))            # (asked: read on the thread)
        for _ in range(100):
            got = md.layer_provenance(key, "buildings")
            if got is not None:
                break
            time.sleep(0.02)
        self.assertEqual(got, {"id": "overture:test"})
        other = mapcells.cells_for_bbox(39.30, -76.60, 39.3001, -76.5999)[0]
        self.assertIsNone(md.layer_provenance(other, "buildings", wait=True))   # (not there: None at once)


if __name__ == "__main__":
    unittest.main()
