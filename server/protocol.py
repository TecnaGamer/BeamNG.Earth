"""Framing: 4-byte little-endian uint32 length + UTF-8 JSON payload.

The game connection is served by asyncio (asyncio.start_server) or, with BNGE_HOST=on, by earthfast's host (the Rust
port's phase 1): Rust owns the socket, the reads, the framing and the writes on threads of its own, without the GIL,
and hands what came to the event loop in batches; HostStream gives the handlers asyncio's reader / writer surface over
it, so they run unchanged. start_server() picks one; listen_port() reads the port of either."""
import asyncio
import atexit
import collections
import json
import logging
import os
import struct
import weakref

HEADER = struct.Struct("<I")
MAX_FRAME = 16 * 1024 * 1024
# the game connection in Rust (earthfast.Host) instead of asyncio's: off by default until proven (the Rust port's
# phase 1; byte-identical sessions, tools/portcheck/msgcmp.py)
HOST = os.environ.get("BNGE_HOST", "off").strip().lower() == "on"
# 1b: consecutive poses merged while they wait for a busy loop (the later pose, slotsGone joined); off for A/B
HOST_MERGE = os.environ.get("BNGE_HOST_MERGE", "on").strip().lower() != "off"
# 1c (developers): every frame the game sends recorded into this folder (tools/hostreplay.py plays it again);
# BNGE_HOST_RECORD_OUT=1: what the server sends too. Recordings hold the player's positions: private
RECORD = os.environ.get("BNGE_HOST_RECORD") or None
RECORD_OUT = os.environ.get("BNGE_HOST_RECORD_OUT") == "1"
HOST_VERSION = 371          # the earthfast with Host / Wire (the pose merge, the recorder, the read flow control)
log = logging.getLogger("earthd.host")


class ProtocolError(Exception):
    pass


class RawJson:
    """A JSON value already encoded (its text: real UTF-8, no \\u escapes), spliced into the frame as it is by encode():
    a tile's inline file (staging.inline_raw, read in the worker that built it) was parsed and encoded again on the
    event loop, ~10-20 ms of the interpreter per large tile file. Read as the value it holds (parsed on first use)."""
    __slots__ = ("text", "_value")
    _UNSET = object()

    def __init__(self, text):
        self.text = text
        self._value = RawJson._UNSET

    def value(self):
        if self._value is RawJson._UNSET:
            self._value = json.loads(self.text)
        return self._value

    def __getitem__(self, k):
        return self.value()[k]

    def __contains__(self, k):
        return k in self.value()

    def __iter__(self):
        return iter(self.value())

    def __len__(self):
        return len(self.value())

    def get(self, k, default=None):
        return self.value().get(k, default)

    def __eq__(self, other):
        return self.value() == (other.value() if isinstance(other, RawJson) else other)

    __hash__ = None


def encode(msg):
    # real UTF-8, not \u escapes: the game's jsonDecode leaves those as text ("Provence-Alpes-Cu00f4te d'Azur" in the status
    # bar, 2026-10-15)
    raw = [k for k, v in msg.items() if type(v) is RawJson]
    if raw:                 # (values already encoded: after the rest, as they are)
        rest = {k: v for k, v in msg.items() if type(v) is not RawJson}
        head = json.dumps(rest, separators=(",", ":"), ensure_ascii=False)
        body = ",".join(json.dumps(k, ensure_ascii=False) + ":" + msg[k].text for k in raw)
        payload = (head[:-1] + ("," if rest else "") + body + "}").encode("utf-8")
    else:
        payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return HEADER.pack(len(payload)) + payload


def decode_payload(payload):
    try:
        msg = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolError(f"bad JSON frame: {e}") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("frame is not an object with a 'type'")
    return msg


def _check_len(n):
    if n > MAX_FRAME:
        raise ProtocolError(f"frame too large: {n} bytes")
    return n


async def read_message(reader):
    """Next message from an asyncio StreamReader (or a HostStream), or None on a clean EOF."""
    if type(reader) is HostStream:
        return await reader.next_message()
    try:
        head = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError as e:
        if not e.partial:
            return None
        raise ProtocolError("connection closed inside a frame header") from None
    n = _check_len(HEADER.unpack(head)[0])
    try:
        payload = await reader.readexactly(n)
    except asyncio.IncompleteReadError:
        raise ProtocolError("connection closed inside a frame") from None
    return decode_payload(payload)


class Decoder:
    """Incremental decoder for blocking clients: feed() bytes, get messages."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= HEADER.size:
            n = _check_len(HEADER.unpack_from(self.buf)[0])
            if len(self.buf) < HEADER.size + n:
                break
            out.append(decode_payload(bytes(self.buf[HEADER.size:HEADER.size + n])))
            del self.buf[:HEADER.size + n]
        return out


class _HostTransport:
    """The part of asyncio's transport earthd uses (get_write_buffer_size, abort), over a HostStream's wire."""

    def __init__(self, stream):
        self._stream = stream

    def get_write_buffer_size(self):
        return self._stream._wire.pending()

    def abort(self):
        self._stream._abort()

    def close(self):
        self._stream.close()

    def is_closing(self):
        return self._stream._wire.is_closing()

    def get_extra_info(self, name, default=None):
        return self._stream.get_extra_info(name, default)


class HostStream:
    """The game connection's reader and writer when earthfast's host owns the socket: asyncio's StreamReader /
    StreamWriter surface that earthd.Connection uses (read_message, write, drain, is_closing, close,
    get_extra_info('peername'), transport.get_write_buffer_size / abort), fed by HostServer on the event loop.

    As asyncio's: drain() returns at once until the queue goes over 64 KiB, then waits until it is at 16 KiB (the
    water marks, kept by the wire); close() sends what is queued, then closes; abort() drops it; writes after either
    are ignored; a connection lost raises ConnectionResetError('Connection lost') in drain(), a socket error raises
    in both read and drain; a peer's EOF reads as None. Reading is flow-controlled as asyncio's StreamReader (limit
    64 KiB): the host stops reading while more than 128 KiB the handler has not consumed waits (in Rust or in _frames)
    and goes on at 64 KiB, so a handler that does not read leaves the rest in the kernel and the game's sends wait."""

    def __init__(self, wire, loop):
        self._wire = wire
        self._loop = loop
        self._frames = collections.deque()
        self._ended = False
        self._end_exc = None            # raised once the frames before it are read (a protocol error)
        self._os_exc = None             # raised at once, by reads and drain (asyncio's reader.set_exception)
        self._read_waiter = None
        self._drain_waiters = []
        self._peer = tuple(wire.peer())
        self.transport = _HostTransport(self)

    # -- fed by HostServer (on the loop)
    def _feed_frame(self, data):
        self._frames.append(data)
        self._wake_reader()

    def _feed_end(self, end):
        self._ended = True
        if end is not None:
            errno_, text, is_protocol = end
            if is_protocol:
                self._end_exc = ProtocolError(text)
            else:
                self._os_exc = OSError(errno_, text) if errno_ is not None else ConnectionResetError(text)
        self._wake_reader()
        self._wake_drain()

    def _wake_reader(self):
        w = self._read_waiter
        if w is not None and not w.done():
            w.set_result(None)

    def _wake_drain(self):
        for w in self._drain_waiters:
            if not w.done():
                w.set_result(None)

    # -- the reader
    async def next_message(self):
        """The next message (decode_payload), or None at EOF; ProtocolError / OSError as read_message raises them."""
        while True:
            if self._os_exc is not None:
                raise self._os_exc
            if self._frames:
                data = self._frames.popleft()
                # (consumed: the host's reader goes on once at most 64 KiB is left unconsumed, as asyncio's
                # StreamReader resumes reading; it stops past 128 KiB)
                self._wire.consumed(len(data) + HEADER.size)
                return decode_payload(data)
            if self._ended:
                if self._end_exc is not None:
                    raise self._end_exc
                return None
            if self._read_waiter is not None:
                raise RuntimeError("next_message() called while another coroutine is already waiting for a message")
            self._read_waiter = self._loop.create_future()
            try:
                await self._read_waiter
            finally:
                self._read_waiter = None

    # -- the writer
    def write(self, data):
        self._wire.write(data if type(data) is bytes else bytes(data))

    async def drain(self):
        if self._os_exc is not None:
            raise self._os_exc
        if self._wire.is_closing():
            await asyncio.sleep(0)
        if self._wire.lost():
            raise ConnectionResetError("Connection lost")
        # (as asyncio's: once waiting, the queue drained or the connection gone both end the wait without an error)
        while self._wire.drain_wait():
            fut = self._loop.create_future()
            self._drain_waiters.append(fut)
            try:
                await fut
            finally:
                self._drain_waiters.remove(fut)

    def is_closing(self):
        return self._wire.is_closing()

    def close(self):
        self._wire.close()

    def _abort(self):
        self._wire.abort()
        self._wake_drain()

    async def wait_closed(self):
        while not (self._ended and self._wire.lost()):
            await asyncio.sleep(0.01)

    def get_extra_info(self, name, default=None):
        return self._peer if name == "peername" else default


class HostServer:
    """earthfast's host serving handler(reader, writer) for each connection (asyncio.start_server's surface that
    earthd.serve and the tests use: close, is_serving, serve_forever, wait_closed, async with; .port). The host
    wakes the loop through a local socket pair (one byte: something came); the loop then takes everything that came
    as one batch (_on_wake). Made by start_server (its wake socket joins the loop there)."""

    def __init__(self, handler, host, port, loop, record=None, record_out=None):
        import socket
        import sys

        import fast
        fast.require(HOST_VERSION, "the game connection in Rust (BNGE_HOST=on)")
        import earthfast
        self._handler, self._loop = handler, loop
        self._streams = {}
        self._tasks = set()
        self._serving = None
        self._wake_tr = None
        self.batches = 0                # (batches the loop took: what came while it was busy joins one batch)
        # (the event loop's treatment for the host's threads: earthd.LOOP_RAISE, Windows; Linux threads inherit it)
        raise_threads = sys.platform == "win32" and os.environ.get("BNGE_LOOP_RAISE", "1") != "0"
        record = RECORD if record is None else record
        if record:
            os.makedirs(record, exist_ok=True)
            log.warning("recording the game connection into %s (BNGE_HOST_RECORD: the player's positions, keep it "
                        "private)", record)
        self._host = earthfast.Host(host, port, log, raise_threads, HOST_MERGE, record or None,
                                    RECORD_OUT if record_out is None else record_out)
        self._wake = socket.socket(fileno=self._host.take_wake())
        self.port = self._host.port
        _LIVE.add(self)

    async def _start(self):
        srv = self

        class _Wake(asyncio.Protocol):
            def data_received(self, data):
                srv._on_wake()
        self._wake_tr, _ = await self._loop.connect_accepted_socket(_Wake, self._wake)

    def _on_wake(self):
        items = self._host.take()
        if not items:
            return
        self.batches += 1
        streams = self._streams
        for cid, kind, value in items:
            if kind == 1:
                s = streams.get(cid)
                if s is not None:
                    s._feed_frame(value)
            elif kind == 0:
                self._accept(cid, value)
            elif kind == 2:
                s = streams.get(cid)
                if s is not None:
                    s._feed_end(value)
            else:
                s = streams.get(cid)
                if s is not None:
                    s._wake_drain()

    def _accept(self, cid, wire):
        s = HostStream(wire, self._loop)
        self._streams[cid] = s
        try:
            res = self._handler(s, s)
        except Exception as e:
            self._failed(s, e)
            return
        if not asyncio.iscoroutine(res):
            return
        task = self._loop.create_task(res)
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._done(t, cid, s))

    def _done(self, task, cid, stream):
        self._tasks.discard(task)
        if self._streams.get(cid) is stream:
            del self._streams[cid]
        # (as asyncio's: a handler cancelled or failed closes its transport; one that returned leaves it as it is)
        if task.cancelled():
            stream.close()
            return
        exc = task.exception()
        if exc is not None:
            self._failed(stream, exc)

    def _failed(self, stream, exc):
        self._loop.call_exception_handler({"message": "Unhandled exception in client_connected_cb",
                                           "exception": exc, "transport": stream.transport})
        stream.close()

    # -- asyncio.Server's surface
    def close(self):
        self._host.close()

    def is_serving(self):
        return self._host.is_serving()

    async def serve_forever(self):
        if self._serving is not None:
            raise RuntimeError("serve_forever is already being awaited")
        self._serving = self._loop.create_future()
        try:
            await self._serving
        except asyncio.CancelledError:
            try:
                self.close()
                await self.wait_closed()
            finally:
                raise
        finally:
            self._serving = None

    async def wait_closed(self):
        """Once closed, until every connection's handler has finished (asyncio 3.12's: every connection gone)."""
        while self.is_serving():
            await asyncio.sleep(0.05)
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.close()
        await self.wait_closed()

    def stats(self, reset=False):
        """The host's numbers (earthfast.Host.stats): frames / bytes in and out, the lane waits (ms), connections."""
        return self._host.stats(reset)

    def summary(self):
        """The host line since the last one (earthd's net_log, once a minute), or None without traffic:
        "host: 1,204 frames in (0.3 MB), 3,880 out (412.0 MB); loop wait p50 1 ms, p99 38 ms, max 7,512 ms"."""
        st = self._host.stats(True)
        if not st["frames_in"] and not st["frames_out"]:
            return None
        line = (f"host: {st['frames_in']:,} frames in ({st['bytes_in'] / 1e6:.1f} MB), {st['frames_out']:,} out "
                f"({st['bytes_out'] / 1e6:.1f} MB); loop wait p50 {st['wait_p50_ms']:,.0f} ms, "
                f"p99 {st['wait_p99_ms']:,.0f} ms, max {st['wait_max_ms']:,.0f} ms")
        if st.get("merged"):
            line += f"; {st['merged']:,} poses merged"
        return line

    def shutdown(self):
        """Every thread of the host ended (connections aborted): before the interpreter finalizes."""
        _LIVE.discard(self)
        self._host.shutdown()
        tr, self._wake_tr = self._wake_tr, None
        try:
            if tr is not None:
                tr.close()
            else:
                self._wake.close()
        except Exception:           # (the loop already closed: the process ends)
            pass


_LIVE = weakref.WeakSet()       # the HostServers not shut down yet (the atexit hook's)


@atexit.register
def _shutdown_all():
    for srv in list(_LIVE):
        try:
            srv.shutdown()
        except Exception:       # (the process ends anyway)
            pass


async def start_server(handler, host, port, rust=None, **host_options):
    """asyncio.start_server(handler, host, port), or earthfast's host with BNGE_HOST=on (rust: True / False forces
    one; host_options: HostServer's record / record_out)."""
    if HOST if rust is None else rust:
        srv = HostServer(handler, host, port, asyncio.get_running_loop(), **host_options)
        try:
            await srv._start()
        except BaseException:
            srv.shutdown()
            raise
        return srv
    return await asyncio.start_server(handler, host, port)


def listen_port(srv):
    """The port an asyncio server or a HostServer listens on."""
    return srv.port if isinstance(srv, HostServer) else srv.sockets[0].getsockname()[1]
