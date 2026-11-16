"""Real loopback connections and pooled files; no game or external map providers."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd
import protocol


class ReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bnge-reconnect-")
        self.s = earthd.Server(self.tmp.name, self.tmp.name)
        self.accepted = []
        self.accepted_event = asyncio.Event()
        self.clients = []
        self.handlers = set()

        async def accept(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            conn = earthd.Connection(self.s, reader, writer)
            conn.finished = asyncio.Event()
            self.accepted.append(conn)
            self.accepted_event.set()
            try:
                await conn.run()
            finally:
                conn.finished.set()
                self.handlers.discard(task)

        self.listener = await asyncio.start_server(accept, "127.0.0.1", 0)
        self.port = self.listener.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        for writer in self.clients:
            writer.close()
        for conn in self.accepted:
            conn.writer.close()
        if self.handlers:
            await asyncio.wait_for(asyncio.gather(*tuple(self.handlers), return_exceptions=True), 3)
        self.listener.close()
        await self.listener.wait_closed()
        self.s.tile_pool.shutdown(wait=True)
        self.s.hm_pool.shutdown(wait=True)
        self.tmp.cleanup()

    async def connect(self, **fields):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.clients.append(writer)
        message = dict(type="hello", protocol=earthd.PROTOCOL, streamLease=1)
        message.update(fields)
        writer.write(protocol.encode(message))
        await writer.drain()
        for _ in range(20):
            message = await asyncio.wait_for(protocol.read_message(reader), 2)
            if message["type"] == "hello":
                return self.accepted[-1], reader, writer, message
        self.fail("no server hello")

    async def disconnect(self, conn, writer):
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(conn.finished.wait(), 2)

    def shape(self, conn, name, content):
        source = Path(self.s.out_dir, name)
        temporary = source.with_suffix(".next")
        temporary.write_bytes(content)
        os.replace(temporary, source)  # production writes atomically: do not mutate an older slot's hard-link inode
        slots = self.s.pool_shapes(conn, {"road": name})
        vfs, serial = slots["road"]
        path = Path(self.s.out_dir, vfs.rsplit("/", 1)[1])
        return path, int(path.stem.rsplit("_", 1)[1]), serial

    def resume(self, hello, slots, held=None):
        return dict(leaseId=hello["leaseId"], leaseSession=self.s.sid, slotSession=self.s.sid,
                    slotsHeld=slots, held=held or [])

    async def test_disconnect_cannot_reassign_a_shape_the_game_still_draws(self):
        old, _, writer, _ = await self.connect()
        path, slot, serial = self.shape(old, "old.cdae", b"STILL DRAWN BY FIRST GAME")
        await self.disconnect(old, writer)
        other, _, _, _ = await self.connect()
        replacement, _, _ = self.shape(other, "other.cdae", b"SECOND GAME")
        self.assertNotEqual(replacement, path, "disconnected game still holds this pooled path")
        self.assertEqual(path.read_bytes(), b"STILL DRAWN BY FIRST GAME")
        self.assertTrue(self.s.slot_held(slot))

    async def test_world_requests_before_hello_get_a_fresh_unresumable_lease(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.clients.append(writer)
        await asyncio.wait_for(self.accepted_event.wait(), 2)
        c = self.accepted[-1]
        # (lenient since the merge: a hello-less client, older tools and test clients, keeps working on a lease of its
        # own that no later hello can resume)
        with mock.patch.object(c, "spawn"):
            try:
                c.dispatch(dict(type="pose"))
            except (ValueError, KeyError, TypeError):
                pass                        # (the empty message itself is malformed: only the lease matters here)
        self.assertIsNotNone(c.lease)
        self.assertFalse(c.lease.resumable)
        self.assertEqual(self.s.anchors, {})

    async def test_repeated_hello_cannot_clear_sends_not_yet_seen_by_the_game(self):
        c, _, _, hello = await self.connect()
        _, n, serial = self.shape(c, "pending.cdae", b"PENDING")
        self.s.attach_stream(c, dict(streamLease=1, **self.resume(hello, [])))
        self.assertEqual(c.slot_holds, {n: serial})

    async def test_reconnect_snapshot_retains_live_slots_and_releases_lost_acknowledgements(self):
        old, _, writer, hello = await self.connect()
        path, n, serial = self.shape(old, "old.cdae", b"OLD")
        await self.disconnect(old, writer)
        c, _, w, response = await self.connect(**self.resume(hello, [[n, serial]]))
        self.assertEqual(response["leaseId"], hello["leaseId"])
        self.assertTrue(self.s.slot_held(n))
        c.slots_gone([[n, serial - 1]])
        self.assertTrue(self.s.slot_held(n))
        await self.disconnect(c, w)
        # The game deleted the object while offline: its delta was lost, its complete snapshot is authoritative.
        resumed, _, _, _ = await self.connect(**self.resume(hello, []))
        self.assertFalse(self.s.slot_held(n))
        replacement, _, _ = self.shape(resumed, "new.cdae", b"NEW")
        self.assertEqual(path, replacement)

    async def test_overlapping_connections_ignore_old_cleanup_and_old_acknowledgements(self):
        old, _, _, hello = await self.connect()
        path, n, serial = self.shape(old, "old.cdae", b"OLD")
        resumed, _, _, response = await self.connect(**self.resume(hello, [[n, serial]]))
        await asyncio.wait_for(old.finished.wait(), 2)
        old.slots_gone([[n, serial]])
        self.assertTrue(self.s.slot_held(n))
        self.assertEqual(resumed.slot_holds, {n: serial})
        self.assertEqual(response["leaseId"], hello["leaseId"])
        self.assertEqual(self.s.pool_shapes(old, {"road": "old.cdae"}), {})
        self.assertFalse(old.send(dict(type="status", text="obsolete")))
        self.assertEqual(path.read_bytes(), b"OLD")

    async def test_one_games_snapshot_cannot_release_another_games_hold(self):
        first, _, _, hello = await self.connect()
        _, n, serial = self.shape(first, "shared.cdae", b"SHARED")
        second, _, _, _ = await self.connect(slotSession=self.s.sid, slotsHeld=[[n, serial]])
        await self.connect(**self.resume(hello, []))
        self.assertTrue(self.s.slot_held(n))
        second.slots_gone([[n, serial]])
        self.assertFalse(self.s.slot_held(n))

    async def test_empty_snapshot_releases_slots_before_old_handler_finishes(self):
        old, _, writer, hello = await self.connect()
        path, n, serial = self.shape(old, "old.cdae", b"OLD")
        with mock.patch.object(old, "abort_stream", side_effect=lambda reason: setattr(old, "closing", True)):
            resumed, _, _, _ = await self.connect(**self.resume(hello, []))
        self.assertIn(old, self.s.connections, "old handler is deliberately still awaiting EOF")
        self.assertFalse(self.s.slot_held(n))
        replacement, new_n, new_serial = self.shape(resumed, "replacement.cdae", b"REPLACEMENT")
        self.assertEqual(path, replacement)
        await self.disconnect(old, writer)
        old.slots_gone([[n, serial]])
        self.assertEqual(resumed.slot_holds, {new_n: new_serial})
        self.assertTrue(self.s.slot_held(n))

    async def test_malformed_snapshot_does_not_replace_the_live_owner(self):
        old, _, _, hello = await self.connect()
        _, n, serial = self.shape(old, "old.cdae", b"OLD")
        other, _, _, _ = await self.connect()
        for slots in [None, [[n, serial], [n + 100, serial]], [[n, float("nan")]], [[True, serial]], [[n, 1.5]]]:
            # (lenient since the merge: no exception, the hello goes on with a lease of its own; the live owner keeps
            # its slots all the same)
            with self.subTest(snapshot=slots):
                token = self.s.attach_stream(other, dict(streamLease=1, **self.resume(hello, slots)))
                self.assertNotEqual(token, hello["leaseId"])
            self.assertIs(old.lease.connection, old)
            self.assertEqual(old.slot_holds, {n: serial})
        _, _, _, fresh = await self.connect(leaseId=hello["leaseId"], leaseSession="another-server")
        self.assertNotEqual(fresh["leaseId"], hello["leaseId"])

    async def test_write_buffer_overflow_keeps_the_games_file_reserved(self):
        old, _, _, _ = await self.connect()
        path, n, _ = self.shape(old, "old.cdae", b"OLD")
        with mock.patch.object(old.writer.transport, "get_write_buffer_size", return_value=earthd.SEND_BUFFER_MAX):
            self.assertFalse(old.send(dict(type="status", text="blocked client")))
        await asyncio.wait_for(old.finished.wait(), 2)
        second, _, _, _ = await self.connect()
        replacement, _, _ = self.shape(second, "new.cdae", b"NEW")
        self.assertNotEqual(path, replacement)
        self.assertTrue(self.s.slot_held(n))
        self.assertEqual(path.read_bytes(), b"OLD")

    async def test_disconnected_anchor_stays_until_complete_reconnect_snapshot(self):
        old, _, w, hello = await self.connect()
        anchor = self.s.new_anchor(0., 0.)
        old.active = anchor.id
        await self.disconnect(old, w)
        self.assertIn(anchor.id, self.s.held_anchors())
        await self.connect(**self.resume(hello, [], []))
        self.assertNotIn(anchor.id, self.s.held_anchors())

    async def test_reconnect_soak_reuses_a_bounded_pool(self):
        cycles = int(os.environ.get("BNE_STREAM_SOAK_CYCLES", "128"))
        self.assertGreater(cycles, 0)
        conn, _, writer, hello = await self.connect()
        previous = None
        for i in range(cycles):
            content = f"tile {i}".encode()
            path, n, serial = self.shape(conn, "travelling.cdae", content)
            if previous is not None:
                old_path, old_n, old_serial, old_content = previous
                self.assertNotEqual(path, old_path)
                self.assertEqual(old_path.read_bytes(), old_content)
                conn.slots_gone([[old_n, old_serial]])
            await self.disconnect(conn, writer)
            self.assertTrue(self.s.slot_held(n))
            conn, _, writer, response = await self.connect(**self.resume(hello, [[n, serial]]))
            self.assertEqual(response["leaseId"], hello["leaseId"])
            self.assertEqual(len(self.s.stream_leases.entries), 1)
            self.assertLessEqual(self.s.matslots.made, 2)
            self.assertEqual(path.read_bytes(), content)
            previous = path, n, serial, content
        conn.slots_gone([[n, serial]])
        await self.disconnect(conn, writer)
        self.assertEqual(len(self.s.stream_leases.entries), 0)
        self.assertEqual(self.s.matslots.stats(), dict(made=min(cycles, 2), owned=0, free=min(cycles, 2), waiting=0))

    async def test_global_tile_files_stay_live_without_pinning_unheld_tiles(self):
        c, _, w, hello = await self.connect()
        ck, idle = (earthd.GLOBAL_AID, "global"), (1, "idle")
        name = self.s.matslots.take(ck)
        self.s.matslots.take(idle)
        n = self.s.matslots.number(name)
        c.slot_holds[n] = serial = next(self.s.slot_serial)
        c.active = 1
        self.assertIn(ck, self.s.in_use())
        self.assertNotIn(idle, self.s.in_use(), "active anchor must not pin every cached tile")
        c.active = None
        await self.disconnect(c, w)
        self.assertIn(ck, self.s.in_use())
        await self.connect(**self.resume(hello, []))
        self.assertNotIn(ck, self.s.in_use())

    async def test_prior_session_folder_stays_claimed_during_disconnect(self):
        c, _, w, hello = await self.connect()
        sid = "older-session"
        self.s.orphans[sid] = 0.
        with mock.patch.object(self.s, "_touch"), mock.patch.object(self.s, "release_session") as release:
            self.s.claim_sessions(c, [sid])
            await self.disconnect(c, w)
            self.assertIn(sid, self.s.claimed_sessions())
            await self.connect(sessions=[], **self.resume(hello, []))
            self.assertNotIn(sid, self.s.claimed_sessions())
            release.assert_called_once_with(sid)

    async def test_legacy_client_disconnect_is_conservatively_reserved(self):
        old, _, w, _ = await self.connect(streamLease=0)
        path, n, _ = self.shape(old, "old.cdae", b"OLD")
        await self.disconnect(old, w)
        other, _, _, _ = await self.connect()
        replacement, _, _ = self.shape(other, "new.cdae", b"NEW")
        self.assertNotEqual(replacement, path)
        self.assertTrue(self.s.slot_held(n))


if __name__ == "__main__": unittest.main()


class LeaseExpiryTest(unittest.TestCase):
    """(merge of PR #817) A game that closed and never reconnected releases its lease after earthd.LEASE_GRACE_S."""

    def test_detached_lease_expires_after_the_grace_and_a_connected_one_never(self):
        import streamleases
        from types import SimpleNamespace
        reg = streamleases.Registry()
        gone = SimpleNamespace(lease=None, held=set(), active=None, sessions={"s1"})
        live = SimpleNamespace(lease=None, held=set(), active=None, sessions=set())
        reg.attach(gone, None, {3: 1}, set(), True)
        reg.attach(live, None, {4: 1}, set(), True)
        reg.detach(gone)
        t = gone.lease.detached_at
        self.assertTrue(reg.held(3))
        self.assertEqual(reg.expire(t + 10, 900), [])           # still in its grace: may reconnect
        self.assertTrue(reg.held(3))
        expired = reg.expire(t + 901, 900)
        self.assertEqual([lease.slots for lease in expired], [{3: 1}])
        self.assertFalse(reg.held(3))
        self.assertTrue(reg.held(4))                            # the connected game's hold stays
        self.assertEqual(reg.expire(t + 10 ** 6, 900), [])
