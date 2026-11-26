"""build_stac_index.py: the pinned Overture release's STAC index (overture.RELEASE), every collection BeamNG.Earth reads
(overture.LAYERS) with every item's file name, box, URLs and size, as one small file the app ships
(server/overture_stac_<release>.json.gz). The server then never asks stac.overturemaps.org for that release: Overture's
index server was down for about an hour on 2026-10-26 and no fresh place got roads or buildings (GitHub #357). Read
through the server's own StacIndex (netfetch's limits; the cache keeps what it fetched). Run again after a release bump.

    python3 tools/build_stac_index.py [--cache DIR] [--release ID --out FILE]

--cache: the cache whose mapdata/raw keeps the fetched collections (default: the app's, ~/.cache/beamng-earth; a
scratch folder for a trial bump).
"""
import argparse
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
    ap.add_argument("--release", default=overture.RELEASE, help="the release (default: the build's own, shipped)")
    ap.add_argument("--out", default=None, help="the file (default: server/overture_stac_<release>.json.gz)")
    a = ap.parse_args()
    if not overture.RELEASE_RE.fullmatch(a.release):
        ap.error(f"--release: not a release id: {a.release}")
    cache = os.path.abspath(os.path.expanduser(a.cache))
    raw = os.path.join(cache, "mapdata", "raw")
    index = overture.StacIndex(a.release, raw, netfetch.default_fetcher())
    index._bundled = {}     # (never a bundle itself: a rerun would copy its own rows instead of Overture's)
    collections = overture.bundle_rows(index, kind="demand")
    for k, c in collections.items():
        print(f"{k}: {len(c['files'])} files")
    path = a.out or os.path.join(HERE, "..", "server", f"overture_stac_{a.release}.json.gz")
    # (written whole, then replaced: an interrupted run leaves the shipped bundle as it was; no name, no time in the
    # gzip: the same bytes each run)
    overture.write_bundle(path, a.release, collections)
    print(path, os.path.getsize(path), "bytes")


if __name__ == "__main__":
    main()
