"""tools/reports.py offline: listing old (note / bench) and new reports, the GitHub issue URL and its
truncation, the fallback issue.md for old reports, and that `issue` builds the URL from issue.md
without opening anything (do_edit / browse off)."""
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import reports  # noqa: E402


def make(root, name, data, issue_md=None):
    d = os.path.join(root, name)
    os.makedirs(d)
    with open(os.path.join(d, "report.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)
    if issue_md is not None:
        with open(os.path.join(d, "issue.md"), "w", encoding="utf-8") as f:
            f.write(issue_md)


class ReportsTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        make(self.root, "20261013_120000_note_1", {"time": "2026-10-13 12:00:00", "kind": "note", "note": "old note",
                                                  "lat": 39.28, "lon": -76.59, "recentFrames": {"fps": 60, "p99": 20, "maxMs": 40}})
        make(self.root, "20261013_130000_bench_2", {"time": "2026-10-13 13:00:00", "kind": "bench",
                                                   "bench": {"fps": 45, "p99": 50, "maxMs": 200}})
        make(self.root, "20261014_100000_report_1", {"time": "2026-10-14 10:00:00", "kind": "map", "title": "Bridge gone"},
             "# [Map issue] Bridge gone\n\n**Kind:** Map issue\nbody é text\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_old_and_new(self):
        reps = reports.load(self.root)
        self.assertEqual([r["kind"] for r in reps], ["note", "bench", "map"])
        self.assertIn("Note", reports.line(1, reps[0]))
        self.assertIn("Performance", reports.line(2, reps[1]))
        self.assertIn("Bridge gone", reports.line(3, reps[2]))
        self.assertEqual(reports.kind_of(reps[1])[1], "performance")

    def test_url_and_truncation(self):
        url, cut = reports.issue_url("A b", "x&y", "map")
        self.assertFalse(cut)
        self.assertEqual(url, "https://github.com/XavierCraggs/BeamNG.Earth/issues/new?title=A%20b&labels=map&body=x%26y")   # fork: was TecnaGamer
        body = "é line\n" * 3000
        url, cut = reports.issue_url("t", body, "bug")
        self.assertTrue(cut)
        self.assertLessEqual(len(url), reports.URL_MAX)
        self.assertGreater(len(url), reports.URL_MAX - 200)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertTrue(q["body"][0].endswith(reports.TRUNCATED))
        self.assertTrue(body.startswith(q["body"][0][:-len(reports.TRUNCATED)]))

    def test_issue_from_issue_md(self):
        reps = reports.load(self.root)
        url = reports.issue(reps[2], do_edit=False, browse=False)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(q["title"], ["[Map issue] Bridge gone"])
        self.assertEqual(q["labels"], ["map"])
        self.assertIn("body é text", q["body"][0])

    def test_fallback_issue_for_old_report(self):
        reps = reports.load(self.root)
        url = reports.issue(reps[1], do_edit=False, browse=False)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(q["labels"], ["performance"])
        self.assertTrue(q["title"][0].startswith("[Performance issue]"))
        self.assertIn("1% low 20.0 fps", q["body"][0])
        self.assertTrue(os.path.exists(os.path.join(reps[1]["_dir"], "issue.md")))
        md = reports.fallback_issue(reps[0])
        self.assertIn("openstreetmap.org/?mlat=39.280000&mlon=-76.590000", md)


if __name__ == "__main__":
    unittest.main()
