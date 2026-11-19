"""Mapillary traffic signs for the area being streamed. The access token stays on this computer."""
from functools import lru_cache
import getpass
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlencode

DATA = Path(__file__).with_name("data") / "pl_signs"
log = logging.getLogger("earthd.plmapillary")
_locks = [threading.Lock() for _ in range(31)]
_retry_after = 0.


def token_path():
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) if os.name == "nt" else Path.home() / ".config"
    return root / "beamng-earth" / "mapillary.token"


@lru_cache(maxsize=1)
def token():
    if value := os.environ.get("MAPILLARY_ACCESS_TOKEN", "").strip():
        return value
    try:
        return token_path().read_text(encoding="utf8").strip()
    except OSError:
        return ""


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(data)
        except BaseException:
            f.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def configure():
    value = getpass.getpass("Wklej token dostepu Mapillary i nacisnij Enter: ").strip()
    if not value or any(ch.isspace() for ch in value):
        raise SystemExit("Nie zapisano tokenu. Potrzebny jest token dostepu klienta Mapillary.")
    atomic_write(token_path(), value.encode("utf8"))
    token.cache_clear()
    print("Token zapisany dla tego konta. Uruchom ponownie BeamNG.Earth.")


@lru_cache(maxsize=1)
def classes():
    return json.loads((DATA / "mapillary-classes.json").read_bytes())


def mapillary_code(value):
    speed = re.fullmatch(r"regulatory--maximum-speed-limit-(\d+)--g1", str(value))
    return "B-33-" + speed[1] if speed else classes().get(value)


def read_pages(bounds, read, depth=0):
    args = {"bbox": ",".join(f"{v:.8f}" for v in bounds), "fields": "id,geometry,object_type,object_value",
            "object_values": "regulatory--*,warning--*,information--*,complementary--*", "limit": 1000}
    seen = set()
    while True:
        page = read("https://graph.mapillary.com/map_features?" + urlencode(args))
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ValueError("Invalid Mapillary response")
        for f in page["data"]:
            if isinstance(f, dict) and f.get("object_type") in ("trafficsign", "traffic_sign"):
                yield f
        paging = page.get("paging") or {}
        if not isinstance(paging, dict):
            raise ValueError("Invalid Mapillary paging")
        if not paging.get("next"):
            if not seen and len(page["data"]) >= args["limit"]:
                if depth >= 6:
                    raise ValueError("Mapillary truncated this area")
                west, south, east, north = bounds
                midx, midy = (west + east) / 2, (south + north) / 2
                for left, right in ((west, midx), (midx, east)):
                    for bottom, top in ((south, midy), (midy, north)):
                        yield from read_pages((left, bottom, right, top), read, depth + 1)
            return
        cursor = paging.get("cursors", {}).get("after")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValueError("Invalid Mapillary page cursor")
        seen.add(cursor)
        args["after"] = cursor


def epoch():
    return time.strftime("%Y-%m", time.gmtime())


def fetch_cell(key, cache_dir, fetcher):
    global _retry_after
    import revisit
    path = Path(cache_dir) / "pl-mapillary-v1" / str(key.z) / str(key.x) / f"{key.y}.json"
    month = epoch()
    with _locks[hash(key) % len(_locks)]:
        old = []
        try:
            stored = json.loads(path.read_bytes())
            rows = stored["rows"]
            if not isinstance(rows, list) or not all(isinstance(row, list) and len(row) == 4 and
                    isinstance(row[0], str) and isinstance(row[3], str) and
                    all(isinstance(v, (int, float)) and math.isfinite(v) for v in row[1:3]) for row in rows):
                raise ValueError("Invalid cached rows")
            old = rows
            if stored["epoch"] == month:
                return old
        except (OSError, KeyError, TypeError, ValueError):
            pass
        if fetcher.offline_flag or time.monotonic() < _retry_after:
            revisit.fault(here=True)
            return old

        def read(url):
            result = fetcher.get(url, headers={"Authorization": "OAuth " + token()}, max_bytes=4*1024*1024)
            return json.loads(result.body)

        try:
            rows = {}
            for f in read_pages(key.bounds(), read):
                geom = f.get("geometry") or {}
                if not isinstance(geom, dict):
                    continue
                value = f.get("object_value")
                coords = geom.get("coordinates")
                if not isinstance(value, str) or not f.get("id") or geom.get("type") != "Point" or not isinstance(coords, list) or len(coords) < 2:
                    continue
                lon, lat = coords[:2]
                if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lon, lat)):
                    continue
                ident = str(f["id"])
                rows[ident] = [ident, lon, lat, value]
            result = list(rows.values())
            atomic_write(path, json.dumps({"epoch": month, "rows": result}, separators=(",", ":")).encode())
            return result
        except (OSError, ValueError) as exc:
            revisit.fault(here=True)
            delay = max(30., float(getattr(exc, "retry_after", 0.) or 0.))
            _retry_after = time.monotonic() + delay
            log.warning("Mapillary unavailable for %s (HTTP %s); using saved signs", key, getattr(exc, "status", None))
            return old
