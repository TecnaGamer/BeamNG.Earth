"""Build index.npz for github.com/maalos/LOD1_2024_PL: what server/polandlod1.py needs to know about the files
without having them (so a map cell can be classified, and the files it needs fetched, on demand).

    git clone https://github.com/maalos/LOD1_2024_PL
    python tools/build_polandlod1_index.py LOD1_2024_PL        # writes LOD1_2024_PL/index.npz
    # commit index.npz at the root of the repository (polandlod1 fetches .../refs/heads/master/index.npz)

The index holds every file's name, size, SHA-256 and building count, and every tile's rectangle (EPSG:2180 metres)
with the file and block it sits in. Rebuild it whenever a file in bld/ changes: polandlod1 checks each download
against it. (Bump polandlod1.VERSION too if the cells' buildings change for players who already have them cached.)
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import polandlod1  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clone", help="a clone of github.com/%s (its %s/ folder holds the files)"
                                  % (polandlod1.REPO, polandlod1.REMOTE_DIR))
    ap.add_argument("--out", help="default: <clone>/index.npz")
    a = ap.parse_args()
    bld = os.path.join(a.clone, polandlod1.REMOTE_DIR)
    paths = sorted(os.path.join(bld, n) for n in os.listdir(bld) if n.endswith(".bld"))
    if not paths:
        sys.exit(f"no .bld files in {bld}")
    out = a.out or os.path.join(a.clone, "index.npz")
    np.savez_compressed(out, **polandlod1.make_index(paths))
    m = polandlod1.Manifest(out)
    print(f"{len(m.names)} files, {len(m.x0)} tiles, {sum(m.buildings)} buildings, "
          f"{os.path.getsize(out) / 1024:.0f} KiB -> {out}")


if __name__ == "__main__":
    main()
