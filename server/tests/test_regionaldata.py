"""regionaldata's writes: each fetch lands through a temporary file of its own, renamed over the file, so processes
fetching the same file at once on a cold cache each land a whole one (a shared temporary name had one's rename fail
and another read a part of the file), and a write that fails leaves nothing behind. Offline: a stand-in for
BeamNG.Earth-data's Pages serves the tests' slice of the regional data (tests/fixtures/regional)."""
import contextlib
import hashlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
import countrybounds  # noqa: E402
import regionaldata  # noqa: E402

FIXTURE = ROOT / "server" / "tests" / "fixtures" / "regional"


class Pages:
    """A fetcher standing in for BeamNG.Earth-data's Pages: the files of folder under <PAGES>/regional/."""

    def __init__(self, folder):
        self.folder, self.asked = Path(folder), []

    def get(self, url, kind="demand"):
        self.asked.append(url.rsplit("/", 1)[-1])
        return mock.Mock(body=(self.folder / url.rsplit("/", 1)[-1]).read_bytes())


@contextlib.contextmanager
def fetching(cache, fetcher):
    """regionaldata as a fresh process has it (no BNE_REGIONAL_DIR), configured with cache / fetcher; as it was
    afterwards."""
    env = {k: v for k, v in os.environ.items() if k != "BNE_REGIONAL_DIR"}
    fresh = {"cache": None, "fetcher": None, "manifest": None, "down_at": 0.0, "cells": {}}
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(regionaldata, "_state", fresh), \
            mock.patch.object(regionaldata, "LOCAL", None):
        regionaldata.configure(cache, fetcher)
        yield


class FetchesAtOnceTest(unittest.TestCase):
    """The band file fetched by several at once on a cold cache (processes that each need it at their first tile).
    Each fetch writes a temporary file of its own and renames it over the file: none fails on another's rename, none
    reads a part of the file, none is left without the borders (a checksum mismatch would keep countrybounds.Absent
    for the process's life)."""

    def test_two_at_once(self):
        real = os.replace
        both = threading.Barrier(2, timeout=10)

        def replace(src, dst):          # (both downloads written before either is renamed)
            both.wait()
            return real(src, dst)
        with tempfile.TemporaryDirectory() as cache, fetching(cache, Pages(FIXTURE)):
            want = regionaldata.manifest()["files"][countrybounds.FILE]
            got, errors = [], []

            def fetch():
                try:
                    got.append(regionaldata.file(countrybounds.FILE))
                except Exception as e:
                    errors.append(e)
            with mock.patch.object(os, "replace", replace):
                threads = [threading.Thread(target=fetch) for _ in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(30)
            self.assertEqual(errors, [])
            self.assertEqual(len(got), 2)
            self.assertEqual(got[0], got[1])
            self.assertEqual(hashlib.sha256(Path(got[0]).read_bytes()).hexdigest(), want)
            self.assertEqual([p.name for p in Path(got[0]).parent.iterdir() if p.name.endswith(".part")], [])

    def test_a_rename_refused_over_a_whole_file(self):
        """The rename refused because another fetch landed the file meanwhile (Windows: it is held open there): that
        file is the same bytes; this fetch's temporary file goes and the file is used."""
        real = os.replace

        def refused(src, dst):
            if os.path.exists(dst):
                raise PermissionError(f"in use: {dst}")
            return real(src, dst)

        class Racing(Pages):
            def get(self, url, kind="demand"):
                got = super().get(url, kind)
                if url.endswith(countrybounds.FILE):        # (another process's fetch lands it meanwhile)
                    os.makedirs(regionaldata._build_dir(), exist_ok=True)
                    Path(regionaldata._build_dir(), countrybounds.FILE).write_bytes(got.body)
                return got
        with tempfile.TemporaryDirectory() as cache, fetching(cache, Racing(FIXTURE)):
            want = regionaldata.manifest()["files"][countrybounds.FILE]
            with mock.patch.object(os, "replace", refused):
                path = regionaldata.file(countrybounds.FILE)
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), want)
            self.assertEqual([p.name for p in Path(path).parent.iterdir() if p.name.endswith(".part")], [])


class WriteTest(unittest.TestCase):
    def test_a_failed_write_leaves_nothing(self):
        """A write that fails part way (a full disk; here a body that is no bytes): its temporary file is removed and
        the error raised, the file as it was."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, countrybounds.FILE)
            with self.assertRaises(TypeError):
                regionaldata._write(path, "not bytes")
            self.assertEqual(os.listdir(d), [])
            regionaldata._write(path, b"whole")
            self.assertEqual(os.listdir(d), [countrybounds.FILE])


if __name__ == "__main__":
    unittest.main()


class OutageTest(unittest.TestCase):
    """GitHub down (the night of 1.9.0: 504s for hours): the manifest pinned on disk is used at once, whatever its age,
    and a stale one is refreshed in the background; it was fetched first by every process, which waited for it."""

    def test_a_stale_pin_is_used_at_once(self):
        import json
        import time

        class Down:
            calls = []

            def get(self, url, kind="demand"):
                self.calls.append(url)
                time.sleep(0.3)
                raise OSError("HTTP 504")
        with tempfile.TemporaryDirectory() as cache:
            d = os.path.join(cache, "pagesdata", "regional")
            os.makedirs(d)
            pin = os.path.join(d, "manifest.json")
            with open(pin, "w") as fh:
                json.dump({"built": "old", "files": {}}, fh)
            old = time.time() - regionaldata.MANIFEST_TTL_S - 60
            os.utime(pin, (old, old))
            down = Down()
            with fetching(cache, down):
                t = time.time()
                self.assertEqual(regionaldata.signature(), "old")
                self.assertLess(time.time() - t, 0.2, "no wait for the network")
                for _ in range(50):
                    if down.calls and regionaldata._state.get("fetching") is None:
                        break
                    time.sleep(0.05)
                self.assertEqual(len(down.calls), 1, "one background refresh")
                self.assertEqual(regionaldata.signature(), "old", "kept when it failed")

    def test_a_fresh_pin_asks_nothing(self):
        import json

        class Never:
            def get(self, url, kind="demand"):
                raise AssertionError("asked the network")
        with tempfile.TemporaryDirectory() as cache:
            d = os.path.join(cache, "pagesdata", "regional")
            os.makedirs(d)
            with open(os.path.join(d, "manifest.json"), "w") as fh:
                json.dump({"built": "new", "files": {}}, fh)
            with fetching(cache, Never()):
                self.assertEqual(regionaldata.signature(), "new")


class DataHostTest(unittest.TestCase):
    """The manifest's "data" (the build's files on the tile host, R2 behind the CDN): files read there, Pages when it
    fails."""

    def test_files_from_the_data_host_then_pages(self):
        import json
        body = (FIXTURE / countrybounds.FILE).read_bytes()
        m = json.loads((FIXTURE / "manifest.json").read_bytes())
        m["data"] = "https://tiles.test/data/regional/k1"

        class Site:
            def __init__(self, down):
                self.down, self.asked = down, []

            def get(self, url, kind="demand"):
                self.asked.append(url)
                if url.endswith("/manifest.json"):
                    return mock.Mock(body=json.dumps(m).encode())
                if self.down and url.startswith("https://tiles.test/"):
                    raise OSError("HTTP 522")
                return mock.Mock(body=body)
        for down in (False, True):
            with tempfile.TemporaryDirectory() as cache:
                site = Site(down)
                with fetching(cache, site):
                    self.assertTrue(regionaldata.file(countrybounds.FILE))
                want = [f"https://tiles.test/data/regional/k1/{countrybounds.FILE}"]
                if down:
                    want.append(f"{regionaldata._base()}/{countrybounds.FILE}")
                self.assertEqual(site.asked[1:], want)
