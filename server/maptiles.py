"""Map tiles for the in-game world map (mod ui/ui-vue/mods/beamngEarth/EarthMap.vue).

The game's UI (CEF) may not reach the internet, so the companion serves 256 px Web Mercator tiles
z/x/y over a small HTTP endpoint bound to 127.0.0.1 only (its port goes in hello.mapPort). The
tiles come from the server's own imagery chain (imagery.py: the same sources, the same disk cache
and network limits, and the same attribution the mod already shows):

  - z <= EOX_FIRST_MAX_Z: Sentinel-2 cloudless (EOX) first: one consistent global picture, and no
    USGS coverage probes for the world view;
  - above: the chain's order (USGS first in the US, z <= 16; EOX elsewhere, native to z14);
  - above a source's max zoom the ancestor tile at that zoom is cropped and scaled up (overzoom),
    so the map zooms to MAX_Z everywhere.

A source with no data there (404, outside its coverage probe, a blank tile) passes to the next one.

Layers (the map's Layers panel): "sat" (the imagery above), "map" (EOX terrain-light, a street / terrain map,
JPEG) and "labels" (EOX overlay_bright: roads and names on transparent PNG, drawn over either). Both EOX layers are
native to z14 (checked 2026-10-16: terrain-light answers above but only scaled up, overlay_bright 404s at z15) and
are overzoomed above like the imagery. They are fetched like imagery tiles (Imagery.raw: the same netfetch host
limits, disk cache and 404 markers), never prefetched. No Google / Esri / OSM tile servers.

As files (TileFiles, the map_tiles message): the game's UI browser cancels every http request, so the server
writes each tile into the game's temp folder (tile_path) and says so (map_tiles_ready) tile by tile, building
FILE_WORKERS at a time; each request replaces the queue (the newest view first; tiles the map no longer asks
for are dropped before they start).

HTTP (GET only, one request per connection):
  /tiles/{z}/{x}/{y}.jpg   -> image/jpeg; 404 no imagery anywhere; 503 fetch failed (retry later)
  /attribution             -> application/json {"attribution": "..."}
  /health                  -> "ok"
No Google or Cesium tiles; every source is credited by imagery.attribution().
"""
import asyncio
import io
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

import imagery as imagerymod
import staging

log = logging.getLogger("earthd.maptiles")

MAX_Z = 18                  # the deepest zoom served (overzoomed above the sources' own)
EOX_FIRST_MAX_Z = 12        # up to here EOX goes before the rest of the chain
JPEG_QUALITY = 85
# netfetch kind: "demand", what the player is looking at (2026-10-15: as "prefetch" the map's tiles were dropped
# while the terrain streamed after a teleport, "bucket low", and the map stayed blank squares for a minute; the
# UI asks for at most 48 at a time, so the terrain still gets most of the budget)
FETCH_KIND = "demand"
MAX_REQUEST = 8192          # bytes of request head accepted
READ_TIMEOUT_S = 10


FILE_WORKERS = 12           # map tile files built at once (network-bound; netfetch caps each host: USGS 12, EOX 6)
RETRY_S = 4.0               # a tile that failed or has no imagery is asked again only after this
# The tile files kept (2026-10-16: ~8k files in ~1.2k folders after a night of map use, every layer, never removed;
# the game watches its user folder): beyond MAX_FILES the oldest go, one at a time PRUNE_GAP_S apart (the game's file
# watcher never sees a burst), when the server starts and after every PRUNE_EVERY new tiles. The map asks again for a
# tile it shows whose file is gone.
MAX_FILES = 4000
PRUNE_EVERY = 500
PRUNE_GAP_S = 0.01


def prune(root, keep=MAX_FILES, gap_s=PRUNE_GAP_S, sleep=time.sleep):
    """The oldest (by mtime) tile files under root deleted down to `keep`, then the emptied folders; the number
    deleted. Errors (a file the map just rewrote, a folder gone) are skipped."""
    files = []
    for d, _, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            try:
                files.append((os.stat(p).st_mtime, p))
            except OSError:
                pass
    if len(files) <= keep:
        return 0
    files.sort()
    n = 0
    for _, p in files[:len(files) - keep]:
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
        if gap_s:
            sleep(gap_s)
    for d, _, _ in os.walk(root, topdown=False):       # children first: a parent is empty once they are gone
        if d != root:
            try:
                if not os.listdir(d):
                    os.rmdir(d)
            except OSError:
                pass
    return n
MAX_FILES_REQUEST = 128     # tiles per map_tiles request (two layers over a 2560 x 1440 view)
EOX_MAP_CREDIT = "EOX IT Services GmbH (maps.eox.at), data © OpenStreetMap contributors and others"
# The map's other layers: XYZ sources outside the imagery chain (so never among --imagery's choices)
LAYER_SOURCES = {
    "map": imagerymod.Source(
        "eoxmap", "https://tiles.maps.eox.at/wmts/1.0.0/terrain-light_3857/default/g/{z}/{y}/{x}.jpg", 14,
        "Terrain Light by " + EOX_MAP_CREDIT),
    "labels": imagerymod.Source(
        "eoxlabels", "https://tiles.maps.eox.at/wmts/1.0.0/overlay_bright_3857/default/g/{z}/{y}/{x}.png", 14,
        "Overlay by " + EOX_MAP_CREDIT),
}
# "Dark" in the map's Layers panel is not a tile source of its own: the mod shows the "map" tiles (terrain-light,
# which already has hillshaded relief baked in) through a CSS invert+hue-rotate filter (EarthMap.vue's .tile-dark),
# the usual trick for a dark map without a dark-themed tile set of its own (checked, 2026-10-27: a colour-inverted
# terrain-light tile over the Alps keeps the relief, the user wanted mountains to still show, not a flat dark colour).
LAYERS = {"sat": "jpg", "map": "jpg", "labels": "png"}     # layer id -> file extension


def tile_path(root, layer, z, x, y):
    """A map tile's file: <root>/z/x/y.jpg for "sat" (where it always was), else <root>/<layer>/z/x/y.<ext>."""
    ext = LAYERS[layer]
    if layer == "sat":
        return os.path.join(root, str(z), str(x), f"{y}.{ext}")
    return os.path.join(root, layer, str(z), str(x), f"{y}.{ext}")


class BadTile(ValueError):
    pass


def check_tile(z, x, y):
    """Validated ints (z, x, y) or BadTile."""
    try:
        z, x, y = int(z), int(x), int(y)
    except (TypeError, ValueError):
        raise BadTile("not a tile") from None
    if not 0 <= z <= MAX_Z:
        raise BadTile(f"zoom must be 0..{MAX_Z}")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise BadTile("tile outside the world")
    return z, x, y


def parse_path(path):
    """'/tiles/z/x/y.jpg' (or .png / no extension, a query string ignored) -> (z, x, y);
    BadTile for anything else."""
    path = path.split("?", 1)[0]
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[0] != "tiles":
        raise BadTile("not a tile path")
    y = parts[3].rsplit(".", 1)[0] if "." in parts[3] else parts[3]
    return check_tile(parts[1], parts[2], y)


def crop_up(rgb, dz, x, y):
    """The part of an ancestor tile (dz levels up) covering tile x, y, scaled back to 256 px."""
    if dz <= 0:
        return rgb
    n = 1 << dz
    span = rgb.shape[0] // n
    if span < 1:                # deeper than one source pixel: take the pixel
        span = 1
    ox, oy = (x % n) * rgb.shape[1] // n, (y % n) * rgb.shape[0] // n
    part = rgb[oy:oy + span, ox:ox + span]
    im = Image.fromarray(np.ascontiguousarray(part)).resize((imagerymod.TILE, imagerymod.TILE), Image.BILINEAR)
    return np.asarray(im)


def encode_jpeg(rgb):
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb, dtype=np.uint8)).save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def encode_png(rgba):
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgba, dtype=np.uint8)).save(buf, "PNG", compress_level=3)
    return buf.getvalue()


def layer_image(data, layer, dz, x, y):
    """A layer's file bytes from its source tile's bytes, dz levels up (cropped and scaled up): the source's own
    bytes when they are already the file's format at 256 px."""
    png = LAYERS[layer] == "png"
    try:
        im = Image.open(io.BytesIO(data))
        if dz <= 0 and im.size == (imagerymod.TILE, imagerymod.TILE) and im.format == ("PNG" if png else "JPEG"):
            return data
        im = im.convert("RGBA" if png else "RGB")
    except (OSError, ValueError) as e:
        raise imagerymod.ImageryError(f"undecodable map layer tile: {e}") from None
    if im.size != (imagerymod.TILE, imagerymod.TILE):
        im = im.resize((imagerymod.TILE, imagerymod.TILE), Image.BILINEAR)
    a = crop_up(np.asarray(im), dz, x, y)
    return encode_png(a) if png else encode_jpeg(a)


class MapTiles:
    """Map tiles from an imagery.Imagery (or anything with .chain, .tiles(keys, kind) and
    .attribution())."""

    def __init__(self, imagery, kind=FETCH_KIND):
        self.imagery = imagery
        self.kind = kind
        self.stats = {"served": 0, "missing": 0, "failed": 0, "bad": 0}

    def order(self, z):
        chain = list(self.imagery.chain)
        if z <= EOX_FIRST_MAX_Z:
            chain.sort(key=lambda s: 0 if s.name.startswith("eox") else 1)     # stable: chain order kept
        return chain

    def tile(self, z, x, y):
        """(jpeg bytes, source name), or None where no source has imagery. Raises
        imagery.ImageryError when a fetch failed (and no later source had the tile)."""
        z, x, y = check_tile(z, x, y)
        err = None
        for src in self.order(z):
            if z < src.min_zoom:
                continue
            sz = min(z, src.max_zoom)
            dz = z - sz
            key = (src, sz, x >> dz, y >> dz)
            if not imagerymod.in_bounds(*key):
                continue                    # a regional source elsewhere
            try:
                t = self.imagery.tiles([key], self.kind).get(key)
            except imagerymod.ImageryError as e:
                err = e
                continue
            if t is None or t.uniform:
                continue
            return encode_jpeg(crop_up(t.rgb, dz, x, y)), src.name
        if err is not None:
            raise err
        return None

    def layer_tile(self, layer, z, x, y):
        """A layer's tile as file bytes (JPEG; PNG for "labels"), or None where it has none. Raises
        imagery.ImageryError when a fetch failed."""
        z, x, y = check_tile(z, x, y)
        if layer == "sat":
            r = self.tile(z, x, y)
            return r[0] if r is not None else None
        src = LAYER_SOURCES[layer]
        sz = min(z, src.max_zoom)
        dz = z - sz
        data = self.imagery.raw(src, sz, x >> dz, y >> dz, self.kind)
        if data is None:
            return None
        return layer_image(data, layer, dz, x, y)

    def attribution(self):
        return self.imagery.attribution()

    # --- HTTP ------------------------------------------------------------------------------
    def respond(self, method, path):
        """(status, content type, body) for one request; runs in a worker thread (fetches block)."""
        if method not in ("GET", "HEAD"):
            return 405, "text/plain", b"GET only"
        p = path.split("?", 1)[0]
        if p == "/health":
            return 200, "text/plain", b"ok"
        if p == "/attribution":
            return 200, "application/json", json.dumps({"attribution": self.attribution()}).encode()
        try:
            z, x, y = parse_path(p)
        except BadTile as e:
            self.stats["bad"] += 1
            return 404, "text/plain", str(e).encode()
        try:
            got = self.tile(z, x, y)
        except imagerymod.ImageryError as e:
            self.stats["failed"] += 1
            return 503, "text/plain", str(e).encode()
        except Exception as e:                  # never take the server down for a map tile
            log.exception("map tile %s/%s/%s failed", z, x, y)
            self.stats["failed"] += 1
            return 500, "text/plain", str(e).encode()
        if got is None:
            self.stats["missing"] += 1
            return 404, "text/plain", b"no imagery here"
        self.stats["served"] += 1
        return 200, "image/jpeg", got[0]

    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        try:
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            method, path, _ = line.split(" ", 2)
        except ValueError:
            method, path = "", ""
        if method and path.startswith("/"):
            status, ctype, body = await asyncio.get_running_loop().run_in_executor(
                None, self.respond, method, path)
        else:
            status, ctype, body = 400, "text/plain", b"bad request"
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
                  500: "Internal Server Error", 503: "Service Unavailable"}.get(status, "")
        hdr = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
               "Access-Control-Allow-Origin: *\r\n"
               f"Cache-Control: {'max-age=86400' if status == 200 else 'no-store'}\r\n"
               "Connection: close\r\n\r\n")
        try:
            writer.write(hdr.encode("latin-1") + (b"" if method == "HEAD" else body))
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def start(self, port=0, host="127.0.0.1"):
        """The HTTP server on host:port (0: any free port); returns (asyncio server, port)."""
        srv = await asyncio.start_server(self.handle, host, port, limit=MAX_REQUEST)
        return srv, srv.sockets[0].getsockname()[1]


class TileFiles:
    """The map's tiles as files (the game's UI cannot fetch http): request() takes the tiles the map wants, newest
    request first; `workers` threads build them (MapTiles.layer_tile), write each to tile_path(root, ...) and call
    on_ready(key, ok) for it as soon as it is done (key = (layer, z, x, y); ok: its file is there).

    Each request replaces the queue: tiles of older requests the newest one does not ask for are dropped before they
    start (the map moved on); tiles being built finish. A tile that failed or had no imagery is skipped by requests
    for retry_s (the map asks again every few seconds while it misses one)."""

    def __init__(self, maptiles, root, on_ready=None, workers=FILE_WORKERS, retry_s=RETRY_S, clock=time.monotonic):
        self.maptiles = maptiles
        self.root = root
        self.on_ready = on_ready
        self.workers = workers
        self.retry_s = retry_s
        self.clock = clock
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="map-tiles")
        self._lock = threading.Lock()
        self._want = []             # keys still to build, in order (the newest request's)
        self._running = set()       # keys being built
        self._failed = {}           # key -> clock() of its failure / "no imagery"
        self._active = 0            # worker loops running
        self.stats = {"built": 0, "had": 0, "missing": 0, "failed": 0, "dropped": 0}
        self._since_prune = PRUNE_EVERY             # a prune at the first request (old files of earlier sessions)
        self._pruning = False

    def request(self, keys):
        """keys: [(layer, z, x, y)], the most wanted first. Never blocks (no file access here)."""
        now = self.clock()
        with self._lock:
            self._failed = {k: t for k, t in self._failed.items() if now - t < self.retry_s}
            want = [k for k in dict.fromkeys(keys) if k not in self._running and k not in self._failed]
            ws = set(want)
            self.stats["dropped"] += sum(1 for k in self._want if k not in ws)
            self._want = want
            n = min(self.workers - self._active, len(want))
            self._active += n
        for _ in range(n):
            self.pool.submit(self._work)
        self._maybe_prune()

    def _maybe_prune(self):
        with self._lock:
            if self._pruning or self._since_prune < PRUNE_EVERY:
                return
            self._pruning, self._since_prune = True, 0

        def run():
            try:
                k = prune(self.root)
                if k:
                    log.info("map tiles: %d old files removed (at most %d kept)", k, MAX_FILES)
            except Exception as e:      # (never the map's business)
                log.debug("map tile prune: %s", e)
            finally:
                self._pruning = False
        threading.Thread(target=run, name="map-prune", daemon=True).start()

    def pending(self):
        """(queued, being built) counts."""
        with self._lock:
            return len(self._want), len(self._running)

    def _work(self):
        while True:
            with self._lock:
                if not self._want:
                    self._active -= 1
                    return
                key = self._want.pop(0)
                self._running.add(key)
            ok = False
            try:
                ok = self._build(key)
            except Exception:                   # never lose a worker over one tile
                log.exception("map tile %s failed", key)
            finally:
                with self._lock:
                    self._running.discard(key)
                    if not ok:
                        self._failed[key] = self.clock()
            cb = self.on_ready
            if cb is not None:
                try:
                    cb(key, ok)
                except Exception as e:          # (the connection closed meanwhile)
                    log.debug("map tile ready %s: %s", key, e)

    def _build(self, key):
        layer, z, x, y = key
        path = tile_path(self.root, layer, z, x, y)
        if os.path.exists(path):
            self.stats["had"] += 1
            return True
        try:
            data = self.maptiles.layer_tile(layer, z, x, y)
        except imagerymod.ImageryError as e:    # an unreachable source: the map asks again later
            log.debug("map tile %s: %s", key, e)
            self.stats["failed"] += 1
            return False
        if data is None:
            self.stats["missing"] += 1
            return False
        # a temporary name of this thread's own: two writers of the same tile renamed each other's file away
        # (blank map squares, Paris 2026-10-15)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            staging.replace_cached(tmp, path)
        except OSError as e:
            log.debug("map tile %s: %s", key, e)
        ok = os.path.exists(path)
        if ok:
            self.stats["built"] += 1
            self._since_prune += 1
        return ok

    def close(self):
        with self._lock:
            self._want = []
        self.pool.shutdown(wait=False)
