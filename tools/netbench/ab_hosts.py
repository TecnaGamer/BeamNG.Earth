#!/usr/bin/env python3
"""Per host (and tgforever path prefix) MB/requests, before vs after, over the places both runs finished."""
import json, os, sys
from collections import defaultdict
from urllib.parse import urlsplit
ROOT = os.environ["AB_ROOT"]          # a bench.sh run: ROOT/{before,after}/{net.log,places.jsonl}
def key(url):
    u = urlsplit(url); h = u.hostname or ""
    if h in ("tiles.tgforever.net", "data.geopf.fr"):
        return h + "/" + u.path.split("/")[1]
    if h == "s3-us-west-2.amazonaws.com":
        return h + "/" + u.path.split("/")[1]
    return h
def load(run, places):
    lines = open(f"{ROOT}/{run}/net.log").read().splitlines()
    out = defaultdict(lambda: [0, 0])
    for rec in map(json.loads, open(f"{ROOT}/{run}/places.jsonl")):
        if places and rec["label"] not in places: continue
        for l in lines[rec["start_line"]:rec["end_line"]]:
            r = l.split(" "); o = out[key(r[4])]; o[0] += int(r[3]); o[1] += 1
    return out
places = sys.argv[1:]
if not places:
    a = {json.loads(l)["label"] for l in open(f"{ROOT}/after/places.jsonl")}
    places = [json.loads(l)["label"] for l in open(f"{ROOT}/before/places.jsonl") if json.loads(l)["label"] in a]
b, a = load("before", places), load("after", places)
print("places:", places)
rows = sorted(set(b) | set(a), key=lambda k: -(abs(b[k][0] - a[k][0])))
for k in rows[:30]:
    print(f"{k:55} {b[k][0]/1e6:8.1f} {a[k][0]/1e6:8.1f} {(a[k][0]-b[k][0])/1e6:+8.1f} MB  {b[k][1]:6} {a[k][1]:6} req")
