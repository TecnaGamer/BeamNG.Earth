"""The German raw block of the highway sign shards (tools/build_sign_tags.py, server/signshards.py): exit records whose
junction lies inside the German outline keep the sign text as mapped (junction name and number, per-lane destinations,
the link chain, the through way, the neighbouring junctions, a 2.5 km approach); every other record stays as it was.
Built with the builder's own functions on tiny networks (no pyosmium, as the data repo's job has it but CI not)."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))
import gzip
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))

import signshards  # noqa: E402
import build_sign_tags as builder  # noqa: E402

BASE_KEYS = {"label", "approach", "ramp", "main_ids", "ramp_id", "side", "through_label"}
DE = (49.02, 12.16)          # Barbing (lat, lon)
CZ = (49.74, 13.37)          # Pilsen: Czech and well away from the border (the outline carries a buffer)
US = (39.0, -76.95)
NEUTRAUBLING_LANES = {
    "lanes": "4",
    "turn:lanes": "none|none|none|slight_right",
    "destination:lanes": "Linz;Passau;Rosenhof|Linz;Passau;Rosenhof|Linz;Passau;Rosenhof|Neutraubling;Donaustauf;Barbing",
    "destination:country:lanes": "A;;|A;;|A;;|",
}


def at(origin):
    lat0, lon0 = origin

    def ll(x, y):
        return lon0 + x / (111320.0 * np.cos(np.radians(lat0))), lat0 + y / 110540.0
    return ll


def places_of(pts):
    ids = np.array(sorted(pts), np.int64)
    return builder.Places(ids, np.array([pts[i][0] for i in ids]), np.array([pts[i][1] for i in ids]))


def neutraubling(origin, step=100.0, length=3000.0):
    """A motorway eastbound (way 10: nodes every `step` m from -length to 0, then way 11 on to +2000), an exit ramp
    (way 20) at junction node 1000 with Neutraubling's tags."""
    ll = at(origin)
    xs = np.arange(-length, 0.0 + step / 2, step)
    pts, refs = {}, []
    for i, x in enumerate(xs):
        nid = 1000 - (len(xs) - 1 - i)
        pts[nid] = ll(x, 0.0)
        refs.append(nid)
    pts[1001], pts[1002] = ll(1000.0, 0.0), ll(2000.0, 0.0)
    pts[2001], pts[2002] = ll(80.0, -40.0), ll(160.0, -120.0)
    main = [(10, np.array(refs, np.int64), {"highway": "motorway", "oneway": "yes", "ref": "A 3", "int_ref": "E 56",
                                             **NEUTRAUBLING_LANES}),
            (11, np.array([1000, 1001, 1002], np.int64), {"highway": "motorway", "oneway": "yes", "ref": "A 3",
                                                           "destination": "Linz;Passau;Rosenhof",
                                                           "destination:country": "A;;"})]
    links = [(20, np.array([1000, 2001, 2002], np.int64), {"highway": "motorway_link", "oneway": "yes",
                                                            "destination": "Neutraubling;Donaustauf;Barbing"})]
    junctions = {1000: {"highway": "motorway_junction", "ref": "102", "name": "Neutraubling"}}
    return main, links, junctions, places_of(pts)


def build(net, german=True):
    """The exit records and the decoded shard bodies of a network, the German pass on or forced off."""
    main, links, junctions, places = net
    off = mock.patch.object(signshards, "in_germany", lambda lon, lat: np.zeros(np.size(lon), bool))
    with tempfile.TemporaryDirectory() as tmp:
        if german:
            found = builder.exits(main, links, junctions, {}, places)
        else:
            with off:
                found = builder.exits(main, links, junctions, {}, places)
        records = builder.exit_records(found)
        builder.write(tmp, records, {"exits": len(records), "through": 0, "miles": 0})
        bodies = {}
        for p in sorted(Path(tmp).rglob("*.json.gz")):
            bodies[str(p.relative_to(tmp))] = json.loads(gzip.decompress(p.read_bytes()))
    return records, bodies


class OutsideGermanyTest(unittest.TestCase):
    def test_outside_germany_unchanged(self):
        for origin in (CZ, US):
            with self.subTest(origin=origin):
                records, bodies = build(neutraubling(origin))
                plain_records, plain_bodies = build(neutraubling(origin), german=False)
                self.assertEqual(len(records), 1)
                self.assertEqual(set(records[0]["d"]), BASE_KEYS)
                self.assertEqual(bodies, plain_bodies)
                self.assertTrue(all("de" not in b for b in bodies.values()))


class InsideGermanyTest(unittest.TestCase):
    def test_inside_germany_adds_block(self):
        records, bodies = build(neutraubling(DE))
        plain, _ = build(neutraubling(DE), german=False)
        self.assertEqual(len(records), 1)
        d = records[0]["d"]
        de = d["de"]
        self.assertEqual(de["v"], 1)
        self.assertEqual(de["node"], {"ref": "102", "name": "Neutraubling", "noref": False})
        src = de["edges"][de["lanes_src"]][3]
        self.assertEqual(src["destination:lanes"], NEUTRAUBLING_LANES["destination:lanes"])
        self.assertEqual(src["destination:country:lanes"], "A;;|A;;|A;;|")     # the empty slots kept
        self.assertEqual(src["turn:lanes"], "none|none|none|slight_right")
        self.assertEqual(de["link"]["destination"], "Neutraubling;Donaustauf;Barbing")
        self.assertEqual(de["through"]["destination"], "Linz;Passau;Rosenhof")
        self.assertEqual(de["through"]["destination:country"], "A;;")
        self.assertEqual(de["main"], {"ref": "A 3", "int_ref": "E 56"})
        self.assertIsNone(de["joins"])
        rest = {k: v for k, v in d.items() if k not in ("de", "de_approach", "de_ramp")}
        self.assertEqual(rest, plain[0]["d"])
        self.assertEqual([b.get("de") for b in bodies.values()], [1])

    def test_trunk_exit_gets_no_block(self):
        """Autobahnen only: an exit off a Bundesstrasse mapped as trunk (Regensburg's GVZ / Hafen exits) keeps the plain
        record, no German block (Codex review of #999)."""
        main, links, junctions, places = neutraubling(DE)
        main = [(w, r, {**t, "highway": "trunk"}) for w, r, t in main]
        links = [(w, r, {**t, "highway": "trunk_link"}) for w, r, t in links]
        records, bodies = build((main, links, junctions, places))
        plain, plain_bodies = build((main, links, junctions, places), german=False)
        self.assertEqual(len(records), 1)
        self.assertNotIn("de", records[0]["d"])
        self.assertEqual(bodies, plain_bodies)

    def test_long_walk_is_separate(self):
        records, _ = build(neutraubling(DE))
        plain, _ = build(neutraubling(DE), german=False)
        d, lon, lat = records[0]["d"], records[0]["lon"], records[0]["lat"]

        def length(path):
            p = np.asarray(signshards.decode_path(path, lon, lat))
            q = p * [111320.0 * np.cos(np.radians(lat)), 110540.0]
            return float(np.hypot(*np.diff(q, axis=0).T).sum())
        self.assertAlmostEqual(length(d["approach"]), 1850.0, delta=60.0)   # (stops at the first node past 1.8 km)
        self.assertGreaterEqual(length(d["de_approach"]), 2300.0)
        self.assertEqual(d["main_ids"], plain[0]["d"]["main_ids"])
        self.assertEqual(d["de"]["stop"], "limit")


def corridor(origin):
    """One eastbound carriageway (way 10, nodes every 100 m from -6000 to +6000) with:
    junction 'Regensburg-Ost' (101) at -2000 (exit link 30) and its entry link 31 joining at -1500;
    junction 'Neutraubling' (102) at 0 (exit link 20); a rest area 'Rastplatz Erlgrund' (noref) at +1000 (link 40);
    junction 'Rosenhof' (103) at +2000 (exit link 50)."""
    ll = at(origin)
    xs = np.arange(-6000.0, 6000.0 + 50.0, 100.0)
    pts, refs = {}, []
    for x in xs:
        nid = 5000 + int(round(x / 100.0))
        pts[nid] = ll(x, 0.0)
        refs.append(nid)
    main = [(10, np.array(refs, np.int64), {"highway": "motorway", "oneway": "yes", "ref": "A 3", "lanes": "3"})]
    links, k = [], 9000

    def ramp(wid, x0, dest, entry=False):
        nonlocal k
        a, b = k, k + 1
        k += 2
        if entry:
            pts[a], pts[b] = ll(x0 - 160.0, -120.0), ll(x0 - 80.0, -40.0)
            refs_ = [a, b, 5000 + int(round(x0 / 100.0))]
        else:
            pts[a], pts[b] = ll(x0 + 80.0, -40.0), ll(x0 + 160.0, -120.0)
            refs_ = [5000 + int(round(x0 / 100.0)), a, b]
        tags = {"highway": "motorway_link", "oneway": "yes"}
        if dest:
            tags["destination"] = dest
        links.append((wid, np.array(refs_, np.int64), tags))
    ramp(30, -2000.0, "Regensburg-Ost")
    ramp(31, -1500.0, None, entry=True)
    ramp(20, 0.0, "Neutraubling;Barbing")
    ramp(40, 1000.0, "Rastplatz Erlgrund")
    ramp(50, 2000.0, "Rosenhof;Mintraching")
    junctions = {4980: {"highway": "motorway_junction", "ref": "101", "name": "Regensburg-Ost"},
                 5000: {"highway": "motorway_junction", "ref": "102", "name": "Neutraubling"},
                 5010: {"highway": "motorway_junction", "noref": "yes", "name": "Rastplatz Erlgrund"},
                 5020: {"highway": "motorway_junction", "ref": "103", "name": "Rosenhof"}}
    return main, links, junctions, places_of(pts)


class NeighboursTest(unittest.TestCase):
    def test_next_and_previous_junction(self):
        records, _ = build(corridor(DE))
        by = {r["d"]["de"]["node"]["ref"]: r["d"]["de"] for r in records if r["d"]["de"]["node"]["ref"]}
        de = by["102"]
        self.assertEqual((de["next"]["name"], de["next"]["ref"]), ("Rosenhof", "103"))   # the rest area is skipped
        self.assertAlmostEqual(de["next"]["s"], 2000.0, delta=1.0)
        # the previous junction's entry joins 1500 m upstream; its merge lane ends 250 m later
        self.assertAlmostEqual(de["prev_merge_s"], 1250.0, delta=1.0)
        # the previous junction's exit (where Regensburg-Ost's link leaves): the dense-junction bound
        self.assertAlmostEqual(de["prev_exit_s"], 2000.0, delta=1.0)
        # a rest area is no junction for the dense-junction bound: Rosenhof's previous exit is Neutraubling's
        self.assertAlmostEqual(by["103"]["prev_exit_s"], 2000.0, delta=1.0)
        self.assertIsNone(by["103"]["next"])


    def test_next_skips_unnamed_junction(self):
        main, links, junctions, places = corridor(DE)
        junctions[5010] = {"highway": "motorway_junction"}          # the rest area's node without name or ref
        records, _ = build((main, links, junctions, places))
        de = next(r["d"]["de"] for r in records if r["d"]["de"]["node"]["ref"] == "102")
        self.assertEqual(de["next"]["ref"], "103")

    def test_own_loop_merge_ignored(self):
        """A cloverleaf: a loop from the crossing motorway (its junction node also numbered 102) enters 300 m before
        Neutraubling's exit; that merge belongs to the same junction, so the previous junction's merge stays 1250."""
        main, links, junctions, places = corridor(DE)
        ll = at(DE)
        pts = {int(n): (float(x), float(y)) for n, x, y in zip(places.nid, places.lon, places.lat)}
        pts[7001], pts[7002], pts[7003] = ll(-300.0, 400.0), ll(-300.0, 0.0 + 200.0), ll(-450.0, 60.0)
        pts[7000] = ll(-300.0, 800.0)
        main.append((70, np.array([7000, 7001], np.int64), {"highway": "motorway", "oneway": "yes", "ref": "A 93"}))
        junctions[7001] = {"highway": "motorway_junction", "ref": "102", "name": "Neutraubling"}
        links.append((71, np.array([7001, 7002, 7003, 4997], np.int64), {"highway": "motorway_link", "oneway": "yes"}))
        records, _ = build((main, links, junctions, places_of(pts)))
        de = next(r["d"]["de"] for r in records if r["d"]["de"]["node"]["ref"] == "102" and r["id"] == -20)
        self.assertAlmostEqual(de["prev_merge_s"], 1250.0, delta=1.0)

class OutlineTest(unittest.TestCase):
    def test_outline_tiles(self):
        ring = np.vstack(signshards.DE_OUTLINE)
        lon = np.linspace(ring[:, 0].min(), ring[:, 0].max(), 400)
        lat = np.linspace(ring[:, 1].min(), ring[:, 1].max(), 400)
        gx, gy = np.meshgrid(lon, lat)
        inside = signshards.in_germany(gx.ravel(), gy.ravel())
        tiles = {signshards.tile(la, lo) for lo, la in zip(gx.ravel()[inside], gy.ravel()[inside])}
        self.assertIn((68, 43), tiles)
        self.assertIn((68, 44), tiles)
        self.assertNotIn((69, 43), tiles)
        self.assertNotIn((69, 44), tiles)
        self.assertLessEqual(sum(len(r) for r in signshards.DE_OUTLINE), 400)

    def test_outline_matches_borders(self):
        import countrybounds
        idx = countrybounds.bundled()
        self.assertEqual(idx.codes_at([(49.02, 12.16)]), ["DE"], "the border fixture is missing")
        lat, lon = np.meshgrid(np.arange(47.30, 49.70, 0.02), np.arange(5.8, 15.1, 0.02), indexing="ij")
        lat, lon = lat.ravel(), lon.ravel()
        codes = np.array([c or "" for c in idx.codes_at(np.c_[lat, lon])])
        inside = signshards.in_germany(lon, lat)
        self.assertTrue(inside[codes == "DE"].all(), "a German point outside the outline")
        foreign = np.isin(codes, ["CZ", "AT", "CH", "FR"])
        # the outline is Germany grown by ~3.3 km and simplified at 2.5 km (6.8 km at most, in the Schaffhausen pocket):
        # a foreign point counts only when no German point lies within 7.5 km of it
        de_pts = np.c_[lat[codes == "DE"], lon[codes == "DE"]]
        far = []
        for i in np.flatnonzero(foreign):
            d = de_pts - [lat[i], lon[i]]
            far.append(np.hypot(d[:, 0] * 111.0, d[:, 1] * 111.0 * np.cos(np.radians(lat[i]))).min() > 7.5)
        far = np.array(far, bool)
        self.assertFalse(inside[np.flatnonzero(foreign)[far]].any(), "a foreign point far from Germany inside")


@unittest.skipUnless(importlib.util.find_spec("osmium"), "pyosmium (the data repo's builder) not installed")
class FirstPassTest(unittest.TestCase):
    def test_extra_keys_survive_pass_one(self):
        import osmium
        ll = at(DE)
        xml = ['<osm version="0.6">']
        for nid, (lon, lat) in {1: ll(0, 0), 2: ll(400, 0)}.items():
            xml.append(f'<node id="{nid}" version="1" lon="{lon}" lat="{lat}"/>')
        tags = {"highway": "motorway", "oneway": "yes", "turn:lanes": "none|slight_right", "int_ref": "E 56",
                "destination:symbol": "none;airport"}
        xml.append('<way id="100" version="1"><nd ref="1"/><nd ref="2"/>' + "".join(
            f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()) + "</way></osm>")
        with tempfile.TemporaryDirectory() as tmp:
            original, pbf = Path(tmp, "in.osm"), Path(tmp, "in.osm.pbf")
            original.write_text("".join(xml), encoding="utf-8")
            with osmium.SimpleWriter(str(pbf)) as writer:
                for obj in osmium.FileProcessor(str(original)):
                    writer.add(obj)
            de_tags = {}
            main, *_ = builder.first_pass(str(pbf), de_tags=de_tags)
            self.assertNotIn("turn:lanes", main[0][2])                 # the shared tag sets are as before
            self.assertEqual(de_tags[100], {"turn:lanes": "none|slight_right", "int_ref": "E 56",
                                            "destination:symbol": "none;airport"})


if __name__ == "__main__":
    unittest.main()
