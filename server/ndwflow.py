"""Detector flow from the NDW open feed (speeds and intensities), cached for the process.

A road whose midpoint is near a detector takes that detector's vehicle flow as a tighter car spacing.
No detector, or a failed download, leaves the spacing alone. This is the open measurement feed, not
the TomTom intensity product.

    weight_of(2400)          # above 1: a high count
    apply(paths, frame)      # sets path["spacing"] only where a detector raises the weight
"""
import gzip
import math
import re
import urllib.request

import netfetch

SITES_URL = "https://opendata.ndw.nu/measurement.xml.gz"
FLOW_URL = "https://opendata.ndw.nu/snelheden_en_intensiteiten_meetgegevens.xml.gz"
NL = (50.75, 3.2, 53.7, 7.3)
REACH_M = 150.0
BASE_SPACING = 180.0
QUIET = 400.0                       # vehicles per hour at which the weight is still 1
TIMEOUT_S = 60
_SITES = None
_ID = re.compile(r'\bid="([^"]+)"')
_LAT = re.compile(r"<latitude>([0-9.]+)</latitude>\s*<longitude>([0-9.]+)</longitude>")
_FLOW = re.compile(r"<com:vehicleFlowRate>([0-9.]+)</com:vehicleFlowRate>")


def weight_of(flow):
    """1 at a quiet count, higher as the count rises, at most 3. Pure."""
    return max(1.0, min(3.0, float(flow) / QUIET))


def sites_from_record(text):
    """(id, lat, lon) from one measurement-site record, or None. Pure."""
    found = _ID.search(text or "")
    loc = _LAT.search(text or "")
    if not found or not loc:
        return None
    return found.group(1), float(loc.group(1)), float(loc.group(2))


def flow_from_record(text):
    """(id, vehicles per hour) from one site-measurement record, or None. Lanes and classes are summed. Pure."""
    found = _ID.search(text or "")
    rates = [float(v) for v in _FLOW.findall(text or "")]
    if not found or not rates:
        return None
    return found.group(1), sum(rates)


def join(sites, flows):
    """[(lat, lon, flow)] for sites that published a positive flow. Pure."""
    by_id = {sid: flow for sid, flow in flows if flow > 0}
    return [(lat, lon, by_id[sid]) for sid, lat, lon in sites if sid in by_id]


def nearest(sites, lat, lon, reach=REACH_M):
    """The flow of the closest detector within reach metres, or None. Pure."""
    best, best_d = None, float(reach)
    scale = 111320.0 * math.cos(math.radians(lat))
    for slat, slon, flow in sites or ():
        dist = math.hypot((slat - lat) * 111320.0, (slon - lon) * scale)
        if dist < best_d:
            best, best_d = flow, dist
    return best


def apply(paths, frame, sites=None):
    """Paths, with a shorter spacing where a detector's flow raises the weight. Does not raise.

    An empty site list leaves every path alone. Omitting sites loads the feed once, and only for a
    frame in the Netherlands."""
    if not paths:
        return paths
    try:
        if sites is None:
            lat0, lon0 = getattr(frame, "lat0", None), getattr(frame, "lon0", None)
            if lat0 is None or not (NL[0] <= float(lat0) <= NL[2] and NL[1] <= float(lon0) <= NL[3]):
                return paths
            sites = load()
        for path in paths:
            pts = path.get("points") or ()
            if len(pts) < 2:
                continue
            mid = pts[len(pts) // 2]
            lat, lon, _ = frame.enu_to_geodetic(float(mid[0]), float(mid[1]), 0.0)
            flow = nearest(sites, float(lat), float(lon))
            if flow is None:
                continue
            weight = weight_of(flow)
            if weight > 1.05:
                path["spacing"] = max(40.0, BASE_SPACING / weight)
    except Exception:
        return paths
    return paths


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": netfetch.USER_AGENT})
    return urllib.request.urlopen(req, timeout=TIMEOUT_S, context=netfetch.windows_ssl_context())


def _records(url, marker, fetch=None):
    getter = fetch or _fetch
    resp = getter(url)
    try:
        gz = gzip.GzipFile(fileobj=resp)
        buf = ""
        while True:
            chunk = gz.read(1 << 20)
            if not chunk:
                if buf:
                    yield buf
                break
            buf += chunk.decode("utf-8", "replace")
            parts = buf.split(marker)
            buf = parts[-1]
            if len(buf) > 2_000_000:
                buf = buf[-2_000_000:]
            for part in parts[:-1]:
                yield part
    finally:
        close = getattr(resp, "close", None)
        if close:
            close()


def load(fetch=None):
    """The cached (lat, lon, flow) rows. [] when the feed cannot be read, and that failure is remembered."""
    global _SITES
    if _SITES is None:
        try:
            sites = [row for part in _records(SITES_URL, "<measurementSiteRecord ", fetch)
                     if (row := sites_from_record(part))]
            flows = [row for part in _records(FLOW_URL, "<roa:siteMeasurements>", fetch)
                     if (row := flow_from_record(part))]
            _SITES = join(sites, flows)
        except Exception:
            _SITES = []
    return _SITES
