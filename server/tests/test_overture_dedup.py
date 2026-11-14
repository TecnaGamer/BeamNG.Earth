"""overture._number_rows: the window dedup by id skipped where no id repeats (DEDUP_IF_REPEATED) gives the same table
as the dedup always; repeated ids keep their highest version."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import duckdb  # noqa: E402
import overture  # noqa: E402


def _rows(data, dedup_if_repeated):
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE s (id VARCHAR, version INTEGER, wkb BLOB, cx DOUBLE, name VARCHAR)")
    con.executemany("INSERT INTO s VALUES (?, ?, ?, ?, ?)", data)
    overture._number_rows(con, dedup_if_repeated)
    return con.execute("SELECT * FROM t ORDER BY fi").fetchall()


class NumberRowsTest(unittest.TestCase):
    """The same rows with the dedup skipped as with it always on; a repeated id keeps its highest version."""

    UNIQUE = [("c", 1, b"\x01", 3.0, "x"), ("a", 2, b"\x02", 1.0, "y"), ("b", 1, b"\x03", 2.0, None)]

    def test_unique_ids_same_rows(self):
        self.assertEqual(_rows(self.UNIQUE, True), _rows(self.UNIQUE, False))
        self.assertEqual([r[1] for r in _rows(self.UNIQUE, True)], ["a", "b", "c"])

    def test_repeated_ids_keep_the_highest_version(self):
        data = self.UNIQUE + [("a", 5, b"\x09", 1.5, "new"), ("a", 1, b"\x00", 0.5, "old")]
        got = _rows(data, True)
        self.assertEqual(got, _rows(data, False))
        self.assertEqual([(r[1], r[4]) for r in got], [("a", "new"), ("b", None), ("c", "x")])


if __name__ == "__main__":
    unittest.main()
