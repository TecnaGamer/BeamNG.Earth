"""build_stac_index.py: the pinned Overture release's STAC index (overture.RELEASE), every collection BeamNG.Earth reads
(overture.LAYERS) with every item's file name, box, URLs and size, as one small file the app ships
(server/overture_stac_<release>.json.gz). The server then never asks stac.overturemaps.org for that release: Overture's
index server was down for about an hour on 2026-10-26 and no fresh place got roads or buildings (GitHub #357). Read
through the server's own StacIndex (netfetch's limits; the cache keeps what it fetched). Run again after a release bump.

    python3 tools/build_stac_index.py
"""
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import netfetch  # noqa: E402
import overture  # noqa: E402


def main():
    cache = os.path.expanduser("~/.cache/beamng-earth")
    raw = os.path.join(cache, "mapdata", "raw")
    index = overture.StacIndex(overture.RELEASE, raw, netfetch.default_fetcher())
    pairs = sorted({(v["theme"], v["type"]) for v in overture.LAYERS.values()})
    out = {"release": overture.RELEASE, "collections": {}}
    for theme, typ in pairs:
        c = index.collection(theme, typ, kind="demand")
        rows = []
        for (name, urls, size), bb in zip(index.files(theme, typ, (-180.0, -90.0, 180.0, 90.0), kind="demand"),
                                          c["bboxes"]):
            rows.append([name, [float(v) for v in bb], urls, size])
        out["collections"][f"{theme}/{typ}"] = {"license": c.get("license"), "files": rows}
        print(f"{theme}/{typ}: {len(rows)} files")
    path = os.path.join(HERE, "..", "server", f"overture_stac_{overture.RELEASE}.json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print(path, os.path.getsize(path), "bytes")


if __name__ == "__main__":
    main()
