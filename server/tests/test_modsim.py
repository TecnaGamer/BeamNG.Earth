"""The Python model of the mod (modsim.py) against the mod's own Lua, under luajit:
  - governor.lua, follow.lua and rebase.lua on a deterministic pseudo-random schedule
    (lua/modsim_trace.lua), the tables they start with (the 1 km block, 1024 x 1 m), then (M4,
    docs/V1_4_SPEC.md §3.5) the 2 km ones after configure(1024, 2.0) (an old server's v1.3.1 block) and the
    1 km ones again after configure(1024, 1.0): fast mode, the commit-time re-check, the stale-anchor rule;
  - farfield.lua (with the offline engine stubs of mod/tests/stubs.lua and the real governor) on a
    scripted far-field session built with the server's own selection and swap grouping
    (lua/farfield_trace.lua): teleport, streaming, waiting swap groups, evictions, crossovers,
    terrain-follow commits with commit gating queries, an anchor switch that keeps the old tiles,
    the old anchor's far_removes and far_done, per-tile imagery materials (created, shared,
    deleted after their tiles), a switch to mesh near-visual mode and back, and a restarted server
    (a new session whose anchor ids start at 1 again while the old session's tiles are kept:
    heldSessions / heldAnchors);
  - edgeguard.lua step (lua/edgeguard_trace.lua) against modsim.EdgeGuard on random poses, speeds,
    heights and blocks (docs/ROADMAP.md "Robustness" 2).
Both must produce the same trace as the ports, frame by frame. Skipped without luajit."""
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import modsim  # noqa: E402
import farfield as ff  # noqa: E402

LUAJIT = shutil.which("luajit")
HERE = Path(__file__).resolve().parent
TRACE = HERE / "lua" / "modsim_trace.lua"
FAR_TRACE = HERE / "lua" / "farfield_trace.lua"
EDGE_TRACE = HERE / "lua" / "edgeguard_trace.lua"
MOD_TESTS = modsim.MOD_ROOT.parent / "tests"
FRAMES, SEED = 3000, 12345
IMPORT_MS, DELETE_MS, BASE_MS = 1.75, 0.25, 11.5      # as in lua/farfield_trace.lua (exact in binary)


class Rng:
    def __init__(self, seed):
        self.x = seed

    def __call__(self):
        self.x = (self.x * 16807) % 2147483647
        return self.x / 2147483647


def python_trace(frames, seed):
    rnd = Rng(seed)
    out = []
    g = modsim.Governor()
    wall, heavy_want, storm = None, False, False
    for fr in range(1, frames + 1):
        r1, r2, r3, r4 = rnd(), rnd(), rnd(), rnd()
        if r3 < 0.01:
            heavy_want = True
        if r3 > 0.995:
            storm = not storm
        g.loading = r4 < 0.05
        g.begin_frame(r1 < 0.8, wall)
        granted = False
        if heavy_want and g.request_heavy(r4 > 0.995):
            granted, heavy_want = True, False
        g.charge_motion(r2 * 0.1)
        units, spent = 0, r2 * 0.1
        for i in range(1, 13):
            kind = "reveal" if i % 3 == 1 else "cdae"
            if not g.try_(kind):
                break
            cost = 0.05 if kind == "reveal" else 1 + ((fr * 7 + i * 3) % 7) * 0.5
            g.charge(kind, cost)
            units, spent = units + 1, spent + cost
        wall = 11.5 + spent + (45 if granted else 0) + (8 if storm and fr % 2 == 0 else 0) + r2 * 3
        out.append(("G", fr, g.b, g.tokens, units, int(granted), g.base if g.base is not None else -1,
                     g.est["cdae"]))
    for i in range(1, frames + 1):
        px, py = (rnd() - 0.5) * 6000, (rnd() - 0.5) * 6000
        vx, vy = (rnd() - 0.5) * 80, (rnd() - 0.5) * 80
        cx, cy = modsim.snap(px + (rnd() - 0.5) * 1600), modsim.snap(py + (rnd() - 0.5) * 1600)
        kind, ready, gate_ok = rnd(), rnd() < 0.5, rnd() < 0.5
        pending = None
        if kind < 0.6:
            pending = {"cx": modsim.snap(px + (rnd() - 0.5) * 1600), "cy": modsim.snap(py + (rnd() - 0.5) * 1600),
                       "ready": ready}
        now, retry = rnd() * 10, rnd() * 10
        a = modsim.follow_decide(px, py, vx, vy, cx, cy, pending, now, retry, lambda *_: gate_ok)
        tgx, tgy = modsim.follow_target(px, py, vx, vy)
        dist, dd = rnd() * 18000, rnd()
        rp = None
        if dd < 0.3:
            rp = {"purpose": "rebase", "ready": dd < 0.2}
        elif dd < 0.4:
            rp = {"purpose": "resync", "ready": True}
        stale = rnd() * 900
        ra = modsim.rebase_decide(dist, rp, stale, modsim.REBASE["distance"], now, retry)
        name = a[0] if a else "nil"
        tx, ty = (a[1], a[2]) if a and a[0] == "prepare" else (0, 0)
        out.append(("F", i, name, tx, ty, tgx, tgy, ra or "nil"))
    # M4, by the block's width: the 2 km tables (1024 x 2 m: lattice.configure sets the session's square, here
    # modsim.LATTICE's), then the 1 km ones again (follow with its fast mode and re-check, the stale-anchor rule)
    for square in (2.0, 1.0):
        with mock.patch.dict(modsim.LATTICE, SQUARE=square):
            out += _m_trace(rnd, frames, int(1024 * square))
    return out


def _m_trace(rnd, frames, width):
    """The 'M' lines of one block (the session's square in modsim.LATTICE; width: the block's, m)."""
    out, size = [], 1024
    for i in range(1, frames + 1):
        px, py = (rnd() - 0.5) * 3000, (rnd() - 0.5) * 3000
        sp, hd = rnd() * 260, rnd() * 2 * math.pi
        vx, vy = sp * math.cos(hd), sp * math.sin(hd)
        cx, cy = modsim.snap(px + (rnd() - 0.5) * 900, size), modsim.snap(py + (rnd() - 0.5) * 900, size)
        kind, ready, gate_ok = rnd(), rnd() < 0.6, rnd() < 0.7
        pending = None
        if kind < 0.7:
            pending = {"cx": modsim.snap(px + (rnd() - 0.5) * 900, size),
                       "cy": modsim.snap(py + (rnd() - 0.5) * 900, size), "ready": ready}
        lr = rnd()
        latency = None if lr < 0.2 else lr * 2
        now, retry = rnd() * 10, rnd() * 10
        a = modsim.follow_decide(px, py, vx, vy, cx, cy, pending, now, retry, lambda *_: gate_ok, latency, size)
        tgx, tgy = modsim.follow_target(px, py, vx, vy, size)
        dist, dd = rnd() * 18000, rnd()
        rp = {"purpose": "rebase", "ready": dd < 0.4} if dd < 0.5 else None
        stale, er = rnd() * 600, rnd()
        edge = None if er < 0.1 else (er - 0.4) * 600
        ra = modsim.rebase_decide(dist, rp, stale, modsim.REBASE["distance"], now, retry, edge, size)
        lead = modsim.rebase_ahead_lead(None if lr < 0.5 else lr * 4)
        nx, ny = modsim.follow_centre_near(vx * lead, vy * lead, vx * (lead - 1), vy * (lead - 1),
                                           modsim.follow_mode(vx, vy, size)[1], size)
        tt = modsim.follow_time_to(px, py, vx, vy, cx, cy, modsim.FOLLOW["RECHECK_SAFE"])
        name = a[0] if a else "nil"
        tx, ty = (a[1], a[2]) if a and a[0] == "prepare" else (0, 0)
        out.append(("M", width, i, name, tx, ty, tgx, tgy, ra or "nil", lead, nx, ny,
                    "inf" if tt == math.inf else tt))
    return out


def python_edge_trace(steps, seed):
    """lua/edgeguard_trace.lua's schedule through modsim.EdgeGuard."""
    rnd = Rng(seed)
    g = modsim.EdgeGuard()
    clock = 0.0
    f = (lambda v: f"{v:.9f}")
    out = []
    for i in range(1, steps + 1):
        clock = clock + 0.05 + rnd() * 0.2
        size = 512 if rnd() < 0.5 else 1024
        cx, cy = math.floor((rnd() - 0.5) * 40) * 128, math.floor((rnd() - 0.5) * 40) * 128
        x0, y0 = cx - size, cy - size
        box = (x0, y0, x0 + (size - 1) * 2, y0 + (size - 1) * 2)
        side, along, off = math.floor(rnd() * 4), rnd(), (rnd() - 0.4) * 500
        if side == 0:
            px, py = box[2] - off, box[1] + along * (box[3] - box[1])
        elif side == 1:
            px, py = box[0] + off, box[1] + along * (box[3] - box[1])
        elif side == 2:
            px, py = box[0] + along * (box[2] - box[0]), box[3] - off
        else:
            px, py = box[0] + along * (box[2] - box[0]), box[1] + off
        vx, vy = (rnd() - 0.5) * 120, (rnd() - 0.5) * 120
        gr = rnd()
        ground = None
        if gr < 0.6:
            ground = rnd() * 3
        elif gr < 0.8:
            ground = 3 + rnd() * 6
        z = 1000 + (rnd() - 0.5) * 40
        mode, setv, rescue, _ = g.step(clock, px, py, z, vx, vy, box, ground)
        out.append(" ".join(str(v) for v in (
            "E", i, mode, f(setv[0]) if setv else "-", f(setv[1]) if setv else "-",
            f(rescue[0]) if rescue else "-", f(rescue[1]) if rescue else "-", f(rescue[2]) if rescue else "-",
            f(g.last_ground_z) if g.last_ground_z is not None else "-", g.holds, g.rescues)))
    return out


def far_script(seed, steps=70):
    """A far-field session as script lines (see lua/farfield_trace.lua), produced the way the
    server produces it: farfield.select around a moving player with the committed and pending
    blocks forced to L0, farfield.swap_groups against what was emitted, tiles before their group."""
    rnd = Rng(seed)
    out = []
    aid, gid = 1, 1
    block, pending = (0.0, 0.0), None
    px, py = 0.0, 0.0
    shown, refined = set(), set()
    old_keys = []

    def tile_line(t):
        x0, y0, _, _ = t.bbox
        sink = f"{0.3 + rnd() * 2:.3f}" if t.level == 0 else "-"
        mat = f"bngEarthSat_{aid}_{t.key}" if rnd() < 0.6 else "-"     # protocol 4 imagery or protocol 3
        # FORMAT 4 (M1): the texture size picks the governor kind (none: FORMAT 3, 'cdaeTex')
        tex = ("", " 128", " 256", " 512")[int(rnd() * 4)]
        return (f"T {aid} {t.key} {t.level} {x0:.1f} {y0:.1f} {(rnd() - 0.5) * 100:.1f} {rnd() * 3:.3f} {sink} "
                f"{mat}{tex}")

    out += [f"A {aid} 0 0 0 s1", "C 0 -8 3 0 1 0 60", "U 0 0 256 0"]   # nothing loaded yet: not ready
    for step in range(steps):
        out.append("H")
        px += 45.0
        py += 25.0 * math.sin(step / 7)
        out.append(f"C {px:.1f} {py - 8:.1f} 3 {0.3 * math.cos(step / 5):.3f} 1 0 60")
        if pending is None and modsim.cheb(px, py, *block) > 300:
            pending = modsim.follow_target(px, py, 30.0, 0.0)
            # asked before the pending block's L0 tiles are swapped in: usually not ready yet
            out.append(f"U {block[0]:.0f} {block[1]:.0f} {pending[0]:.0f} {pending[1]:.0f}")
        forced = [ff.Block(*b).bbox for b in (block, pending) if b is not None]
        desired, refined = ff.select(px, py, forced, refined, radius=12000.0)
        for adds, removes in ff.swap_groups(shown, desired):
            for k in adds:
                out.append(tile_line(ff.tile_of(k)))
            if rnd() < 0.15:                 # a tile sent, then evicted before any swap needs it
                out.append(f"T {aid} 0_{900 + step}_{900 + step} 0 {(900 + step) * 256}.0 230400.0 0 0.5 0.4")
                out.append(f"R {aid} 0_{900 + step}_{900 + step}")
            d = "- -" if rnd() < 0.3 else f"{rnd() * 4:.2f} {rnd() * 4:.2f}"
            out.append(f"S {aid} {gid} {d} {len(adds)} {' '.join(adds)} {len(removes)} {' '.join(removes)}"
                       .replace("  ", " ").rstrip())
            gid += 1
            shown = (shown - set(removes)) | set(adds)
            out += ["F"] * int(rnd() * 3)
        if old_keys and step % 3 == 0:     # the previous anchor's tiles, once covered
            for _ in range(min(len(old_keys), 40)):
                out.append(f"R {aid - 1} {old_keys.pop()}")
            if not old_keys:
                out.append(f"D {aid}")
        if step % 9 == 0:
            out.append(f"D {aid}")
        if step % 10 == 5:                  # a block far ahead: coarse tiles overlap it, not ready
            out.append(f"U {block[0]:.0f} {block[1]:.0f} {block[0] + 4096:.0f} {block[1]:.0f}")
        out += ["F"] * (2 + int(rnd() * 12))
        if pending is not None and modsim.cheb(px, py, *block) > 450:
            out.append(f"U {block[0]:.0f} {block[1]:.0f} {pending[0]:.0f} {pending[1]:.0f}")
            if rnd() < 0.8:
                out.append(f"B {pending[0]:.0f} {pending[1]:.0f}")
                block, pending = pending, None
        if step == 52:                      # the server says mesh near visual: nothing covered any more
            out.append("N mesh")
        if step == 62:
            out.append("N terrain")
        if step == 40:                      # anchor switch keeping the old tiles (rebase-like)
            old_keys = sorted(shown)
            aid += 1
            out.append(f"A {aid} 1 {block[0]:.0f} {block[1]:.0f} s1")
            shown, refined, pending = set(), set(), None
        if step == 60:                      # a restarted server: session s2, anchor ids from 1 again; the
            aid = 1                         # resync keeps every tile (no far_removes for them ever come)
            old_keys = []
            out.append(f"A {aid} 1 {block[0]:.0f} {block[1]:.0f} s2")
            out.append("H")
            shown, refined, pending = set(), set(), None
    # a burst of in-view replacements near the player, preloaded, then swapped all at once: more than
    # the transition cap, so near groups wait for a crossover slot
    x0b, y0b, x1b, y1b = ff.Block(*block).bbox
    burst = sorted((k for k in shown if ff.tile_of(k).level == 0), key=lambda k: abs(ff.tile_of(k).bbox[0] - px))
    burst = [k for k in burst if not (ff.tile_of(k).bbox[0] >= x0b and ff.tile_of(k).bbox[2] <= x1b
                                      and ff.tile_of(k).bbox[1] >= y0b and ff.tile_of(k).bbox[3] <= y1b)][:32]
    out += ["F"] * 400                      # let the session's own swaps drain first
    out.append(f"C {px - 3000:.1f} {py:.1f} 300 1 0 -0.1 60")
    for k in burst:
        out.append(tile_line(ff.tile_of(k)))
    out += ["F"] * 200
    for k in burst:
        out.append(f"S {aid} {gid} 1.00 1.00 1 {k} 1 {k}")
        gid += 1
    out += ["F"] * 400
    out.append("H")
    return out


def python_far_trace(lines, final=None):
    """The port's trace of a script; final: a dict that receives the FarField at the end."""
    far = modsim.FarField({"cdae": IMPORT_MS, "cdaeTex": IMPORT_MS, "material": IMPORT_MS, "dae": 8.0, "reveal": 0.0,
                           "delete": DELETE_MS, "matDelete": DELETE_MS, "motion": 0.0, "cdaeTex128": IMPORT_MS,
                           "cdaeTex256": IMPORT_MS, "cdaeTex512": IMPORT_MS}, lambda shape: __file__)
    gov = modsim.Governor()
    out, keys, cam, wall, frame = [], set(), None, None, 0
    folder = "x"

    def h(s, v):
        for ch in s.encode():
            v = (v * 31 + ch) % 2147483647
        return v
    for line in lines:
        w = line.split()
        c = w[0]
        num = (lambda v: None if v == "-" else float(v))
        if c == "A":
            far.switch_anchor(int(w[1]), w[2] == "1", (float(w[3]), float(w[4])), w[5] if len(w) > 5 else None)
            folder = w[5] if len(w) > 5 else folder
        elif c == "H":
            out.append("H " + ",".join(far.held_sessions()) + " " + ",".join(str(a) for a in far.held_anchors()))
        elif c == "B":
            far.on_block_commit(float(w[1]), float(w[2]))
        elif c == "C":
            cam = dict(zip(("x", "y", "z", "fx", "fy", "fz", "fovDeg"), (float(v) for v in w[1:8])))
        elif c == "T":
            keys.add(w[2])
            mat = w[9] if len(w) > 9 and w[9] != "-" else None
            far.on_tile({"type": "far_tile", "anchorId": int(w[1]), "key": w[2], "level": int(w[3]),
                         "shape": f"/temp/beamngEarth/{folder}/far_{w[2]}.cdae",
                         "pos": [float(w[4]), float(w[5]), float(w[6])],
                         "err": float(w[7]), "sink": num(w[8]), "state": "group", "material": mat,
                         "texture": f"/temp/beamngEarth/{folder}/far_{w[2]}.dds" if mat else None,
                         "detail": mat is not None,
                         "tex": num(w[10]) if len(w) > 10 else None})
        elif c == "S":
            i = 5
            n = int(w[i])
            add = w[i + 1:i + 1 + n]
            i += n + 1
            rem = w[i + 1:i + 1 + int(w[i])]
            far.on_swap({"type": "far_swap", "anchorId": int(w[1]), "id": int(w[2]), "add": add, "remove": rem,
                         "dIn": num(w[3]), "dOut": num(w[4])})
        elif c == "R":
            far.on_remove({"type": "far_remove", "anchorId": int(w[1]), "key": w[2]})
        elif c == "D":
            far.on_done({"type": "far_done", "anchorId": int(w[1]), "count": 0})
        elif c == "N":
            far.set_near_visual(w[1])
        elif c == "U":
            out.append("U " + ("true" if far.uncover_ready(*(float(v) for v in w[1:5])) else "false"))
        elif c == "F":
            frame += 1
            gov.begin_frame(far.has_work(), wall)
            far.update(gov, 1 / 60, cam)
            wall = BASE_MS + gov.frame_spent
            hv, nv = 7, 0
            for k in sorted(keys):
                inst = far.live.get(k)
                if inst is not None and inst.loaded and not far.hidden(inst):
                    hv, nv = h(k + ",", hv), nv + 1
            fr = far.frame
            covered = sum(1 for i in far.live.values() if i.cover != "none")
            st = [frame, f"{gov.b:.9f}", fr["units"], len(far.live), covered,
                  sum(1 for i in far.pending.values() if i.loaded), far.n_queued,
                  sum(1 for g in far.groups if g.state == "wait"), far.active_groups, len(far.old), len(far.doomed),
                  far.counters["imports"], far.counters["crossovers"], far.counters["instant"], nv, hv, len(far.mats)]
            out.append("F " + " ".join(str(v) for v in st))
    if final is not None:
        final["far"], final["gov"] = far, gov
    return out


@unittest.skipIf(LUAJIT is None, "luajit not installed")
class ModSimTest(unittest.TestCase):
    def test_ports_match_lua(self):
        res = subprocess.run([LUAJIT, str(TRACE), str(modsim.MOD_DIR.parent), str(FRAMES), str(SEED)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        lua = [ln.split() for ln in res.stdout.splitlines()]
        py = python_trace(FRAMES, SEED)
        self.assertEqual(len(lua), len(py))
        granted = units = 0
        for a, b in zip(lua, py):
            self.assertEqual(a[0], b[0])
            for u, v in zip(a[1:], b[1:]):
                if isinstance(v, str):
                    self.assertEqual(u, v, (a, b))
                else:
                    self.assertTrue(math.isclose(float(u), float(v), rel_tol=1e-9, abs_tol=1e-9), (a, b))
            if a[0] == "G":
                granted += int(a[5])
                units += int(a[4])
        # the schedule exercises the interesting paths
        self.assertGreater(granted, 10)
        self.assertGreater(units, 1000)
        self.assertGreater(sum(1 for b in py if b[0] == "F" and b[2] == "commit"), 20)
        self.assertGreater(sum(1 for b in py if b[0] == "F" and b[2] == "prepare"), 20)
        # (the 1 km block's stale distance: fewer of the schedule's prepared rebases are fresh than the 2 km one's)
        self.assertGreater(sum(1 for b in py if b[0] == "F" and b[7] == "commit"), 10)
        # M4: the 1 km table's paths, its fast mode and re-check, and the stale-anchor rule ran
        m1k = [b for b in py if b[0] == "M" and b[1] == 1024]
        for act in ("prepare", "commit", "discard"):
            self.assertGreater(sum(1 for b in m1k if b[3] == act), 20, act)
        self.assertGreater(sum(1 for b in m1k if b[8] == "commit"), 20)
        self.assertGreater(sum(1 for b in m1k if b[8] == "discard"), 20)
        self.assertNotEqual([b[3:9] for b in m1k], [b[3:9] for b in py if b[0] == "M" and b[1] == 2048])

    def test_edgeguard_port_matches_lua(self):
        res = subprocess.run([LUAJIT, str(EDGE_TRACE), str(modsim.MOD_DIR.parent), "4000", str(SEED)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        lua = res.stdout.splitlines()
        py = python_edge_trace(4000, SEED)
        self.assertEqual(len(lua), len(py))
        for i, (a, b) in enumerate(zip(lua, py)):
            self.assertEqual(a, b, f"line {i}: lua {a!r} python {b!r}")
        modes = [ln.split()[2] for ln in py]
        for m in ("clear", "warn", "hold", "air"):
            self.assertGreater(modes.count(m), 50, m)
        last = py[-1].split()
        self.assertGreater(int(last[-2]), 50)           # guard pauses
        self.assertGreater(int(last[-1]), 50)           # rescues (rate-limited)
        self.assertGreater(sum(1 for ln in py if ln.split()[3] == "-" and ln.split()[2] == "hold"), 50)

    def test_farfield_port_matches_lua(self):
        lines = far_script(7)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "far.script")
            with open(path, "w") as f:
                f.write("\n".join(lines) + "\n")
            res = subprocess.run([LUAJIT, str(FAR_TRACE), str(modsim.MOD_DIR.parent), str(MOD_TESTS), path],
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        lua = res.stdout.splitlines()
        final = {}
        py = python_far_trace(lines, final)
        self.assertEqual(len(lua), len(py))
        for i, (a, b) in enumerate(zip(lua, py)):
            self.assertEqual(a, b, f"line {i}: lua {a!r} python {b!r}")
        # the session exercises what matters: imports, crossovers and instant swaps, covered tiles,
        # waiting groups, an old anchor, gating answers both ways, deletes
        f = [ln.split() for ln in py if ln[0] == "F"]
        col = lambda n: [int(r[n]) for r in f]  # noqa: E731
        # columns: F frame b units live covered preloaded queued groups transitions old deleting
        #          imports crossovers instant visible hash materials
        self.assertGreater(max(col(12)), 300)       # imports
        self.assertGreater(max(col(13)), 5)         # crossovers
        self.assertGreater(max(col(14)), 5)         # instant swaps
        self.assertGreater(max(col(5)), 12)         # covered tiles (a 1 km block covers 4 x 4 L0 tiles)
        self.assertGreater(max(col(8)), 0)          # waiting groups
        self.assertGreater(max(col(9)), 0)          # running transitions
        self.assertGreater(max(col(10)), 50)        # old-anchor tiles
        self.assertGreater(max(col(11)), 0)         # deletes queued
        self.assertEqual(col(10)[-1], 0)            # the old anchor went
        self.assertGreater(max(col(17)), 20)        # per-tile materials live
        self.assertLess(col(17)[-1], max(col(17)))  # ... and deleted with their tiles
        far = final["far"]
        self.assertGreater(far.counters["mat_deletes"], 0)
        self.assertGreater(far.counters["textured"], far.counters["materials"] * 0.9)   # shared by re-sent keys
        self.assertEqual(far.near_visual, "terrain")                                      # mesh, then back
        # FORMAT 4 tiles (M1): every textured kind ran, each against its own estimate
        self.assertLessEqual({"cdaeTex", "cdaeTex128", "cdaeTex256", "cdaeTex512"}, final["gov"].used)
        self.assertGreater(far.counters["cap_waits"], 0)                                  # the burst hit the cap
        self.assertGreater(far.counters["old_covered"], 0)      # mesh mode dropped covered old-anchor tiles
        self.assertEqual(max(col(9)), modsim.XO["MAX_GROUPS"])                            # ... never above it
        u = [ln for ln in py if ln[0] == "U"]
        self.assertIn("U true", u)
        self.assertIn("U false", u)
        # the restarted server (session s2, anchor ids from 1 again): the old session's folder is held
        # while its tiles are, never its anchor numbers
        h = [ln.split(" ") for ln in py if ln[0] == "H"]
        self.assertIn(["H", "s1", "1"], h)                          # after the rebase: s1's anchor 1 held
        both = [r for r in h if r[1] == "s1,s2"]
        self.assertTrue(both, "the old session held next to the new one")
        self.assertTrue(all("1" not in r[2].split(",") and "2" not in r[2].split(",") for r in both), both[:3])
        # at the end no s1 tile object is left; s1 stays held only through materials an s2 tile of the same
        # name (anchor 1 again, same key) still uses: satmat keeps a material's texture while it is in use
        objs = list(far.live.values()) + list(far.old.values()) + list(far.pending.values()) + far.doomed
        self.assertFalse([i.key for i in objs if i.loaded and modsim.folder_of(i.shape) == "s1"])
        s1 = [n for n, t in far.mat_tex.items() if modsim.folder_of(t) == "s1"]
        self.assertTrue(all(far.mats[n] > 0 for n in s1), s1[:5])


if __name__ == "__main__":
    unittest.main()
