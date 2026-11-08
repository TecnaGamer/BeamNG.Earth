"""RDW garage and lot polygons (Open Data Parkeren, CC0): the published capacity and charge points.

The geometry is PARKEERGEBIED's area polygons (nsk3-v9n7) and the counts are SPECIFICATIES
PARKEERGEBIED (b3us-f26s). No live occupancy. Outside the Netherlands, and when the feed fails,
there is no record and the garage stays as it was.

    info = lookup(lat, lon)     # {"capacity", "chargers", "areaid"} or None
"""
import json
import urllib.parse
import urllib.request

import netfetch

GEOM = "https://opendata.rdw.nl/resource/nsk3-v9n7.json"
SPEC = "https://opendata.rdw.nl/resource/b3us-f26s.json"
NL = (50.75, 3.2, 53.7, 7.3)
TIMEOUT_S = 20
STALL_M2 = 28.0
_INDEX = None


def in_nl(lat, lon):
    return lat is not None and lon is not None and NL[0] <= float(lat) <= NL[2] and NL[1] <= float(lon) <= NL[3]


def parse_wkt(text):
    """The outer ring [(lon, lat), ...] of a WKT polygon, or None. Pure."""
    raw = str(text or "").strip()
    if not raw.upper().startswith("POLYGON"):
        return None
    body = raw[raw.find("("):].strip()
    if body.startswith("(("):
        body = body[1:]
    body = body.split(")")[0].strip("()")
    ring = []
    for part in body.split(","):
        bits = part.split()
        if len(bits) >= 2:
            ring.append((float(bits[0]), float(bits[1])))
    return ring if len(ring) >= 4 else None


def contains(ring, lon, lat):
    """Whether (lon, lat) is inside a simple ring. Pure."""
    inside = False
    j = len(ring) - 1
    for i, (x, y) in enumerate(ring):
        xj, yj = ring[j]
        if (y > lat) != (yj > lat) and lon < (xj - x) * (lat - y) / ((yj - y) or 1e-18) + x:
            inside = not inside
        j = i
    return inside


def row_of(areaid, manager, wkt, end=None):
    """One index row, or None when the polygon has ended or does not parse. Pure."""
    if end and str(end)[:10] < "2026-01-01":
        return None
    ring = parse_wkt(wkt)
    if not ring:
        return None
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return {"areaid": str(areaid), "manager": str(manager), "ring": ring,
            "bbox": (min(lons), min(lats), max(lons), max(lats))}


def find(lat, lon, rows):
    """The row whose polygon contains the point, or None. Pure."""
    for row in rows or ():
        box = row["bbox"]
        if box[0] <= lon <= box[2] and box[1] <= lat <= box[3] and contains(row["ring"], lon, lat):
            return row
    return None


def counts_of(spec):
    """{"capacity", "chargers"} from a specificaties row. Pure."""
    try:
        capacity = int(float(spec.get("capacity") or 0))
        chargers = int(float(spec.get("chargingpointcapacity") or 0))
    except (TypeError, ValueError):
        return None
    if capacity <= 0 and chargers <= 0:
        return None
    return {"capacity": capacity, "chargers": chargers}


def floors_for(capacity, area):
    """Storeys that hold `capacity` stalls on a footprint of `area` square metres. Pure."""
    per = max(float(area) / STALL_M2, 8.0)
    return max(1, min(8, int(round(float(capacity) / per))))


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": netfetch.USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=netfetch.windows_ssl_context()) as resp:
        return resp.read()


def _index(fetch=None):
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    getter = fetch or _fetch
    rows, offset = [], 0
    where = urllib.parse.quote("starts_with(areageometryastext,'POLYGON')")
    try:
        while offset < 20000:
            url = (f"{GEOM}?$select=areaid,areamanagerid,enddatearea,areageometryastext"
                   f"&$where={where}&$limit=5000&$offset={offset}")
            page = json.loads(getter(url))
            if not page:
                break
            for item in page:
                row = row_of(item.get("areaid"), item.get("areamanagerid"), item.get("areageometryastext"),
                             item.get("enddatearea"))
                if row:
                    rows.append(row)
            if len(page) < 5000:
                break
            offset += 5000
    except Exception:
        rows = []
    _INDEX = rows
    return _INDEX


def _counts(areaid, manager, fetch=None):
    url = (f"{SPEC}?areaid={urllib.parse.quote(str(areaid))}"
           f"&areamanagerid={urllib.parse.quote(str(manager))}"
           "&$order=startdatespecifications%20DESC&$limit=1")
    try:
        page = json.loads((fetch or _fetch)(url))
    except Exception:
        return None
    return counts_of(page[0]) if page else None


def lookup(lat, lon, rows=None, fetch=None):
    """The published capacity and chargers of the polygon under the point, or None. Does not raise."""
    if not in_nl(lat, lon):
        return None
    try:
        hit = find(float(lat), float(lon), rows if rows is not None else _index(fetch))
        if hit is None:
            return None
        counts = _counts(hit["areaid"], hit["manager"], fetch) if rows is None else hit.get("counts")
    except Exception:
        return None
    if not counts:
        return None
    return {"capacity": counts["capacity"], "chargers": counts["chargers"], "areaid": hit["areaid"]}
