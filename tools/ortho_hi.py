"""The finest public orthophoto of a square HALF_M around LAT LON (north up, square in metres), for comparing the game's
roads and markings with the real ones (the user, 2026-10-28: "compare against high res satellite photos").

    python3 tools/ortho_hi.py LAT LON HALF_M OUT.jpg [SOURCE] [PX]

SOURCE: auto (the first that covers the point and has a picture there; also the server's regional sources,
imagery_regional.REGIONAL), md3in (Maryland 0.076 m), penn (Pennsylvania 0.15 m), virginia (0.3 m;
0.076 m in cities), jeffco (Jefferson County CO 0.15 m), naip (USGS NAIP, ~0.6 m, the US). Sources and licences:
docs/research/2026-10-10-us-state-imagery.md. Fetched through the server's netfetch (its limits, the project's
User-Agent)."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import io  # noqa: E402

import netfetch  # noqa: E402
from PIL import Image  # noqa: E402

R = 6378137.0
# name: (kind, url, (lat0, lon0, lat1, lon1), zoom for xyz)
SOURCES = {
    "md3in": ("export", "https://mdgeodata.md.gov/imagery/rest/services/ThreeInch/MD_ThreeInchImagery/ImageServer/"
              "exportImage?bandIds=0,1,2&bbox={bbox}&bboxSR=3857&imageSR=3857&size={w},{h}&format=jpg&f=image",
              (37.88, -79.49, 39.73, -75.04), None),
    "penn": ("xyz", "https://imagery.pasda.psu.edu/arcgis/rest/services/PEMAImagery2021_2023cache/MapServer/tile/"
             "{z}/{y}/{x}", (39.72, -80.52, 42.27, -74.69), 20),
    "virginia": ("xyz", "https://vginmaps.vdem.virginia.gov/arcgis/rest/services/VBMP_Imagery/MostRecentImagery_WGS/"
                 "MapServer/tile/{z}/{y}/{x}", (36.54, -83.68, 39.47, -75.24), 19),
    "jeffco": ("export", "https://gisportal.jeffco.us/image/rest/services/DRAPP/DRAPP2022/ImageServer/exportImage?"
               "bbox={bbox}&bboxSR=3857&imageSR=3857&size={w},{h}&format=jpg&f=image",
               (39.40, -105.40, 39.95, -105.05), None),
    "naip": ("export", "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer/exportImage?"
             "bbox={bbox}&bboxSR=3857&imageSR=3857&size={w},{h}&format=jpg&f=image", (18.0, -180.0, 72.0, -60.0), None),
}
# ... and the server's regional XYZ sources (imagery_regional.REGIONAL: Switzerland, France, the Netherlands, Japan,
# New South Wales, US states...), read at z19
import imagery_regional  # noqa: E402
XYZ_MAX_ZOOM = {"japan": 18}        # (GSI's seamless photo stops at z18: z19 was all 404s, a black picture of Atami)
for _n, _v in imagery_regional.REGIONAL.items():
    if _n not in SOURCES:           # (an "export" one: a WMS / ArcGIS export, its url takes {bbox} / {bbox4326}, {size})
        SOURCES[_n] = (_v[3], _v[0], _v[2], XYZ_MAX_ZOOM.get(_n, 19) if _v[3] == "xyz" else None)
ORDER = ("md3in", "penn", "virginia", "jeffco") + tuple(n for n in SOURCES if n not in
                                                        ("md3in", "penn", "virginia", "jeffco", "naip")) + ("naip",)


def merc(lat, lon):
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def unmerc(x, y):
    return math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2), math.degrees(x / R)


def fetch(f, name, x0, y0, x1, y1, px):
    """The picture of the mercator box from source name, or None when it has nothing there (every tile failed, or a
    blank answer: the source's box takes in a neighbour it does not cover, Austria's basemap and Regensburg)."""
    kind, url, _, zoom = SOURCES[name]
    if kind == "export":
        (s, w), (n_, e) = unmerc(x0, y0), unmerc(x1, y1)
        try:
            r = f.get(url.format(bbox=f"{x0},{y0},{x1},{y1}", bbox4326=f"{s},{w},{n_},{e}", w=px, h=px, size=px))
            img = Image.open(io.BytesIO(r.body)).convert("RGB")
        except Exception as ex:
            print(name, "failed:", ex)
            return None
    else:
        n = 1 << zoom
        span = 2 * math.pi * R
        tx0, tx1 = int((x0 + span / 2) / span * n), int((x1 + span / 2) / span * n)
        ty0, ty1 = int((span / 2 - y1) / span * n), int((span / 2 - y0) / span * n)
        mos = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
        got = 0
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                try:
                    t = f.get(url.format(z=zoom, x=tx, y=ty, ty=n - 1 - ty))
                    mos.paste(Image.open(io.BytesIO(t.body)).convert("RGB"), ((tx - tx0) * 256, (ty - ty0) * 256))
                    got += 1
                except Exception as e:
                    print("tile", tx, ty, e)
        if not got:
            return None
        res = span / n / 256
        ox, oy = tx0 * 256 * res - span / 2, span / 2 - ty0 * 256 * res
        box = ((x0 - ox) / res, (oy - y1) / res, (x1 - ox) / res, (oy - y0) / res)
        img = mos.crop(tuple(int(round(v)) for v in box)).resize((px, px), Image.LANCZOS)
    lo, hi = img.convert("L").getextrema()
    return None if hi - lo < 8 else img         # (one flat colour: a nodata answer)


def main():
    lat, lon, half, out = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
    name = sys.argv[5] if len(sys.argv) > 5 else "auto"
    px = int(sys.argv[6]) if len(sys.argv) > 6 else 1024
    if name == "auto":              # every source whose box covers the point, in ORDER: the first with a picture
        names = [n for n in ORDER if SOURCES[n][2][0] <= lat <= SOURCES[n][2][2]
                 and SOURCES[n][2][1] <= lon <= SOURCES[n][2][3]]
    else:
        names = [name]
    k = 1.0 / math.cos(math.radians(lat))           # mercator metres per ground metre
    cx, cy = merc(lat, lon)
    x0, y0, x1, y1 = cx - half * k, cy - half * k, cx + half * k, cy + half * k
    f = netfetch.Fetcher(stats_dir=os.path.join(os.path.expanduser("~/.cache/beamng-earth"), "netstats"))
    for name in names:
        img = fetch(f, name, x0, y0, x1, y1, px)
        if img is not None:
            img.save(out, quality=92)
            print(name, out)
            return
        print(name, "has nothing here")
    sys.exit("no source has a picture of this place: " + ", ".join(names))


if __name__ == "__main__":
    main()
