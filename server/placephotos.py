"""Place photos for the modding API (api.placePhotos, 1.11): a photo of each place an add-on shows, as a JPEG file in
the game's temp folder, with who took it and under what licence.

The game's UI may not fetch anything (its browser's policy allows only connections to the computer itself, and
cancels even those: maptiles.py), so the server looks the photo up, downloads it, shrinks it and writes it where the
UI can show it by path (/temp/beamngEarth/photos/<key>.jpg), as it does the world map's tiles. Two sources, both
CC BY-SA, both credited in the answer:

  * Wikimedia Commons: a file within COMMONS_M of the place whose title names it (half the name's words, one of
    them more than a kind of place: "church" alone never matches). Most landmarks, churches, parks, museums.
  * Mapillary street-level photos, with the player's own client token (read only; an add-on passes it, it goes in
    the request's Authorization header and is never written anywhere): the photo taken from the road MLY_NEAR_M -
    MLY_FAR_M from the storefront, best MLY_BEST_AIM_DEG off the camera's heading (straight ahead the shop is far
    down the street and small) at MLY_BEST_M, the newer the better; no panoramas. Phones' positions are often
    5-10 m off, so it is the right block more often than the exact door: fx (where across the picture the place
    should be) lets the add-on lean its crop that way.

Every request goes through netfetch (its per-host limits, retries and breakers). An answer, a photo or none, is
kept on disk (<root>/photos/<key>.json next to the .jpg) for KEEP_S / NONE_S, at most KEEP_FILES files.
"""
import hashlib
import io
import json
import logging
import math
import os
import re
import time
import unicodedata
import urllib.parse

log = logging.getLogger("earthd.placephotos")

PHOTOS_MAX = 8              # places per request
ID_MAX = 64
NAME_MAX = 200
COMMONS_M = 120
MLY_BOX_M = 55              # Mapillary's search box half-size round the place
MLY_NEAR_M, MLY_FAR_M = 6.0, 48.0
MLY_BEST_M = 18.0
MLY_AIM_DEG = 38.0          # the place within this of the camera's heading (a phone's or dashcam's 60-70 degree lens)
MLY_BEST_AIM_DEG = 22.0
LENS_DEG = 70.0
ZOOM = 1.3                  # how much an add-on may enlarge a Mapillary photo round fx (no more: positions are rough)
MAX_W, MAX_H = 640, 420     # the file's size (the card shows it ~340 px wide)
JPEG_QUALITY = 82
KEEP_S = 30 * 86400         # a photo is looked up again after this
NONE_S = 7 * 86400          # "no photo" is asked again after this (or at once when a token came since)
KEEP_FILES = 800            # .jpg files kept (the oldest go first)
VFS_DIR = "/temp/beamngEarth/photos/"
TOKEN_RE = re.compile(r"^MLY\|[0-9]+\|[0-9a-fA-F]+$")
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
MLY_API = "https://graph.mapillary.com/images"
IMG_HOSTS = re.compile(r"^(upload\.wikimedia\.org|thumb\.wikimedia\.org|[a-z0-9.-]+\.fbcdn\.net|[a-z0-9.-]+\.mapillary\.com)$")

R = 6371008.8


def request(msg):
    """The message {"places": [{"id", "lat", "lon", "name"}], "mapillaryToken"} -> ([(id, lat, lon, name)], token),
    None or (None, why). Pure."""
    places = msg.get("places") if isinstance(msg, dict) else None
    if not isinstance(places, list) or not places:
        return None, "no places"
    if len(places) > PHOTOS_MAX:
        return None, f"at most {PHOTOS_MAX} places"
    out = []
    for i, p in enumerate(places, 1):
        if not isinstance(p, dict):
            return None, f"place {i} needs id, lat and lon"
        pid, lat, lon = p.get("id"), p.get("lat"), p.get("lon")
        if not isinstance(pid, str) or not pid or len(pid) > ID_MAX:
            return None, f"place {i} needs id, lat and lon"
        if isinstance(lat, bool) or isinstance(lon, bool) or not isinstance(lat, (int, float)) \
                or not isinstance(lon, (int, float)) or not (-90 <= lat <= 90 and -180 <= lon <= 180) \
                or math.isnan(lat) or math.isnan(lon):
            return None, f"place {i} needs id, lat and lon"
        name = p.get("name") if isinstance(p.get("name"), str) else ""
        out.append((pid, float(lat), float(lon), name[:NAME_MAX]))
    tok = msg.get("mapillaryToken")
    tok = tok.strip() if isinstance(tok, str) and len(tok) < 120 and TOKEN_RE.match(tok.strip()) else None
    return out, tok


# --- geometry (pure) --------------------------------------------------------------------------------------------
def enu(lat0, lon0, lat, lon):
    """Metres east, north of (lat0, lon0) (equirectangular: exact enough within a street)."""
    return (math.radians(lon - lon0) * R * math.cos(math.radians(lat0)), math.radians(lat - lat0) * R)


def bearing(lat0, lon0, lat, lon):
    e, n = enu(lat0, lon0, lat, lon)
    return (math.degrees(math.atan2(e, n)) + 360.0) % 360.0


def bbox(lat, lon, m):
    dlat = math.degrees(m / R)
    dlon = dlat / math.cos(math.radians(lat))
    return ",".join(f"{v:.6f}" for v in (lon - dlon, lat - dlat, lon + dlon, lat + dlat))


def across(aim_deg):
    """Where across a picture (0 left, 1 right) something aim_deg right of the camera's heading is."""
    f = 0.5 + math.tan(math.radians(max(-89.0, min(89.0, aim_deg)))) / (2 * math.tan(math.radians(LENS_DEG / 2)))
    return max(0.0, min(1.0, f))


def pick_mapillary(rows, lat, lon, now_ms):
    """The Graph API /images row that shows the place at (lat, lon) best, as (row, aim degrees), or None. Pure."""
    best, best_s = None, math.inf
    for im in rows or []:
        if not isinstance(im, dict) or im.get("is_pano") or not isinstance(im.get("thumb_1024_url"), str):
            continue
        g = im.get("computed_geometry") or im.get("geometry") or {}
        c = g.get("coordinates") if isinstance(g, dict) else None
        if not (isinstance(c, list) and len(c) >= 2 and all(isinstance(v, (int, float)) for v in c[:2])):
            continue
        clon, clat = c[0], c[1]
        e, n = enu(clat, clon, lat, lon)
        dist = math.hypot(e, n)
        if dist < MLY_NEAR_M or dist > MLY_FAR_M:
            continue
        heading = im.get("computed_compass_angle")
        if not isinstance(heading, (int, float)):
            heading = im.get("compass_angle")
        if not isinstance(heading, (int, float)):
            continue
        aim = (bearing(clat, clon, lat, lon) - heading + 540.0) % 360.0 - 180.0
        if abs(aim) > MLY_AIM_DEG:
            continue
        cap = im.get("captured_at")
        years = max(0.0, (now_ms - cap) / 3.156e10) if isinstance(cap, (int, float)) else 8.0
        s = abs(abs(aim) - MLY_BEST_AIM_DEG) / MLY_BEST_AIM_DEG + abs(dist - MLY_BEST_M) / 20.0 + min(years, 12.0) * 0.06
        if s < best_s:
            best, best_s = (im, aim), s
    return best


STOP = {"the", "and", "des", "les", "del", "von", "und", "jpg", "jpeg", "png", "file", "tif", "tiff", "webp", "img",
        "dsc", "photo", "view", "from"}
KIND = {"church", "chapel", "cathedral", "park", "school", "museum", "library", "hall", "centre", "center", "station",
        "hotel", "restaurant", "cafe", "bar", "house", "building", "street", "avenue", "united", "catholic", "anglican",
        "baptist", "presbyterian", "saint", "city", "store", "shop"}


def words(s):
    """The words of a name or a title worth matching: lower case, no accents, 3+ letters, no numbers. Pure."""
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return [w for w in re.split(r"[^a-z0-9]+", s) if len(w) >= 3 and w not in STOP and not w.isdigit()]


def _strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", str(s or ""))).strip()


def pick_commons(pages, name):
    """The Commons file (a query.pages value with imageinfo) whose title names the place, or None: half the name's
    words in the title, and its own words (not just the kind of place) up to two of them ("Ottawa Mission" is not
    "Ottawa City Hall"). Pure."""
    nw = words(name)
    own = [w for w in nw if w not in KIND]
    if not own or not isinstance(pages, dict):
        return None
    best, best_s = None, 0.0
    for p in pages.values():
        ii = (p.get("imageinfo") or [None])[0] if isinstance(p, dict) else None
        title = p.get("title") if isinstance(p, dict) else None
        if not isinstance(ii, dict) or not isinstance(title, str) or not re.search(r"\.(jpe?g|png|webp)$", title, re.I):
            continue
        if not isinstance(ii.get("thumburl") or ii.get("url"), str):
            continue
        tw = set(words(title))
        hit = sum(1 for w in nw if w in tw)
        if sum(1 for w in own if w in tw) < min(2, len(own)) or hit / len(nw) < 0.5:
            continue
        s = hit / len(nw) - (p.get("index") or 0) * 0.001
        if s > best_s:
            best, best_s = p, s
    return best


def commons_url(lat, lon):
    return COMMONS_API + "?" + urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "geosearch", "ggscoord": f"{lat:.6f}|{lon:.6f}",
        "ggsradius": COMMONS_M, "ggsnamespace": 6, "ggslimit": 40, "prop": "imageinfo", "iiprop": "url|extmetadata",
        "iiurlwidth": MAX_W, "iiextmetadatafilter": "Artist|LicenseShortName"})


def mapillary_url(lat, lon):
    return MLY_API + "?" + urllib.parse.urlencode({
        "fields": "id,thumb_1024_url,computed_geometry,geometry,computed_compass_angle,compass_angle,captured_at,"
                  "is_pano,creator", "limit": 100, "bbox": bbox(lat, lon, MLY_BOX_M)})


def key_of(pid):
    return hashlib.sha1(pid.encode("utf-8")).hexdigest()[:20]


class Photos:
    """The lookups over net (a netfetch.Fetcher), the files under root/photos. find() blocks (an executor's)."""

    def __init__(self, net, root, clock=time.time):
        self.net = net
        self.dir = os.path.join(root, "photos")
        self.clock = clock
        self._since_prune = 10 ** 6

    def _json(self, url, headers=None):
        return json.loads(self.net.get(url, kind="demand", headers=headers).body.decode("utf-8"))

    def _meta(self, k):
        try:
            with open(os.path.join(self.dir, k + ".json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write(self, k, meta, jpg=None):
        os.makedirs(self.dir, exist_ok=True)
        if jpg is not None:
            tmp = os.path.join(self.dir, f"{k}.jpg.{os.getpid()}.tmp")
            with open(tmp, "wb") as f:
                f.write(jpg)
            os.replace(tmp, os.path.join(self.dir, k + ".jpg"))
        tmp = os.path.join(self.dir, f"{k}.json.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, os.path.join(self.dir, k + ".json"))

    def _image(self, url):
        """The picture at url shrunk to MAX_W x MAX_H as JPEG bytes (only from the photo hosts)."""
        host = urllib.parse.urlsplit(url).hostname or ""
        if not url.startswith("https://") or not IMG_HOSTS.match(host):
            raise ValueError(f"not a photo host: {host}")
        from PIL import Image
        im = Image.open(io.BytesIO(self.net.get(url, kind="demand").body))
        im.draft("RGB", (MAX_W * 2, MAX_H * 2))
        im = im.convert("RGB")
        im.thumbnail((MAX_W, MAX_H))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue()

    def _look(self, lat, lon, name, token, now):
        """(meta, image url, failed) of the best photo, or (None, None, failed): failed = a source could not be
        asked (a "none" then is not remembered: asked again next time)."""
        failed = False
        try:
            p = pick_commons((self._json(commons_url(lat, lon)).get("query") or {}).get("pages"), name)
        except Exception as e:      # (netfetch errors, a broken answer: none from this source, for now)
            log.debug("commons: %s: %s", type(e).__name__, e)
            p, failed = None, True
        if p is not None:
            ii = p["imageinfo"][0]
            md = ii.get("extmetadata") or {}
            return ({"source": "Wikimedia Commons", "by": _strip_tags((md.get("Artist") or {}).get("value"))[:60],
                     "licence": _strip_tags((md.get("LicenseShortName") or {}).get("value")) or "see Commons",
                     "year": None, "fx": 0.5, "zoom": 1.0, "ref": p["title"][:200]}, ii.get("thumburl") or ii["url"], False)
        if not token:
            return None, None, failed
        try:
            rows = self._json(mapillary_url(lat, lon), {"Authorization": "OAuth " + token}).get("data")
        except Exception as e:
            log.debug("mapillary: %s: %s", type(e).__name__, e)
            return None, None, True
        got = pick_mapillary(rows, lat, lon, now * 1000.0)
        if got is None:
            return None, None, failed
        im, aim = got
        cap = im.get("captured_at")
        year = time.gmtime(cap / 1000.0).tm_year if isinstance(cap, (int, float)) else None
        by = (im.get("creator") or {}).get("username") if isinstance(im.get("creator"), dict) else None
        return ({"source": "Mapillary", "by": str(by or "")[:60], "licence": "CC BY-SA 4.0", "year": year,
                 "fx": round(across(aim), 3), "zoom": ZOOM, "ref": str(im.get("id"))[:40]}, im["thumb_1024_url"], False)

    def find(self, wanted, token=None):
        """wanted: [(id, lat, lon, name)] -> (photos [{"id", "file", "source", "by", "year", "licence", "fx",
        "zoom"}], missing [id]): missing has none to be had (now)."""
        now = self.clock()
        found, missing = [], []
        for pid, lat, lon, name in wanted:
            k = key_of(pid)
            meta = self._meta(k)
            fresh = meta is not None and (
                (meta.get("file") and now - meta.get("t", 0) < KEEP_S
                 and os.path.exists(os.path.join(self.dir, k + ".jpg")))
                or (not meta.get("file") and now - meta.get("t", 0) < NONE_S and (meta.get("mly") or not token)))
            if not fresh:
                meta = {"t": now, "mly": bool(token), "file": None}
                got, url, failed = self._look(lat, lon, name, token, now)
                if got is not None:
                    try:
                        jpg = self._image(url)
                        meta.update(got, file=VFS_DIR + k + ".jpg")
                        self._write(k, meta, jpg)
                        self._since_prune += 1
                    except Exception as e:
                        log.debug("photo %s: %s: %s", url, type(e).__name__, e)
                        meta = None             # (a download that failed: asked again next time)
                elif failed:
                    meta = None                 # (a source down: nothing remembered, asked again next time)
                else:
                    try:
                        self._write(k, meta)
                    except OSError:
                        pass
            if meta and meta.get("file"):
                found.append({"id": pid, **{f: meta.get(f) for f in ("file", "source", "by", "year", "licence", "fx",
                                                                     "zoom")}})
            else:
                missing.append(pid)
        if self._since_prune >= 50:
            self._since_prune = 0
            self.prune()
        return found, missing

    def prune(self, keep=KEEP_FILES):
        """The oldest photos (and their notes) beyond keep removed; old "none" notes too."""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return 0
        jpgs = []
        for n in names:
            if n.endswith(".jpg"):
                try:
                    jpgs.append((os.path.getmtime(os.path.join(self.dir, n)), n))
                except OSError:
                    pass
        jpgs.sort()
        gone = 0
        for _, n in jpgs[:max(0, len(jpgs) - keep)]:
            for f in (n, n[:-4] + ".json"):
                try:
                    os.remove(os.path.join(self.dir, f))
                    gone += 1
                except OSError:
                    pass
        if len(names) > 4 * keep:            # "none" notes pile up: the oldest go
            notes = sorted((os.path.getmtime(os.path.join(self.dir, n)), n) for n in names
                           if n.endswith(".json") and n[:-5] + ".jpg" not in names)
            for _, n in notes[:len(notes) - 2 * keep]:
                try:
                    os.remove(os.path.join(self.dir, n))
                except OSError:
                    pass
        return gone
