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
        skin = blockworker._skin_key
        return (ck, skin(ck, 40.758, -73.9855, 15, True, 0.04, 1 / 512),
                skin("c" * 40, 40.758, -73.9855, 15, True, 0.04, 1 / 512),
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
        import earthd
        import revisit
        source = types.SimpleNamespace(version=lambda: "1")
        md = types.SimpleNamespace(highway_signs=source, versions=lambda: {})
        anchor = types.SimpleNamespace(lat=0., lon=0., zooms=[15])
        photo = types.SimpleNamespace(chain=[], leaf_on=True, season_mode="fixed")
        server = types.SimpleNamespace(imagery=photo, layout=types.SimpleNamespace(block=512), base_px=512,
                                       tex_modes={}, tone=0, buildings=False, mapdata=md, surface=None,
                                       sea=None, airports=None, lids=None, road_widths=None)
        class CacheRead(Exception):
            pass
        args = (1, 0., 0., 15, 0, 0, "", "")
        keys = []
        with mock.patch.object(revisit, "ON", True):
            for grading in (False, True):
                state = types.SimpleNamespace(**dict(BASE, md=md, cache_dir="fixture", grading=grading))
                server.grading = grading
                base = earthd.Server.base_revisit_key(server, anchor, 0., 0.)
                self.assertIsNotNone(base)
                stored = [base]
                with mock.patch.object(blockworker, "_s", state), mock.patch.object(revisit, "load", side_effect=CacheRead) as load:
                    for fn, kind in ((blockworker.veg_geo, "vegg"), (blockworker.signs_tile, "signs")):
                        with self.assertRaises(CacheRead):
                            fn(*args)
                        directory, actual_kind, key = load.call_args.args
                        self.assertEqual((directory, actual_kind), ("fixture", kind))
                        stored.append(key)
                keys.append(stored)
        for kind, a, b in zip(("photo", "vegetation", "signs"), *keys):
            self.assertNotEqual(a, b, kind)


if __name__ == "__main__":
    unittest.main()
