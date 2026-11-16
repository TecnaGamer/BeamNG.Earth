#!/usr/bin/env python3
"""build_cis_data.py: the post-Soviet housing art as the data the kernel reads (plan 048).

    python tools/build_cis_data.py          # writes server/data/cis_atlases.json and server/data/cis_modules.json
    python tools/build_cis_data.py --check  # only says whether they are up to date

cis_atlases.json: {kit: the kit's cells.json, "common": the common atlas's} (earthfast cis_facade reads them).
cis_modules.json: {"modules": [{id, kind, fits, weight, width_m, top_only, geo: [{mat, p, uv}]}]}: every module of
bne_cis_modules.json with its triangles read from its DAE (positions in the module's frame, 9 numbers a triangle, uv 6),
which earthfast cis_modules stamps on the slots of a facade. Deterministic; the DAEs and JSONs are the art's
(mod/.../beamngEarth/cis, tools/cis_assets).
"""
import argparse
import json
import os
import sys
import subprocess
import xml.etree.ElementTree as ET

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ART = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth", "cis")
OUT = os.path.join(ROOT, "server", "data")
NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
KITS = ("khrush_panel", "khrush_brick", "panel9", "panel16", "new_mono", "common")


def _round(vals, nd=4):
    return [round(float(v), nd) for v in vals]


def read_dae(path):
    """[(material name, positions [9 per triangle], uvs [6 per triangle])] of a DAE's first mesh."""
    root = ET.parse(path).getroot()
    mesh = root.find(".//c:geometry/c:mesh", NS)
    sources = {s.get("id"): [float(v) for v in s.find("c:float_array", NS).text.split()] for s in mesh.findall("c:source", NS)}
    strides = {s.get("id"): int(s.find("c:technique_common/c:accessor", NS).get("stride")) for s in mesh.findall("c:source", NS)}
    vert = mesh.find("c:vertices", NS)
    pos_src = vert.find("c:input[@semantic='POSITION']", NS).get("source")[1:]
    out = []
    for tri in mesh.findall("c:triangles", NS):
        inputs = {i.get("semantic"): (int(i.get("offset")), i.get("source")[1:]) for i in tri.findall("c:input", NS)}
        stride = max(o for o, _ in inputs.values()) + 1
        p = [int(v) for v in tri.find("c:p", NS).text.split()]
        vo = inputs["VERTEX"][0]
        to, ts = inputs.get("TEXCOORD", (None, None))
        P, UV = [], []
        for k in range(0, len(p), stride):
            vi = p[k + vo]
            P += sources[pos_src][vi * strides[pos_src]: vi * strides[pos_src] + 3]
            if to is not None:
                ti = p[k + to]
                UV += sources[ts][ti * strides[ts]: ti * strides[ts] + 2]
            else:
                UV += [0.0, 0.0]
        out.append((tri.get("material"), P, UV))
    return out


def _png(name):
    """The art's PNG (the working copy's where a generator left one, else the commit the S-1 PNGs were last in)."""
    import io
    import numpy as np
    from PIL import Image
    path = os.path.join(ART, name)
    if os.path.exists(path):
        return np.asarray(Image.open(path).convert("RGB"), np.float64)
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    blob = subprocess.run(["git", "show", f"{PNG_REV}:{rel}"], cwd=ROOT, capture_output=True, check=True).stdout
    return np.asarray(Image.open(io.BytesIO(blob)).convert("RGB"), np.float64)


PNG_REV = "f1829d85"        # (the commit before the S-1 PNGs became DDS)
WIN = 7                     # px: the window a plain texel is looked for with
STEP = 6


def plain_texel(color, normal, px):
    """The texel (x, y) of the cell px = [x, y, w, h] with the plainest colour and the flattest normal: the middle of a
    panel, not a joint, a vent or a stain. A quad that has no picture of its own samples one texel; the cell's centre
    gave the skirt under a wall a dark band (a joint's tilted normal)."""
    import numpy as np
    x0, y0, w, h = (int(round(v)) for v in px)
    m = max(10, int(0.1 * min(w, h)))
    best = None
    lum = color.mean(axis=2)
    med = float(np.median(lum[y0 + m:y0 + h - m, x0 + m:x0 + w - m])) if w > 2 * m and h > 2 * m else 0.0
    for y in range(y0 + m, y0 + h - m - WIN, STEP):
        for x in range(x0 + m, x0 + w - m - WIN, STEP):
            c = color[y:y + WIN, x:x + WIN]
            n = normal[y:y + WIN, x:x + WIN]
            dev = np.abs(n - np.array([128.0, 128.0, 255.0])).max()
            score = float(c.std(axis=(0, 1)).mean()) + 0.6 * dev + 0.3 * abs(float(c.mean()) - med)
            if best is None or score < best[0]:
                best = (score, x + WIN // 2, y + WIN // 2)
    return None if best is None else [best[1], best[2]]


def build():
    atlases = {}
    for k in KITS:
        with open(os.path.join(ART, f"bne_cis_{k}.cells.json"), encoding="utf-8") as f:
            atlases[k] = json.load(f)
        color, normal = _png(f"bne_cis_{k}.color.png"), _png(f"bne_cis_{k}.normal.png")
        for cell in atlases[k]["cells"]:
            if cell["role"] in ("end", "plinth"):
                cell["plain"] = plain_texel(color, normal, cell["px"])
    with open(os.path.join(ART, "bne_cis_modules.json"), encoding="utf-8") as f:
        meta = json.load(f)["modules"]
    modules = []
    for m in meta:
        geo = [{"mat": mat, "p": _round(P), "uv": _round(UV, 5)} for mat, P, UV in read_dae(os.path.join(ART, m["dae"]))]
        modules.append({"id": m["id"], "kind": m["kind"], "fits": m["fits"], "weight": m.get("weight", 1.0),
                        "width_m": m["width_m"], "top_only": bool(m.get("top_only")), "geo": geo})
    return (json.dumps(atlases, separators=(",", ":"), sort_keys=True),
            json.dumps({"modules": modules}, separators=(",", ":"), sort_keys=True))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    at, mo = build()
    files = {"cis_atlases.json": at, "cis_modules.json": mo}
    stale = []
    for name, text in files.items():
        path = os.path.join(OUT, name)
        have = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if a.check and name == "cis_atlases.json" and have is not None:
            # (the plain texels follow the pixels a generator is rewriting: a check is of the cells' layout)
            strip = lambda t: json.dumps({k: {**v, "cells": [{c: x for c, x in cell.items() if c != "plain"} for cell in v["cells"]]}
                                          for k, v in json.loads(t).items()}, sort_keys=True)
            have, text = strip(have), strip(text)
        if have != text:
            stale.append(name)
            if not a.check:
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
        print(f"{name}: {len(text) / 1024:.0f} KB{' (stale)' if name in stale else ''}")
    if a.check and stale:
        sys.exit(1)


if __name__ == "__main__":
    main()
