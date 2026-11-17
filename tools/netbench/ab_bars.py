#!/usr/bin/env python3
"""Per-place traffic of the A/B runs (AB_ROOT/{before,after}, bench.sh) as markdown: sectioned text bars by
category, plus a table of the totals. Prints to stdout."""
import json
import os
from urllib.parse import urlsplit

ROOT = os.environ["AB_ROOT"]          # a bench.sh run: ROOT/{before,after}/{net.log,places.jsonl}
RUNS = ("before", "after")
PLACES = (("lonepine", "Whitney Portal (start)"), ("hartford", "Hartford"), ("amsterdam", "Amsterdam"),
          ("lyon", "Lyon"), ("sanfrancisco", "San Francisco"))
# (key, label, glyph)
CATS = (("photo", "Photos", "█"), ("terrain", "Terrain & sea floor", "▓"), ("lidar", "US bridge lidar", "▒"),
        ("map", "Map data (Overture, OSM)", "░"), ("other", "Other", "·"))
PHOTO = ("basemap.nationalmap.gov", "apps.geo.fpac.usda.gov", "service.pdok.nl", "cteco.uconn.edu",
         "tiles.maps.eox.at", "orthos.its.ny.gov", "tiles.arcgis.com")
TERRAIN = ("tiles.mapterhorn.com", "download.mapterhorn.com", "s3.amazonaws.com", "gis.ngdc.noaa.gov",
           "canelevation-dem.s3.ca-central-1.amazonaws.com")
MAP = ("tiles.overturemaps.org", "data.openstreetmap.us")
MB_PER_CHAR = 10


def category(url):
    u = urlsplit(url)
    h, p = u.hostname or "", u.path
    if h == "tiles.tgforever.net":
        return "photo" if p.startswith("/s2") else "terrain" if p.startswith("/dem") else "map"
    if h == "data.geopf.fr":
        return "photo" if p.startswith("/wmts") else "other"
    if h in PHOTO:
        return "photo"
    if h in TERRAIN:
        return "terrain"
    if h == "s3-us-west-2.amazonaws.com" and p.startswith("/usgs-lidar-public"):
        return "lidar"
    if h in MAP or "overturemaps" in h:
        return "map"
    return "other"


def load(run):
    lines = open(os.path.join(ROOT, run, "net.log")).read().splitlines()
    out = {}
    for rec in map(json.loads, open(os.path.join(ROOT, run, "places.jsonl"))):
        by = dict.fromkeys((c[0] for c in CATS), 0)
        nb = dict.fromkeys((c[0] for c in CATS), 0)
        n = 0
        for l in lines[rec["start_line"]:rec["end_line"]]:
            r = l.split(" ")
            c = category(r[4])
            by[c] += int(r[3])
            nb[c] += 1
            n += 1
        out[rec["label"]] = (by, n, nb)
    return out


def bar(by):
    s, carry = "", 0.0
    for key, _, g in CATS:
        w = by[key] / 1e6 / MB_PER_CHAR + carry
        k = int(round(w))
        carry = w - k
        s += g * k
    return s


def pct(a, b):
    if not a:
        return "–"
    x = 100 * (b - a) / a
    return (f"{x:+.1f} %" if abs(x) < 1 else f"{x:+.0f} %").replace("-", "−")


def main():
    data = {run: load(run) for run in RUNS if os.path.exists(os.path.join(ROOT, run, "places.jsonl"))}
    print("```text")
    print("   ".join(f"{g} {label}" for _, label, g in CATS) + f"   (one character = {MB_PER_CHAR} MB)")
    print()
    for key, name in PLACES:
        for run in data:
            if key not in data[run]:
                continue
            by, n, _ = data[run][key]
            label = name if run == RUNS[0] else ""
            print(f"{label:<23}{run:<7}{bar(by)} {sum(by.values()) / 1e6:.0f} MB, {n:,} requests")
        print()
    print("```")
    print()
    b, a = data["before"], data["after"]
    print("| Place | MB before | MB after | MB change | Requests before | Requests after | Requests change |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    t = [0, 0, 0, 0]
    for key, name in PLACES:
        if key not in b or key not in a:
            continue
        mb0, mb1 = sum(b[key][0].values()), sum(a[key][0].values())
        n0, n1 = b[key][1], a[key][1]
        t = [t[0] + mb0, t[1] + mb1, t[2] + n0, t[3] + n1]
        print(f"| {name} | {mb0 / 1e6:,.0f} | {mb1 / 1e6:,.0f} | {pct(mb0, mb1)} | {n0:,} | {n1:,} | {pct(n0, n1)} |")
    print(f"| **All** | **{t[0] / 1e6:,.0f}** | **{t[1] / 1e6:,.0f}** | **{pct(t[0], t[1])}** | **{t[2]:,}** | "
          f"**{t[3]:,}** | **{pct(t[2], t[3])}** |")
    print()
    print("| Kind | MB before | MB after | MB change | Requests before | Requests after | Requests change |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for key, label, _ in CATS:
        v = [sum(d[p][0][key] for p in d) for d in (b, a)]
        n = [sum(d[p][2][key] for p in d) for d in (b, a)]
        print(f"| {label} | {v[0] / 1e6:,.0f} | {v[1] / 1e6:,.0f} | {pct(*v)} | {n[0]:,} | {n[1]:,} | {pct(*n)} |")


if __name__ == "__main__":
    main()
