"""What the German Autobahn boards say at the user's benchmark junctions, from real OpenStreetMap data (fixtures/
de_autobahn, ODbL): the sign-shard builder's German block -> the shards' reader -> the glue's raw fields -> the kernel,
on straight synthetic geometry (the placement on real roads is test_de_autobahn_tile's). Checked against the photos
of these junctions (KartaView 1371954 / 7991305, Panoramax 889978ac / bb565635) and their mapped tags."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))

import build_sign_tags as builder  # noqa: E402
import deautobahn  # noqa: E402
import deautobahnfont  # noqa: E402
import fast  # noqa: E402
import railtags  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "de_autobahn"


def shards(region, tmp):
    """The German sign shards of a fixture region, read back as the server reads them."""
    d = json.loads((FIX / f"{region}.json").read_text(encoding="utf-8"))
    nodes = {n: (lon, lat, t) for n, lon, lat, t in d["nodes"]}
    main, links = [], []
    for wid, refs, tags in d["ways"]:
        row = (wid, np.array(refs, np.int64), tags)
        (links if tags.get("highway", "").endswith("_link") else main).append(row)
    junctions = {n: t for n, (_, _, t) in nodes.items() if t and t.get("highway") == "motorway_junction"}
    ids = np.array(sorted(nodes), np.int64)
    places = builder.Places(ids, np.array([nodes[i][0] for i in ids]), np.array([nodes[i][1] for i in ids]))
    found = builder.exits(main, links, junctions, {}, places)
    records = builder.exit_records(found)
    builder.write(tmp, records, {"exits": len(records), "through": 0, "miles": 0})
    return railtags.SignTags(None, None, local=tmp), nodes


def boards_at(tags, nodes, node, link_dest=None):
    """The kernel's boards (parsed JSON) of the exit record at junction node (the one whose link destination starts
    with link_dest, if given), on a straight synthetic approach with the record's own lanes and neighbours."""
    lon, lat, _ = nodes[node]
    recs = [r for r in tags.query(lon - 1e-4, lat - 1e-4, lon + 1e-4, lat + 1e-4, kinds=("exit",)) if "de" in r["exit"]]
    if link_dest is not None:
        recs = [r for r in recs if r["exit"]["de"]["link"].get("destination", "").startswith(link_dest)]
    assert recs, (node, link_dest)
    d = recs[0]["exit"]
    de = d["de"]
    raw = deautobahn.raw_fields(de)
    cols = {c: np.nan for c in deautobahn.JUNCTION_COLS}
    cols.update(id=recs[0]["id"], side=float(d["side"]), gore_s=-60.0, ref_s=65.0,
                prev_exit_s=de["prev_exit_s"] if de["prev_exit_s"] is not None else np.nan,
                other_motorway=float(bool(de["joins"]) and de["joins"] != de["main"]["ref"]),
                gore_x=60.0, gore_y=-9.0, gore_z=0.0, gore_dx=1.0, gore_dy=-0.1)
    if de["fork_s"] is not None and len(de["branches"]) == 2:
        cols.update(fork_s=de["fork_s"], fork_x=60.0 + de["fork_s"], fork_y=-20.0, fork_z=0.0, fork_dx=1.0, fork_dy=-0.3,
                    b0_dx=0.9, b0_dy=0.3, b1_dx=0.6, b1_dy=-0.8)
    s = np.arange(0.0, 2600.0, 10.0)
    lanes = np.array([deautobahn.lanes_at(de["edges"], v) or 3 for v in s], np.float64)
    prof = {c: np.zeros_like(s) for c in deautobahn.PROFILE_COLS}
    prof.update(s=s, x=-s, dx=np.ones_like(s), edge_l=-(lanes * 4 + 6) / 2, edge_r=(lanes * 4 + 6) / 2,
                opp_edge=np.full_like(s, np.nan), lanes=lanes, road_z_max=np.full_like(s, 0.2), de=np.ones_like(s))
    rows = np.ascontiguousarray(np.column_stack([prof[c] for c in deautobahn.PROFILE_COLS]))
    cps, glyphs, faces, params, kern = deautobahnfont.tables()
    picto = sorted(deautobahnfont.PICTOGRAMS.items())
    out = fast._ef.autobahn_signs(raw, np.array([[cols[c] for c in deautobahn.JUNCTION_COLS]], np.float64), rows,
                                  np.array([0, len(s)], np.int64), np.zeros((0, 2)), np.array([0, 0], np.int64),
                                  np.zeros((0, 2)), np.zeros(len(s) + 1, np.int64),
                                  cps, glyphs, faces, params, kern, [k for k, _ in picto],
                                  np.array([v for _, v in picto], np.int64), [-3000.0, -500.0, 3000.0, 500.0])
    return [json.loads(b) for b in out[4]]


def board(bs, name):
    got = [b for b in bs if b["board"] == name]
    return got[0] if got else None


class GoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fast.require(deautobahn.KERNEL, "German Autobahn sign tests")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.reg = shards("regensburg", os.path.join(cls.tmp.name, "r"))
        cls.ing = shards("ingolstadt", os.path.join(cls.tmp.name, "i"))

    def test_neutraubling_eastbound(self):
        bs = boards_at(*self.reg, 61681879)
        a = board(bs, "announce")
        self.assertEqual((a["mount"], a["panels"][0]["lines"], a["panels"][0]["number"], a["panels"][0]["printed"]),
                         ("cantilever", [["Neutraubling"]], "102", "1000 m"))
        adv = board(bs, "advance500")
        self.assertEqual(adv["mount"], "portal")
        self.assertEqual(adv["panels"][0]["lines"], [["Linz (A)", "Passau"], ["Rosenhof"]])
        self.assertEqual(adv["panels"][0]["plates"], ["3"])
        self.assertEqual(adv["panels"][1]["lines"], [["Neutraubling"], ["Donaustauf"], ["Barbing"]])
        self.assertEqual(adv["panels"][1]["printed"], "500 m")
        self.assertEqual([b["board"] for b in bs if b["board"].startswith("countdown")],
                         ["countdown3", "countdown2", "countdown1"])
        self.assertIsNotNone(board(bs, "direction"))
        self.assertEqual(board(bs, "gore")["panels"][0]["lines"], [["Ausfahrt"]])

    def test_neutraubling_westbound(self):
        bs = boards_at(*self.reg, 164128664)
        adv = board(bs, "advance500")
        self.assertEqual(adv["panels"][0]["lines"], [["Regensburg-Ost"]])
        self.assertEqual(adv["panels"][-1]["lines"], [["Neutraubling"], ["Barbing"]])

    def test_kreuz_regensburg_a3_west(self):
        bs = boards_at(*self.reg, 34554798)
        a = board(bs, "announce")
        self.assertEqual((a["mount"], a["panels"][0]["lines"], a["panels"][0]["number"], a["panels"][0]["printed"]),
                         ("portal", [["Kreuz Regensburg"]], "99", "2000 m"))
        adv = board(bs, "advance1000")
        self.assertEqual(adv["panels"][0]["lines"], [["Nürnberg"], ["Sinzing"]])
        self.assertEqual(adv["panels"][1]["lines"], [["Hof", "Weiden"], ["Regensburg"], ["München"], ["Ingolstadt"]])
        self.assertEqual(adv["panels"][1]["plates"], ["93"])
        fork = board(bs, "fork")
        self.assertEqual(sorted(p["lines"] for p in fork["panels"]),
                         sorted([[["Hof", "Weiden"], ["Regensburg"]], [["München"], ["Ingolstadt"]]]))
        self.assertIsNone(board(bs, "gore"))

    def test_kreuz_regensburg_a93_south_is_dense(self):
        """Kumpfmühl's exit leaves 618 m before the Kreuz's first A93 exit: the chain is squeezed after it."""
        bs = boards_at(*self.reg, 61097114)
        a, v = board(bs, "announce"), board(bs, "advance500")
        self.assertEqual(a["panels"][0]["lines"], [["Kreuz Regensburg"]])
        self.assertEqual(a["panels"][0]["printed"], "450 m")
        self.assertEqual(v["panels"][0]["printed"] or v["panels"][-1]["printed"], "300 m")
        self.assertIsNone(board(bs, "advance1000"))

    def test_lenting_keeps_the_mapped_text(self):
        bs = boards_at(*self.ing, 26869438)
        lines = [e for p in board(bs, "advance500")["panels"] for l in p["lines"] for e in l]
        self.assertIn("Gaimershaim", lines)              # as mapped (the sign says Gaimersheim): fix it in OSM
        self.assertIn("Audi GVZ", lines)

    def test_holledau(self):
        bs = boards_at(*self.ing, 139417)
        a = board(bs, "announce")
        self.assertEqual((a["panels"][0]["lines"], a["panels"][0]["number"]), ([["Dreieck Holledau"]], "65"))


if __name__ == "__main__":
    unittest.main()
