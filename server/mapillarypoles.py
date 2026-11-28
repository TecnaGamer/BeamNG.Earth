"""Read triangulated utility poles from Mapillary's zoom-14 point-feature tiles."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import hashlib
import struct
import tempfile
import time
import os
import json
import re
from pathlib import Path

import mapcells
import netfetch
import revisit


def token_path():
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) if os.name == "nt" else Path.home() / ".config"
    return root / "beamng-earth" / "mapillary.token"


@lru_cache(maxsize=1)
def token():
    user = os.environ.get("BNGE_USER_DIR")
    if user:
        try:
            value = json.loads((Path(user) / "settings" / "beamngearth-mapillary.json").read_bytes())["token"]
            return value if isinstance(value, str) and re.fullmatch(r"[!-~]{1,4096}", value) else ""
        except FileNotFoundError:
            pass  # Existing installations may have configured the launcher or environment.
        except (OSError, ValueError, TypeError, KeyError):
            return ""
    if value := os.environ.get("MAPILLARY_ACCESS_TOKEN", "").strip():
        return value
    try:
        return token_path().read_text(encoding="utf8").strip()
    except OSError:
        return ""


def enabled():
    return bool(token())


def _tile(cell):
    import mapbox_vector_tile
    from google.protobuf.message import DecodeError
    url = f"https://tiles.mapillary.com/maps/vtp/mly_map_feature_point/2/{cell.z}/{cell.x}/{cell.y}"
    headers = {"Authorization": "OAuth " + token()}
    response = netfetch.default_fetcher().get(url, headers=headers, max_bytes=4 << 20)
    if not response.body:
        return ()
    try:
        layers = mapbox_vector_tile.decode(response.body, default_options={"y_coord_down": True})
    except (DecodeError, ValueError) as error:
        raise OSError("Unreadable Mapillary vector tile") from error
    n = 1 << cell.z
    points = []
    for layer in layers.values():
        extent = layer["extent"]
        for feature in layer["features"]:
            geom = feature["geometry"]
            if geom["type"] == "Point" and feature["properties"].get("value") == "object--support--utility-pole":
                x, y = geom["coordinates"]
                points.append((mapcells._lon_of(cell.x + x / extent, n), mapcells._lat_of(cell.y + y / extent, n)))
    return tuple(points)


# Pin the inputs of nested content and skin builds together. Disk probes on the
# server thread only inspect the cache; a cache miss is fetched by a road worker.
_job = ContextVar("mapillary_pole_cells", default=None)
MAX_AGE = 7 * 86400
COORD = struct.Struct("<dd")


@contextmanager
def build_inputs():
    if _job.get() is not None:
        yield
        return
    reset = _job.set({})
    try:
        yield
    finally:
        _job.reset(reset)


def _cell_points(cell, fetch):
    root = netfetch.SHARED_DIR
    path = Path(root) / "mapillary-poles-v1" / str(cell.x) / f"{cell.y}.bin" if root else None
    if path:
        try:
            if time.time() - path.stat().st_mtime < MAX_AGE:
                return tuple(COORD.iter_unpack(path.read_bytes()))
        except (OSError, struct.error):
            pass
    if not fetch:
        return None
    try:
        result = tuple(sorted(set(_tile(cell))))
    except OSError:
        return None
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
                name = Path(f.name)
                try:
                    f.write(b"".join(COORD.pack(*point) for point in result))
                except OSError:
                    f.close()
                    name.unlink(missing_ok=True)
                    raise
            try:
                os.replace(name, path)
            finally:
                name.unlink(missing_ok=True)
        except OSError:
            pass  # The job still holds the exact observations used for this tile.
    return result


def observations(extent, *, fetch):
    south, west, north, east = extent
    positions = set()
    complete = True
    pinned = _job.get()
    for cell in mapcells.cells_for_bbox(*extent, z=14):
        if pinned is not None:
            if cell not in pinned:
                pinned[cell] = _cell_points(cell, fetch)
            rows = pinned[cell]
        else:
            rows = _cell_points(cell, fetch)
        if rows is None:
            complete = False
        else:
            positions.update((lon, lat) for lon, lat in rows if west <= lon <= east and south <= lat <= north)
    return sorted(positions), complete


def signature(extent):
    rows, complete = observations(extent, fetch=_job.get() is not None)
    digest = hashlib.sha256()
    for point in rows:
        digest.update(COORD.pack(*point))
    return ("mapillary-poles-v2", complete, digest.hexdigest())


def points(extent):
    rows, complete = observations(extent, fetch=True)
    if not complete:
        revisit.fault()
    return rows
