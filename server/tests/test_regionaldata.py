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


if __name__ == "__main__":
    unittest.main()
