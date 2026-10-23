"""Build the LidarBC indexes in server/: the files of some NTS 1:250k sheets with their CRS and bounds, so the server
finds a tile's files without asking the bucket.

    python3 tools/build_lidarbc_index.py [--product dem|dsm|ortho] [092B 092C ...]   # default: dem, the island

  dem    lidarbc_index.json.gz        bare earth (terrain; lidarbc.py)
  dsm    lidarbc_dsm_index.json.gz    first-return surface (canopy height = DSM - DEM; maprasters.bc_canopy)
  ortho  lidarbc_ortho_index.json.gz  orthophotos, the streamable ones only: JPEG, or with overviews (bcortho.py)

Lists https://nrs.objectstore.gov.bc.ca/gdwuts/<NNN>/<sheet>/ (S3 ListObjectsV2), keeps <year>/<product>/*.tif, and
reads each file's header (a few kB: cog.Cog over rangeread) for its EPSG and bounds. ~2 minutes for the island's
~7,700 DEM files. Licence of the data: Open Government Licence - British Columbia.
"""
import gzip
import json
import os
import re
import sys
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import bcortho  # noqa: E402
import bcproj  # noqa: E402
import cog  # noqa: E402
import lidarbc  # noqa: E402
import netfetch  # noqa: E402
import rangeread  # noqa: E402

ISLAND = ["092B", "092C", "092E", "092F", "092G", "092K", "092L", "102I"]
BUCKET = "https://nrs.objectstore.gov.bc.ca/gdwuts"


def list_keys(fetcher, prefix):
    out, token = [], None
    while True:
        q = {"list-type": "2", "prefix": prefix}
        if token:
            q["continuation-token"] = token
        body = fetcher.get(BUCKET + "?" + urllib.parse.urlencode(q)).body.decode("utf-8")
        out += re.findall(r"<Key>([^<]*)</Key>", body)
        nxt = re.findall(r"<NextContinuationToken>([^<]*)<", body)
        if "<IsTruncated>true</IsTruncated>" not in body or not nxt:
            return out
        token = nxt[0]


PRODUCTS = {"dem": ("dem", lidarbc.INDEX_PATH), "dsm": ("dsm", lidarbc.DSM_INDEX_PATH),
            "ortho": ("orthophoto", bcortho.INDEX_PATH)}


def header(fetcher, tmp, key, ortho=False):
    try:
        c = cog.Cog(rangeread.RemoteFile(BUCKET + "/" + key, None, os.path.join(tmp, key.replace("/", "_")), fetcher))
    except (netfetch.FetchError, cog.CogError, OSError) as e:
        print("skip", key, e, file=sys.stderr)
        return None
    if c.epsg is None or not bcproj.supported(c.epsg) or c.sx is None:
        print("skip", key, "CRS", c.epsg, file=sys.stderr)
        return None
    x0, y1 = c.x0, c.y0
    x1, y0 = x0 + c.width * c.sx, y1 - c.height * c.sy
    row = [key, c.epsg, round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2), int(key.split("/")[2])]
    if ortho:
        # streamable only: JPEG, or with overviews (an uncompressed 15 cm file without them: ~350 MB, ~100 MB a km2)
        has_ov = bool(c._next)
        if c.compression != "jpeg" and not (has_ov and c.sx <= 0.6):
            return None
        row.append(round(c.sx, 3))
    return row


def main(sheets, product="dem"):
    folder, out = PRODUCTS[product]
    fetcher = netfetch.Fetcher()
    keys = []
    for s in sheets:
        s = s.lower()
        keys += [k for k in list_keys(fetcher, f"{s[:3]}/{s}/") if f"/{folder}/" in k and k.endswith(".tif")]
    print(len(keys), product, "files", file=sys.stderr)
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(16) as ex:
        rows = [r for r in ex.map(lambda k: header(fetcher, tmp, k, product == "ortho"), keys) if r]
    rows.sort(key=lambda r: r[0])
    fields = ["key", "epsg", "xmin", "ymin", "xmax", "ymax", "year"] + (["res"] if product == "ortho" else [])
    doc = {"source": f"LidarBC (Province of British Columbia), bucket {BUCKET}, product '{folder}'",
           "licence": "Open Government Licence - British Columbia", "product": product,
           "sheets": [s.upper() for s in sheets], "fields": fields, "files": rows}
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    print(len(rows), "files ->", out, file=sys.stderr)


if __name__ == "__main__":
    args = sys.argv[1:]
    product = "dem"
    if args[:1] == ["--product"]:
        product, args = args[1], args[2:]
    main(args or ISLAND, product)
