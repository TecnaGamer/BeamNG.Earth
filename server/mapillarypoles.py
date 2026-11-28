"""Read triangulated utility poles from Mapillary's zoom-14 point-feature tiles."""
from functools import lru_cache
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


@lru_cache(maxsize=256)
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


def points(extent):
    south, west, north, east = extent
    positions = set()
    for cell in mapcells.cells_for_bbox(*extent, z=14):
        try:
            positions.update((lon, lat) for lon, lat in _tile(cell) if west <= lon <= east and south <= lat <= north)
        except OSError:
            revisit.fault()
            break
    return sorted(positions)
