"""Exercise the highway tag reader against a real local Parquet file, without HTTP."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import osmtags
from test_roadlanes import highway

try:
    import duckdb
except ImportError:
    duckdb = None


@unittest.skipIf(duckdb is None, "DuckDB not installed")
class HighwayTagReaderTest(unittest.TestCase):
    def test_lane_sql_rejects_ambiguous_and_fractional_counts(self):
        con = duckdb.connect()
        try:
            for value, expected in ((" 3 ", 3), ("2;3", None), ("2.5", None),
                                    ("3 lanes", None), ("-2", None), (None, None)):
                got = con.execute(f"SELECT {osmtags.COLUMNS['lanes']} FROM (SELECT ? AS lanes)", [value]).fetchone()[0]
                self.assertEqual(got, expected, value)
        finally:
            con.close()

    def test_reader_keeps_tagged_highway_geometry_and_raceways(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = (Path(tmp) / "highways.parquet").as_posix()
            con = duckdb.connect()
            columns = sorted((osmtags.USED | set(osmtags.RACEWAY_COLUMNS)) - {"id", "bbox"})
            con.execute("CREATE TABLE highways (id BIGINT, bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE), "
                        + ", ".join(f'"{c}" VARCHAR' for c in columns) + ", geometry BLOB)")
            coords = highway().lonlat
            wkb = struct.pack("<BII", 1, 2, len(coords)) + np.asarray(coords, "<f8").tobytes()
            bbox = dict(xmin=float(coords[0, 0]), ymin=float(coords[0, 1]),
                        xmax=float(coords[-1, 0]), ymax=float(coords[-1, 1]))
            for wid, cls, lanes in ((1, "motorway", "3"), (2, "motorway", "2;3"),
                                    (3, "residential", "2"), (4, "raceway", "2")):
                values = dict(highway=cls, lanes=lanes, oneway="yes", name="Example", surface="asphalt")
                row = [wid, bbox] + [values.get(c) for c in columns] + [wkb]
                con.execute("INSERT INTO highways VALUES (" + ",".join("?" for _ in row) + ")", row)
            con.execute(f"COPY highways TO '{path}' (FORMAT PARQUET)")

            class LocalConnection:
                def execute(self, sql):
                    return con.execute(sql.replace("fixture://osm/highways.parquet", path))

                def close(self):
                    con.close()

            store = osmtags.OsmTags(tmp, None)
            store._reader = SimpleNamespace(_connect=lambda *args: (LocalConnection(), "fixture", []), _fetch=mock.Mock())
            groups = {0: {"first": 0, "n": 4, "cols": {c: [] for c in columns + ["id", "bbox", "geometry"]}}}
            with mock.patch.object(store, "_groups", return_value=groups):
                tags, raceways = store._cell(None, (bbox["xmin"], bbox["ymin"], bbox["xmax"], bbox["ymax"]), "demand")
            self.assertEqual(tags[1]["lanes"], 3)
            self.assertEqual(tags[1]["_highway"], "motorway")
            np.testing.assert_allclose(tags[1]["_coords"], coords)
            self.assertNotIn("lanes", tags[2])
            self.assertNotIn("_coords", tags[2])
            self.assertNotIn("_coords", tags[3], "only highways need the geometry fallback")
            self.assertEqual([w["id"] for w in raceways], [4])
            np.testing.assert_allclose(raceways[0]["coords"], coords)


if __name__ == "__main__":
    unittest.main()
