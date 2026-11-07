"""telemetry.py: anonymous usage reports (counts since the last report, every process's, kept when a send fails)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netfetch  # noqa: E402
import telemetry  # noqa: E402


class ReporterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent = []
        self.ok = True
        self.f = netfetch.Fetcher()
        self.proc = os.path.join(self.tmp.name, "proc")
        self.r = telemetry.Reporter(self.tmp.name, "1.9.0", lambda: (True, False), proc_dir=self.proc,
                                    send=lambda b: (self.sent.append(b), self.ok)[1])

    def count(self, host, n, nbytes=0, s429=0):
        h = self.f.host(host)
        self.f._bump(h, "requests", n)
        self.f._bump(h, "bytes", nbytes)
        self.f._bump(h, "s429", s429)

    def test_counts_since_the_last_report_and_a_stable_id(self):
        self.count("a.example.org", 10, 3_000_000, 1)
        self.r.report()
        src = self.sent[-1]["sources"]["a.example.org"]
        self.assertEqual((src["req"], src["mb"], src["e429"]), (10, 3.0, 1))
        self.count("a.example.org", 4)
        self.r.report()
        self.assertEqual(self.sent[-1]["sources"]["a.example.org"]["req"], 4)
        self.r.report()
        self.assertNotIn("a.example.org", self.sent[-1]["sources"])            # nothing new
        self.assertEqual(set(self.sent[-1]), {"id", "v", "os", "game", "mp", "interval", "sources"})
        again = telemetry.Reporter(self.tmp.name, "1.9.0", lambda: (False, False), send=lambda b: True)
        self.assertEqual(again.id, self.r.id)                                    # the install's id is kept

    def test_a_failed_send_carries_its_counts_to_the_next(self):
        self.count("b.example.org", 7)
        self.ok = False
        self.r.report()
        self.ok = True
        self.count("b.example.org", 2)
        self.r.report()
        self.assertEqual(self.sent[-1]["sources"]["b.example.org"]["req"], 9)

    def test_other_processes_totals_are_summed_once(self):
        os.makedirs(self.proc)
        with open(os.path.join(self.proc, "999999.json"), "w") as fh:
            json.dump({"pid": 999999, "at": 1e12, "hosts": {"c.example.org": {"requests": 5, "bytes": 1_000_000}}}, fh)
        self.r.report()
        self.assertEqual(self.sent[-1]["sources"]["c.example.org"]["req"], 5)
        with open(os.path.join(self.proc, "999999.json"), "w") as fh:
            json.dump({"pid": 999999, "at": 1e12, "hosts": {"c.example.org": {"requests": 8, "bytes": 1_000_000}}}, fh)
        self.r.report()
        self.assertEqual(self.sent[-1]["sources"]["c.example.org"]["req"], 3)    # only what changed


class OptOutTest(unittest.TestCase):
    def test_off_by_default_for_direct_runs_and_the_launcher_switch(self):
        import earthd
        self.assertEqual(earthd.build_parser().parse_args([]).telemetry, "off") if hasattr(earthd, "build_parser") \
            else None
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import launcher
        old = os.environ.get("BNE_TELEMETRY")
        try:
            os.environ["BNE_TELEMETRY"] = "off"
            self.assertFalse(launcher.telemetry_on())
            os.environ.pop("BNE_TELEMETRY")
            self.assertTrue(launcher.telemetry_on())
        finally:
            if old is not None:
                os.environ["BNE_TELEMETRY"] = old


if __name__ == "__main__":
    unittest.main()
