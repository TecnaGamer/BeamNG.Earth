"""Comparing two recorded sessions (session.py's OUT/sessions/<place>): the shared part of tilecmp, blkcmp, farcmp and
msgcmp.

A session's messages are grouped by what they are about (a far tile by its key, a terrain block by its centre, a road
geotile by its key and type; the rest by type and order), and each group's last message (the state the game ends
with) is compared with the other side's: its fields as JSON values (floats exactly), the files it names by content
(the recorder replaced each path by "@<sha256><ext>"; files that differ are compared as JSON values, PNG pixels or
bytes). Fields that only say when or in which order something happened (a pooled shape's send serial, a swap's
counter, timings, the session's id) are left out, and so are message types that are pure progress (status, load and
anchor progress, memory, cache usage, performance)."""
import json
import os
import sys

import pcommon

# message types that report progress or timing only
PROGRESS = {"status", "anchor_progress", "load_progress", "memory", "cache", "catchup", "runtime_profiler",
            "runtime_profiler_pong", "route_download", "areas", "update", "update_status", "far_done"}
# fields that say when / in which order, not what (per type; "*": every type)
VOLATILE = {"*": {"slotSerial"}, "hello": {"sessionId", "serverVersion", "appVersion", "startSettings", "restartable",
                                           "startFixed", "memoryTier", "memoryNote", "leaseId", "mapPort"},
            "far_swap": {"id"}}
# what each comparison looks at
KINDS = {
    "tiles": {"roads_tile", "roads_skin", "roads_exact", "roads_drop", "veg_tile", "veg_drop", "traffic_tile",
              "traffic_drop", "traffic_far_tile", "traffic_far_drop", "turbines_tile", "turbines_drop",
              "bldfar_tile", "bldfar_drop", "bldsky_tile", "bldsky_drop", "brfar_tile", "brfar_drop", "bldpack_tile",
              "bldpack_drop", "bldhz_tile", "bldhz_drop", "navgraph", "deck_spans", "sea", "tunnel_floor"},
    "blocks": {"terrain_ready", "terrain_refresh", "block_base", "terrain_patch", "ground_ahead", "anchor_ready"},
    "far": {"far_tile", "far_field"},
}
# messages sent again as the session goes (how often is timing): only the last one compared
LATEST = {"ground_ahead", "credits", "weather", "gravity", "sea", "terrain_ready_set"}
# the order-dependent far field bookkeeping: its sets compared, not its messages (msgcmp)
ORDERED = {"far_swap", "far_remove", "swap_begin", "swap_end", "swap_abort"}


def ident(m, nth):
    """What a message is about: messages with the same ident replace each other."""
    t = m.get("type")
    if "key" in m:
        return (t, m["key"])
    if t in ("terrain_ready", "terrain_refresh", "block_base"):
        return (t, m.get("cx"), m.get("cy"))
    if t == "deck_spans":
        return (t, m.get("cx"), m.get("cy"))
    if t == "navgraph":
        return (t, m.get("cx"), m.get("cy"))
    if t in LATEST:
        return (t,)
    return (t, nth)


def clean(m):
    t = m.get("type")
    drop = VOLATILE["*"] | VOLATILE.get(t, set())
    out = {}
    for k, v in m.items():
        if k in drop:
            continue
        if k == "slots" and isinstance(v, dict):        # {what: [shape, serial]}: the serial is the send order's
            v = {w: (s[:1] if isinstance(s, list) else s) for w, s in v.items()}
        out[k] = v
    return out


def load(session_dir):
    """{ident: (last cleaned message, times sent)}, {type: count} of the order-dependent ones. The far field's swaps
    and removals are replayed into its end state, compared as ("far_field", anchor): the tiles shown at the end, every
    tile taken away, each terrain generation swap's last message."""
    got, nth, ordered, field = {}, {}, {}, {}
    with open(os.path.join(session_dir, "messages.jsonl"), encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)["m"]
            t = m.get("type")
            if t in PROGRESS:
                continue
            if t in ORDERED:
                ordered[t] = ordered.get(t, 0) + 1
                st = field.setdefault(m.get("anchorId"), {"shown": set(), "removed": set(), "swaps": {}})
                if t == "far_swap":
                    st["shown"].difference_update(m.get("remove") or [])
                    st["removed"].update(m.get("remove") or [])
                    st["shown"].update(m.get("add") or [])
                elif t == "far_remove":
                    st["shown"].discard(m.get("key"))
                    st["removed"].add(m.get("key"))
                else:           # swap_begin / swap_end / swap_abort: each generation's last word
                    st["swaps"][str(m.get("gen"))] = clean(m)
                continue
            n = nth.get(t, 0)
            nth[t] = n + 1
            k = ident(m, n)
            prev = got.get(k)
            got[k] = (clean(m), (prev[1] if prev else 0) + 1)
    for aid, st in field.items():
        got[("far_field", aid)] = ({"shown": sorted(map(str, st["shown"])), "removed": sorted(map(str, st["removed"])),
                                    "swaps": st["swaps"]}, 1)
    return got, ordered


def refs(v, out):
    if isinstance(v, str) and v.startswith("@") and not v.startswith("@missing:"):
        out.append(v[1:])
    elif isinstance(v, list):
        for x in v:
            refs(x, out)
    elif isinstance(v, dict):
        for x in v.values():
            refs(x, out)
    return out


def compare_message(ma, mb, da, db):
    """Differences of two cleaned messages; named files that differ compared by content (their names masked)."""
    ra, rb = refs(ma, []), refs(mb, [])
    diffs = []
    if len(ra) == len(rb):
        for x, y in zip(ra, rb):
            if x != y:
                pa, pb = os.path.join(da, "blobs", x), os.path.join(db, "blobs", y)
                if os.path.exists(pa) and os.path.exists(pb):
                    sub = pcommon.file_diff(pa, pb)
                    diffs += [f"file {os.path.splitext(x)[1]}: {d}" for d in sub[:3]]
                else:
                    diffs.append(f"file {x} / {y} not recorded")
        ma, mb = _mask(ma), _mask(mb)
    diffs += [f"{p}: {x} vs {y}" for p, x, y in pcommon.json_diff(ma, mb)]
    return diffs


def _mask(v):
    if isinstance(v, str) and v.startswith("@") and not v.startswith("@missing:"):
        return "@file" + os.path.splitext(v)[1]
    if isinstance(v, list):
        return [_mask(x) for x in v]
    if isinstance(v, dict):
        return {k: _mask(x) for k, x in v.items()}
    return v


def compare(root_a, root_b, kind, places=None, show=20, what=None):
    """Compare the sessions of root_a and root_b for kind ("tiles", "blocks", "far" or None: every message)."""
    rep = pcommon.Report(what or kind or "messages", show)
    names = places or sorted({n for r in (root_a, root_b) for n in _ls(os.path.join(r, "sessions"))})
    for name in names:
        da, db = (os.path.join(r, "sessions", name) for r in (root_a, root_b))
        if not (os.path.exists(os.path.join(da, "messages.jsonl")) and
                os.path.exists(os.path.join(db, "messages.jsonl"))):
            rep.only("A" if os.path.exists(da) else "B", [name + " (no session on the other side)"])
            continue
        (ga, oa), (gb, ob) = load(da), load(db)
        if kind is not None:
            ga = {k: v for k, v in ga.items() if k[0] in KINDS[kind]}
            gb = {k: v for k, v in gb.items() if k[0] in KINDS[kind]}
        elif oa != ob:
            print(f"NOTE {name}: far field bookkeeping counts {oa} vs {ob} (the order things came in)")
        for k in sorted(set(ga) & set(gb), key=repr):
            (ma, na), (mb, nb) = ga[k], gb[k]
            diffs = compare_message(ma, mb, da, db)
            if na != nb and not diffs:
                print(f"NOTE {name} {k}: sent {na} vs {nb} times, the last the same")
            rep.item(f"{name} {k}", diffs)
        rep.only("A", [f"{name} {k}" for k in sorted(set(ga) - set(gb), key=repr)])
        rep.only("B", [f"{name} {k}" for k in sorted(set(gb) - set(ga), key=repr)])
    return rep.finish()


def _ls(d):
    return os.listdir(d) if os.path.isdir(d) else []


def main(kind, doc, argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--places", default="")
    ap.add_argument("--show", type=int, default=20)
    a = ap.parse_args(argv)
    return compare(a.a, a.b, kind, [n for n in a.places.split(",") if n] or None, a.show)


if __name__ == "__main__":
    sys.exit(main(None, __doc__))
