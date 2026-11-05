"""Pooled far-tile material names (matslots.py) and the server's use of them (reclaim_names, reslot)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import matslots  # noqa: E402


class PoolTest(unittest.TestCase):
    def test_names_reused_after_release(self):
        p = matslots.MatSlots("ab12")
        a = p.take((1, "0_0_0"))
        self.assertEqual(a, "bngEarthFar_ab12_00000")
        self.assertEqual(p.take((1, "0_0_0")), a)            # the tile keeps its name
        b = p.take((1, "0_1_0"))
        self.assertNotEqual(a, b)
        p.release((1, "0_0_0"))
        self.assertFalse(p.owns((1, "0_0_0"), a))
        self.assertEqual(p.take((2, "0_0_0")), a)            # the freed name goes to the next tile
        self.assertEqual(p.stats(), {"made": 2, "owned": 2, "free": 0, "waiting": 0})

    def test_reclaim_until_target(self):
        p = matslots.MatSlots("ab12")
        cks = [(1, f"0_{i}_0") for i in range(matslots.TARGET + 10)]
        for ck in cks:
            p.take(ck)
        self.assertTrue(p.short())
        n = p.reclaim(iter(cks))
        self.assertEqual(n, matslots.TARGET)
        self.assertFalse(p.short())
        self.assertTrue(p.owns(cks[-1], p.name(len(cks) - 1)))     # beyond the target: kept

    def test_held_slot_waits_for_the_game(self):
        held = {0}
        p = matslots.MatSlots("ab12", held=lambda n: n in held)
        a = p.take((1, "0_0_0"))
        p.release((1, "0_0_0"))                 # the tile let go, a game still draws it
        self.assertNotEqual(p.take((1, "0_1_0")), a)
        held.clear()
        p.unheld(0)
        self.assertEqual(p.take((1, "0_2_0")), a)

    def test_reclaim_skips_held(self):
        held = {1}
        p = matslots.MatSlots("ab12", held=lambda n: n in held)
        cks = [(1, f"0_{i}_0") for i in range(3)]
        for ck in cks:
            p.take(ck)
        self.assertEqual(p.reclaim(cks), 2)
        self.assertTrue(p.owns(cks[1], p.name(1)))

    def test_take_skips_new_hold_and_uses_another_free_slot(self):
        held = set()
        p = matslots.MatSlots("ab12", held=lambda n: n in held)
        a, b = p.take("a"), p.take("b")
        p.release("a")
        p.release("b")
        held.add(p.number(b))              # a hello restored the top free slot's hold
        self.assertEqual(p.take("c"), a)
        self.assertEqual(p.stats(), {"made": 2, "owned": 1, "free": 0, "waiting": 1})
        held.clear()
        p.unheld(p.number(b))
        self.assertEqual(p.take("d"), b)

    def test_restamp_same_length(self):
        data = b"\x01\x16bngEarthFar_ab12_00003\x03rest"
        out = matslots.restamp(data, "bngEarthFar_ab12_00003", "bngEarthFar_ab12_00104")
        self.assertEqual(out, b"\x01\x16bngEarthFar_ab12_00104\x03rest")
        with self.assertRaises(ValueError):
            matslots.restamp(data, "bngEarthFar_ab12_00009", "bngEarthFar_ab12_00104")


class Conn:
    def __init__(self):
        self.slot_holds, self.sent, self.desired, self.inflight, self.active = {}, {}, {}, {}, 1


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = earthd.Server(self.tmp.name, self.tmp.name)

    def tearDown(self):
        self.s.tile_pool.shutdown(wait=True)
        self.s.hm_pool.shutdown(wait=True)
        self.tmp.cleanup()

    def add(self, key):
        ck = (1, key)
        name = self.s.matslots.take(ck)
        with open(os.path.join(self.s.out_dir, self.s.tile_name(*ck)), "wb") as fh:
            fh.write(b"head" + name.encode() + b"tail")
        self.s.tiles[ck] = earthd.TileInfo([0, 0, 0], 0.0, None, 0.0, None, 0, 0.0, name,
                                           self.s.texture_name(1, key))
        return ck, name

    def test_photo_only_far_slots_are_isolated_and_released(self):
        ck,original=self.add('1_0_0')
        info=self.s.tiles[ck]
        path=os.path.splitext(os.path.join(self.s.out_dir,self.s.tile_name(*ck)))[0]+'_plain.cdae'
        source=b'head'+original.encode()+b'tail'
        with open(path,'wb') as fh: fh.write(source)
        on,off=Conn(),Conn()
        on.distant_cliff_faces=True;off.distant_cliff_faces=False
        self.s.connections.update((on,off))
        self.assertIsNone(self.s.plain_far_shape(on,ck,info))
        first=self.s.plain_far_shape(off,ck,info)
        second=self.s.plain_far_shape(off,ck,info)
        self.assertNotEqual(first['shape'],second['shape'],'held shapes are never overwritten')
        self.assertNotEqual(first['material'],original)
        with open(path,'rb') as fh: self.assertEqual(fh.read(),source)
        for variant in (first,second):
            n=matslots.MatSlots.number(variant['material'])
            self.assertEqual(off.slot_holds[n],variant['slotSerial'])
            with open(os.path.join(self.s.out_dir,matslots.MatSlots.file(variant['material'])),'rb') as fh:
                self.assertEqual(fh.read(),source.replace(original.encode(),variant['material'].encode()))
            del off.slot_holds[n]
            self.s.slots_let_go([n])
        self.assertIn(path,self.s.tile_files(self.s.tile_name(*ck)), 'variant pruned with its tile')

    def test_photo_only_packed_far_shape_preserves_mesh_and_source(self):
        import cdae
        import numpy as np
        ck, original = self.add('1_0_0')
        path = os.path.splitext(os.path.join(self.s.out_dir, self.s.tile_name(*ck)))[0] + '_plain.cdae'
        source = cdae.encode(np.array([[0, 0, 0], [10, 0, 0], [0, 10, 1.]]),
                             np.array([[0, 1, 2]]), material=original)
        with open(path, 'wb') as fh:
            fh.write(source)
        off = Conn()
        off.distant_cliff_faces = False
        variant = self.s.plain_far_shape(off, ck, self.s.tiles[ck])
        with open(os.path.join(self.s.out_dir, matslots.MatSlots.file(variant['material'])), 'rb') as fh:
            result = fh.read()
        self.assertEqual(cdae.is_packed(result), cdae.is_packed(source))
        self.assertEqual(cdae.plain(result), cdae.plain(source).replace(original.encode(), variant['material'].encode()))
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(), source)

    def test_idle_tiles_reclaimed_lru_first_and_reslotted(self):
        cks = [self.add(f"0_{i}_0") for i in range(matslots.TARGET + 20)]
        busy = cks[0][0]
        self.s.names_in_use = lambda: {busy}            # a connection still shows the first tile
        self.s.reclaim_names()
        self.assertTrue(self.s.matslots.owns(busy, cks[0][1]))
        self.assertFalse(self.s.matslots.owns(*cks[1]))         # least recently used idle ones first
        self.assertTrue(self.s.matslots.owns(*cks[-1]))
        # sent again: a copy of the shape with a fresh name, under a new file name
        ck, old = cks[1]
        info = self.s.reslot(ck, self.s.tiles[ck])
        self.assertTrue(self.s.matslots.owns(ck, info.material))
        self.assertNotEqual(info.material, old)
        self.assertEqual(info.shape, f"slot_{info.material.rsplit('_', 1)[1]}.cdae")       # its pooled file
        with open(os.path.join(self.s.out_dir, info.shape), "rb") as fh:
            self.assertEqual(fh.read(), b"head" + info.material.encode() + b"tail")
        self.assertIs(self.s.tiles[ck], info)

    def test_reslot_own_name_back_rewrites_its_file(self):
        ck, name = self.add("0_0_0")
        slot = os.path.join(self.s.out_dir, matslots.MatSlots.file(name))
        with open(slot, "wb") as fh:            # another tile had the slot meanwhile
            fh.write(b"other")
        self.s.matslots.release(ck)
        info = self.s.reslot(ck, self.s.tiles[ck])          # the stack hands its own name back
        self.assertEqual(info.material, name)
        with open(slot, "rb") as fh:
            self.assertEqual(fh.read(), b"head" + name.encode() + b"tail")

    def test_reslot_again_restamps_the_files_own_name(self):
        """A tile renamed twice (a global far tile kept across rebases, its name reclaimed at each): the second rename
        restamps the name its own file carries, not the first rename's (that failed, and the tile was sent under a
        name another tile had: it drew that tile's texture, red Utah far tiles on Pikes Peak's ground, 2026-10-29)."""
        ck, built = self.add("0_0_0")
        others = []
        for rename in range(3):
            self.s.matslots.release(ck)
            others.append(self.s.matslots.take((2, f"x_{rename}")))    # its name goes to another tile
            info = self.s.reslot(ck, self.s.tiles[ck])
            self.assertTrue(self.s.matslots.owns(ck, info.material), rename)
            self.assertNotIn(info.material, others)
            with open(os.path.join(self.s.out_dir, info.shape), "rb") as fh:
                self.assertEqual(fh.read(), b"head" + info.material.encode() + b"tail")

    def test_reslot_without_a_name_keeps_none(self):
        """A shape with no pooled name in it cannot be renamed: the tile does not own the name it had (the streamer
        then builds it again instead of sending it under another tile's name)."""
        ck, name = self.add("0_0_0")
        with open(os.path.join(self.s.out_dir, self.s.tile_name(*ck)), "wb") as fh:
            fh.write(b"no name here")
        self.s.matslots.release(ck)
        self.s.matslots.take((2, "x"))
        info = self.s.reslot(ck, self.s.tiles[ck])
        self.assertFalse(self.s.matslots.owns(ck, info.material))

    def test_name_in(self):
        p = matslots.MatSlots("ab12")
        self.assertEqual(p.name_in(b"\x01\x16bngEarthFar_ab12_00042\x03"), "bngEarthFar_ab12_00042")
        self.assertIsNone(p.name_in(b"bngEarthFar_zz99_00042"))

    def test_place_is_a_link_or_copy(self):
        src = os.path.join(self.s.out_dir, "a.cdae")
        dst = os.path.join(self.s.out_dir, "slot_00001.cdae")
        with open(src, "wb") as fh:
            fh.write(b"one")
        matslots.place(src, dst)
        with open(src + "2", "wb") as fh:
            fh.write(b"two")
        matslots.place(src + "2", dst)          # replaced, not appended / partial
        with open(dst, "rb") as fh:
            self.assertEqual(fh.read(), b"two")
        self.assertFalse(os.path.exists(dst + ".tmp"))

    def test_slots_wait_for_the_mods_report(self):
        ck, name = self.add("0_0_0")
        c = Conn()
        c.s = self.s
        self.s.connections.add(c)
        n = matslots.MatSlots.number(name)
        c.slot_holds[n] = 5                     # sent with serial 5
        self.s._evict_tile(ck)
        self.assertEqual(self.s.matslots.stats()["waiting"], 1)
        earthd.Connection.slots_gone(c, [[n, 4]])       # an older send's object: still held
        self.assertEqual(self.s.matslots.stats()["free"], 0)
        earthd.Connection.slots_gone(c, [[n, 5]])
        self.assertEqual(self.s.matslots.stats(), {"made": 1, "owned": 0, "free": 1, "waiting": 0})

    def test_reconnect_snapshot_protects_a_slot_already_in_free(self):
        """Post-hello guard only: this does not protect the disconnected interval."""
        c = Conn()
        c.s = self.s
        self.s.connections.add(c)
        name = self.s.matslots.take(("shape", 1))
        n = matslots.MatSlots.number(name)
        c.slot_holds[n] = 5
        self.s.matslots.release(("shape", 1))  # pool_shapes releases its owner, leaving the game hold
        self.assertEqual(self.s.matslots.stats()["waiting"], 1)
        self.s.connections.discard(c)        # Connection.run's disconnect cleanup
        self.s.slots_let_go([n])
        self.assertIn(n, self.s.matslots.free)
        reconnect = Conn()
        reconnect.s = self.s
        reconnect.slot_holds[n] = 5          # hello restores slotsHeld before allocating new shapes
        self.s.connections.add(reconnect)
        self.assertNotEqual(self.s.matslots.take(("shape", 2)), name)
        self.assertNotIn(n, self.s.matslots.free)
        self.assertIn(n, self.s.matslots.waiting)
        earthd.Connection.slots_gone(reconnect, [[n, 5]])
        self.assertNotIn(n, self.s.matslots.waiting)
        self.assertEqual(self.s.matslots.take(("shape", 3)), name)

    def test_road_shapes_pooled_and_held(self):
        c = Conn()
        c.s = self.s
        self.s.connections.add(c)
        with open(os.path.join(self.s.out_dir, "road_1_0_0.cdae"), "wb") as fh:
            fh.write(b"road")
        slots = self.s.pool_shapes(c, {"road": "road_1_0_0.cdae", "deck": "deck_1_0_0.cdae"})   # no deck file
        self.assertEqual(list(slots), ["road"])
        path, serial = slots["road"]
        n = int(path.rsplit("_", 1)[1].split(".")[0])
        with open(os.path.join(self.s.out_dir, f"slot_{n:05d}.cdae"), "rb") as fh:
            self.assertEqual(fh.read(), b"road")
        self.assertEqual(c.slot_holds, {n: serial})
        self.assertEqual(self.s.matslots.stats()["waiting"], 1)      # nobody owns it; the game holds it
        earthd.Connection.slots_gone(c, [[n, serial]])
        self.assertEqual(self.s.matslots.stats()["free"], 1)

    def test_disabled_faces_skip_pooled_copies_and_lod_edits(self):
        from unittest.mock import patch
        path=os.path.join(self.s.out_dir,'old_cliff.cdae')
        with open(path,'wb') as fh:
            fh.write(b'cached shape')
        c=Conn()
        c.cliff_faces=False
        c.cliff_detail='low'
        with patch('clifflod.place',side_effect=AssertionError('disabled LOD copy')):
            self.assertEqual(self.s.pool_shapes(c,{'cliff':'old_cliff.cdae','x:cliff2':'old_cliff.cdae'}),{})
        self.assertEqual(self.s.matslots.stats()['made'],0)

    def test_cliff_quality_never_rewrites_a_held_shape_or_shared_source(self):
        import cdae
        import clifflod
        import numpy as np
        import struct
        from pathlib import Path
        p = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.]])
        t = np.array([[0, 1, 2]])
        coarse = dict(positions=p, tris=t)
        data = cdae.encode(p, t, material="rock", split_meshes=True,
                           lods=[(1200, coarse), (600, coarse), (240, coarse)])
        src = Path(self.s.out_dir)/"cliff.cdae"
        src.write_bytes(data)
        c = Conn()
        c.s = self.s
        self.s.connections.add(c)
        held = {}
        for level in ("low", "extreme", "medium", "high", "normal"):
            c.cliff_detail = level
            slots = self.s.pool_shapes(c, {"cliff": src.name, "x:cliff": src.name, "road": src.name})
            self.assertEqual(len(slots), 3)
            for what, (path, serial) in slots.items():
                disk = Path(self.s.out_dir)/path.rsplit("/", 1)[-1]
                self.assertNotIn(disk, held)
                actual = disk.read_bytes()
                if what == "road":
                    self.assertEqual(actual, data)
                else:
                    shape = cdae.decode(actual)
                    sizes = [v[3] for v in struct.iter_unpack("<3i3fi2i2IfI", shape["details"][2])]
                    expected = [round(v/clifflod.SCALES[level]) for v in (1200,600,240)]+[2]
                    self.assertEqual(sizes, expected)
                held[disk] = actual
            self.assertEqual(src.read_bytes(), data, "source remains reusable for every connection")
            for disk, previous in held.items():
                self.assertEqual(disk.read_bytes(), previous, "previously loaded slot is immutable")

    def test_eviction_and_prune_free_names(self):
        ck, name = self.add("0_0_0")
        self.s._evict_tile(ck)
        self.assertEqual(self.s.matslots.stats()["free"], 1)
        ck2, _ = self.add("0_1_0")
        self.s.anchors[1] = None
        self.s.prune(2)
        self.assertEqual(self.s.matslots.stats()["owned"], 0)



class PackedReslotTest(unittest.TestCase):
    """A compressed shape (cdae.PACKED) renamed by reslot: unpacked, its name restamped, packed again (the name lives
    in the zstd body: 'material name not renewed' for every tile before, the gate's drives 2026-11-04)."""

    def test_packed_shape_renamed(self):
        import numpy as np
        import cdae
        P = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 1]], float)
        old = "bngEarthFar_edc9_00007"
        data = cdae.encode(P, np.array([[0, 1, 2]]), uvs=np.zeros((3, 2)), material=old)
        self.assertTrue(cdae.is_packed(data))
        self.assertNotIn(old.encode(), data)
        flat = cdae.plain(data)
        new = old[:-5] + "00042"
        again = cdae.pack(matslots.restamp(flat, old, new))
        self.assertTrue(cdae.is_packed(again))
        self.assertIn(new.encode(), cdae.plain(again))
        self.assertNotIn(old.encode(), cdae.plain(again))

if __name__ == "__main__":
    unittest.main()
