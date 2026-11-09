#!/usr/bin/env python3
"""Germany's measured roofs: the shards server/lod2roofs.py reads, from the states' open LoD2 models.

Each state's tiles are fetched one at a time, reduced to one small row per building and thrown away, so the ~600 GB of
CityGML never lie on disk at once (rows: ~20 bytes; shards: ~6 bytes a building).

  python tools/build_lod2_roofs.py fetch HE [workers]    resumable: <root>/out/HE.done, HE.csv.gz
  python tools/build_lod2_roofs.py shards DIR HE BY ...  <DIR>/<x>/<y>.npz and manifest.json

The root is BNE_LOD2_ROOT. Niedersachsen's tile list is its index GeoJSON, saved as <root>/ni.geojson.
Needs lxml and pyproj. Licences and credits of each state: CREDITS.md.
"""
import csv
import ctypes
import gzip
import html
import io
import json
import math
import multiprocessing as mp
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse as P
import urllib.request as U
import zipfile

import numpy as np
from lxml import etree
from pyproj import Transformer

ROOT = os.environ.get("BNE_LOD2_ROOT", "lod2")
OUT = os.path.join(ROOT, "out")
TMP = os.path.join(ROOT, "tmp")
ZOOM = 10
CHUNK = 1 << 20
RECORD = np.dtype([("lat", "<i4"), ("lon", "<i4"), ("code", "<i2"), ("height", "<u2"), ("roof", "<u2")])


FIELDS = ("x", "y", "epsg", "area", "roof", "height", "z0", "zr", "zt", "ridge", "part")
EPSG = re.compile(r"(?:EPSG[:/]+(?:[\d.]*:)?|ETRS89_UTM|UTM)(\d{2,5})", re.I)


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def rings(el):
    """[(n, 3) point lists] of every posList / pos ring under el."""
    out = []
    for e in el.iter():
        n = local(e.tag)
        if n == "posList" and e.text:
            v = e.text.split()
            if len(v) >= 9:
                out.append([(float(v[i]), float(v[i + 1]), float(v[i + 2])) for i in range(0, len(v) - 2, 3)])
        elif n == "LinearRing":
            ps = [p.text.split() for p in e if local(p.tag) == "pos" and p.text]
            if len(ps) >= 3:
                out.append([(float(p[0]), float(p[1]), float(p[2])) for p in ps if len(p) >= 3])
    return out


def centroid(rs):
    """(x, y, area) of the rings' outlines added up (the surveyed ground, in plan)."""
    A = cx = cy = 0.0
    for r in rs:
        x0, y0 = r[0][0], r[0][1]
        a = sx = sy = 0.0
        for (x1, y1, _), (x2, y2, _) in zip(r, r[1:] + r[:1]):
            c = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
            a += c
            sx += (x1 + x2 - 2 * x0) * c
            sy += (y1 + y2 - 2 * y0) * c
        if abs(a) < 1e-9:
            continue
        a2 = abs(a) / 2
        A += a2
        cx += (x0 + sx / (3 * a)) * a2
        cy += (y0 + sy / (3 * a)) * a2
    return (cx / A, cy / A, A) if A > 0 else None


def ridge(rs):
    """Azimuth 0..180 of the longest level edge at the top of the roof, or None (a flat roof has no ridge)."""
    top = max((p[2] for r in rs for p in r), default=None)
    if top is None:
        return None
    best, az = 1.0, None
    for r in rs:
        for a, b in zip(r, r[1:]):
            if top - a[2] < 0.15 and top - b[2] < 0.15:
                d = math.hypot(b[0] - a[0], b[1] - a[1])
                if d > best:
                    best, az = d, math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 180
    return az


def row(el, epsg, part):
    roof = height = None
    ground, roofs, walls = [], [], []
    for e in el.iter():
        n = local(e.tag)
        if n == "BuildingPart" and e is not el:
            continue
        if n == "roofType" and e.text and roof is None:
            roof = e.text.strip()
        elif n == "measuredHeight" and e.text and height is None:
            try:
                height = float(e.text)
            except ValueError:
                pass
        elif n == "GroundSurface":
            ground += rings(e)
        elif n == "RoofSurface":
            roofs += rings(e)
        elif n == "WallSurface":
            walls += rings(e)
    if not ground and not roofs:
        return None
    c = centroid(ground or roofs)
    if c is None:
        return None
    zs = [p[2] for r in ground for p in r] or [p[2] for r in walls for p in r]
    rz = [p[2] for r in roofs for p in r]
    az = ridge(roofs)
    return {"x": round(c[0], 2), "y": round(c[1], 2), "epsg": epsg, "area": round(c[2], 1), "roof": roof or "",
            "height": "" if height is None else round(height, 2), "z0": round(min(zs), 2) if zs else "",
            "zr": round(min(rz), 2) if rz else "", "zt": round(max(rz), 2) if rz else "",
            "ridge": "" if az is None else round(az), "part": int(part)}


def clear(el):
    el.clear()
    while el.getprevious() is not None:
        del el.getparent()[0]


def parse(f, default_epsg=None):
    """Rows of the CityGML file object f."""
    epsg = default_epsg
    for _, el in etree.iterparse(f, events=("end",), huge_tree=True, recover=True):
        n = local(el.tag)
        if n == "Envelope" and epsg is None:
            m = EPSG.search(el.get("srsName") or "")
            if m:
                s = m.group(1)
                epsg = int(s) if len(s) > 2 else 25800 + int(s)
        elif n == "Building":
            parts = [p for p in el.iter() if local(p.tag) == "BuildingPart"]
            if parts:
                for p in parts:
                    r = row(p, epsg, True)
                    if r:
                        yield r
            else:
                r = row(el, epsg, False)
                if r:
                    yield r
            clear(el)


UA = {"User-Agent": "BeamNG.Earth LoD2 roof import (open source, one-off, low rate)"}


def get(url, timeout=120):
    return U.urlopen(U.Request(url, headers=UA), timeout=timeout)


def text(url):
    return get(url).read().decode("utf-8", "replace")


def grid(fmt, e0, e1, es, n0, n1, ns):
    return [(f"{e}_{n}", fmt.format(e=e, n=n)) for e in range(e0, e1 + 1, es) for n in range(n0, n1 + 1, ns)]


def links(url, pattern):
    return sorted({P.urljoin(url, html.unescape(m)) for m in re.findall(pattern, text(url))})


def named(urls):
    return [(re.sub(r"\W+", "_", u[-70:]) if "?" in u else u.rsplit("/", 1)[-1], u) for u in urls]


def hessen():
    base = "https://gds.hessen.de"
    root = "/INTERSHOP/rest/WFS/HLBG-Geodaten-Site/-/downloadcenter?path=" + P.quote(
        "3D-Daten/3D-Gebäudemodelle/3D-Gebäudemodelle LoD2", safe="") + "&navigation=all"
    nav = [x for x in json.loads(text(base + root))["navigation"] if x.get("level") == 4]
    out = []
    for x in nav:
        time.sleep(0.6)
        for p in json.loads(text(base + x["uri"]))["searchresult"].get("packages", []):
            out.append((x["name"], base + P.quote(p["downloadLink"]["uri"], safe="/")))
    return out


SH = "https://geodaten.schleswig-holstein.de/gaialight-sh/_apps/dladownload/"


def schleswig_holstein():
    """Tiles found 10 km at a time, one to a job (the service builds each job's ZIP and allows one file in it)."""
    tiles = {}
    for e in range(420000, 680000, 10000):
        for n in range(5900000, 6110000, 10000):
            time.sleep(0.2)
            q = {"bbox": f"{e},{n},{e + 10000},{n + 10000}", "crs": "EPSG:25832", "type[]": "lod2"}
            d = json.loads(text(SH + "_ajax/overview.php?" + P.urlencode(q)))
            for f in (d.get("result") or {}).get("features", []) if d.get("success") else []:
                tiles[f["properties"]["kachel_n"]] = f["properties"]["ogc_fid"]
    return [(n, "sh:" + json.dumps({n: tiles[n]})) for n in sorted(tiles)]


def sh_order(url):
    """The file of a job: started, waited for."""
    t = json.loads(url[3:])
    q = [("file[]", n + ".xml") for n in t] + [("id[]", i) for i in t.values()]
    q += [("action", "start"), ("type", "lod2"), ("buttonClass", "grau"), ("username", "")]
    d = json.loads(text(SH + "multi.php?" + P.urlencode(q)))
    if not d.get("success"):
        raise IOError("job refused: " + json.dumps(d)[:200])
    job = d["id"]
    for _ in range(120):
        time.sleep(3)
        s = json.loads(text(SH + "multi.php?" + P.urlencode({"action": "status", "job": job})))
        if s.get("finished") or s.get("done") or s.get("status") in ("done", "finished", "ready") or s.get("url") or s.get("download"):
            return SH + "multi.php?" + P.urlencode({"action": "download", "job": job})
    raise IOError("job not finished: " + json.dumps(s)[:200])


def jobs(state):
    if state == "NW":
        b = "https://www.opengeodata.nrw.de/produkte/geobasis/3dg/lod2_gml/lod2_gml/"
        d = json.loads(text(b + "index.json"))
        return [(f["name"], b + f["name"]) for f in d["datasets"][0]["files"] if f["name"].endswith(".gml")]
    if state == "BY":
        s = text("https://geodaten.bayern.de/odd/a/lod2/citygml/meta/metalink/09.meta4")
        return named(sorted(set(re.findall(r"https://download1\.bayernwolke\.de/a/lod2/citygml/[\d_]+\.gml", s))))
    if state == "RP":
        return named(links("https://geobasis-rlp.de/data/geb3dlo/current/gml/", r'href="([^"]+\.gml)"'))
    if state == "NI":
        d = json.load(open(os.path.join(ROOT, "ni.geojson"),
                           encoding="utf-8"))
        return named(f["properties"]["xml"] for f in d["features"])
    if state == "BB":
        return named(links("https://data.geobasis-bb.de/geobasis/daten/3d_gebaeude/lod2_gml/", r'href="([^"]+\.zip)"'))
    if state == "BE":
        return named(links("https://gdi.berlin.de/data/a_lod2/atom/0.atom", r'href="([^"]+\.zip)"'))
    if state == "HE":
        return hessen()
    if state == "HH":
        return [("HH", "https://daten-hamburg.de/opendata/3d_stadtmodell_lod2/LoD2-DE_HH_2026-04-28.zip")]
    if state == "HB":
        return named("https://gdi2.geo.bremen.de/inspire/download/LoD/data/" + n
                     for n in ("LOD2_CITYGML_HB.zip", "LOD2_CITYGML_BHV.zip"))
    if state == "MV":
        return named(links("https://www.geodaten-mv.de/dienste/gebaeude_atom?type=dataset&id=8397b554-5cb9-4274-8be8-c20490d9a6e8",
                           r'href="([^"]+(?:\.zip|format=zip[^"]*))"'))
    if state == "ST":
        ids = re.findall(r'"id":\s*"(\d+)",\s*"label"', text("https://www.lvermgeo.sachsen-anhalt.de/de/gdp-download-lod2.html"))
        return [(i, "https://www.lvermgeo.sachsen-anhalt.de/de/mod/4,1965,501/ajax/1/prepare/?items=" + i + "&format=zip")
                for i in sorted(set(ids))]
    if state == "SH":
        return schleswig_holstein()
    if state == "BW":
        return grid("https://opengeodata.lgl-bw.de/data/lod2/LoD2_32_{e}_{n}_2_bw.zip", 387, 611, 2, 5262, 5518, 2)
    if state == "SN":
        return grid("https://geocloud.landesvermessung.sachsen.de/public.php/dav/files/AyJqXpJAZJXomCb/"
                    "lod2_33{e}_{n}_2_sn_citygml.zip", 276, 502, 2, 5556, 5732, 2)
    raise SystemExit("unknown state " + state)


def rows_of(path, name, depth=0):
    """Rows of a GML, or of every GML in a ZIP (and in ZIPs inside it)."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] != b"PK":
        with open(path, "rb") as f:
            yield from parse(f)
        return
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            low = n.lower()
            if low.endswith(".zip") and depth < 3:
                fd, p = tempfile.mkstemp(dir=TMP, suffix=".zip")
                with os.fdopen(fd, "wb") as o, z.open(n) as i:
                    while b := i.read(1 << 20):
                        o.write(b)
                try:
                    yield from rows_of(p, n, depth + 1)
                finally:
                    os.remove(p)
            elif low.endswith((".gml", ".xml")) and "meta" not in low:
                with z.open(n) as f:
                    yield from parse(f)


def work(job):
    name, url = job
    p = os.path.join(TMP, f"{os.getpid()}.part")
    for attempt in range(6):
        try:
            real = text(url).strip() if "/prepare/" in url else sh_order(url) if url.startswith("sh:") else url
            with get(real, 300) as r, open(p, "wb") as o:
                while b := r.read(1 << 20):
                    o.write(b)
            break
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return name, None, 0, "empty"
            err = f"http {e.code}"
        except Exception as e:      # (a dropped connection: the next attempt, later)
            err = repr(e)[:120]
        time.sleep(15 * (attempt + 1))
    else:
        return name, None, 0, err
    try:
        size = os.path.getsize(p)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        n = 0
        for r in rows_of(p, name):
            w.writerow([r[k] for k in FIELDS])
            n += 1
        return name, gzip.compress(buf.getvalue().encode(), 6), size, n
    except Exception as e:
        return name, None, 0, "parse " + repr(e)[:120]
    finally:
        os.remove(p)


def low():
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    except Exception:
        pass


def rows(state):
    with gzip.open(os.path.join(ROOT, "out", state + ".csv.gz"), "rt", newline="") as f:
        for r in csv.reader(f):
            yield r


def num(s):
    return float(s) if s else float("nan")


def read(state):
    """(epsg, rows) of a state, a chunk at a time: (x, y, code, height, roof height)."""
    out = {}
    for x, y, epsg, area, roof, height, z0, zr, zt, ridge, part in rows(state):
        try:
            code = int(roof) if roof else 0
        except ValueError:
            code = 0
        h = num(height)
        lo, hi, g = num(zr), num(zt), num(z0)
        if not h > 0 and hi > g:
            h = hi - g
        rh = hi - lo if hi >= lo else 0.0
        if not 0.5 < h < 400:
            continue
        v = out.setdefault(int(epsg or 0), [])
        v.append((float(x), float(y), code, h, rh))
        if len(v) >= CHUNK:
            yield int(epsg or 0), v
            out[int(epsg or 0)] = []
    yield from out.items()


def tiles(lat, lon):
    n = 1 << ZOOM
    r = np.radians(lat)
    return ((lon + 180) / 360 * n).astype(np.int32), ((1 - np.arcsinh(np.tan(r)) / math.pi) / 2 * n).astype(np.int32)


def spool(tmp, epsg, v):
    """Rows onto the end of their tiles' files: a country does not have to fit in memory."""
    a = np.array(v, np.float64)
    lon, lat = Transformer.from_crs(epsg, 4326, always_xy=True).transform(a[:, 0], a[:, 1])
    r = np.empty(len(a), RECORD)
    r["lat"], r["lon"] = np.round(lat * 1e6), np.round(lon * 1e6)
    r["code"], r["height"] = a[:, 2], np.round(a[:, 3] * 10)
    r["roof"] = np.round(np.minimum(a[:, 4], 200) * 10)
    tx, ty = tiles(lat, lon)
    t = tx.astype(np.int64) * (1 << ZOOM) + ty
    order = np.argsort(t, kind="stable")
    r, t = r[order], t[order]
    for sl in np.split(np.arange(len(t)), np.flatnonzero(np.diff(t)) + 1):
        x, y = divmod(int(t[sl[0]]), 1 << ZOOM)
        with open(os.path.join(tmp, f"{x}_{y}.bin"), "ab") as f:
            f.write(r[sl].tobytes())


def build(dest, states):
    per = {}
    total = files = records = 0
    with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
        for s in states:
            t0 = time.time()
            per[s] = 0
            for epsg, v in read(s):
                if v:
                    spool(tmp, epsg, v)
                    per[s] += len(v)
            print(s, per[s], round(time.time() - t0), "s", flush=True)
        for name in sorted(os.listdir(tmp)):
            r = np.fromfile(os.path.join(tmp, name), RECORD)
            key = (r["lat"].astype(np.int64) // 5) * (1 << 32) + (r["lon"].astype(np.int64) // 5)   # (a tile border: the same building twice)
            r = r[np.unique(key, return_index=True)[1]]
            x, y = name[:-4].split("_")
            d = os.path.join(dest, x)
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, f"{y}.npz")
            np.savez_compressed(p, **{k: r[k] for k in RECORD.names})
            total += os.path.getsize(p)
            files += 1
            records += len(r)
    json.dump({"built": time.strftime("%Y-%m-%d"), "states": per, "records": records, "shards": files},
              open(os.path.join(dest, "manifest.json"), "w"), indent=1)
    print("records", records, "shards", files, "MB", round(total / 1e6, 1), "bytes/record", round(total / records, 1))


def fetch(state, workers=2):
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)
    low()
    done_p = os.path.join(OUT, state + ".done")
    done = set(open(done_p, encoding="utf-8").read().split("\n")) if os.path.exists(done_p) else set()
    todo = [j for j in jobs(state) if j[0] not in done]
    print(state, "todo", len(todo), "done", len(done), flush=True)
    t0, got, rows_n, errs = time.time(), 0, 0, 0
    with mp.Pool(workers, initializer=low) as pool, open(os.path.join(OUT, state + ".csv.gz"), "ab") as out, \
            open(done_p, "a", encoding="utf-8") as dl, open(os.path.join(OUT, state + ".err"), "a", encoding="utf-8") as el:
        for i, (name, blob, size, res) in enumerate(pool.imap_unordered(work, todo), 1):
            if blob is not None:
                out.write(blob)
                out.flush()
                got += size
                rows_n += res
            if blob is not None or res == "empty":
                dl.write(name + "\n")
                dl.flush()
            else:
                errs += 1
                el.write(f"{name}\t{res}\n")
                el.flush()
            if i % 200 == 0 or i == len(todo):
                print(f"{state} {i}/{len(todo)} rows {rows_n} GB {got / 1e9:.1f} err {errs} "
                      f"min {(time.time() - t0) / 60:.0f}", flush=True)
    print(state, "done: rows", rows_n, "errors", errs, flush=True)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "fetch":
        fetch(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 2)
    elif len(sys.argv) >= 4 and sys.argv[1] == "shards":
        build(sys.argv[2], sys.argv[3:])
    else:
        raise SystemExit(__doc__)
