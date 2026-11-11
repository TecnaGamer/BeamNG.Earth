"""overture.tolerant: a plain column a release dropped is read as NULL AS it (research 2026-11-10: Overture's schema
issue #698 proposes moving level, min_floor and roof_direction out of the core schema)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import overture  # noqa: E402


class TolerantTest(unittest.TestCase):
    def test_dropped_columns_read_as_null(self):
        spec = overture.LAYERS["buildings"]
        present = {c.strip() for c in overture._top_items(spec["cols"]) if c.strip().isidentifier()} | {"names", "sources"}
        self.assertIs(overture.tolerant(spec, present, "buildings"), spec)         # (nothing dropped: as it was)
        with self.assertLogs("earthd.overture" if overture.log.name == "earthd.overture" else overture.log.name,
                             "WARNING"):
            got = overture.tolerant(spec, present - {"level", "roof_direction"}, "buildings_t")
        items = [c.strip() for c in overture._top_items(got["cols"])]
        self.assertIn("NULL AS level", items)
        self.assertIn("NULL AS roof_direction", items)
        self.assertIn("roof_shape", items)
        self.assertTrue(any(c.startswith("COALESCE(") for c in items))          # (an expression left as it is)

    def test_unknown_present_changes_nothing(self):
        spec = overture.LAYERS["buildings"]
        self.assertIs(overture.tolerant(spec, None), spec)


if __name__ == "__main__":
    unittest.main()
