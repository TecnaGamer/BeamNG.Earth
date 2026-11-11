"""The German Autobahn signs' kernel (earthfast autobahn_signs / autobahn_columns): its Python contract, input checks,
the boards of one synthetic exit, the fixed material set and the vertex cap. The rules themselves are tested in Rust
(native/earthfast/src/deautobahn/*.rs)."""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import deautobahn  # noqa: E402
import deautobahnfont  # noqa: E402
import fast  # noqa: E402

NEUTRAUBLING = {
    "node_name": "Neutraubling", "node_ref": "102", "main_ref": "A 3",
    "lanes_destination": "Linz;Passau;Rosenhof|Linz;Passau;Rosenhof|Linz;Passau;Rosenhof|Neutraubling;Donaustauf;Barbing",
    "lanes_destination_country": "A;;|A;;|A;;|", "lanes_turn": "none|none|none|slight_right",
    "link_destination": "Neutraubling;Donaustauf;Barbing",
}


def junction(x0=0.0, y0=0.0, fields=NEUTRAUBLING, jid=-20, lanes=3.0):
    """One exit at (x0, y0) on a straight eastbound carriageway: (raw, junction row, profile rows, skips)."""
    raw = [fields.get(f, "") for f in deautobahn.RAW_FIELDS]
    cols = {c: np.nan for c in deautobahn.JUNCTION_COLS}
    cols.update(id=jid, side=1.0, gore_s=-60.0, ref_s=65.0, other_motorway=0.0, gore_x=x0 + 60.0, gore_y=y0 - 9.0,
                gore_z=0.0, gore_dx=1.0, gore_dy=-0.1)
    row = [cols[c] for c in deautobahn.JUNCTION_COLS]
    s = np.arange(0.0, 2600.0, 10.0)
    prof = {c: np.zeros_like(s) for c in deautobahn.PROFILE_COLS}
    prof.update(s=s, x=x0 - s, y=np.full_like(s, y0), dx=np.ones_like(s), dy=np.zeros_like(s),
                edge_l=np.full_like(s, -7.0), edge_r=np.full_like(s, 7.0), opp_edge=np.full_like(s, np.nan),
                lanes=np.full_like(s, lanes), ground_l=np.zeros_like(s), ground_r=np.zeros_like(s),
                road_z_max=np.full_like(s, 0.2), de=np.ones_like(s))
    rows = np.column_stack([prof[c] for c in deautobahn.PROFILE_COLS])
    return raw, row, rows, np.zeros((0, 2))


def call(junctions, tile=(-3000.0, -500.0, 3000.0, 500.0), block=None):
    """The kernel on these junctions; block: one occupied lateral interval (y0, y1) at every profile sample."""
    raws, jrows, profs, skips = zip(*junctions)
    raw = [v for r in raws for v in r]
    poff = np.cumsum([0] + [len(p) for p in profs]).astype(np.int64)
    soff = np.cumsum([0] + [len(s) for s in skips]).astype(np.int64)
    m = int(poff[-1])
    blocks = np.tile(np.array([block], np.float64), (m, 1)) if block else np.zeros((0, 2))
    boff = (np.arange(m + 1) if block else np.zeros(m + 1)).astype(np.int64)
    cps, glyphs, faces, params, kern = deautobahnfont.tables()
    picto = sorted(deautobahnfont.PICTOGRAMS.items())
    return fast._ef.autobahn_signs(
        raw, np.ascontiguousarray(np.array(jrows, np.float64)), np.ascontiguousarray(np.vstack(profs)), poff,
        np.ascontiguousarray(np.vstack(skips).reshape(-1, 2)), soff, np.ascontiguousarray(blocks), boff,
        cps, glyphs, faces, params, kern,
        [k for k, _ in picto], np.array([v for _, v in picto], np.int64), [float(v) for v in tile])


class KernelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fast.require(deautobahn.KERNEL, "German Autobahn sign tests")

    def test_columns_agree(self):
        raw, jcols, pcols = fast._ef.autobahn_columns()
        self.assertEqual((tuple(raw), tuple(jcols), tuple(pcols)),
                         (deautobahn.RAW_FIELDS, deautobahn.JUNCTION_COLS, deautobahn.PROFILE_COLS))

    def test_bad_shapes_raise_value_error(self):
        raw, row, prof, skips = junction()
        cps, glyphs, faces, params, kern = deautobahnfont.tables()
        good = dict(raw=raw, jun=np.array([row]), prof=prof, poff=np.array([0, len(prof)], np.int64),
                    skips=np.zeros((0, 2)), soff=np.array([0, 0], np.int64), glyphs=glyphs)

        def run(**kw):
            a = {**good, **kw}
            m = len(a["prof"])
            return fast._ef.autobahn_signs(a["raw"], np.ascontiguousarray(a["jun"], np.float64),
                                           np.ascontiguousarray(a["prof"], np.float64), a["poff"],
                                           np.ascontiguousarray(a["skips"], np.float64), a["soff"],
                                           np.zeros((0, 2)), a.get("boff", np.zeros(m + 1, np.int64)), cps,
                                           np.ascontiguousarray(a["glyphs"]), faces, params, kern, [], np.zeros(0, np.int64),
                                           [-3000.0, -500.0, 3000.0, 500.0])
        run()
        for bad in (dict(glyphs=glyphs[:, :5]), dict(raw=raw[:-1]), dict(poff=np.array([0, len(prof) + 5], np.int64)),
                    dict(jun=np.array([row[:-1]])), dict(soff=np.array([0, 3], np.int64)),
                    dict(boff=np.zeros(3, np.int64))):
            with self.subTest(bad=list(bad)), self.assertRaises(ValueError):
                run(**bad)

    def test_one_exit_mesh(self):
        p, t, uv, mi, boards = call([junction()])
        got = [json.loads(b) for b in boards]
        kinds = [(b["board"], b["mount"]) for b in got]
        self.assertEqual(kinds, [("announce", "cantilever"), ("advance500", "portal"), ("countdown3", "post"),
                                 ("countdown2", "post"), ("countdown1", "post"), ("direction", "portal"),
                                 ("gore", "goresign")])
        ann = got[0]
        self.assertEqual((ann["panels"][0]["lines"], ann["panels"][0]["number"], ann["panels"][0]["printed"]),
                         ([["Neutraubling"]], "102", "1000 m"))
        adv = got[1]["panels"]
        self.assertEqual(adv[0]["lines"], [["Linz (A)", "Passau"], ["Rosenhof"]])
        self.assertEqual(adv[1]["printed"], "500 m")
        for b in got:
            if b["mount"] in ("portal", "cantilever"):
                self.assertTrue(all(pa["z0"] >= 0.2 + 5.0 for pa in b["panels"]), b)
        self.assertEqual(p.shape[1], 3)
        self.assertEqual(t.shape[1], 3)
        self.assertEqual(len(mi), len(t))
        self.assertLess(int(mi.max()), len(deautobahnfont.MATERIALS))
        self.assertLess(int(t.max()), len(p))
        # the panels' faces (blue) look against the travel direction (+x): toward the driver
        blue = t[mi == deautobahnfont.MATERIALS.index("bne_de_blue")]
        n = np.cross(p[blue[:, 1]] - p[blue[:, 0]], p[blue[:, 2]] - p[blue[:, 0]])
        front = n[np.abs(n[:, 0]) > 1e-9]
        self.assertGreater((front[:, 0] < 0).mean(), 0.45)
        letters = t[mi == 0]
        self.assertGreater(len(letters), 2 * len("Neutraubling"))

    def test_posts_keep_off_other_pavement(self):
        """A ramp 1 m outside the right edge all along (lateral 8..16 m): no post foot on it; the gantries stay
        (their posts go past it), the countdown posts beside the road find no place and are left out."""
        p, t, uv, mi, boards = call([junction()], block=(8.0, 16.0))
        got = [json.loads(b) for b in boards]
        for b in got:
            for x, y in b["posts"]:
                lateral = -(y - 500.0)          # travel +x: the driver's right is -y; the approach lies at y = 0
                if b["board"] != "gore":
                    self.assertFalse(8.0 - 0.75 < lateral < 16.0 + 0.75, (b["board"], lateral))
        kinds = {b["board"] for b in got}
        self.assertIn("advance500", kinds)
        self.assertFalse(any(k.startswith("countdown") for k in kinds))

    def test_boards_keep_off_decks(self):
        """A deck from 1050 to 1075 m over the announcement's 1065 m: the board moves to its end, and the sample it
        stands at is off the deck too (1080 m, not 1070 m: the two are equally near; Codex review of #999)."""
        raw, row, rows, _ = junction()
        p, t, uv, mi, boards = call([(raw, row, rows, np.array([[1050.0, 1075.0]]))])
        got = [json.loads(b) for b in boards]
        ann = [b for b in got if b["board"] == "announce"]
        self.assertEqual(len(ann), 1)
        for b in got:
            if b["board"] not in ("gore", "fork"):
                self.assertFalse(1050.0 < -b["at"][0] < 1075.0, (b["board"], b["at"]))

    def test_materials_fixed_set(self):
        _, _, _, mi, _ = call([junction(), junction(0.0, 300.0, jid=-21)])
        self.assertTrue(set(np.unique(mi).tolist()) <= set(range(len(deautobahnfont.MATERIALS))))

    def test_vertex_cap(self):
        js = [junction(0.0, 60.0 * k, jid=-100 - k) for k in range(40)]
        p, t, uv, mi, boards = call(js, tile=(-3000.0, -2500.0, 3000.0, 2500.0))   # centred on junction -100
        self.assertLessEqual(len(p), 60000)
        ids = {json.loads(b)["j"] for b in boards}
        self.assertIn(-100, ids)                        # the junction nearest the tile's centre is kept
        self.assertIn(-101, ids)
        self.assertNotIn(-139, ids)                     # the farthest is dropped
        self.assertLess(len(ids), 40)


if __name__ == "__main__":
    unittest.main()
