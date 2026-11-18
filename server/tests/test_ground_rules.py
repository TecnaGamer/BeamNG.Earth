"""The ground rules fingerprint (blockcache.ground_rules): every server setting that changes the ground or the road
geometry on it (--grading, --road-widths, --surface, --sea, --airports, the tunnel lids, roads in the ground) is in the
key of every kept item that stands on that ground: a terrain block, a road geotile's content, a block photo. None of
them is a BNE_* switch, so the revisit code key does not hold them: a road geotile kept with --grading on was a hit
with it off, on terrain that had changed (the review of 2026-11-13)."""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import blockcache  # noqa: E402
import blockworker  # noqa: E402
import geotiles  # noqa: E402
import roads  # noqa: E402

BASE = dict(md=None, grading=True, widths=object(), surface=None, sea=object(), airports=None, lids=object(),
            cache_dir=None)
# one setting changed at a time: what the server's command line turns on or off
CHANGES = {"--grading off": dict(grading=False), "--road-widths off": dict(widths=None),
           "--surface on": dict(surface=types.SimpleNamespace(gas_pads=False, bus_pads=False, extra_pads=None)),
           "--sea off": dict(sea=None),
           "--airports on": dict(airports=types.SimpleNamespace(db=types.SimpleNamespace(snapshot="oa-1"))),
           "tunnel lids off": dict(lids=None)}


def _keys(**change):
    """(content key, its skin key and a fixed content's skin key where the skins are kept, the rules) under BASE with
    change."""
    tile = geotiles.geobox(38600, 49258).key
    with mock.patch.object(blockworker, "_s", types.SimpleNamespace(**dict(BASE, **change))):
        ck = blockworker._geo_content_key(tile, 15, True, False, True, True, 0.0, None, "US")
        skin = getattr(blockworker, "_skin_key", None)
        return (ck, skin(ck, 40.758, -73.9855, 15, True, 0.04, 1 / 512) if skin else None,
                skin("c" * 40, 40.758, -73.9855, 15, True, 0.04, 1 / 512) if skin else None,
                blockworker._ground_rules())


class GroundRulesTest(unittest.TestCase):
    def test_the_same_settings_are_the_same_keys(self):
        self.assertEqual(_keys(), _keys())

    def test_each_setting_changed_is_another_content_and_skin_key(self):
        base = _keys()
        seen = {base[3]}
        for name, change in CHANGES.items():
            got = _keys(**change)
            self.assertNotEqual(got[3], base[3], f"{name}: other ground rules")
            self.assertNotEqual(got[0], base[0], f"{name}: the road geotile's content must miss")
            if base[1] is not None:     # (the skins kept in the revisit cache: their keys hold the rules too, and
                # not only through their content's key)
                self.assertNotEqual(got[1], base[1], f"{name}: its skin must miss")
                self.assertNotEqual(got[2], base[2], f"{name}: a skin of the same content must miss")
            self.assertNotIn(got[3], seen, f"{name}: its own fingerprint")
            seen.add(got[3])

    def test_roads_in_the_ground_and_the_airports_snapshot(self):
        base = _keys()
        with mock.patch.object(roads, "ROADS_IN_GROUND", not roads.ROADS_IN_GROUND):
            self.assertNotEqual(_keys()[0], base[0])
        a = _keys(airports=types.SimpleNamespace(db=types.SimpleNamespace(snapshot="oa-1")))
        b = _keys(airports=types.SimpleNamespace(db=types.SimpleNamespace(snapshot="oa-2")))
        self.assertNotEqual(a[0], b[0], "another OurAirports snapshot: other runway grading")

    def test_the_pad_switches_are_not_ground_rules(self):
        # (they come on with a session's first pads: the blocks' own keys hold the pads themselves)
        s1 = types.SimpleNamespace(gas_pads=False, bus_pads=False, extra_pads=None)
        s2 = types.SimpleNamespace(gas_pads=True, bus_pads=True, extra_pads=object())
        self.assertEqual(_keys(surface=s1)[3], _keys(surface=s2)[3])
        self.assertNotEqual(blockcache.settings(True, None, None, s1, None, None, None),
                            blockcache.settings(True, None, None, s2, None, None, None))

    def test_the_server_and_its_road_processes_make_the_same_fingerprint(self):
        import earthd
        for name, change in [("base", {})] + list(CHANGES.items()):
            st = dict(BASE, **change)
            server = types.SimpleNamespace(grading=st["grading"], mapdata=st["md"], road_widths=st["widths"],
                                           surface=st["surface"], sea=st["sea"], airports=st["airports"],
                                           lids=st["lids"])
            self.assertEqual(earthd.Server.ground_rules(server), _keys(**change)[3], name)

    def test_the_blocks_settings_begin_with_the_ground_rules(self):
        s = blockcache.settings(True, None, object(), None, object(), None, object())
        self.assertEqual(blockcache.ground_rules(True, None, object(), None, object(), None, object()), s[:9])

    def test_the_other_kept_kinds_hold_the_rules(self):
        # (their keys are made inline: the rules are a part of each, as the source says)
        import inspect
        self.assertIn("Server.ground_rules(self)", inspect.getsource(__import__("earthd").Server.base_revisit_key))
        for name in ("veg_geo", "signs_tile"):      # (kept in the revisit cache only with the stale revisit work)
            fn = getattr(blockworker, name)
            src = inspect.getsource(getattr(fn, "__wrapped__", fn))
            if "revisit.key(" in src:
                self.assertIn('("rules", _ground_rules())', src, name)


if __name__ == "__main__":
    unittest.main()
