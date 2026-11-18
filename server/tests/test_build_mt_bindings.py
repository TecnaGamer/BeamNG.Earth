"""tools/build_mt_bindings.py: each mirror's copy of a Mapterhorn archive read whole (ranges ahead, in order, every one
after the first If-Match its ETag) through MD5; --mirrors; exit status 3 when nothing was bound. No network."""
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import build_mt_bindings as bmb  # noqa: E402
import mtarchive  # noqa: E402

SRC, MTD = mtarchive.HOSTS


class FakeCopies:
    """{host: (bytes, ETag)} served as build_mt_bindings._get would."""

    def __init__(self, copies):
        self.copies, self.calls, self.lock = copies, [], threading.Lock()

    def get(self, url, start, end, etag=None, tries=6):
        host = url.split("/")[2]
        data, tag = self.copies[host]
        with self.lock:
            self.calls.append((host, start, end, etag))
        if etag is not None and etag != tag:
            raise bmb.Changed("412")
        return data[start:end], tag, len(data)


class HashTest(unittest.TestCase):
    def test_ranges_ahead_in_order_pinned(self):
        data = os.urandom(10_000)
        f = FakeCopies({SRC: (data, '"e-3"')})
        with mock.patch.object(bmb, "_get", f.get):
            md5, etag = bmb.hash_copy(f"https://{SRC}/x/6-1-2.pmtiles", len(data), chunk=999, ahead=4)
        self.assertEqual((md5, etag), (hashlib.md5(data).hexdigest(), '"e-3"'))
        self.assertIsNone(f.calls[0][3], "the first read learns the ETag")
        self.assertTrue(all(c[3] == '"e-3"' for c in f.calls[1:]), "every later one If-Match it")
        self.assertEqual(sorted((c[1], c[2]) for c in f.calls),
                         [(a, min(len(data), a + 999)) for a in range(0, len(data), 999)])

    def _run(self, copies, items, args):
        with tempfile.TemporaryDirectory() as d:
            lst = os.path.join(d, "urls.json")
            with open(lst, "w") as fh:
                json.dump({"version": "t", "items": items}, fh)
            out = os.path.join(d, "out.json")
            f = FakeCopies(copies)
            argv = ["build_mt_bindings.py", "--list", lst, "--out", out] + args
            code = 0
            with mock.patch.object(bmb, "_get", f.get), mock.patch.object(sys, "argv", argv), \
                    mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                try:
                    bmb.main()
                except SystemExit as e:
                    code = e.code
            if not os.path.exists(out):
                return code, None, f
            with open(out) as fh:
                return code, json.load(fh), f

    def test_mirrors_and_exit_status(self):
        good, bad = b"archive bytes" * 100, b"another release" * 100
        items = [{"name": "6-1-2.pmtiles", "md5sum": hashlib.md5(good).hexdigest(), "size": len(good),
                  "min_zoom": 13, "max_zoom": 14}]
        code, doc, f = self._run({SRC: (good, '"a"'), MTD: (bad[:len(good)], '"b"')}, items, [])
        self.assertEqual(code, 0)
        self.assertEqual(doc["archives"]["6-1-2.pmtiles"]["mirrors"], {SRC: '"a"'}, "the other copy's MD5 differs")
        code, doc, f = self._run({SRC: (good, '"a"'), MTD: (good, '"b"')}, items, ["--mirrors", MTD])
        self.assertEqual(doc["archives"]["6-1-2.pmtiles"]["mirrors"], {MTD: '"b"'})
        self.assertEqual({c[0] for c in f.calls}, {MTD}, "only the asked mirror read")
        code, doc, f = self._run({SRC: (bad[:len(good)], '"a"'), MTD: (good, '"b"')}, items, ["--mirrors", SRC])
        self.assertEqual((code, doc["archives"]), (3, {}), "asked and nothing bound: a failed run")
        code, doc, f = self._run({}, items, ["--mirrors", "example.org"])
        self.assertTrue(code and doc is None and not f.calls, "an unknown mirror: refused before any read")


if __name__ == "__main__":
    unittest.main()
