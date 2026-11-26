"""terrainpacks.py: the ledger of the packs we write into the game's terrain texture cache, its prune and cap, and the
one-time wipe of leaked packs (strict: ours beyond doubt, a game-made decoy survives, once only, an unfamiliar folder
left alone); and the server's use of them (earthd: the start with no game running, a released session, the packs
before the photo, the exit's flush, an unmarked session folder)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bc7  # noqa: E402
import fast  # noqa: E402
import terraincache  # noqa: E402
import terrainpacks as tp  # noqa: E402

KEY_A, KEY_B, KEY_C, KEY_D = "0123456789abcdef", "fedcba9876543210", "00000000000000aa", "1111111111111111"
_PACKS = {}


def pack_bytes(px=2048, alpha=tp.BASE_AO, seed=1):
    """A photo pack as our writer makes it (terraincache.base_entry: the photo + a flat AO, BC7 mode 6, the mips)."""
    k = (px, alpha, seed)
    if k not in _PACKS:
        rng = np.random.default_rng(seed)
        rgb = rng.integers(0, 256, (px // 64, px // 64, 3), np.uint8).repeat(64, 0).repeat(64, 1)
        _PACKS[k] = bc7.dds(fast._ef.block_rgba(rgb, alpha))
    return _PACKS[k]


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "cache")
        self.pdir = tp.pack_dir(os.path.join(self.tmp.name, "user"))
        self.led = tp.Ledger(self.cache)

    def test_record_and_read(self):
        self.led.record(KEY_A, "s1", now=100)
        self.led.record(KEY_B, "s2", now=200)
        self.led.record(KEY_A.upper(), "s3", now=300)       # (the last line of a key wins; keys lower case)
        with open(self.led.path, "a") as f:
            f.write("garbage line\nzz s 1\n")
        self.assertEqual(self.led.entries(), {KEY_A: ("s3", 300), KEY_B: ("s2", 200)})
        self.assertEqual(self.led.keys_of("s2"), [KEY_B])
        self.assertEqual(tp.Ledger(None).entries(), {})

    def test_prune_by_session_not_photo(self):
        for key, sid, t in ((KEY_A, "gone", 1), (KEY_B, "live", 2), (KEY_C, "gone", 3)):
            self.led.record(key, sid, now=t)
        a = write(os.path.join(self.pdir, KEY_A + ".dds"), b"x" * 10)
        b = write(os.path.join(self.pdir, KEY_B + ".dds"), b"y" * 20)
        # (KEY_C's pack never written: its line goes too)
        n, freed = tp.prune(self.led, self.pdir, {"live"})
        self.assertEqual((n, freed), (1, 10))
        self.assertFalse(os.path.exists(a))
        self.assertTrue(os.path.exists(b))
        self.assertEqual(self.led.entries(), {KEY_B: ("live", 2)})

    def test_cap_oldest_first_and_protected(self):
        for i, key in enumerate((KEY_A, KEY_B, KEY_C, KEY_D)):
            self.led.record(key, "s", now=10 + i)
            write(os.path.join(self.pdir, key + ".dds"), b"z" * 100)
        n, freed = tp.cap(self.led, self.pdir, max_bytes=250, protect={KEY_A})
        self.assertEqual((n, freed), (2, 200))
        left = sorted(k for k in (KEY_A, KEY_B, KEY_C, KEY_D) if os.path.exists(os.path.join(self.pdir, k + ".dds")))
        self.assertEqual(left, sorted([KEY_A, KEY_D]), "the oldest unprotected went (B, C); A protected, D newest")
        self.assertEqual(set(self.led.entries()), {KEY_A, KEY_D})
        self.assertEqual(tp.cap(self.led, self.pdir, max_bytes=250), (0, 0), "under the cap: nothing")

    def test_existing_ledger_reads_back(self):
        """A ledger the Python module wrote (Linux "\\n", Windows "\\r\\n") reads back; a rewrite keeps its format."""
        os.makedirs(self.cache)
        with open(self.led.path, "wb") as f:
            f.write(f"{KEY_A} 20261124-120000-bbbb 1764000000\r\n{KEY_B} s2 1764000001\n{KEY_C} s3 0042\r".encode())
        self.assertEqual(self.led.entries(), {KEY_A: ("20261124-120000-bbbb", 1764000000), KEY_B: ("s2", 1764000001),
                                              KEY_C: ("s3", 42)})
        self.assertEqual(self.led.keys_of("s2"), [KEY_B])
        self.assertEqual(tp.prune(self.led, self.pdir, {"s2", "20261124-120000-bbbb"}), (0, 0))
        self.assertEqual(self.led.entries(), {KEY_A: ("20261124-120000-bbbb", 1764000000), KEY_B: ("s2", 1764000001)})
        with open(self.led.path, "rb") as f:
            self.assertEqual(f.read(), f"{KEY_A} 20261124-120000-bbbb 1764000000{os.linesep}{KEY_B} s2 1764000001"
                             f"{os.linesep}".encode(), "the lines dropped (s3 gone), oldest first, the platform's ends")

    @unittest.skipIf(os.name == "nt", "symbolic links need rights on Windows")
    def test_links_never_followed(self):
        """Guard: a link at a recorded pack's name is never measured or deleted, nor what it points at."""
        outside = write(os.path.join(self.tmp.name, "elsewhere", "precious.dds"), b"p" * 1000)
        os.makedirs(self.pdir)
        os.symlink(outside, os.path.join(self.pdir, KEY_A + ".dds"))
        self.led.record(KEY_A, "gone", now=1)
        self.assertEqual(tp.prune(self.led, self.pdir, set()), (0, 0))
        self.led.record(KEY_A, "s", now=2)
        self.assertEqual(tp.cap(self.led, self.pdir, max_bytes=0), (0, 0))
        self.assertTrue(os.path.islink(os.path.join(self.pdir, KEY_A + ".dds")) and os.path.exists(outside))

    def test_no_python_fallback(self):
        """The work is the native module's: an older one is refused, nothing done in Python."""
        ef = fast._ef
        self.addCleanup(setattr, fast, "_ef", ef)
        fast._ef = type("Old", (), {"VERSION": tp.NATIVE - 1})()
        with self.assertRaises(fast.NativeMissing):
            tp.prune(self.led, self.pdir, set())
        with self.assertRaises(fast.NativeMissing):
            tp.is_ours(os.path.join(self.pdir, KEY_A + ".dds"))


class OursTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pdir = tp.pack_dir(os.path.join(self.tmp.name, "user"))

    def test_sizes(self):
        self.assertEqual(tp.ours_size(2048), 5592580)
        self.assertEqual(tp.ours_size(4096), 22369796)
        self.assertEqual(len(pack_bytes()), 5592580)

    def test_strict_matching(self):
        ours = write(os.path.join(self.pdir, KEY_A + ".dds"), pack_bytes())
        self.assertTrue(tp.is_ours(ours))
        # a game-made pack of a stock texture: the same size, header and even mode 6, but its own alpha
        decoy = write(os.path.join(self.pdir, KEY_B + ".dds"), pack_bytes(alpha=255, seed=2))
        self.assertFalse(tp.is_ours(decoy))
        # the game's encoder: other BC7 modes among the blocks
        body = bytearray(pack_bytes())
        for i in range(148, len(body), 16 * 3):     # (the game's own packs: 30-98 % of other modes)
            body[i] = 0x20              # mode 5
        self.assertFalse(tp.is_ours(write(os.path.join(self.pdir, KEY_C + ".dds"), bytes(body))))
        # a linear (data) header, a cut file, another size, another name
        lin = bytearray(pack_bytes())
        lin[128] = bc7.DXGI_BC7_UNORM
        self.assertFalse(tp.is_ours(write(os.path.join(self.pdir, KEY_D + ".dds"), bytes(lin))))
        self.assertFalse(tp.is_ours(write(os.path.join(self.pdir, "1" * 16 + ".dds"), pack_bytes()[:-16])))
        self.assertFalse(tp.is_ours(write(os.path.join(self.pdir, "2" * 16 + ".dds"), pack_bytes(px=1024))))
        self.assertFalse(tp.is_ours(write(os.path.join(self.tmp.name, "notakey.dds"), pack_bytes())))

    @unittest.skipIf(os.name == "nt", "symbolic links need rights on Windows")
    def test_links_are_not_ours(self):
        """Guard: a link to one of our packs is no pack (not read through), and makes the folder unfamiliar (no wipe)."""
        real = write(os.path.join(self.tmp.name, "elsewhere", KEY_A + ".dds"), pack_bytes())
        write(os.path.join(self.pdir, KEY_B + ".dds"), b"x")
        link = os.path.join(self.pdir, KEY_A + ".dds")
        os.symlink(real, link)
        self.assertTrue(tp.is_ours(real))
        self.assertFalse(tp.is_ours(link))
        self.assertFalse(tp.familiar(self.pdir))
        mark = os.path.join(self.tmp.name, "cache", tp.WIPE_MARK)
        self.assertIsNone(tp.wipe_v2(self.pdir, set(), mark))
        self.assertTrue(os.path.exists(real) and os.path.islink(link) and not os.path.exists(mark))

    def test_familiar(self):
        write(os.path.join(self.pdir, KEY_A + ".dds"), b"x")
        self.assertTrue(tp.familiar(self.pdir))
        self.assertFalse(tp.familiar(os.path.join(self.tmp.name, "user", "temp", "art")), "not named so")
        write(os.path.join(self.pdir, "readme.txt"), b"x")
        self.assertFalse(tp.familiar(self.pdir), "a file that is no pack")


class WipeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pdir = tp.pack_dir(os.path.join(self.tmp.name, "user"))
        self.mark = os.path.join(self.tmp.name, "cache", tp.WIPE_MARK)

    def test_only_ours_without_a_photo_once(self):
        leaked = write(os.path.join(self.pdir, KEY_A + ".dds"), pack_bytes())
        kept = write(os.path.join(self.pdir, KEY_B + ".dds"), pack_bytes(seed=3))            # its photo is on disk
        decoy = write(os.path.join(self.pdir, KEY_C + ".dds"), pack_bytes(alpha=255, seed=4))  # the game's own
        small = write(os.path.join(self.pdir, KEY_D + ".dds"), pack_bytes(px=1024))           # a stock 1024 pack size
        got = tp.wipe_v2(self.pdir, {KEY_B.upper()}, self.mark)
        self.assertEqual(got, (1, 5592580))
        self.assertFalse(os.path.exists(leaked))
        self.assertTrue(all(os.path.exists(p) for p in (kept, decoy, small)))
        self.assertTrue(os.path.exists(self.mark))
        again = write(os.path.join(self.pdir, "3" * 16 + ".dds"), pack_bytes(seed=5))
        self.assertIsNone(tp.wipe_v2(self.pdir, set(), self.mark), "once only")
        self.assertTrue(os.path.exists(again))

    def test_unfamiliar_folder_left_alone(self):
        leaked = write(os.path.join(self.pdir, KEY_A + ".dds"), pack_bytes())
        write(os.path.join(self.pdir, "notes.txt"), b"mine")
        self.assertIsNone(tp.wipe_v2(self.pdir, set(), self.mark))
        self.assertTrue(os.path.exists(leaked))
        self.assertFalse(os.path.exists(self.mark), "tried again at a later start")
        self.assertIsNone(tp.wipe_v2(os.path.join(self.tmp.name, "nowhere"), set(), self.mark))


class ServerPacksTest(unittest.TestCase):
    """earthd's use: a real Server over a temporary user folder (no game running unless said)."""

    def setUp(self):
        import earthd
        self.earthd = earthd
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.user = self.tmp.name
        self.root = os.path.join(self.user, "temp", "beamngEarth")
        self.cache = os.path.join(self.user, "cache")
        self.pdir = tp.pack_dir(self.user)
        running = earthd.game_running
        self.addCleanup(setattr, earthd, "game_running", running)

    def server(self, game=False):
        self.earthd.game_running = lambda *a: game
        s = self.earthd.Server(self.user, self.cache, imagery=None)
        self.addCleanup(s.tile_pool.shutdown)
        self.addCleanup(s.hm_pool.shutdown)
        self.addCleanup(lambda: s.trash_thread is not None and s.trash_thread.join(30))
        return s

    def session(self, sid, marker=True, age=0):
        d = os.path.join(self.root, sid)
        os.makedirs(d, exist_ok=True)
        if marker:
            open(os.path.join(d, self.earthd.SESSION_MARKER), "w").close()
        if age:
            t = time.time() - age
            for p in [d] + [os.path.join(d, n) for n in os.listdir(d)]:
                os.utime(p, (t, t))
        return d

    def test_no_game_start(self):
        e = self.earthd
        led = tp.Ledger(self.cache)
        # an earlier session (gone by the start): its recorded packs go, its photo deleted long ago
        gone = write(os.path.join(self.pdir, KEY_A + ".dds"), pack_bytes())
        led.record(KEY_A, "20261101-000000-aaaa", now=1)
        # a leaked pack of ours recorded nowhere, a game-made decoy, a pack of a photo still on disk
        leaked = write(os.path.join(self.pdir, KEY_B + ".dds"), pack_bytes(seed=6))
        decoy = write(os.path.join(self.pdir, KEY_C + ".dds"), pack_bytes(alpha=255, seed=7))
        # a session folder that lost its marker, 2 days old; one of another program (no session name) stays
        nomark = self.session("20261113-113742-01cf", marker=False, age=2 * 86400)
        write(os.path.join(nomark, "far_1_x.json"), b"{}")
        other = os.path.join(self.root, "maptiles")
        os.makedirs(other)
        s = self.server(game=False)
        self.assertFalse(os.path.exists(gone), "a gone session's recorded pack")
        self.assertFalse(os.path.exists(leaked), "a leaked pack of ours")
        self.assertTrue(os.path.exists(decoy), "the game's own pack survives")
        self.assertFalse(os.path.exists(nomark), "the session folder without its marker")
        self.assertTrue(os.path.isdir(other))
        self.assertTrue(os.path.exists(os.path.join(self.cache, tp.WIPE_MARK)))
        self.assertEqual(tp.Ledger(self.cache).entries(), {})
        # this session's packs are recorded as they are written
        s._record_pack(s.base_name((1, 0.0, 0.0)))
        key = terraincache.base_key(f"{s.vfs_dir}/{s.base_name((1, 0.0, 0.0))}", e.base_ao_vfs(s.base_px))
        self.assertEqual(tp.Ledger(self.cache).keys_of(s.sid), [key])

    def test_game_running_keeps_young_sessions_and_wipes_nothing(self):
        led = tp.Ledger(self.cache)
        sid = "20261124-120000-bbbb"
        self.session(sid, age=600)
        mine = write(os.path.join(self.pdir, KEY_A + ".dds"), pack_bytes())
        led.record(KEY_A, sid, now=1)
        self.server(game=True)
        self.assertTrue(os.path.exists(mine), "a session a game may still show keeps its packs")
        self.assertFalse(os.path.exists(os.path.join(self.cache, tp.WIPE_MARK)), "no wipe with a game running")

    def test_game_running_prunes_and_caps_nothing(self):
        """Guard: with a game running even a gone session's recorded pack and a leaked one stay (the game may show
        them), whatever the cap."""
        led = tp.Ledger(self.cache)
        gone = write(os.path.join(self.pdir, KEY_A + ".dds"), pack_bytes())
        led.record(KEY_A, "20261101-000000-aaaa", now=1)
        leaked = write(os.path.join(self.pdir, KEY_B + ".dds"), pack_bytes(seed=6))
        self.server(game=True)
        self.assertTrue(os.path.exists(gone) and os.path.exists(leaked))
        self.assertEqual(set(tp.Ledger(self.cache).entries()), {KEY_A})

    def test_release_takes_the_recorded_packs_first(self):
        sid = "20261124-120000-cccc"
        d = self.session(sid, age=600)
        write(os.path.join(d, "far_1_x.json"), b"{}")
        led = tp.Ledger(self.cache)
        pack = write(os.path.join(self.pdir, KEY_A + ".dds"), pack_bytes())
        led.record(KEY_A, sid, now=1)                       # (its photo already gone)
        s = self.server(game=True)
        self.assertIn(sid, s.orphans)
        queued = []
        s.queue_delete = lambda paths, ck=None: queued.extend(paths)
        s.release_session(sid)
        self.assertEqual(queued[0], pack, "the pack first")
        self.assertIn(os.path.join(d, "far_1_x.json"), queued)

    def test_base_paths_and_exit_flush(self):
        s = self.server()
        name = s.base_name((2, 128.0, -256.0))
        paths = s._base_paths(name)
        self.assertEqual(paths[-1], os.path.join(s.out_dir, name), "the photo last")
        self.assertTrue(all(os.path.dirname(p) == terraincache.cache_dir(s.user_dir) for p in paths[:-1]))
        for p in paths:
            write(p, b"x")
        s.delete_queue.extend((p, None) for p in paths)
        self.assertEqual(s.flush_pack_deletes(), len(paths) - 1)
        self.assertTrue(os.path.exists(paths[-1]) and not any(os.path.exists(p) for p in paths[:-1]),
                        "the packs went at exit, the photo waits for the session clean-up")

    def test_base_photo_names(self):
        s = self.server()
        key = (3, -512.0, 384.0)
        for n in (s.base_name(key), s.base_name(key, 7), s.quick_base_name(key)):
            self.assertTrue(self.earthd.BASE_PHOTO.fullmatch(n), n)
        self.assertFalse(self.earthd.BASE_PHOTO.fullmatch("hm_3_-512_384_base_g2.jpg"), "a stand-in has its own way")
        self.assertFalse(self.earthd.BASE_PHOTO.fullmatch("hm_3_-512_384_7.png"))


if __name__ == "__main__":
    unittest.main()
