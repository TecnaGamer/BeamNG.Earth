"""roadscan's build worker: a batch of geotiles built one after another in one process with tools/widecorpus build.py
of a checkout (TREE: this one or a git worktree, one code state), each tile in its own folder with its centre as the
build's anchor; a tile that fails leaves KEY.err and no folder, the others go on. Also the region mode's road list.

    python buildtiles.py TREE JOB.json
JOB: {"mode": "build", "cache": DIR, "out": DIR, "offline": bool, "tiles": [{"key", "lat", "lon"}]}
     {"mode": "region", "cache": DIR, "out": FILE.json, "box": [s, w, n, e], "classes": [..]}"""
import json
from pathlib import Path
import shutil
import sys
import traceback
import types


def _tree_imports(tree):
    here = Path(__file__).resolve().parent
    sys.path = [p for p in sys.path if p and Path(p).resolve() != here]
    sys.path[:0] = [str(tree / "tools" / "widecorpus"), str(tree / "server"), str(tree / "tools")]
    sys.modules.setdefault("pwd", types.ModuleType("pwd"))      # (build.py's command line only; Windows)


def build_tiles(tree, job):
    import build
    import netfetch
    if Path(build.__file__).resolve().parent != tree / "tools" / "widecorpus":
        raise SystemExit(f"build.py came from {build.__file__}, not {tree}")
    gate = netfetch.shared_gate         # (build.build wraps it on every call: restored before each)
    out = Path(job["out"])
    out.mkdir(parents=True, exist_ok=True)
    bad = 0
    for t in job["tiles"]:
        d = out / t["key"]
        if (d / "tiles.json").exists():
            continue
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        place = dict(id=t["key"].replace("_", "-").lower(), name=t["key"], lat=float(t["lat"]), lon=float(t["lon"]),
                     country=t.get("country"))
        netfetch.shared_gate = gate
        try:
            build.build(place, Path(job["cache"]), d, 1.0, bool(job["offline"]))
            (d / "place.json").write_text(json.dumps(place))
            (out / (t["key"] + ".err")).unlink(missing_ok=True)
            print("built", t["key"], flush=True)
        except Exception:
            bad += 1
            (out / (t["key"] + ".err")).write_text(traceback.format_exc())
            shutil.rmtree(d, ignore_errors=True)
            print("failed", t["key"], flush=True)
    return bad


def region_roads(job):
    """[{"cls", "width", "ll": [[lat, lon], ...]}] of the mapped roads of the classes in the box (every level)."""
    import logging
    import numpy as np
    import blockworker as bw
    import mapcells
    import roads
    bw.init(str(job["cache"]), False, True, True, True, True, None, logging.WARNING, False, True)
    s, w, n, e = job["box"]
    want = set(job["classes"])
    keys = mapcells.cells_for_bbox(s, w, n, e)
    out, seen = [], set()
    for cell in bw._s.md.cells(keys, layers=("roads",)):
        fs = cell.layers.get("roads")
        if fs is None or not len(fs):
            continue
        for i, spec in enumerate(roads.road_specs(fs)):
            fid = str(fs.ids[i])
            if spec is None or fid in seen or str(spec.cls) not in want:
                continue
            seen.add(fid)
            for level in ("ground", "bridge", "tunnel"):
                for ring in roads._parts_by_level_new(fs, i, level, rngs=roads.tag_level_ranges(fs, i)):
                    ll = np.column_stack((ring[:, 1], ring[:, 0]))
                    keep = (ll[:, 0] >= s) & (ll[:, 0] <= n) & (ll[:, 1] >= w) & (ll[:, 1] <= e)
                    if len(ll) >= 2 and keep.any():
                        out.append(dict(id=fid, cls=str(spec.cls), width=float(spec.width), ll=ll.round(7).tolist()))
    out.sort(key=lambda r: (r["id"], r["ll"][0]))
    Path(job["out"]).write_text(json.dumps(out))


if __name__ == "__main__":
    tree = Path(sys.argv[1]).resolve()
    job = json.loads(Path(sys.argv[2]).read_text())
    _tree_imports(tree)
    if job["mode"] == "region":
        region_roads(job)
    else:
        sys.exit(1 if build_tiles(tree, job) else 0)
