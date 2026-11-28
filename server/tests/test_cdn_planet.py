"""cdn.json's "bnwt" and "cip" keys: the planet builds on the tile host turned on for every player from the data repo's
switch file. The release must be the one the process reads, the environment overrides (BNGE_BNWT_URL, BNE_V2_CIP,
BNE_V2_CIP_BUILD), and a switch that cannot be read, a key that is wrong or a server too old for the codec is the
sources as before."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bnwt  # noqa: E402
import cdn  # noqa: E402
import cip  # noqa: E402
import fast  # noqa: E402

REL = "2026-11-18.0"
HAVE_CIP = getattr(fast._ef, "VERSION", 0) >= cip.NATIVE


def sw(doc):
    return mock.patch.object(cdn, "switch", lambda fetcher, cache_dir: doc)


class Keys(unittest.TestCase):
    def test_bnwt_on_for_the_matching_release(self):
        with sw({"bnwt": {"enabled": True, "release": REL}}):
            self.assertEqual(cdn.bnwt_base(None, "x", REL), cdn.CDN_BASE)

    def test_bnwt_off_or_other(self):
        for doc in ({}, {"bnwt": {"enabled": False, "release": REL}}, {"bnwt": {"release": REL}},
                    {"bnwt": {"enabled": True, "release": "2026-12-16.0"}}, {"bnwt": {"enabled": True}},
                    {"bnwt": {"enabled": True, "release": "../x"}}, {"bnwt": "on"}, {"bnwt": None},
                    {"bnwt": {"enabled": True, "release": 5}}):
            with sw(doc):
                self.assertIsNone(cdn.bnwt_base(None, "x", REL), doc)

    def test_cip_on_with_and_without_a_build(self):
        with sw({"cip": {"enabled": True, "release": REL, "build": "6f0baf33df92ef53"}}):
            self.assertEqual(cdn.cip_build(None, "x", REL), (cdn.CDN_BASE, "6f0baf33df92ef53"))
        with sw({"cip": {"enabled": True, "release": REL}}):
            self.assertEqual(cdn.cip_build(None, "x", REL), (cdn.CDN_BASE, None))

    def test_cip_off_or_other(self):
        for doc in ({}, {"cip": {"enabled": False, "release": REL}}, {"cip": {"enabled": True, "release": "2027-01-01.0"}},
                    {"cip": {"enabled": True, "release": REL, "build": "../x"}},
                    {"cip": {"enabled": True, "release": REL, "build": "a/b"}}, {"cip": []}, {"cip": 1}):
            with sw(doc):
                self.assertIsNone(cdn.cip_build(None, "x", REL), doc)


class Bnwt(unittest.TestCase):
    def test_switch_decides_when_the_environment_is_silent(self):
        with mock.patch.object(bnwt, "BASE_URL", ""), mock.patch.object(bnwt, "ENV_OFF", False), \
                mock.patch.object(fast, "_ef", mock.Mock(VERSION=10 ** 6)), \
                sw({"bnwt": {"enabled": True, "release": REL}}):
            self.assertEqual(bnwt.base_for(None, "x", REL, True), cdn.CDN_BASE)
            self.assertEqual(bnwt.base_for(None, "x", "2027-01-01.0", True), "")

    def test_environment_wins(self):
        with mock.patch.object(fast, "_ef", mock.Mock(VERSION=10 ** 6)), \
                sw({"bnwt": {"enabled": True, "release": REL}}):
            with mock.patch.object(bnwt, "BASE_URL", "file:///x"), mock.patch.object(bnwt, "ENV_OFF", False):
                self.assertEqual(bnwt.base_for(None, "x", REL, True), "file:///x")
            with mock.patch.object(bnwt, "BASE_URL", ""), mock.patch.object(bnwt, "ENV_OFF", True):
                self.assertEqual(bnwt.base_for(None, "x", REL, True), "")

    def test_unreadable_switch_or_old_native_is_the_sources(self):
        def boom(fetcher, cache_dir):
            raise OSError("offline")
        with mock.patch.object(bnwt, "BASE_URL", ""), mock.patch.object(bnwt, "ENV_OFF", False), \
                mock.patch.object(fast, "_ef", mock.Mock(VERSION=10 ** 6)), mock.patch.object(cdn, "switch", boom):
            self.assertEqual(bnwt.base_for(None, "x", REL, True), "")
        with mock.patch.object(bnwt, "BASE_URL", ""), mock.patch.object(bnwt, "ENV_OFF", False), \
                mock.patch.object(fast, "_ef", mock.Mock(VERSION=bnwt.NATIVE - 1)), \
                sw({"bnwt": {"enabled": True, "release": REL}}):
            self.assertEqual(bnwt.base_for(None, "x", REL, True), "")


class CipReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cip._READERS.clear()
        self.addCleanup(cip._READERS.clear)

    def read(self, doc, env="", on=False, build="", url="", use=True):
        with mock.patch.object(cip, "ENV", env), mock.patch.object(cip, "ON", on), mock.patch.object(cip, "BUILD", build), \
                mock.patch.object(cip, "BASE_URL", url), sw(doc):
            return cip.reader(self.tmp.name, None, REL, use)

    def test_off_by_default(self):
        self.assertIsNone(self.read({}))
        self.assertIsNone(self.read({"cip": {"enabled": False, "release": REL}}))

    @unittest.skipUnless(HAVE_CIP, "needs earthfast with cip")
    def test_switch_turns_it_on_with_its_build(self):
        r = self.read({"cip": {"enabled": True, "release": REL, "build": "6f0baf33df92ef53"}})
        self.assertIsNotNone(r)
        self.assertEqual((r.base, r.build, r.release), (cdn.CDN_BASE, "6f0baf33df92ef53", REL))

    @unittest.skipUnless(HAVE_CIP, "needs earthfast with cip")
    def test_switch_without_a_build_takes_latest_json(self):
        r = self.read({"cip": {"enabled": True, "release": REL}})
        self.assertIsNone(r.build)          # (resolved on the first read: latest.json)

    @unittest.skipUnless(HAVE_CIP, "needs earthfast with cip")
    def test_environment_overrides(self):
        doc = {"cip": {"enabled": True, "release": REL, "build": "aaaa"}}
        self.assertIsNone(self.read(doc, env="off"))
        cip._READERS.clear()
        r = self.read(doc, env="on", on=True, build="bbbb", url="file:///x")
        self.assertEqual((r.base, r.build), ("file:///x", "bbbb"))
        cip._READERS.clear()
        r = self.read({}, env="on", on=True)
        self.assertEqual(r.base, cdn.CDN_BASE)

    def test_a_server_too_old_for_the_codec_is_the_sources(self):
        with mock.patch.object(fast, "_ef", mock.Mock(VERSION=cip.NATIVE - 1)):
            self.assertIsNone(self.read({"cip": {"enabled": True, "release": REL}}))
            with self.assertRaises(fast.NativeMissing):
                self.read({}, env="on", on=True)

    def test_a_switch_that_cannot_be_read_is_the_sources(self):
        def boom(fetcher, cache_dir):
            raise OSError("offline")
        with mock.patch.object(cip, "ENV", ""), mock.patch.object(cip, "ON", False), mock.patch.object(cdn, "switch", boom):
            self.assertIsNone(cip.reader(self.tmp.name, None, REL, True))


if __name__ == "__main__":
    unittest.main()
