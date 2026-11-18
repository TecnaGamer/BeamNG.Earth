"""New York's shard datasets keyed in the tiles that use them (nycshards.py; nycmassing / nyctanks): the build frozen per
process and named in the keys (blockworker._nyc_key), "none" before one is pinned, a new build (an in-place rebuild,
added cells, a first publication) only for the next process and under another identity, the shards checked against
the manifest, kept on disk by build. No network."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1])]
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import netfetch  # noqa: E402
import nycmassing as nm  # noqa: E402
import nycshards  # noqa: E402
import nyctanks as nt  # noqa: E402

LAT, LON = 40.7484, -73.9857
KEY = mapcells.cell_of(LAT, LON)
REL = f"{KEY.z}/{KEY.x}/{KEY.y}"
BASE = "https://example.invalid/nycmassing/v1"


def tanks_shard(n):
    import io
    b = io.BytesIO()
    np.savez_compressed(b, **nt.encode(np.zeros((n, 2)), [30] * n, [0] * n, [200] * n))
    return b.getvalue()


class Host:
    """Files by URL under BASE (the manifest whole); anything else a 404; fail: raised for every get."""

    def __init__(self, files):
        self.files, self.fail, self.asked = dict(files), None, []

    def get(self, url, kind="demand"):
        self.asked.append(url)
        if self.fail is not None:
            raise self.fail
        if url not in self.files:
            raise netfetch.NotFound(url)
        return netfetch.Response(200, self.files[url], {})


def manifest(cells, built="2026-11-10", data=None):
    """cells {rel: body}: the manifest naming each body's bytes and sha256."""
    m = {"format": 1, "built": built,
         "cells": {r: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()} for r, b in cells.items()}}
    if data:
        m["data"] = data
    return json.dumps(m).encode()


def site(cells, built="2026-11-10"):
    files = {f"{BASE}/manifest.json": manifest(cells, built)}
    files.update({f"{BASE}/{r}.npz": b for r, b in cells.items()})
    return Host(files)


class FrozenBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nycshards-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def src(self, host):
        return nt.Source(self.tmp, fetcher=host, base=BASE)

    def test_the_first_ask_fetches_a_missing_manifest(self):
        host = site({REL: tanks_shard(1)})
        s = self.src(host)
        self.assertEqual(s.shard(KEY), {}, "before a key decides: no data")
        self.assertEqual(host.asked, [], "nothing asked before a key")
        ident = s.identity()                        # (a tile's key: the manifest fetched now, decided)
        self.assertRegex(ident, r"^2026-11-10-[0-9a-f]{12}$")
        self.assertEqual(len(s.shard(KEY)["d"]), 1, "the same cell after the decision: not the earlier empty one")

    def test_none_is_for_the_whole_process(self):
        """Not published at the first ask: "none" for the process, even once it is published and pinned (a tile keyed
        "none" never reads a build); the next process takes the build."""
        host = Host({})
        s = self.src(host)
        self.assertEqual(s.identity(), "none")
        host.files.update(site({REL: tanks_shard(2)}).files)
        self.assertTrue(s.pin())
        self.assertEqual(s.identity(), "none")
        self.assertEqual(s.shard(KEY), {})
        self.assertEqual(len(self.src(host).shard(KEY) or {}), 0, "(another process: not decided before its key)")
        nxt = self.src(host)
        self.assertNotEqual(nxt.identity(), "none")
        self.assertEqual(len(nxt.shard(KEY)["d"]), 2)

    def test_a_slow_first_manifest_is_none(self):
        host = site({REL: tanks_shard(1)})
        s = self.src(host)

        def slow_pin(self_):
            import time as _t
            _t.sleep(0.5)
            return False
        with mock.patch.object(nycshards, "FIRST_WAIT_S", 0.05), mock.patch.object(nt.Source, "pin", slow_pin):
            self.assertEqual(s.identity(), "none")

    def test_overlapping_tiles_across_the_decision(self):
        """Tiles whose builds overlap the first ask: one decision for all, each tile's data the build its key names."""
        import threading
        host = site({REL: tanks_shard(3)})
        s = self.src(host)
        gate = threading.Event()
        real_get = host.get

        def slow_get(url, kind="demand"):
            gate.wait(2.0)
            return real_get(url, kind)
        host.get = slow_get
        early = s.shard(KEY)                        # (a Fetch before any key: nothing)
        out = {}

        def tile(name):
            ident = s.identity()
            out[name] = (ident, s.shard(KEY))
        ts = [threading.Thread(target=tile, args=(n,)) for n in ("a", "b", "c")]
        for t in ts:
            t.start()
        gate.set()
        for t in ts:
            t.join(5.0)
        self.assertEqual(early, {})
        idents = {v[0] for v in out.values()}
        self.assertEqual(len(idents), 1, out)
        self.assertNotEqual(idents.pop(), "none")
        self.assertTrue(all(len(v[1]["d"]) == 3 for v in out.values()))

    def test_an_in_place_rebuild_is_the_next_process_s(self):
        """A rebuild (same URLs, another shard, the same day): this process keeps its build and never reads the new
        shard under the old identity (it does not match: not had); the next process takes it under a new identity."""
        a, b = tanks_shard(1), tanks_shard(3)
        host = site({REL: a})
        s = self.src(host)
        s.pin()
        ident_a = s.identity()
        host.files.update(site({REL: b}).files)     # (republished in place)
        s.pin()                                     # (pinned for the next process)
        self.assertEqual(s.identity(), ident_a, "frozen for this process")
        self.assertIsNone(s.shard(KEY), "the new bytes are not build A's: not had (the tile faulted)")
        nxt = self.src(host)
        ident_b = nxt.identity()
        self.assertNotEqual(ident_b, ident_a)
        self.assertEqual(len(nxt.shard(KEY)["d"]), 3)
        self.assertNotEqual(nxt.path(KEY), s.path(KEY), "kept on disk under each build")

    def test_added_cells_change_the_identity(self):
        other = mapcells.CellKey(KEY.z, KEY.x + 1, KEY.y)
        s1 = self.src(site({REL: tanks_shard(1)}))
        s1.pin()
        host2 = site({REL: tanks_shard(1), f"{other.z}/{other.x}/{other.y}": tanks_shard(2)})
        s2 = nt.Source(tempfile.mkdtemp(dir=self.tmp), fetcher=host2, base=BASE)
        s2.pin()
        self.assertNotEqual(s1.identity(), s2.identity())
        self.assertEqual(s1.shard(other), {}, "not listed by the frozen build: none there")
        self.assertEqual(len(s2.shard(other)["d"]), 2)

    def test_a_404_unpins_and_a_listed_cell_gone_is_not_had(self):
        host = site({REL: tanks_shard(1)})
        s = self.src(host)
        s.pin()
        s.identity()
        host.files.pop(f"{BASE}/{REL}.npz")         # (the build's shard gone)
        self.assertIsNone(s.shard(KEY))
        host.files.clear()                          # (withdrawn: the manifest a 404)
        self.assertFalse(s.pin())
        self.assertEqual(self.src(host).identity(), "none", "the next process: none")

    def test_a_shard_of_other_bytes_is_not_read_nor_kept(self):
        good = tanks_shard(1)
        host = site({REL: good})
        host.files[f"{BASE}/{REL}.npz"] = tanks_shard(2)
        s = self.src(host)
        s.pin()
        s.identity()
        self.assertIsNone(s.shard(KEY))
        self.assertFalse(os.path.exists(s.path(KEY)))

    def test_a_legacy_manifest_checks_bytes(self):
        """The published manifests before sha256 (2026-11-10): the byte count is checked."""
        body = tanks_shard(1)
        files = {f"{BASE}/manifest.json": json.dumps({"format": 1, "built": "x", "cells": {REL: {"bytes": len(body)}}
                                                      }).encode(), f"{BASE}/{REL}.npz": body + b"x"}
        s = self.src(Host(files))
        s.pin()
        s.identity()
        self.assertIsNone(s.shard(KEY))
        self.assertTrue(nycshards.matches({"bytes": len(body)}, body))
        self.assertTrue(nycshards.matches({}, body))

    def test_off(self):
        with mock.patch.object(nt, "ON", False):
            self.assertEqual(nt.identity(self.tmp), "off")
        with mock.patch.object(nm, "ON", False):
            self.assertEqual(nm.identity(self.tmp), "off")
        self.assertEqual(nt.identity(None), "off")


class TileKeyTest(unittest.TestCase):
    def test_keys_near_new_york_only(self):
        import blockworker
        tmp = tempfile.mkdtemp(prefix="nycshards-key-")
        with mock.patch.object(blockworker, "_s", SimpleNamespace(md=SimpleNamespace(cache_dir=tmp))), \
                mock.patch.object(nycshards.Shards, "_pin_later", lambda self: None), \
                mock.patch.object(nycshards.Shards, "pin", lambda self: False):
            nm._SOURCES.clear()
            nt._SOURCES.clear()
            self.assertIsNone(blockworker._nyc_key(geodesy.EnuFrame(39.28, -76.59), (-500, -500, 500, 500)))
            self.assertEqual(blockworker._nyc_key(geodesy.EnuFrame(LAT, LON), (-500, -500, 500, 500)),
                             ("none", "none"))
            for name, src_of in (("nycmassing", nm.source), ("nyctanks", nt.source)):
                d = os.path.join(tmp, "pagesdata", name, "v1")
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "manifest.json"), "wb") as f:
                    f.write(manifest({REL: b"x"}, built="2026-11-14"))
            self.assertEqual(blockworker._nyc_key(geodesy.EnuFrame(LAT, LON), (-500, -500, 500, 500)),
                             ("none", "none"), "decided at the first key: none for this process")
            nm._SOURCES.clear()                     # (the main process: its own sources, never deciding)
            nt._SOURCES.clear()
            self.assertTrue(all(v.startswith("2026-11-14-") for v in blockworker._nyc_key(
                geodesy.EnuFrame(LAT, LON), (-500, -500, 500, 500), cache_dir=tmp)), "the main process's peek")
            nm._SOURCES.clear()                     # (the next process)
            nt._SOURCES.clear()
            got = blockworker._nyc_key(geodesy.EnuFrame(LAT, LON), (-500, -500, 500, 500))
            self.assertTrue(all(v.startswith("2026-11-14-") for v in got), got)
            self.assertEqual(blockworker._nyc_key(geodesy.EnuFrame(LAT, LON), (0, 0, 10, 10)), got, "frozen")
        nm._SOURCES.clear()
        nt._SOURCES.clear()


if __name__ == "__main__":
    unittest.main()
