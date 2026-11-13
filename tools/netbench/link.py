"""A player's internet line, emulated inside the cache proxy (cacheproxy.py): answers from the store reach a server at
the line's speed and latency instead of at once, so a run measures how long a place takes to load, not only its bytes.

A server picks its line with its proxy credentials (netprobe --link): HTTPS_PROXY=http://link-MBIT-RTT-ID:x@host:port.
Every process of that server (its workers too) shares one Line: the bandwidth is split evenly between the answers in
flight (as TCP connections sharing a bottleneck roughly do), and each answer waits one round trip before its first
byte. A new connection (a CONNECT) waits two (TCP and TLS 1.3 handshakes).

Not modelled: TCP slow start, a host's own speed and per-connection limits, distance to each host (one RTT for all).
"""
import asyncio
import base64
import re

USER = re.compile(r"link-(?P<mbit>\d+(?:\.\d+)?)-(?P<rtt>\d+(?:\.\d+)?)-(?P<id>[\w.]+)")
HEADER_BYTES = 400          # an answer's status line and headers, roughly
HANDSHAKE_RTTS = 2          # a new HTTPS connection: TCP, then TLS 1.3


def user_of(proxy_authorization):
    """The user name of a Basic Proxy-Authorization header, or None."""
    if not proxy_authorization or not proxy_authorization.lower().startswith("basic "):
        return None
    try:
        return base64.b64decode(proxy_authorization[6:].strip()).decode().split(":", 1)[0]
    except (ValueError, UnicodeDecodeError):
        return None


class Line:
    """One emulated line: answers in flight share `mbit` evenly (fluid processor sharing), each after `rtt_ms`."""

    def __init__(self, mbit, rtt_ms):
        self.bytes_per_s = mbit * 1e6 / 8
        self.rtt = rtt_ms / 1000
        self.active = {}            # future -> bytes still to come
        self.at = None              # loop time the remaining counts were last brought up to date
        self.timer = None

    async def connect(self):
        await asyncio.sleep(HANDSHAKE_RTTS * self.rtt)

    async def answer(self, n):
        """Waits as long as an answer of n body bytes takes on this line."""
        await asyncio.sleep(self.rtt)
        loop = asyncio.get_running_loop()
        self._advance(loop.time())
        fut = loop.create_future()
        self.active[fut] = n + HEADER_BYTES
        self._schedule()
        await fut

    def _advance(self, now):
        if self.active and self.at is not None:
            share = self.bytes_per_s * (now - self.at) / len(self.active)
            for fut in self.active:
                self.active[fut] -= share
        self.at = now

    def _schedule(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        for fut in [f for f, left in self.active.items() if left <= 1 or f.done()]:
            del self.active[fut]
            if not fut.done():
                fut.set_result(None)
        if self.active:
            first = min(self.active.values())
            loop = asyncio.get_running_loop()
            self.timer = loop.call_later(first * len(self.active) / self.bytes_per_s, self._tick)

    def _tick(self):
        self.timer = None
        self._advance(asyncio.get_running_loop().time())
        self._schedule()


class Lines:
    """The lines by proxy user name, and which client connection uses which."""

    def __init__(self):
        self.lines = {}             # user name -> Line
        self.conns = {}             # client connection id -> Line

    def line(self, user):
        if user not in self.lines:
            m = USER.fullmatch(user or "")
            self.lines[user] = Line(float(m["mbit"]), float(m["rtt"])) if m else None
        return self.lines[user]

    def bind(self, flow):
        """The line of a flow: its own Proxy-Authorization (a CONNECT, a plain HTTP request), else its connection's."""
        user = user_of(flow.request.headers.get("proxy-authorization"))
        flow.request.headers.pop("proxy-authorization", None)
        if user is not None:
            self.conns[flow.client_conn.id] = self.line(user)
        return self.conns.get(flow.client_conn.id)

    def forget(self, client):
        self.conns.pop(client.id, None)
