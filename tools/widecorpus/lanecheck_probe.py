"""lanecheck.py's tunnel probe: one offline build of a pin (lanecheck_build.py's) recording every tunnel part the road
tiles were given (roads._tunnel_chunks' input: fid, width, class, line and floor as geodetic lat/lon/h) and the twin
pairs (roads.twin_tunnels), as JSON.

    python lanecheck_probe.py TREE PLACE_JSON CACHE OUT OFFLINE(0|1) DUMP_JSON
"""
import json
from pathlib import Path
import sys
import types

if __name__ == "__main__":
    tree = Path(sys.argv[1]).resolve()
    here = Path(__file__).resolve().parent
    sys.path = [p for p in sys.path if p and Path(p).resolve() != here]
    sys.path[:0] = [str(tree / "tools" / "widecorpus"), str(tree / "server"), str(tree / "tools")]
    sys.modules.setdefault("pwd", types.ModuleType("pwd"))
    import numpy as np
    import build
    import roads
    if Path(build.__file__).resolve().parent != tree / "tools" / "widecorpus":
        raise SystemExit(f"build.py came from {build.__file__}, not {tree}")
    rec = {"parts": {}, "open": {}, "twins": [], "errors": []}
    orig = roads._tunnel_chunks

    def geo(fr, xy, z):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        la, lo, h = fr.enu_to_geodetic(xy[:, 0], xy[:, 1], np.asarray(z, np.float64))
        return np.c_[np.asarray(la), np.asarray(lo), np.asarray(h)].round(8).tolist()

    def probe(tunnels, cells, exact, *a, **k):
        try:
            fr = tunnels.frame
            parts = [p for p in tunnels.parts(cells, exact) if np.isfinite(p[3]).all()]
            for fid, spec, xy, z, _lon in parts:
                rec["parts"][str(fid)] = dict(width=float(spec.width), cls=str(spec.cls), ll=geo(fr, xy, z),
                                              h=float(tunnels.height(fid)) if getattr(tunnels, "height", None) else None,
                                              mouth=list(tunnels.mouth(fid)) if getattr(tunnels, "mouth", None) else None)
            if getattr(tunnels, "open_parts", None) is not None:
                for fid, spec, xy, z, _lon in tunnels.open_parts(cells, exact):
                    if np.isfinite(z).all():
                        rec["open"][str(fid)] = dict(width=float(spec.width), ll=geo(fr, xy, z))
            for tw in roads.twin_tunnels(parts):
                i, j, mid, w = (tw.i, tw.j, tw.mid, tw.width) if hasattr(tw, "i") else tuple(tw)[:4]
                mid = np.asarray(mid, np.float64)
                rec["twins"].append(dict(a=str(parts[i][0]), b=str(parts[j][0]), width=float(w),
                                         mid=geo(fr, mid[:, :2], mid[:, 2])))
        except Exception as e:      # (the probe never changes the build)
            rec["errors"].append(f"{type(e).__name__}: {e}")
        out = orig(tunnels, cells, exact, *a, **k)
        try:
            fr = tunnels.frame
            for ch in out:
                if not ch.get("tunnel"):
                    continue
                ln = np.asarray(ch.get("line", ch.get("pts", ch.get("n"))), np.float64)
                if ln.ndim != 2 or len(ln) < 2:
                    continue
                rec.setdefault("chunks", []).append(dict(
                    w=float(ch.get("w", ch.get("width", 0.0))), deck=bool(ch.get("deck")), no_tube=bool(ch.get("no_tube")),
                    tube_only=bool(ch.get("tube_only")), portal=ch.get("portal"), h=ch.get("tunnel_h"),
                    ll=geo(fr, ln[:, :2], ln[:, 2] if ln.shape[1] > 2 else np.zeros(len(ln)))))
        except Exception as e:
            rec["errors"].append(f"chunks {type(e).__name__}: {e}")
        return out
    roads._tunnel_chunks = probe
    if hasattr(roads, "bore_floors"):        # (the one-bore solve's calls: which parts, their lines and floors)
        orig_bf = roads.bore_floors

        def bf(lines, widths, floors, measured=None, ground=None, roads_=None, **k):
            new = orig_bf(lines, widths, floors, measured, ground, k.pop("roads", roads_))
            rec.setdefault("bore_calls", []).append(dict(
                roads=k.get("roads") or roads_, widths=[float(w) for w in widths],
                lines=[[[round(float(v), 3) for v in pt] for pt in ln] for ln in lines],
                floors=[[round(float(v), 3) for v in z] for z in floors],
                out=[[round(float(v), 3) for v in z] for z in new],
                measured=[None if m is None else [None if not (v == v) else round(float(v), 3) for v in m] for m in (measured or [None] * len(lines))]))
            return new
        roads.bore_floors = bf
    build.build(json.loads(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), 1.0, sys.argv[5] == "1")
    Path(sys.argv[6]).write_text(json.dumps(rec))
