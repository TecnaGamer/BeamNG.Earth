"""tools/package.py writes zip entry times that are in the past in every time zone (the game reads zip times as local
time; future times made it re-pack terrain textures at every block swap)."""
import importlib.util
import os
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load():
    spec = importlib.util.spec_from_file_location("bne_package", os.path.join(ROOT, "tools", "package.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PastStamp(unittest.TestCase):
    def test_in_the_past_everywhere(self):
        pkg = load()
        now = time.time()
        stamp = pkg.past_stamp(now)
        # the most western local time (UTC-12) right now, as zip wall time
        west = time.gmtime(now - 12 * 3600)[:6]
        self.assertLess(stamp, west)
        # and not absurdly old: within a day of the file's real time
        self.assertGreater(stamp, time.gmtime(now - 24 * 3600)[:6])


if __name__ == "__main__":
    unittest.main()
