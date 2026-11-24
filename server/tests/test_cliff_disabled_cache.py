"""Cliffs off (BNE_CLIFFS unset, the default) leaves every cache key as it is without cliffs (PR #1412 review R14: a
behaviour test, not the key expressions' text pinned to one upstream commit).

The road tile ("roads"), road geotile content ("roadsg") and finished far shape ("farfin") keys are captured as built
with cliffs off and on: on, the only difference is the cliff key part (one more argument); off, no cliff part at all,
and the farfin key is the plain revisit.key(KIND, base, px, name_len) it was before cliffs. The settings' revisit
signature options are empty when off. That the tiles' bytes are trunk's is shown by the identity runs at merge (plans,
facts, a session's tiles, far tiles and blocks)."""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blockworker  # noqa: E402
import cliffsettings  # noqa: E402
import farrestore  # noqa: E402
import geotiles  # noqa: E402
import revisit  # noqa: E402

OFFS = (cliffsettings.CliffSettings(), cliffsettings.CliffSettings(False, False, "high"),
        cliffsettings.CliffSettings(False, False, "low", "4k"))
ON = cliffsettings.CliffSettings(True, True)


def _roads_parts(settings):
    got = []

    def capture(kind, *parts):
        got.append((kind, parts))
        raise StopIteration

    with mock.patch.object(cliffsettings, "SETTINGS", settings), mock.patch.object(revisit, "key", capture), \
            mock.patch.object(blockworker, "_s", types.SimpleNamespace(md=None)):
        try:
            blockworker._roads_tile_cached("a", 39.28, -76.6, 14, 0, 0, "/nonexistent", None, True, 0.0,
                                           False, False, True, None, False, 0.0, None)
        except StopIteration:
            pass
    assert len(got) == 1 and got[0][0] == "roads", got
    return got[0][1]


def _roadsg_parts(settings):
    tile = geotiles.geobox(38600, 49258).key
    s = types.SimpleNamespace(md=None, grading=True, widths=None, surface=None, sea=None, airports=None, lids=None,
                              cache_dir=None)
    with mock.patch.object(cliffsettings, "SETTINGS", settings), mock.patch.object(blockworker, "_s", s), \
            mock.patch.object(revisit, "key", lambda kind, *parts: (kind, parts)):
        kind, parts = blockworker._geo_content_key(tile, 15, True, False, True, True, 0.0, None, "US")
    assert kind == "roadsg"
    return parts


def _without_one(longer, part):
    i = list(longer).index(part)
    return tuple(longer[:i]) + tuple(longer[i + 1:])


def _has_cliff_part(parts):
    return any(isinstance(p, tuple) and p and str(p[0]).startswith("dynamic-cliffs") for p in parts)


class DisabledKeysTest(unittest.TestCase):
    def test_settings_add_nothing_when_off(self):
        for settings in OFFS:
            self.assertFalse(settings.enabled)
            self.assertEqual(settings.cache_parts, ())
            self.assertEqual(settings.cache_options, {})
        self.assertEqual(cliffsettings.CliffSettings.from_env({}), cliffsettings.CliffSettings())
        self.assertEqual(cliffsettings.CliffSettings.from_env({"BNE_CLIFFS": "off"}).cache_parts, ())

    def test_road_tile_key(self):
        on = _roads_parts(ON)
        self.assertTrue(_has_cliff_part(on))
        for settings in OFFS:
            off = _roads_parts(settings)
            self.assertFalse(_has_cliff_part(off))
            self.assertEqual(off, _without_one(on, ON.cache_key))

    def test_road_geotile_content_key(self):
        on = _roadsg_parts(ON)
        self.assertTrue(_has_cliff_part(on))
        for settings in OFFS:
            off = _roadsg_parts(settings)
            self.assertFalse(_has_cliff_part(off))
            self.assertEqual(off, _without_one(on, ON.cache_key))

    def test_finished_far_key(self):
        with mock.patch.object(revisit, "code_key", return_value="same-generation"):
            for settings in OFFS:
                with mock.patch.object(cliffsettings, "SETTINGS", settings):
                    for photo in (None, "photo-digest"):
                        self.assertEqual(farrestore.key("tile", 256, 32, photo), revisit.key("farfin", "tile", 256, 32))
            keys = set()
            for natural, cuts in ((False, False), (False, True), (True, False), (True, True)):
                with mock.patch.object(cliffsettings, "SETTINGS", cliffsettings.CliffSettings(natural, cuts)):
                    keys.add(farrestore.key("tile", 256, 32, "photo-digest"))
            self.assertEqual(len(keys), 4)


if __name__ == "__main__":
    unittest.main()
