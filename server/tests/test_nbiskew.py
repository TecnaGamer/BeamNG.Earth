"""nbiskew.py (the US bridge inventory's skews: a local build read, the checks) and roadshape.nbi_record's match."""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import nbiskew  # noqa: E402
import roadshape  # noqa: E402


def npz(lat, lon, skew, length, extra=None):
    buf = io.BytesIO()
    cols = dict(lat=np.asarray(lat, "<f4"), lon=np.asarray(lon, "<f4"), skew=np.asarray(skew, "|u1"),
                length=np.asarray(length, "<u2"))
    if extra:
        cols.update(extra)
    np.savez(buf, **cols)
    return buf.getvalue()


def local_build(d, body, records):
    os.makedirs(os.path.join(d, "nbiskew"), exist_ok=True)
    with open(os.path.join(d, "nbiskew", "nbiskew.npz"), "wb") as f:
        f.write(body)
    sha = hashlib.sha256(body).hexdigest()
    m = {"format": 1, "name": "nbiskew", "version": "v1", "build": sha[:16], "records": records,
         "files": [{"path": "nbiskew.npz", "size": len(body), "sha256": sha}]}
    with open(os.path.join(d, "nbiskew", "manifest.json"), "w") as f:
        json.dump(m, f)
    return m


class ReaderTest(unittest.TestCase):
    def tearDown(self):
        nbiskew.configure(None, None, local=None)
        nbiskew._pack = None

    def test_a_local_build_is_read_and_keyed(self):
        with tempfile.TemporaryDirectory() as d:
            m = local_build(d, npz([38.78, 38.79], [-77.18, -77.17], [30, 99], [256, 0]), 2)
            nbiskew.configure(None, None, local=d)
            self.assertEqual(nbiskew.identity(), "v1-" + m["build"])
            t = nbiskew.table()
            self.assertEqual(list(t.skew), [30, 99])
            self.assertEqual(list(t.near(38.7799, -77.1801, 38.7801, -77.1799, 30)), [0])
            self.assertEqual(list(t.near(38.70, -77.30, 38.71, -77.29, 30)), [])

    def test_bad_tables_are_refused(self):
        with self.assertRaises(ValueError):
            nbiskew.decode(npz([2.0, 1.0], [0, 0], [0, 0], [0, 0]))         # (not sorted)
        with self.assertRaises(ValueError):
            nbiskew.decode(npz([1.0], [0], [0], [0], extra={"x": np.zeros(1)}))
        buf = io.BytesIO()
        np.savez(buf, lat=np.zeros(1, "<f8"), lon=np.zeros(1, "<f4"), skew=np.zeros(1, "|u1"), length=np.zeros(1, "<u2"))
        with self.assertRaises(ValueError):
            nbiskew.decode(buf.getvalue())                                    # (a column's dtype)
        buf = io.BytesIO()
        np.savez(buf, lat=np.array([{"a": 1}], dtype=object), lon=np.zeros(1, "<f4"), skew=np.zeros(1, "|u1"),
                 length=np.zeros(1, "<u2"))
        with self.assertRaises(ValueError):
            nbiskew.decode(buf.getvalue())                                    # (no pickles)
        self.assertIsNone(nbiskew.check_manifest({"format": 1, "name": "nbiskew", "version": "v1"}))

    def test_no_table_no_record(self):
        nbiskew.configure(None, None, local=None)
        nbiskew._pack = None
        self.assertIsNone(nbiskew.table())
        self.assertIsNone(roadshape.nbi_record(np.array([[0.0, 0.0], [40.0, 0.0]]), geodesy.EnuFrame(38.78, -77.18)))

    def test_the_nearest_record_within_30_m_of_the_deck_line(self):
        fr = geodesy.EnuFrame(38.78, -77.18)
        # records 10 m and 20 m off a 40 m deck along x, and one 50 m off
        la, lo, _ = fr.enu_to_geodetic(np.array([20.0, 5.0, 20.0]), np.array([10.0, -20.0, 50.0]), np.zeros(3))
        o = np.lexsort((lo, la))
        with tempfile.TemporaryDirectory() as d:
            local_build(d, npz(la[o], lo[o], np.array([30, 12, 45])[o], np.array([400, 410, 420])[o]), 3)
            nbiskew.configure(None, None, local=d)
            rec = roadshape.nbi_record(np.array([[0.0, 0.0], [40.0, 0.0]]), fr)
            self.assertEqual(rec[0], 30.0)
            self.assertAlmostEqual(rec[1], 40.0)
            self.assertAlmostEqual(rec[2], np.cos(np.radians(30.0)), places=12)
            self.assertIsNone(roadshape.nbi_record(np.array([[0.0, 200.0], [40.0, 200.0]]), fr))


if __name__ == "__main__":
    unittest.main()
