"""decisions.py / earthfast decisions.rs / tools/build_decisions.py: the shared decision archives. A block gives its
items back byte for byte (the npz transform is lossless and exact), is accepted only with a trusted key's signature,
its own block and version key and an intact body; a player's server finds a cell's plans and facts in a block served
over HTTP, plans the same heights from them without reading any map data, keeps them in its revisit cache under its
own keys, and falls back to planning on every miss: another version, no archive (404), a block not built, a damaged or
foreign-signed block. The version key follows the formats and the map data's versions, and no per-computer setting
(the telemetry's folder) is a build switch."""
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import decisions  # noqa: E402
import fast  # noqa: E402
import grading  # noqa: E402
import plancache  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402
import roadsurf  # noqa: E402
from test_grading import FakeMD  # noqa: E402
from test_roads import features, FRAME  # noqa: E402

HAVE = getattr(fast._ef, "VERSION", 0) >= decisions.NATIVE
SEED = bytes(range(32))
OTHER_SEED = bytes(range(1, 33))
VERSIONS = {"pipeline": 9, "overture": "2026-test", "rails": "r1", "lanes": "l1", "stations": "s1"}


class VersionedMD(FakeMD):
    def __init__(self, feats, versions=None):
        super().__init__(feats)
        self._v = dict(VERSIONS if versions is None else versions)

    def versions(self):
        return dict(self._v)


class Handler(http.server.BaseHTTPRequestHandler):
    root = None
    hits = []
    log_message = lambda *a: None  # noqa: E731

    def do_GET(self):
        Handler.hits.append(self.path)
        path = os.path.join(self.root, self.path.lstrip("/"))
        if not os.path.isfile(path):
            self.send_error(404)
            return
        body = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def npz(arrays):
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    return buf.getvalue()


def publish(root, versions, items, seed=SEED, blocks=None):
    """items {(z, x, y) cell: {key: (bytes, npz)}} into signed blocks and a manifest under root (as the builder)."""
    vk, vkb = decisions.vkey(versions)
    by = {}
    for cell, its in items.items():
        by.setdefault(decisions.block_of(cell), {}).update(its)
    for (bx, bby), its in by.items():
        body = fast._ef.dec_pack_block(decisions.BLOCK_Z, bx, bby, vkb, [(k, v[0], v[1]) for k, v in its.items()], seed)
        decisions._write(os.path.join(root, f"d{decisions.FORMAT}", vk, "12", str(bx), f"{bby}.bngd"), body)
    man = {"format": decisions.FORMAT, "vkey": vk, "doc": decisions.vkey_doc(versions),
           "blocks": sorted([list(b) for b in (blocks if blocks is not None else by)])}
    decisions._write(os.path.join(root, f"d{decisions.FORMAT}", vk, "manifest.bngm"),
                     fast._ef.dec_make_manifest(json.dumps(man).encode(), seed))
    return vk


@unittest.skipUnless(HAVE, f"earthfast {decisions.NATIVE} not built")
class CodecTest(unittest.TestCase):
    def test_npz_transform_round_trip(self):
        rng = np.random.default_rng(1)
        data = npz({"p": rng.normal(size=(300, 2)) * 1000.0, "z": rng.normal(size=300), "n": np.arange(9),
                    "meta": np.frombuffer(b'{"a": 1}', np.uint8), "e": np.zeros((0, 2)), "one": np.ones(1)})
        t = fast._ef.dec_npz_transform(data, True)
        self.assertNotEqual(t, data)
        self.assertEqual(len(t), len(data))
        self.assertEqual(fast._ef.dec_npz_transform(t, False), data)

    def test_block_items_and_checks(self):
        vk, vkb = decisions.vkey(VERSIONS)
        p = npz({"p": np.cumsum(np.full((500, 2), 0.37), axis=0) + 4.0e5})
        items = [("p/w1s1/14_1_2", p, True), ("f/roads/specs/[true]/14/1/2", b'[{"a": 1}]' * 50, False)]
        b = fast._ef.dec_pack_block(12, 0, 0, vkb, items, SEED)
        pub = fast._ef.dec_public_key(SEED)
        self.assertEqual(fast._ef.dec_verify_block(b, 12, 0, 0, vkb, [pub]), 2)
        self.assertEqual(fast._ef.dec_block_item(b, "p/w1s1/14_1_2"), p)
        self.assertEqual(fast._ef.dec_block_item(b, "f/roads/specs/[true]/14/1/2"), b'[{"a": 1}]' * 50)
        self.assertIsNone(fast._ef.dec_block_item(b, "p/w0s0/14_1_2"))
        self.assertLess(len(b), len(p) + 600, "packed")
        for bad, why in ((lambda: fast._ef.dec_verify_block(b, 12, 0, 1, vkb, [pub]), "another block"),
                         (lambda: fast._ef.dec_verify_block(b, 12, 0, 0, bytes(32), [pub]), "another version"),
                         (lambda: fast._ef.dec_verify_block(b, 12, 0, 0, vkb, [fast._ef.dec_public_key(OTHER_SEED)]),
                          "another builder's key"),
                         (lambda: fast._ef.dec_verify_block(b, 12, 0, 0, vkb), "the test key is not the project's"),
                         (lambda: fast._ef.dec_verify_block(b[:-1] + bytes([b[-1] ^ 1]), 12, 0, 0, vkb, [pub]),
                          "a changed byte")):
            with self.assertRaises(ValueError, msg=why):
                bad()
        # the project's builder key only (made on the mini PC, 2026-11-13, with the user's go)
        self.assertEqual([bytes(k).hex() for k in fast._ef.dec_trusted_keys()],
                         ["33255e3e9e9182e0edcf1d528f347b520ae6fd17e7eb9f145349fc2b97aee57e"])

    def test_manifest(self):
        m = fast._ef.dec_make_manifest(b'{"blocks": []}', SEED)
        self.assertEqual(fast._ef.dec_open_manifest(m, [fast._ef.dec_public_key(SEED)]), b'{"blocks": []}')
        with self.assertRaises(ValueError):
            fast._ef.dec_open_manifest(m, [fast._ef.dec_public_key(OTHER_SEED)])
        with self.assertRaises(ValueError):
            fast._ef.dec_open_manifest(m[:-2] + b"xx", [fast._ef.dec_public_key(SEED)])


class KeyTest(unittest.TestCase):
    def test_version_key(self):
        a = decisions.vkey(VERSIONS)
        self.assertEqual(a, decisions.vkey(json.dumps(VERSIONS, sort_keys=True)), "a cell id's JSON: the same")
        self.assertIsNone(decisions.vkey(dict(VERSIONS, rails=None)), "a weekly build not known yet")
        self.assertNotEqual(a, decisions.vkey(dict(VERSIONS, overture="2026-other")))
        with mock.patch.object(plancache, "PLANS_FORMAT", plancache.PLANS_FORMAT + 1):
            self.assertNotEqual(a[0], decisions.vkey(VERSIONS)[0], "a plans format bump")
        with mock.patch.object(roads, "FACTS_FORMAT", roads.FACTS_FORMAT + 1):
            self.assertNotEqual(a[0], decisions.vkey(VERSIONS)[0], "a facts format bump")

    def test_per_computer_settings_are_not_switches(self):
        revisit._switches.clear()
        base = revisit.switches_key()
        try:
            with mock.patch.dict(os.environ, {"BNE_NETSTATS_PROC": "/home/someone/.cache/x/netstats/proc",
                                              "BNE_LIDAR_GAPS_WAIT": "120"}):
                revisit._switches.clear()
                self.assertEqual(revisit.switches_key(), base)
            flip = "on" if os.environ.get("BNE_ROADSURF", "").lower() == "off" else "off"    # (the gate runs with off)
            with mock.patch.dict(os.environ, {"BNE_ROADSURF": flip}):
                revisit._switches.clear()
                self.assertNotEqual(revisit.switches_key(), base, "a real switch still counts")
        finally:
            revisit._switches.clear()

    def test_pins(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.assertIsNone(decisions.pinned_keys(d, ["decisions/d1/v/12/1/1.bngd"]))
        decisions.pin(d, "v", (10, 20, 11, 21))
        got = decisions.pinned_keys(d, ["decisions/d1/v/12/10/21.bngd", "decisions/d1/v/12/12/21.bngd",
                                        "decisions/d1/w/12/10/21.bngd", "decisions/d1/v/manifest.bngm",
                                        "decisions/pins.json", "mapdata/x"])
        self.assertEqual(got, [True, False, False, True, True, False])


@unittest.skipUnless(HAVE, f"earthfast {decisions.NATIVE} not built")
class SharedPlansTest(unittest.TestCase):
    """A builder's plans served over HTTP: a player's server reads them instead of planning."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        Handler.root, Handler.hits = os.path.join(self.dir, "cdn"), []
        env = {plancache.RUN_ENV: "build", decisions.ENV: "on",
               "BNGE_DECISIONS_URL": f"http://127.0.0.1:{self.srv.server_address[1]}",
               "BNGE_DECISIONS_PUBKEY": fast._ef.dec_public_key(SEED).hex()}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        decisions._STORES.clear()
        self.addCleanup(decisions._STORES.clear)
        old = revisit.CACHE_DIR
        self.addCleanup(setattr, revisit, "CACHE_DIR", old)
        # (the photo's road-surface verdicts of this test's own folders only: none)
        revisit.CACHE_DIR = os.path.join(self.dir, "builder")
        roadsurf._entries.clear()
        roadsurf._digests.clear()
        self.addCleanup(roadsurf._digests.clear)
        self.addCleanup(roadsurf._entries.clear)
        self.f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        self.la, self.lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)

    def grader(self, cache, md):
        g = grading.Grader(FRAME, md, lambda la, lo: 10.0 + 0.01 * np.asarray(la) * 0)
        g.shared = plancache.for_grader(cache, g, None, None)
        return g

    def build(self):
        """The builder's run: (heights, {cell: {key: (npz bytes, True)}}) from its run folder."""
        cache = os.path.join(self.dir, "builder")
        z = self.grader(cache, VersionedMD(self.f)).apply(self.la, self.lo, np.zeros(50))
        run = os.path.join(cache, "plans", "build", plancache.FRAMES)
        items = {}
        for sig in os.listdir(run):
            for n in os.listdir(os.path.join(run, sig)):
                if n.endswith(".npz"):
                    cell = tuple(int(v) for v in n[:-4].split("_"))
                    items.setdefault(cell, {})[decisions.plans_key(sig, cell)] = (
                        Path(run, sig, n).read_bytes(), True)
        self.assertTrue(items)
        return z, items

    def player(self, name, md):
        os.environ[plancache.RUN_ENV] = name
        cache = os.path.join(self.dir, name)
        revisit.CACHE_DIR = cache
        return cache, self.grader(cache, md)

    def test_plans_from_the_archive(self):
        za, items = self.build()
        publish(Handler.root, VERSIONS, items)
        md = VersionedMD(self.f)
        cache, g = self.player("player", md)
        zb = g.apply(self.la, self.lo, np.zeros(50))
        self.assertEqual(md.calls, 0, "nothing planned: no map data read")
        self.assertTrue(np.array_equal(za, zb))
        st = decisions.store_for(cache).stats
        self.assertEqual((st["hits"], st["bad"]), (len(items), 0))
        # (kept in the player's own revisit cache: a later run reads them without the archive)
        os.environ[plancache.RUN_ENV] = "later"
        shutil.rmtree(os.path.join(cache, decisions.SUBDIR))
        decisions._STORES.clear()
        Handler.hits = []
        md2 = VersionedMD(self.f)
        self.assertTrue(np.array_equal(za, self.grader(cache, md2).apply(self.la, self.lo, np.zeros(50))))
        self.assertEqual((md2.calls, Handler.hits), (0, []))

    def test_misses_fall_back_to_planning(self):
        za, items = self.build()
        cases = {
            "another version": lambda: publish(Handler.root, dict(VERSIONS, overture="2026-other"), items),
            "no archive at all": lambda: None,
            "a block not built": lambda: publish(Handler.root, VERSIONS, {}, blocks=[]),
            "another builder's key": lambda: publish(Handler.root, VERSIONS, items, seed=OTHER_SEED),
        }
        for name, setup in cases.items():
            with self.subTest(name):
                shutil.rmtree(Handler.root, True)
                decisions._STORES.clear()
                setup()
                md = VersionedMD(self.f)
                _cache, g = self.player(name.replace(" ", "_"), md)
                zb = g.apply(self.la, self.lo, np.zeros(50))
                self.assertGreater(md.calls, 0, "planned here")
                self.assertTrue(np.array_equal(za, zb))

    def test_damaged_block_rejected(self):
        za, items = self.build()
        vk = publish(Handler.root, VERSIONS, items)
        for p in Path(Handler.root, f"d{decisions.FORMAT}", vk, "12").rglob("*.bngd"):
            b = bytearray(p.read_bytes())
            b[-3] ^= 0x40
            p.write_bytes(bytes(b))
        md = VersionedMD(self.f)
        cache, g = self.player("damaged", md)
        self.assertTrue(np.array_equal(za, g.apply(self.la, self.lo, np.zeros(50))))
        self.assertGreater(md.calls, 0)
        self.assertGreater(decisions.store_for(cache).stats["bad"], 0)
        self.assertFalse(list(Path(cache, decisions.SUBDIR).rglob("*.bngd")), "never kept")

    def test_off_by_default(self):
        _za, items = self.build()
        publish(Handler.root, VERSIONS, items)
        Handler.hits = []
        with mock.patch.dict(os.environ, {decisions.ENV: "off"}):
            md = VersionedMD(self.f)
            _cache, g = self.player("off", md)
            g.apply(self.la, self.lo, np.zeros(50))
            self.assertGreater(md.calls, 0)
            self.assertEqual(Handler.hits, [])

    def test_facts_from_the_archive(self):
        cell = "14/4686/6270"
        cid = (cell, "roads", json.dumps(VERSIONS, sort_keys=True))
        extra = (True, False)
        data = json.dumps([{"kind": "x"}]).encode()
        publish(Handler.root, VERSIONS, {(14, 4686, 6270): {decisions.facts_key("roads", "specs", list(extra), cell):
                                                             (data, False)}})
        cache = os.path.join(self.dir, "factsplayer")
        revisit.CACHE_DIR = cache

        class F:
            pass
        f = F()
        f.__dict__["_bnge_cell"] = cid
        self.assertEqual(roads._facts_baked(f, "specs", extra), data)
        self.assertIsNone(roads._facts_baked(f, "ranges", None), "not in the block")
        got = revisit.load(cache, "cellfacts", roads._facts_key(f, "specs", extra))
        self.assertEqual(got[0]["facts.json"], data, "kept under the player's own key")


if __name__ == "__main__":
    unittest.main()
