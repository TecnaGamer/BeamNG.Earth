"""roads._stuck_dump: the server's stacks when a road tile is stuck (GitHub #1058), at most once per STUCK_DUMP_S."""
import os
import sys
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402
import stackdump  # noqa: E402


class StuckDumpTest(unittest.TestCase):
    def test_once_per_interval(self):
        seen = []
        with unittest.mock.patch.object(stackdump, "dump", lambda what: seen.append(what)):
            roads._stuck_dumped[0] = -1e9
            roads._stuck_dump("a")
            roads._stuck_dump("b")                  # (within the interval: none)
            for _ in range(50):
                if seen:
                    break
                time.sleep(0.02)
        self.assertEqual(seen, ["a"])


if __name__ == "__main__":
    unittest.main()
