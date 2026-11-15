"""tools/build_decisions.py: the shared decision archives' builder. Only plans made with the final map data versions
(the weekly datasets' builds included) are packed and count as done, so a cold cache's first plans, keyed before the
weekly manifests came, are planned again instead of dropped for good; the workers wait for those manifests; each
item's ring digest is the one a player's server makes from its own shards (decisions.Store); a rebuilt block is a new
file name (its content hash) with the old one retired, its items replaced by base key and the others kept; the plan
cache's revisit keys follow the map data's versions once they come (plancache.PlanCache.persist)."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "tools"))

import build_decisions as bd  # noqa: E402
import decisions  # noqa: E402
import fast  # noqa: E402
import netfetch  # noqa: E402
import plancache  # noqa: E402
from test_decisions import CELL, FAR, HAVE, SEED, VERSIONS, FakeShards, lane_shard  # noqa: E402

COLD = dict(VERSIONS, lanes=None)       # (a cold cache's first minutes: the lane manifest not read yet)


class FilesFetcher:
    """netfetch's get() over a folder (the CDN as the builder's output folder lays it out)."""

    def __init__(self, root, base):
        self.root, self.base = root, base

    def get(self, url, kind=None, lane=None):
        p = os.path.join(self.root, url[len(self.base) + 1:])
        if not os.path.isfile(p):
            raise netfetch.NotFound(url)

        class R:
            body = Path(p).read_bytes()
        return R()


class CompleteTest(unittest.TestCase):
    def test_cold_records_are_not_done(self):
        recs = [{"plans": [14, 1, 1], "versions": COLD}, {"plans": [14, 1, 2], "versions": VERSIONS},
                {"facts": "specs", "cell": "14/1/3", "versions": json.dumps(VERSIONS)}]
        final = bd.final_versions([{"versions": VERSIONS}, {"versions": dict(VERSIONS)}])
        self.assertEqual(final, VERSIONS)
        self.assertEqual(bd.complete_cells(recs, final), {(14, 1, 2)},
                         "the cell planned before the lane manifest came is planned again")
        self.assertIsNone(bd.final_versions([{"versions": VERSIONS}, {"versions": COLD}]), "workers disagree")
        self.assertIsNone(bd.final_versions([{"versions": VERSIONS, "error": "x"}]))
        self.assertEqual(bd.complete_cells(recs, None), set())

    def test_workers_wait_for_the_weekly_manifests(self):
        class MD:
            def __init__(self, known_after):
                self.calls, self.after = 0, known_after
                self.rail_tags = self.lane_tags = self.station_tags = None

            def versions(self):
                self.calls += 1
                return dict(VERSIONS) if self.calls > self.after else dict(COLD)
        slept = []
        md = MD(3)
        self.assertEqual(bd.wait_weekly(md, wait_s=60, sleep=slept.append), bd.weekly_of(VERSIONS))
        self.assertEqual(len(slept), 3)
        with mock.patch.object(bd.time, "monotonic", side_effect=[0.0, 0.0, 100.0]):
            self.assertIsNone(bd.wait_weekly(MD(99), wait_s=60, sleep=lambda s: None), "never known: not planned")


@unittest.skipUnless(HAVE, f"earthfast {decisions.NATIVE} not built")
class PackTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.out = os.path.join(self.dir, "out")
        self.vk, self.vkb = decisions.vkey(VERSIONS)
        self.doc = decisions.vkey_doc(VERSIONS)

    def build(self, shard, data=b"[1]", plans=b"npz-bytes", cells=(CELL, FAR)):
        """The builder's run over cells: records, every ring cell's way ids and slice from shard, packed."""
        src = {"rails": None, "lanes": FakeShards(shard)}
        ids = {CELL: ["101", "102"], FAR: ["555"]}
        ways = {}
        for c in cells:
            for rc in decisions.ring(c, decisions.RING_PLANS):
                ways[rc] = (ids.get(rc, []), decisions.slice_digest(src, rc, ids.get(rc, [])))
        pf = os.path.join(self.dir, "p.npz")
        Path(pf).write_bytes(plans)
        recs = []
        for c in cells:
            cs = f"{c[0]}/{c[1]}/{c[2]}"
            recs.append({"plans": list(c), "sig": "w1s1", "path": pf, "versions": VERSIONS})
            recs.append({"plans": list(c), "sig": "w0s0", "path": pf, "versions": COLD})       # (dropped)
            recs.append({"facts": "specs", "layer": "roads", "extra": [True], "cell": cs,
                         "versions": json.dumps(VERSIONS), "data": data.decode()})
        by = bd.block_items(recs, ways, VERSIONS, set(cells))
        return bd.pack(by, self.out, self.vk, self.vkb, self.doc, SEED, {"tool": "test"})

    def store(self, name, shard):
        base = "http://cdn.test/dec"
        return decisions.Store(os.path.join(self.dir, name), fetcher=FilesFetcher(self.out, base), base=base,
                               sources={"rails": None, "lanes": FakeShards(shard)})

    def test_the_players_digest_is_the_builders(self):
        self.build(lane_shard())
        pub = fast._ef.dec_public_key(SEED).hex()
        with mock.patch.dict(os.environ, {"BNGE_DECISIONS_PUBKEY": pub}):
            st = self.store("player", lane_shard())
            fk = decisions.facts_key("roads", "specs", [True], "14/4706/6245")
            self.assertEqual(st.item(VERSIONS, CELL, fk, decisions.RING_FACTS), b"[1]")
            self.assertEqual(st.item(VERSIONS, CELL, decisions.plans_key("w1s1", CELL), decisions.RING_PLANS),
                             b"npz-bytes")
            self.assertIsNone(st.item(VERSIONS, CELL, decisions.plans_key("w0s0", CELL), decisions.RING_PLANS),
                              "a cold record never packed")
            self.assertTrue(st.has_item(VERSIONS, CELL, decisions.plans_key("w1s1", CELL), decisions.RING_PLANS))

    def test_a_rebuild_is_a_new_name(self):
        first = self.build(lane_shard())
        root = Path(self.out, f"d{decisions.FORMAT}", self.vk)
        cur1 = json.loads((root / "current.json").read_text())
        bx, by = decisions.block_of(CELL)
        # (next week: way 101 retagged, CELL rebuilt alone; FAR's block untouched, CELL's merged)
        second = self.build(lane_shard(lanes_101="3"), data=b"[2]", cells=(CELL,))
        cur2 = json.loads((root / "current.json").read_text())
        self.assertNotEqual(cur1[f"{bx}/{by}"], cur2[f"{bx}/{by}"])
        fx, fy = decisions.block_of(FAR)
        self.assertEqual(cur1[f"{fx}/{fy}"], cur2[f"{fx}/{fy}"])
        old = f"12/{bx}/{by}.{cur1[f'{bx}/{by}']}.bngd"
        self.assertIn(old, json.loads((root / "retired.json").read_text()), "kept for its grace period")
        self.assertTrue((root / old).exists())
        man = json.loads(fast._ef.dec_open_manifest((root / "manifest.bngm").read_bytes(),
                                                    [fast._ef.dec_public_key(SEED)]))
        self.assertEqual(sorted(tuple(b) for b in man["blocks"]),
                         sorted((int(k.split("/")[0]), int(k.split("/")[1]), h) for k, h in cur2.items()))
        for (x, y), n in second.items():
            p = root / "12" / str(x) / f"{y}.{cur2[f'{x}/{y}']}.bngd"
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest()[:decisions.HASH_HEX], cur2[f"{x}/{y}"])
            self.assertEqual(p.stat().st_size, n)
        body = (root / "12" / str(bx) / f"{by}.{cur2[f'{bx}/{by}']}.bngd").read_bytes()
        keys = fast._ef.dec_block_info(body)[4]
        fk = decisions.facts_key("roads", "specs", [True], "14/4706/6245")
        self.assertEqual(len([k for k in keys if decisions.base_key(k) == fk]), 1, "replaced, not added")
        self.assertIsNotNone(first)


class PersistTest(unittest.TestCase):
    def test_revisit_keys_follow_the_versions(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        now = [dict(COLD)]

        class MD:
            def versions(self):
                return dict(now[0])

        class G:
            md = MD()
        with mock.patch.dict(os.environ, {plancache.RUN_ENV: "t"}), \
                mock.patch.object(plancache, "VERSIONS_RECHECK_S", 0.0):
            pc = plancache.for_grader(d, G(), None, None)
            self.assertIsNone(pc.persist[1][2]["lanes"])
            now[0] = dict(VERSIONS)
            self.assertEqual(pc.persist[1][2]["lanes"], VERSIONS["lanes"], "the lane manifest came")
            now[0] = dict(VERSIONS, lanes="l2")
            self.assertEqual(pc.persist[1][2]["lanes"], "l2", "a pinned week-old build refreshed")
            pc.persist = (d, ("f", "s", {"x": 1}))
            now[0] = dict(VERSIONS)
            self.assertEqual(pc.persist[1][2], {"x": 1}, "set by hand: as set")


if __name__ == "__main__":
    unittest.main()
