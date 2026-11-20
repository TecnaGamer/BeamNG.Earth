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
import re
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
    "far": {"far_tile", "far_field", "far_retex"},
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


def load(session_dir, place):
    """{ident: (last cleaned message, times sent)}, {type: count} of the order-dependent ones. The far field's swaps
    and removals are replayed into its end state, compared as ("far_field", anchor): the tiles shown at the end, every
    tile taken away, each terrain generation swap's last message."""
    got, nth, ordered, field = {}, {}, {}, {}
    import session
    summary_file = os.path.join(session_dir, "summary.json")
    summary = pcommon.read_json(summary_file) if os.path.isfile(summary_file) else {}
    count, last_file, done, progress = 0, 0., None, None
    with open(os.path.join(session_dir, "messages.jsonl"), encoding="utf-8") as f:
        for count, line in enumerate(f, 1):
            record = json.loads(line)
            if record.get("i") != count - 1:
                raise ValueError(f"missing or reordered message {count - 1} in {session_dir}")
            m = record["m"]
            if "settled" not in summary:
                # Old recordings have timestamps and a final duration but no explicit settled flag.
                if any(refs(m)):
                    last_file = record["t"]
                if m.get("anchorId") == summary.get("anchor"):
                    if m.get("type") == "far_done" and done is None:
                        done = record["t"]
                    if m.get("type") == "load_progress":
                        progress = m
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
    settled = summary.get("settled") is True
    if "settled" not in summary:
        elapsed = summary.get("seconds", 0)
        settle = pcommon.corpus()["places"].get(place, {}).get("settle_s", 20)
        settled = (summary.get("far_done") is True and done is not None and session.loaded(progress)
                   and elapsed - done >= settle + .051 and elapsed - last_file >= session.QUIET_S + .051)
    if not settled or count <= 0 or summary.get("messages") != count:
        raise ValueError(f"incomplete session, message count or settlement unverified: {session_dir}")
    return got, ordered


def refs(v):
    if isinstance(v, str) and v.startswith("@"):
        yield v[1:]
    elif isinstance(v, list):
        for x in v:
            yield from refs(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from refs(x)


def check_blob(directory, name, verified):
    """Verify each recorded file once per side, including files named inside JSON."""
    if name in verified:
        return
    if not re.fullmatch(r"[0-9a-f]{64}(?:\.[^/\\.]*)?", name):
        raise ValueError(f"invalid or missing blob reference: {name}")
    path = os.path.join(directory, "blobs", name)
    with open(path, "rb") as f:
        data = f.read()
    if pcommon.sha(data) != name.split(".")[0]:
        raise ValueError(f"blob content differs from its recorded digest: {name}")
    if name.endswith(".json"):
        try:
            content = json.loads(data)
        except (ValueError, UnicodeError):  # Recorder also preserves non-JSON bytes.
            content = None
        for nested in refs(content):
            check_blob(directory, nested, verified)
    verified.add(name)


def compare_message(ma, mb, da, db, verified):
    diffs = [f"{p}: {x} vs {y}" for p, x, y in pcommon.json_diff(ma, mb)]
    ra, rb = list(refs(ma)), list(refs(mb))
    for directory, names, seen in zip((da, db), (ra, rb), verified):
        try:
            for name in names:
                check_blob(directory, name, seen)
        except (OSError, ValueError) as exc:
            return diffs + [str(exc)]
    for x, y in zip(ra, rb):
        if x != y:
            diffs += pcommon.file_diff(os.path.join(da, "blobs", x), os.path.join(db, "blobs", y))[:3]
    return diffs


def compare(root_a, root_b, kind, places=None, show=20, what=None):
    """Compare the sessions of root_a and root_b for kind ("tiles", "blocks", "far" or None: every message)."""
    with pcommon.comparing(root_a, root_b):
        return _compare(root_a, root_b, kind, places, show, what)


def _compare(root_a, root_b, kind, places, show, what):
    rep = pcommon.Report(what or kind or "messages", show)
    names = pcommon.comparison_places(root_a, root_b, "sessions", places)
    for name in names:
        da, db = (os.path.join(r, "sessions", name) for r in (root_a, root_b))
        if not any(os.path.isfile(os.path.join(d, "messages.jsonl")) for d in (da, db)):
            rep.item(name, ["session missing on both sides"])
            continue
        if not (os.path.exists(os.path.join(da, "messages.jsonl")) and
                os.path.exists(os.path.join(db, "messages.jsonl"))):
            rep.only("A" if os.path.exists(os.path.join(da, "messages.jsonl")) else "B", [name + " (no session on the other side)"])
            continue
        try:
            (ga, oa), (gb, ob) = load(da, name), load(db, name)
        except (ValueError, OSError) as exc:
            rep.item(name, [str(exc)])
            continue
        verified = (set(), set())
        if kind is not None:
            ga = {k: v for k, v in ga.items() if k[0] in KINDS[kind]}
            gb = {k: v for k, v in gb.items() if k[0] in KINDS[kind]}
        elif oa != ob:
            print(f"NOTE {name}: far field bookkeeping counts {oa} vs {ob} (the order things came in)")
        if not ga and not gb:
            rep.item(name, ["no matching messages on either side"])
        for k in sorted(set(ga) & set(gb), key=repr):
            (ma, na), (mb, nb) = ga[k], gb[k]
            diffs = compare_message(ma, mb, da, db, verified)
            if na != nb and not diffs:
                print(f"NOTE {name} {k}: sent {na} vs {nb} times, the last the same")
            rep.item(f"{name} {k}", diffs)
        rep.only("A", [f"{name} {k}" for k in sorted(set(ga) - set(gb), key=repr)])
        rep.only("B", [f"{name} {k}" for k in sorted(set(gb) - set(ga), key=repr)])
    return rep.finish()


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
