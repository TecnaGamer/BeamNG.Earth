"""The companion feed: the player's position for programs beside the game (docs/ADDONS.md "Companion feed"), read-only,
over HTTP from this server. LiveGPS (Leopri2008/BeamNG.Earth-LiveGPS) gets the position today by sitting between the
game and this server as a relay (the game started with -bngeport 47614 so it connects to the relay, which forwards to
47613 and reads the mod's pose messages on the way): a launch option to set and undo, and a third-party program in
the world's connection. The feed gives the same in a supported way.

    GET /v1/info     {"api": 1, "app": "<BeamNG.Earth version>"}
    GET /v1/pose     the latest pose (below), or {"ok": false} before the first one
    GET /v1/stream   text/event-stream: one "data: <pose>" event per pose the mod sends (about 2 a second), a
                     ": keep-alive" comment every KEEPALIVE_S

    pose: {"ok": true, "lat", "lon" (WGS84 degrees), "speed" (m/s), "ve", "vn" (m/s east and north in the world's
           frame), "bearing" (degrees from north of the velocity; null below 0.5 m/s), "anchorId", "t" (unix time s),
           "age" (s since it came, /v1/pose only)}

Bound to 127.0.0.1 (mode "local", the default) or every address (mode "lan", for a phone on the same Wi-Fi; the
player's choice). Read-only: it takes no input but the path. Only requests that name this computer in their Host
header are answered in local mode (a web page that points its own name at 127.0.0.1 cannot read it: DNS rebinding);
web pages of other origins cannot read the answers either unless their origin is listed (origins: an add-on's own
page, e.g. LiveGPS's map at http://127.0.0.1:47700). At most MAX_CLIENTS streams at once.
"""
import asyncio
import json
import logging
import time
import dmath

log = logging.getLogger("companionfeed")

PORT = 47615
MAX_CLIENTS = 16
MAX_REQUEST = 8192
KEEPALIVE_S = 15.0


class Feed:
    def __init__(self, app_version="", origins=(), mode="local", clock=time.time):
        self.app_version = app_version or ""
        self.origins = set(origins)
        self.mode = mode
        self.clock = clock
        self.pose = None
        self.port = None
        self._streams = set()       # asyncio.Queue per open stream

    def publish(self, lat, lon, speed=0.0, vx=0.0, vy=0.0, anchor_id=None):
        """A pose from the mod (Connection.on_pose): kept, and sent to every open stream."""
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return
        import math
        ve, vn = float(vx or 0.0), float(vy or 0.0)
        bearing = (math.degrees(dmath.atan2(ve, vn)) + 360.0) % 360.0 if math.hypot(ve, vn) >= 0.5 else None
        self.pose = {"ok": True, "lat": lat, "lon": lon, "speed": float(speed or 0.0), "ve": ve, "vn": vn,
                     "bearing": bearing, "anchorId": anchor_id if isinstance(anchor_id, int) else None,
                     "t": self.clock()}
        for q in list(self._streams):
            if q.qsize() < 8:       # (a slow reader gets the newest few, not an ever longer queue)
                q.put_nowait(self.pose)

    # --- HTTP ---
    def _host_ok(self, host):
        if self.mode == "lan":
            return True
        host = (host or "").strip().lower()
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _headers(self, status, ctype, origin, extra=""):
        h = (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nCache-Control: no-store\r\n"
             "X-Content-Type-Options: nosniff\r\n")
        if origin and origin in self.origins:
            h += f"Access-Control-Allow-Origin: {origin}\r\nVary: Origin\r\n"
        return h + extra

    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        hdr = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            hdr[k.strip().lower()] = v.strip()
        origin = hdr.get("origin", "")
        method, path = (parts[0], parts[1].split("?", 1)[0]) if len(parts) >= 3 else ("", "")

        async def reply(status, body, ctype="application/json"):
            data = body.encode()
            writer.write((self._headers(status, ctype, origin, f"Content-Length: {len(data)}\r\nConnection: close\r\n")
                          + "\r\n").encode() + data)
            try:
                await writer.drain()
            except ConnectionError:
                pass
            writer.close()

        if not method:
            return await reply("400 Bad Request", '{"error": "bad request"}')
        if not self._host_ok(hdr.get("host")):
            return await reply("403 Forbidden", '{"error": "forbidden"}')
        if method != "GET":
            return await reply("405 Method Not Allowed", '{"error": "GET only"}')
        if path == "/v1/info":
            return await reply("200 OK", json.dumps({"api": 1, "app": self.app_version}))
        if path == "/v1/pose":
            p = dict(self.pose, age=round(self.clock() - self.pose["t"], 3)) if self.pose else {"ok": False}
            return await reply("200 OK", json.dumps(p))
        if path == "/v1/stream":
            if len(self._streams) >= MAX_CLIENTS:
                return await reply("503 Service Unavailable", '{"error": "too many streams"}')
            return await self._stream(reader, writer, origin)
        return await reply("404 Not Found", '{"error": "not found"}')

    async def _stream(self, reader, writer, origin):
        q = asyncio.Queue()
        self._streams.add(q)
        gone = asyncio.ensure_future(reader.read(1))     # the reader hanging up (anything it sends ends it too)
        try:
            writer.write((self._headers("200 OK", "text/event-stream", origin, "Connection: keep-alive\r\n")
                          + "\r\n").encode())
            if self.pose:
                q.put_nowait(self.pose)
            while not gone.done():
                nxt = asyncio.ensure_future(q.get())
                done, _ = await asyncio.wait((nxt, gone), timeout=KEEPALIVE_S, return_when=asyncio.FIRST_COMPLETED)
                if nxt in done:
                    writer.write(f"data: {json.dumps(nxt.result())}\n\n".encode())
                else:
                    nxt.cancel()
                    if gone.done():
                        break
                    writer.write(b": keep-alive\n\n")
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            gone.cancel()
            self._streams.discard(q)
            writer.close()

    async def start(self, port=PORT):
        """The HTTP server; returns (asyncio server, port). Raises OSError when the port is taken."""
        host = "0.0.0.0" if self.mode == "lan" else "127.0.0.1"
        srv = await asyncio.start_server(self.handle, host, port, limit=MAX_REQUEST)
        self.port = srv.sockets[0].getsockname()[1]
        return srv, self.port
