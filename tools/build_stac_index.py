"""build_stac_index.py: the pinned Overture release's STAC index (overture.RELEASE), every collection BeamNG.Earth reads
(overture.LAYERS) with every item's file name, box, URLs and size, as one small file the app ships
(server/overture_stac_<release>.json.gz). The server then never asks stac.overturemaps.org for that release: Overture's
index server was down for about an hour on 2026-10-26 and no fresh place got roads or buildings (GitHub #357). Read
through the server's own StacIndex (netfetch's limits; the cache keeps what it fetched). Run again after a release bump.

    python3 tools/build_stac_index.py [--cache DIR]

--cache: the cache whose mapdata/raw keeps the fetched collections (default: the app's, ~/.cache/beamng-earth; a
scratch folder for a trial bump).
"""
import argparse
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (loaded by its path into another process, a test: that process's entry point guards)
    pass
import netfetch  # noqa: E402
import overture  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default="~/.cache/beamng-earth", help="the cache the collections are kept in")
    cache = os.path.abspath(os.path.expanduser(ap.parse_args().cache))
    raw = os.path.join(cache, "mapdata", "raw")
    index = overture.StacIndex(overture.RELEASE, raw, netfetch.default_fetcher())
    index._bundled = {}     # (never the shipped bundle itself: a rerun would copy its own rows instead of Overture's)
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
    tmp = path + ".tmp"     # (written whole, then replaced: an interrupted run leaves the shipped bundle as it was)
    with open(tmp, "wb") as raw_f, gzip.GzipFile(fileobj=raw_f, mode="wb", filename="", mtime=0) as f:
        f.write(json.dumps(out, separators=(",", ":")).encode("utf-8"))     # (no name, no time: the same bytes each run)
    os.replace(tmp, path)
    print(path, os.path.getsize(path), "bytes")


if __name__ == "__main__":
    main()
