"""Bus lines from Västtrafik's own timetable data, not Overpass (the user, 2026-11-17: "yes do the västtrafik data", after
every public Overpass server was overloaded for an hour and the Bus lines window had nothing to show).

The data is Trafiklab's GTFS Regional feed for Västtrafik, operator "vt" (https://trafiklab.se/api/gtfs-datasets/gtfs-
regional/): "available under the CC0 1.0 Universal (CC0 1.0) Public Domain Dedication license", updated daily. Its
download needs a free Trafiklab API key (Bronze: 50 calls a month); with none, the same feed's daily copy in the
Mobility Database (feed mdb-3248, "GTFS Regional Västtrafik", official) is read instead. Either is fetched at most once
every REFRESH_S and made into a small index on disk (cache_dir/transit/vt.json.gz); the zip (92 MB, 2026-11-17) is not
kept.

A line is a route of route_type 700 (bus service; 1501, the communal taxis, and trams, trains and boats are left out)
in one direction, as most of its trips run it: its stops in order. Each stop stands on the trip's own path (shapes.txt
at the stop's shape_dist_traveled), the carriageway the bus drives, not the platform beside it: the GPS's route then
needs no turn to reach a stop on the other side. A stop whose place on the path is more than SNAP_MAX_M from the stop
itself keeps the stop's own place (beside the road).

The key: the environment variable TRAFIKLAB_KEY or the file cache_dir/trafiklab.key (one line). It is never logged:
messages that could hold the URL are passed through _scrub. (Not a BNE_* variable: those enter the tile caches'
code_key, revisit.keyed_env.)
"""
import bisect
import csv
import gzip
import io
import json
import logging
import math
import os
import tempfile
import threading
import time
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

TRAFIKLAB_URL = "https://opendata.samtrafiken.se/gtfs/vt/vt.zip?key={key}"
MIRROR_URL = "https://files.mobilitydatabase.org/mdb-3248/latest.zip"
# Västtrafik's stops, measured in the feed of 2026-11-16 (27,227 stops): lat 57.104-59.394, lon 11.008-15.224
AREA = (57.05, 10.95, 59.45, 15.30)
REFRESH_S = 7 * 86400           # lines and stops change seldom; Bronze's 50 calls a month stay untouched
RETRY_S = 3600                  # a failed download: not again before this
INDEX_VERSION = 1
BUS_TYPES = {"3", "700"}        # (GTFS's bus, and the extended types' bus service; 1501 communal taxi is not a line)
SNAP_MAX_M = 40.0
ATTRIBUTION = "Bus lines: Västtrafik (Trafiklab GTFS Regional, CC0)"

_lock = threading.Lock()
_state = {"index": None, "loaded": None, "building": False, "failed_at": 0.0, "error": None}


class Preparing(Exception):
    """The index is being made (downloading, reading): no lines from here yet."""


def covers(lat, lon):
    return AREA[0] <= lat <= AREA[2] and AREA[1] <= lon <= AREA[3]


def api_key(cache_dir):
    """The Trafiklab key (TRAFIKLAB_KEY, else cache_dir/trafiklab.key), or None."""
    k = os.environ.get("TRAFIKLAB_KEY", "").strip()
    if not k and cache_dir is not None:
        try:
            k = (Path(cache_dir) / "trafiklab.key").read_text(encoding="utf-8").strip()
        except OSError:
            k = ""
    return k or None


def _scrub(text, key):
    return str(text).replace(key, "<key>") if key else str(text)


def index_path(cache_dir):
    return Path(cache_dir) / "transit" / "vt.json.gz"


def _metres(lat0, lon0, lat1, lon1):
    k = 111_320.0
    return math.hypot((lat1 - lat0) * k, (lon1 - lon0) * k * math.cos(math.radians(lat0)))


def _rows(z, name):
    with z.open(name) as fh:
        r = csv.reader(io.TextIOWrapper(fh, "utf-8-sig"))
        head = next(r)
        yield {h: i for i, h in enumerate(head)}
        yield from r


def build_index(zip_path):
    """The index of a GTFS zip: {"version", "feed", "lines": [{"id", "ref", "from", "to", "operator", "stops": [[name,
    lat, lon, on_road]...]}]}. Pure but for reading the file; streams stop_times.txt and shapes.txt (230 and 277 MB
    in Västtrafik's), whose rows come trip by trip and shape by shape."""
    z = zipfile.ZipFile(zip_path)
    rows = _rows(z, "agency.txt")
    c = next(rows)
    agency = {r[c["agency_id"]]: r[c["agency_name"]] for r in rows}
    rows = _rows(z, "routes.txt")
    c = next(rows)
    routes = {r[c["route_id"]]: (r[c["route_short_name"]], agency.get(r[c["agency_id"]], ""))
              for r in rows if r[c["route_type"]] in BUS_TYPES}
    rows = _rows(z, "trips.txt")
    c = next(rows)
    trips = {r[c["trip_id"]]: (r[c["route_id"]], r[c["direction_id"]], r[c["shape_id"]]) for r in rows
             if r[c["route_id"]] in routes}
    rows = _rows(z, "stops.txt")
    c = next(rows)
    stops = {r[c["stop_id"]]: (r[c["stop_name"]], float(r[c["stop_lat"]]), float(r[c["stop_lon"]])) for r in rows}
    # each trip's stops: how many trips run each (route, direction, stops) pattern, one trip's path and distances
    count, sample = {}, {}
    rows = _rows(z, "stop_times.txt")
    c = next(rows)
    ti, si, hi, di, qi = c["trip_id"], c["stop_id"], c["stop_headsign"], c["shape_dist_traveled"], c["stop_sequence"]

    def close(trip, seq):
        if trip not in trips or len(seq) < 2:
            return
        seq.sort()
        rid, d, shape = trips[trip]
        key = (rid, d, tuple(s for _, s, _, _ in seq))
        count[key] = count.get(key, 0) + 1
        if key not in sample:
            sample[key] = (shape, [x for _, _, x, _ in seq], seq[0][3])
    trip, seq = None, []
    for r in rows:
        if r[ti] != trip:
            close(trip, seq)
            trip, seq = r[ti], []
        if trip in trips:
            try:
                dist = float(r[di]) if r[di] else None
            except ValueError:
                dist = None
            seq.append((int(r[qi]), r[si], dist, r[hi]))
    close(trip, seq)
    # the pattern most trips run, for each route and direction
    best = {}
    for key, n in count.items():
        k = key[:2]
        if k not in best or n > count[best[k]]:
            best[k] = key
    need = {sample[key][0] for key in best.values() if sample[key][0]}
    shapes = {}
    rows = _rows(z, "shapes.txt")
    c = next(rows)
    for r in rows:
        sid = r[c["shape_id"]]
        if sid in need:
            try:
                shapes.setdefault(sid, []).append((int(r[c["shape_pt_sequence"]]), float(r[c["shape_dist_traveled"]]),
                                                   float(r[c["shape_pt_lat"]]), float(r[c["shape_pt_lon"]])))
            except ValueError:
                continue
    for pts in shapes.values():
        pts.sort()
    lines = []
    for (rid, d), key in sorted(best.items()):
        shape, dists, headsign = sample[key]
        path = shapes.get(shape) or []
        along = [p[1] for p in path]
        out = []
        for sid, dist in zip(key[2], dists):
            name, la, lo = stops.get(sid, ("", None, None))
            if la is None:
                continue
            on_road, at = False, (la, lo)
            if path and dist is not None:
                q = _on_path(path, along, dist)
                if q is not None and _metres(la, lo, *q) <= SNAP_MAX_M:
                    on_road, at = True, q
            if out and out[-1][0] == name and name:
                continue        # (a trip at two platforms of one stop in a row, a terminus's: 11 lines, 7-54 m apart)
            out.append([name, round(at[0], 6), round(at[1], 6), on_road])
        if len(out) < 2:
            continue
        ref, op = routes[rid]
        lines.append({"id": f"vt{rid}-{d}", "ref": ref, "from": out[0][0], "to": headsign or out[-1][0],
                      "operator": op, "stops": out})
    feed = ""
    try:
        rows = _rows(z, "feed_info.txt")
        c = next(rows)
        feed = next(rows)[c["feed_version"]]
    except (KeyError, StopIteration):
        pass
    return {"version": INDEX_VERSION, "feed": feed, "lines": lines}


def _on_path(path, along, dist):
    """(lat, lon) at dist along path [(seq, dist, lat, lon)] (along: its dists), None off its ends."""
    if not path or dist < along[0] - 1.0 or dist > along[-1] + 1.0:
        return None
    i = bisect.bisect_left(along, dist)
    if i <= 0:
        return path[0][2], path[0][3]
    if i >= len(path):
        return path[-1][2], path[-1][3]
    a, b = path[i - 1], path[i]
    t = 0.0 if b[1] <= a[1] else (dist - a[1]) / (b[1] - a[1])
    return a[2] + (b[2] - a[2]) * t, a[3] + (b[3] - a[3]) * t


def _load(cache_dir):
    p = index_path(cache_dir)
    try:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("version") == INDEX_VERSION and isinstance(data.get("lines"), list):
            return data, p.stat().st_mtime
    except (OSError, ValueError):
        pass
    return None, None


def _write(cache_dir, data):
    p = index_path(cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".vt-")
    try:
        with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def refresh(cache_dir, net):
    """Download the feed (Trafiklab with a key, else the Mobility Database's copy), make the index, keep it. Blocking.
    Returns the index; raises with the key scrubbed from the message."""
    key = api_key(cache_dir)
    url = TRAFIKLAB_URL.format(key=key) if key else MIRROR_URL
    t0 = time.time()
    try:
        body = net.get(url, kind="demand").body
    except Exception as e:      # (its message holds the URL: the key out of it)
        raise RuntimeError(_scrub(f"{type(e).__name__}: {e}", key)) from None
    folder = index_path(cache_dir).parent
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".vt-", suffix=".zip")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        del body
        data = build_index(tmp)
    finally:
        os.unlink(tmp)
    _write(cache_dir, data)
    log.info("bus lines: Västtrafik's feed %s from %s: %d lines in %.0f s", data["feed"],
             "Trafiklab" if key else "the Mobility Database", len(data["lines"]), time.time() - t0)
    return data


def _build_in_background(cache_dir, net):
    try:
        data = refresh(cache_dir, net)
        with _lock:
            _state.update(index=data, loaded=time.time(), error=None)
    except Exception as e:
        log.warning("bus lines: Västtrafik's feed: %s", _scrub(e, api_key(cache_dir)))
        with _lock:
            _state.update(failed_at=time.time(), error=_scrub(e, api_key(cache_dir)))
    finally:
        with _lock:
            _state["building"] = False


def index(cache_dir, net):
    """The index, refreshing it when older than REFRESH_S (in a thread: the old one meanwhile). Raises Preparing when
    there is none yet (its download started), RuntimeError when it cannot be had."""
    with _lock:
        if _state["index"] is None:
            data, mtime = _load(cache_dir)
            if data is not None:
                _state.update(index=data, loaded=mtime)
        data, loaded = _state["index"], _state["loaded"]
        stale = data is None or time.time() - (loaded or 0) > REFRESH_S
        start = stale and not _state["building"] and time.time() - _state["failed_at"] > RETRY_S
        if start:
            _state["building"] = True
        error = _state["error"]
    if start:
        threading.Thread(target=_build_in_background, args=(cache_dir, net), name="vt-feed", daemon=True).start()
    if data is None:
        if error and not _state["building"]:
            raise RuntimeError(error)
        raise Preparing("Västtrafik's lines are downloading (92 MB, once a week)")
    return data


def near(lat, lon, data, radius_m, max_lines, ref_key, colour="#ff6600"):
    """The lines of index data with a stop within radius_m of lat, lon, in buslines.parse's form, nearest stop first
    per line, at most max_lines (ordered by ref_key, then distance). colour: every line's (the feed has none)."""
    out, seen = [], set()
    cos = math.cos(math.radians(lat))
    for ln in data["lines"]:
        st = ln["stops"]
        best, bi = None, 0
        for i, s in enumerate(st):
            d = math.hypot((s[1] - lat) * 111_320.0, (s[2] - lon) * 111_320.0 * cos)
            if best is None or d < best:
                best, bi = d, i
        if best is None or best > radius_m:
            continue
        key = (ln["ref"], tuple(s[0] for s in st))
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": ln["id"], "ref": ln["ref"], "name": f'{ln["ref"]} {ln["from"]} - {ln["to"]}',
                    "from": ln["from"], "to": ln["to"], "colour": colour, "operator": ln["operator"],
                    "stops": [{"name": s[0], "lat": s[1], "lon": s[2], "on_road": bool(s[3])} for s in st],
                    "near_m": round(best, 1), "near_i": bi, "source": "vt"})
    out.sort(key=lambda l: (ref_key(l["ref"]), l["near_m"]))
    return out[:max_lines]
