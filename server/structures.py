"""Road revamp track RS, "one structure per level" (.claude/work/road-revamp-structures-design-2026-11-18.md): the glue
of the Rust kernels in native/earthfast/src/structures.

Step S1 (earthfast 480): the structure groups of a bridge chain (earthfast.structure_groups: union-find over the chain's
decks with the design's edge table, never across OSM levels, walkways, rail, tunnels; data conflicts recorded). With
BNE_DECK_STRUCT=shadow every bridge chain solved fresh (roads.BridgeProfiles._chain_solve: once per chain, in its owner
store's frame, from its canonical cells, CANON_LOAD) writes one JSON of its groups under the cache's debug/structures/
folder; nothing reads it. off (the default) and shadow build the same plans, facts and tiles: shadow reads nothing the
solve had not read (the chain's own pool and ground and the bridge outlines its structure already read), changes no
value the solve publishes and swallows its own errors.

The JSON (FORMAT): the chain's key, the store's frame, the members (key, class, level, kind, half width, length, group,
line as lat/lon and heights), the groups (members, the edges inside, a digest), every edge (its kind, |dz|, whether it
united two groups), the pairs over a height limit, the data conflicts (why they never join), the chain's digest. The
digests hold the member keys, edge kinds and |dz| in mm, and the in-chain conflicts: the same across start tiles, cell
orders, processes and platforms (tests/test_structures.py)."""
import hashlib
import json
import logging
import os
import re
import threading

import numpy as np

log = logging.getLogger("earthd.structures")

MODE = os.environ.get("BNE_DECK_STRUCT", "off")         # off / shadow (S1); on comes with S3
SHADOW = MODE == "shadow"
FORMAT = 1
EDGE_KINDS = ("JOIN", "END_ON", "OVERLAP_ALONG", "OVERLAP_CROSS", "BESIDE", "BESIDE_OUTLINE")
REASONS = ("level", "dz", "walk", "rail", "tunnel", "outside")
ROAD, WALK, RAIL, TUNNEL, OUTSIDE = 0, 1, 2, 3, 4
KIND_NAMES = ("road", "walk", "rail", "tunnel", "outside")
WALK_CLASSES = frozenset(("footway", "path", "cycleway", "pedestrian", "steps", "bridleway", "corridor", "track_foot"))
OUTSIDE_NEAR_M = 8.0    # m (+ the widest half width) round the chain's box: the pool's other parts offered as OUTSIDE
DEBUG_DIR = ("debug", "structures")


def groups(members, outlines=()):
    """The structure groups of members [{"key", "fid", "xy" (k, 2), "z" (k,), "half", "kind", "level"}] in their keys'
    order (earthfast.structure_groups); outlines: OSM bridge outline rings (k, 2) in the same frame. {"root": [index of
    each member's group root], "edges": [(a, b, kind, united, dz)], "rejected": [(a, b, kind, dz)], "conflicts":
    [(a, b, reason, kind, dz)]} (indices into members, kinds / reasons as numbers: EDGE_KINDS, REASONS)."""
    import fast
    fast.require(480, "structure_groups")
    pts, off = [], [0]
    for m in members:
        xy = np.asarray(m["xy"], np.float64).reshape(-1, 2)
        z = np.asarray(m["z"], np.float64).reshape(-1)
        pts.append(np.c_[xy, z])
        off.append(off[-1] + len(xy))
    fids = sorted({str(m["fid"]) for m in members})
    fid_of = {f: k for k, f in enumerate(fids)}
    rings = [np.asarray(o, np.float64).reshape(-1, 2) for o in outlines if len(o) >= 3]
    ooff = np.r_[0, np.cumsum([len(r) for r in rings])].astype(np.int64)
    root, edges, rejected, conflicts = fast._ef.structure_groups(
        np.ascontiguousarray(np.concatenate(pts) if pts else np.zeros((0, 3))), np.asarray(off, np.int64),
        np.asarray([float(m["half"]) for m in members], np.float64),
        np.asarray([int(m["kind"]) for m in members], np.int64),
        np.asarray([np.nan if m.get("level") is None else float(m["level"]) for m in members], np.float64),
        np.asarray([fid_of[str(m["fid"])] for m in members], np.int64),
        np.ascontiguousarray(np.concatenate(rings) if rings else np.zeros((0, 2))), ooff)
    return {"root": list(root), "edges": edges, "rejected": rejected, "conflicts": conflicts}


def _mm(v):
    return None if not np.isfinite(v) else int(round(float(v) * 1000.0))


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def document(members, got, n_chain, ll=None):
    """The shadow JSON's body of groups() got for members (the first n_chain the chain's own, the rest OUTSIDE).
    ll(xy (k, 2)) -> [[lat, lon]] for the members' lines (None: left out)."""
    keys = [m["key"] for m in members]
    root = got["root"]
    by = {}
    for i in range(n_chain):
        by.setdefault(root[i], []).append(i)
    edges = [{"a": keys[a], "b": keys[b], "kind": EDGE_KINDS[k], "dz": _mm(dz), "united": bool(u)}
             for a, b, k, u, dz in got["edges"]]
    out_groups = []
    for r in sorted(by):
        mem = by[r]
        inner = sorted([EDGE_KINDS[k], keys[a], keys[b], _mm(dz)] for a, b, k, _u, dz in got["edges"]
                       if root[a] == r)
        body = {"members": [keys[i] for i in mem], "edges": inner}
        out_groups.append({"id": keys[r], "n": len(mem), "members": body["members"], "edges": inner,
                           "classes": sorted({str(members[i]["cls"]) for i in mem}),
                           "levels": sorted({members[i]["level"] for i in mem if members[i].get("level") is not None}),
                           "digest": _digest(body)})
    conflicts = [{"a": keys[a], "b": keys[b], "reason": REASONS[r], "kind": EDGE_KINDS[k], "dz": _mm(dz)}
                 for a, b, r, k, dz in got["conflicts"]]
    rejected = [{"a": keys[a], "b": keys[b], "kind": EDGE_KINDS[k], "dz": _mm(dz)} for a, b, k, dz in got["rejected"]]
    own = sorted([c["reason"], c["kind"], c["a"], c["b"], c["dz"]] for c in conflicts if c["reason"] != "outside")
    mlist = []
    for i, m in enumerate(members):
        xy = np.asarray(m["xy"], np.float64).reshape(-1, 2)
        z = np.asarray(m["z"], np.float64).reshape(-1)
        d = np.diff(xy, axis=0)
        s = float(np.sqrt(d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1]).sum()) if len(xy) > 1 else 0.0
        item = {"key": keys[i], "cls": str(m["cls"]), "level": m.get("level"), "kind": KIND_NAMES[int(m["kind"])],
                "half": _mm(m["half"]), "len_m": round(s, 1), "group": keys[root[i]] if i < n_chain else None,
                "z_mm": [_mm(v) for v in z]}
        if ll is not None:
            item["ll"] = ll(xy)
        mlist.append(item)
    return {"format": FORMAT, "members": mlist, "groups": out_groups, "edges": edges, "rejected": rejected,
            "conflicts": conflicts,
            "digest": _digest({"groups": [g["digest"] for g in out_groups], "conflicts": own})}


def _outlines(frame, infra):
    """The OSM bridge outline rings (man_made=bridge: the infrastructure layer's bridge polygons) of the infrastructure
    Features infra in frame, each once (by its feature id), in a canonical order."""
    seen = {}
    for inf in infra or ():
        if inf is None or not len(inf):
            continue
        sub = inf.attrs.get("subtype")
        if sub is None:
            continue
        for i in range(len(inf.kind)):
            if inf.kind[i] != 3 or str(sub[i]) != "bridge":
                continue
            fid = str(inf.ids[i])
            if fid in seen:
                continue
            rings = []
            for part in inf.parts(i):
                ring = np.asarray(part[0], np.float64)
                if len(ring) >= 4:
                    e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                    rings.append(np.c_[np.asarray(e, np.float64), np.asarray(n, np.float64)])
            seen[fid] = rings
    return [r for f in sorted(seen) for r in seen[f]]


def _name(ck, block):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{ck[0]}_{ck[1]}")[:120]
    return s + (f"_w{block[0]}_{block[1]}" if block is not None else "") + ".json"


def _write(name, doc):
    """doc as the JSON name in the cache's debug/structures/ folder (written whole: a new file renamed into place)."""
    import revisit
    if revisit.CACHE_DIR is None:
        return None
    d = os.path.join(revisit.CACHE_DIR, *DEBUG_DIR)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, sort_keys=True, separators=(",", ":"), allow_nan=False)
        f.write("\n")
    os.replace(tmp, path)
    return path


def capped(prof, comp, block):
    """BNE_DECK_STRUCT=shadow, a window solve (a chain over roads.CHAIN_CELLS cells, solved per block's window): no
    groups (the legacy path in v1, the design's "Determinism / keys"), a marker naming the window's parts, written by
    every solve of that window (the same content whichever solves first). Never raises."""
    try:
        import revisit
        if revisit.CACHE_DIR is None or not comp:
            return None
        ck = min(comp)
        doc = {"format": FORMAT, "chain": f"{ck[0]}:{ck[1]}", "capped": [int(v) for v in block],
               "skipped": "window-capped chain", "parts": [f"{k[0]}:{k[1]}" for k in comp],
               "region": getattr(prof, "region", None)}
        return _write(_name(ck, block), doc)
    except Exception as ex:
        log.debug("structure groups (capped): %s: %s", type(ex).__name__, ex)
        return None


def shadow(prof, comp, pool, zs, infra):
    """BNE_DECK_STRUCT=shadow: chain comp's structure groups (its solved heights zs, comp's order) as a JSON in the
    cache's debug/structures/ folder. prof: the BridgeProfiles that solved it (its frame); pool: the solve's parts
    {key: (spec, xy, lon, level, ...)}; infra: the infrastructure Features its structure read (bridge outlines). A whole
    chain's solve only (capped: a window's). Never raises; reads nothing new."""
    try:
        import revisit
        if revisit.CACHE_DIR is None or not comp:
            return None
        members = []
        for k, z in zip(comp, zs):
            spec, xy = pool[k][0], np.asarray(pool[k][1], np.float64)[:, :2]
            z = np.asarray(z, np.float64).reshape(-1)
            if len(z) != len(xy):
                z = np.full(len(xy), np.nan)
            cls = str(getattr(spec, "cls", ""))
            members.append({"key": f"{k[0]}:{k[1]}", "fid": str(k[0]), "cls": cls, "xy": xy, "z": z,
                            "half": float(spec.width) / 2, "kind": WALK if cls in WALK_CLASSES else ROAD,
                            "level": float(pool[k][3])})
        n_chain = len(members)
        allp = np.concatenate([m["xy"] for m in members])
        grow = OUTSIDE_NEAR_M + max(float(pool[k][0].width) for k in pool) / 2
        lo, hi = allp.min(0) - grow, allp.max(0) + grow
        inside = set(comp)
        for k in sorted((k for k in pool if k not in inside), key=str):
            spec, xy = pool[k][0], np.asarray(pool[k][1], np.float64)[:, :2]
            if len(xy) < 2 or (xy.max(0) < lo).any() or (xy.min(0) > hi).any():
                continue
            members.append({"key": f"{k[0]}:{k[1]}", "fid": str(k[0]), "cls": str(getattr(spec, "cls", "")),
                            "xy": xy, "z": np.full(len(xy), np.nan), "half": float(spec.width) / 2, "kind": OUTSIDE,
                            "level": float(pool[k][3])})
        got = groups(members, _outlines(prof.frame, infra))
        frame = prof.frame

        def ll(xy):
            la, lo_, _ = frame.enu_to_geodetic(np.round(xy[:, 0], 3), np.round(xy[:, 1], 3), 0.0)
            return [[round(float(a), 7), round(float(b), 7)] for a, b in zip(la, lo_)]
        doc = document(members, got, n_chain, ll)
        ck = min(comp)
        doc.update({"chain": f"{ck[0]}:{ck[1]}", "capped": None,
                    "frame": [float(frame.lat0), float(frame.lon0)], "region": getattr(prof, "region", None)})
        return _write(_name(ck, None), doc)
    except Exception as ex:             # (a shadow: never in the way of the chain's solve)
        log.debug("structure groups (shadow): %s: %s", type(ex).__name__, ex)
        return None
