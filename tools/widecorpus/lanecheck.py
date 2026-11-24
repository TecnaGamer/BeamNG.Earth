"""Lane check: can a vehicle drive every lane of a place, judged on the generated map alone (no game, no physics, no
car). The wide corpus's production builds (build.py: blockworker.roads_geo's meshes, blockworker.build's physics
terrain) and the M0 audit (native/roadsys audit.rs: envelope intrusions, double owners, holes, hand-over lips and
in-lane steps on 0.5 m lane cells), on every geotile a site's roads cross, for several code states (git worktrees)
built offline from one snapshot of inputs, so that only the code differs. On Windows too (run.py is Linux-only).

Added to the audit's lanes: a between-lanes gauge along every two tunnel lines side by side (a pair whose tubes would
overlap: one bore), spanning centre line to centre line, so a step between two carriageways lies inside a lane. Added
to its measurements: every flagged cell placed along the site's axis (s, m) and across it, and the drivable floor of
the tunnel lanes against measured tunnel lines (swissTLM3D cells: the truth).

    python tools/widecorpus/lanecheck.py tiles  SITE
    python tools/widecorpus/lanecheck.py warm   SITE --tree WORKTREE
    python tools/widecorpus/lanecheck.py build  SITE --state NAME --tree WORKTREE
    python tools/widecorpus/lanecheck.py probe  SITE --state NAME --tree WORKTREE
    python tools/widecorpus/lanecheck.py measure SITE --state NAME
    python tools/widecorpus/lanecheck.py report SITE [--state NAME ...]

SITE: a JSON file {"id", "country", "lines": [[[lat, lon], ...], ...] (the roads whose tiles to build),
"axis": [[lat, lon(, h)], ...] (where s runs; with heights: the measured line the floors are compared with),
"lanes": [{"id", "width", "xy": [[lat, lon], ...], "level"}] (optional: the place's own carriageways, as mapped, audited
whatever the road code makes of them, with a gauge between two of them one bore apart), "tlm": a folder of swissTLM3D
road-height cells made by tools/build_swisstlm.py (optional; or LANECHECK_TLM; copied into the site's folder and read through
BNE_ROADZ_TLM_DIR)};
the id is lower-case letters, digits, _ and -. tools/widecorpus/sites/milchbuck.json is one. Work under ~/bnge-tests/lanecheck/<id>/. LANECHECK_AUDITOR:
another roadsys binary than bin/roadsys-target/release's.
"""
import argparse
from collections import defaultdict
import json
import re
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1]
sys.path[:0] = [str(TREE / "server"), str(HERE)]
import geodesy  # noqa: E402
import geotiles  # noqa: E402

ROOT = Path.home() / "bnge-tests" / "lanecheck"
RAW = ("dem", "mapdata", "pagesdata", "roadz", "gravity", "cdn", "airports")
AUDITOR = Path(os.environ.get("LANECHECK_AUDITOR") or
               TREE / "bin" / "roadsys-target" / "release" / ("roadsys.exe" if os.name == "nt" else "roadsys"))
PAIR_MAX_M = 15.0       # m between two tunnel centre lines for a between-lanes gauge (their tubes would overlap)
PAIR_COS = 0.85
PAIR_CHUNK_M = 20.0     # m: a gauge's pieces, each as wide as its narrowest spacing
SITE_PAIR_MAX_M = 10.0  # m: the site's own carriageways this close (centre to centre) are one bore: gauged between
TRUTH_NEAR_M = 6.0      # m from a measured tunnel line for a lane cell to be compared with it


def site_of(path):
    s = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(s.get("id"), str) or not re.fullmatch(r"[a-z0-9_-]+", s["id"]):
        raise ValueError(f"site id {s.get('id')!r}: lower-case letters, digits, _ and - only")
    s["dir"] = ROOT / s["id"]
    return s


def densify_ll(line, step):
    ll = np.asarray(line, np.float64)
    out = [ll[0]]
    for a, b in zip(ll[:-1], ll[1:]):
        d = math.hypot((b[0] - a[0]) * 111320.0, (b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0])))
        k = max(1, int(d / step))
        for t in np.linspace(0.0, 1.0, k + 1)[1:]:
            out.append(a + t * (b - a))
    return np.asarray(out)


def tiles(site):
    """{key: (gx, gy)} of the geotiles the site's lines cross."""
    got = {}
    for line in site["lines"]:
        for la, lo in densify_ll(line, 10.0)[:, :2]:
            fr = geodesy.EnuFrame(float(la), float(lo))
            for gx, gy, _m in geotiles.tiles_near(fr, 0.0, 0.0, 0.5, exact=True):
                got[geotiles.key(gx, gy)] = (gx, gy)
    return dict(sorted(got.items()))


def pins(site):
    """One place per tile, at its centre (radius 1 m: that tile alone)."""
    out = []
    for key, (gx, gy) in tiles(site).items():
        la, lo = geotiles.centre(gx, gy)
        out.append(dict(id=key.replace("/", "-").replace("_", "-").lower(), key=key, lat=float(la), lon=float(lo),
                        country=site.get("country"), name=f"{site['id']} {key}"))
    return out


def run_build(tree, place, cache, out, offline, log, probe=None, tlm=None):
    out.mkdir(parents=True)
    runtime = out.parent / ("rt-" + out.name)
    temp = runtime / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BNE_", "BNGE_"))}
    env.update(HOME=str(runtime), XDG_CACHE_HOME=str(runtime / "xdg"), TMPDIR=str(temp), TEMP=str(temp),
               TMP=str(temp), PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", RAYON_NUM_THREADS="1", BNGE_NET_SHARED=str(ROOT / "netgate"),
               BNE_REVISIT="off", BNGE_PLAN_RUN="wide")
    if tlm is not None:             # (the site's swissTLM3D cells: roadz's "tlm" source, its plans tagged)
        env["BNE_ROADZ_TLM_DIR"] = str(tlm)
    cmd = [sys.executable, str(HERE / "lanecheck_build.py"), str(Path(tree).resolve()), json.dumps(place), str(cache),
           str(out), "1", "1" if offline else "0"]
    if probe is not None:           # (lanecheck_probe.py: the same build, its tunnel parts recorded)
        cmd = [sys.executable, str(HERE / "lanecheck_probe.py"), str(Path(tree).resolve()), json.dumps(place),
               str(cache), str(out), "1" if offline else "0", str(probe)]
    with open(log, "w") as f:
        r = subprocess.run(cmd, env=env, stdout=f, stderr=subprocess.STDOUT, cwd=runtime)
    if r.returncode:
        raise RuntimeError(f"build of {place['id']} exited {r.returncode}: see {log}")


def site_tlm(site):
    """The site's copy of its swissTLM3D cells (warm), or None."""
    d = site["dir"] / "tlm"
    return d if d.is_dir() else None


def warm(site, tree):
    """Every pin built online once into one snapshot of inputs (seed); only the raw input folders kept."""
    d = site["dir"]
    seed = d / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    tlm = site.get("tlm") or os.environ.get("LANECHECK_TLM")
    if tlm and not (d / "tlm").exists():
        shutil.copytree(Path(tlm).expanduser(), d / "tlm")   # (a copy: never a link into another cache)
    work = d / "warm"
    for p in pins(site):
        if (work / p["id"]).exists():
            continue
        print("warm", p["id"], flush=True)
        run_build(tree, p, seed, work / p["id"], False, d / f"warm-{p['id']}.log", tlm=site_tlm(site))
    for item in seed.iterdir():
        if item.is_dir() and item.name not in RAW:
            shutil.rmtree(item)
    (d / "seed.json").write_text(json.dumps({"pins": [p["id"] for p in pins(site)]}))


def build_state(site, state, tree):
    d = site["dir"]
    cache = d / f"cache-{state}"
    if not cache.exists():
        cache.mkdir(parents=True)
        for part in RAW:
            if (d / "seed" / part).is_dir():
                shutil.copytree(d / "seed" / part, cache / part)
    for p in pins(site):
        out = d / f"tiles-{state}" / p["id"]
        if (out / "tiles.json").exists():
            continue
        if out.exists():
            shutil.rmtree(out)
        print(state, "build", p["id"], flush=True)
        run_build(tree, p, cache, out, True, d / f"build-{state}-{p['id']}.log", tlm=site_tlm(site))


def probe_state(site, state, tree):
    """The tunnel parts and twin pairs every pin's road tiles were given (lanecheck_probe.py), offline from the state's
    inputs, merged by part id into probe-<state>.json: {"parts": {fid: {width, cls, ll: [[lat, lon, h]], h, mouth}},
    "open": {...}, "twins": [{a, b, width, mid}]}."""
    d = site["dir"]
    cache = d / f"cache-{state}"
    work = d / f"probe-{state}"
    merged = {"parts": {}, "open": {}, "twins": {}, "errors": []}
    for p in pins(site):
        rec = work / (p["id"] + ".json")
        if not rec.exists():
            out = work / p["id"]
            if out.exists():
                shutil.rmtree(out)
            print(state, "probe", p["id"], flush=True)
            run_build(tree, p, cache, out, True, work / f"probe-{p['id']}.log", probe=rec, tlm=site_tlm(site))
        got = json.loads(rec.read_text())
        merged["parts"].update(got["parts"])
        merged["open"].update(got["open"])
        for t in got["twins"]:
            merged["twins"][t["a"] + "|" + t["b"]] = t
        merged["errors"] += got["errors"]
    merged["twins"] = list(merged["twins"].values())
    (d / f"probe-{state}.json").write_text(json.dumps(merged))
    return merged


# -- measuring ---------------------------------------------------------------------------------------------------

def _seg_near(P, L):
    """Per point of P (k, 2): (distance to polyline L, nearest point, unit direction of L's segment there)."""
    a, b = L[:-1], L[1:]
    dd = b - a
    l2 = np.maximum((dd * dd).sum(1), 1e-12)
    out_d, out_q, out_t = np.empty(len(P)), np.empty((len(P), 2)), np.empty((len(P), 2))
    for i, p in enumerate(P):
        t = np.clip(((p - a) * dd).sum(1) / l2, 0.0, 1.0)
        q = a + t[:, None] * dd
        dist = np.hypot(*(q - p).T)
        j = int(np.argmin(dist))
        out_d[i], out_q[i] = dist[j], q[j]
        out_t[i] = dd[j] / math.sqrt(l2[j])
    return out_d, out_q, out_t


def _densify_xy(xy, step):
    xy = np.asarray(xy, np.float64)
    out = [xy[0]]
    for a, b in zip(xy[:-1], xy[1:]):
        k = max(1, int(math.hypot(*(b - a)) / step))
        for t in np.linspace(0.0, 1.0, k + 1)[1:]:
            out.append(a + t * (b - a))
    return np.asarray(out)


def pair_lanes(lines, max_m=None):
    """Between-lanes gauges: for every two tunnel lines side by side (1 m < spacing < PAIR_MAX_M, near parallel), a
    line along their midline spanning centre line to centre line, in PAIR_CHUNK_M pieces each as wide as its
    narrowest spacing (+1 m: the audit's lane interior is the half width less 0.5 m)."""
    tun = [l for l in lines if l.get("level") == "tunnel" and len(l["xy"]) >= 2]
    out = []
    for i, a in enumerate(tun):
        A = _densify_xy(a["xy"], 1.0)
        ta = np.gradient(A, axis=0)
        ta = ta / np.maximum(np.hypot(*ta.T), 1e-9)[:, None]
        for b in tun[i + 1:]:
            if a["id"] == b["id"]:
                continue
            d, q, tb = _seg_near(A, np.asarray(b["xy"], np.float64))
            ok = (d > 1.0) & (d < (PAIR_MAX_M if max_m is None else max_m)) & (np.abs((ta * tb).sum(1)) >= PAIR_COS)
            mid = (A + q) / 2
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(A, axis=0).T))]
            k = 0
            while k < len(A):
                if not ok[k]:
                    k += 1
                    continue
                j = k
                while j + 1 < len(A) and ok[j + 1] and s[j + 1] - s[k] <= PAIR_CHUNK_M:
                    j += 1
                if j > k:
                    out.append(dict(id=f"pair:{a['id']}|{b['id']}", level="tunnel", width=float(d[k:j + 1].min()) + 1.0,
                                    lanes=1, paved=True, xy=mid[k:j + 1].tolist(), pair=True))
                k = j if j > k else k + 1
    return out


def site_lanes(site, frame):
    """The site's own carriageways (site["lanes"]: the real ones, as mapped, whatever the road code makes of them) as
    audit lines in frame's coordinates, and the between-lanes gauges between them."""
    out = []
    for lane in site.get("lanes", ()):
        ll = np.asarray(lane["xy"], np.float64)
        x, y, _ = frame.geodetic_to_enu(ll[:, 0], ll[:, 1], 0.0)
        out.append(dict(id="site:" + lane["id"], level=lane.get("level", "tunnel"), width=float(lane["width"]),
                        lanes=1, paved=True, xy=np.c_[x, y].tolist(), site=True))
    return out + pair_lanes(out, SITE_PAIR_MAX_M)


def neighbour_entries(tiles_dir, pin, built, frame):
    """The audit entries (checks.measure's: kind, collides, [(flags, triangles)]) of the decks and tunnel tubes of the
    built tiles next to pin's (the 8 around it), in frame."""
    import checks
    import roadmesh
    _z, gx, gy = geotiles.parse(pin["key"])
    out = []
    for q in built.values():
        _zq, qx, qy = geotiles.parse(q["key"])
        if q["id"] == pin["id"] or abs(qx - gx) > 1 or abs(qy - gy) > 1:
            continue
        qdir = tiles_dir / q["id"]
        data = checks.read(qdir / f"rg_{q['key']}.json")
        layers = checks.read(qdir / (q["key"] + ".source.json")).get("layers", {})
        src = geotiles.frame(*geotiles.parse(q["key"])[1:])
        for what in ("deck", "tunnel"):
            entry = data.get(what)
            if not isinstance(entry, dict) or "shape" not in entry:
                continue
            audited = []
            for mat, tri in checks.groups(qdir, entry, frame, src, None):
                audited.append(((1 if mat == roadmesh.DECK_TOP else 0) | (2 if mat == roadmesh.DECK_SIDE else 0) |
                                (4 if mat == "curbs" else 0) |
                                (8 if layers.get(mat) == "mark" or mat.startswith(("line_", "crossing")) else 0), tri))
            out.append((checks.audit_kind(what), True, audited))
    return out


def measure_state(site, state):
    """checks.measure on every pin's tile, the C++ scanner left out (collision snags and visual overlap are not this
    question), the audit given the site's carriageways and the between-lanes gauges, and asked for its flagged and
    sampled cells."""
    import checks
    d = site["dir"]
    noscan = d / ("noscan.bat" if os.name == "nt" else "noscan.sh")
    if not noscan.exists():
        noscan.write_text("@exit /b 0\r\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n")
        if os.name != "nt":
            noscan.chmod(0o755)
    checks.collider_scan = lambda *a, **k: {}

    current = {}
    built = {p["id"]: p for p in pins(site) if (d / f"tiles-{state}" / p["id"] / "tiles.json").exists()}

    def audit_with(directory, quad, terrain, entries, lines, auditor):
        extra = site_lanes(site, current["frame"]) if site.get("lanes") else pair_lanes(lines)
        every = list(lines) + extra
        # the neighbouring tiles' decks and tubes too: a deck is cut square across its road where the tile's box cuts
        # its line, the next tile's piece covering the rest; alone, a tile's lane cells there were holes
        entries = list(entries) + neighbour_entries(d / f"tiles-{state}", current["pin"], built, current["frame"])
        src, out = directory / "audit.bin", directory / "audit.json"
        checks.audit_input(src, quad, terrain, entries, every)
        env = dict(os.environ, ROADSYS_AUDIT_DUMP="1", ROADSYS_AUDIT_SAMPLES="1")
        with open(directory / "audit.dump", "w") as f:
            subprocess.run([str(auditor), "audit", str(src), str(out)], check=True, env=env, stderr=f)
        (directory / "audit.lines.json").write_text(json.dumps(
            [dict(id=l["id"], level=l["level"], width=l["width"], pair=bool(l.get("pair")), site=bool(l.get("site")))
             for l in every]))
        return checks.read(out)
    checks.audit = audit_with
    results = {}
    for p in pins(site):
        tdir = d / f"tiles-{state}" / p["id"]
        if not (tdir / "tiles.json").exists():
            print(state, "no build for", p["id"], flush=True)
            continue
        current["frame"] = geodesy.EnuFrame(p["lat"], p["lon"])      # (checks.measure's frame: the pin's)
        current["pin"] = p
        got = checks.measure(p, tdir, noscan, AUDITOR)
        results[p["id"]] = got
        print(state, "measured", p["id"], flush=True)
    (d / f"measure-{state}.json").write_text(json.dumps(results, indent=1, sort_keys=True))
    return results


# -- reporting ---------------------------------------------------------------------------------------------------

class Axis:
    """s (m) along the site's axis, the signed offset across it (left positive) and, where the axis carries measured
    heights (lat, lon, h), the measured height at s."""

    def __init__(self, axis):
        raw = np.asarray(axis, np.float64)
        ll = densify_ll(raw[:, :2], 2.0)
        self.lat0, self.lon0 = float(ll[0][0]), float(ll[0][1])
        self.k = math.cos(math.radians(self.lat0))
        self.xy = self._xy(ll[:, 0], ll[:, 1])
        self.s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(self.xy, axis=0).T))]
        self.length = float(self.s[-1])
        if raw.shape[1] >= 3:
            rxy = self._xy(raw[:, 0], raw[:, 1])
            rs = np.r_[0.0, np.cumsum(np.hypot(*np.diff(rxy, axis=0).T))]
            self.h = lambda s: np.interp(s, rs, raw[:, 2])
        else:
            self.h = None

    def _xy(self, la, lo):
        return np.c_[(np.asarray(lo) - self.lon0) * 111320.0 * self.k, (np.asarray(la) - self.lat0) * 111320.0]

    def place(self, la, lo):
        P = self._xy(la, lo)
        a, b = self.xy[:-1], self.xy[1:]
        dd = b - a
        l2 = np.maximum((dd * dd).sum(1), 1e-12)
        s_out, off = np.empty(len(P)), np.empty(len(P))
        for i, p in enumerate(P):
            t = np.clip(((p - a) * dd).sum(1) / l2, 0.0, 1.0)
            q = a + t[:, None] * dd
            dist = np.hypot(*(q - p).T)
            j = int(np.argmin(dist))
            s_out[i] = self.s[j] + t[j] * math.sqrt(l2[j])
            off[i] = np.sign(dd[j][0] * (p - a[j])[1] - dd[j][1] * (p - a[j])[0]) * dist[j]
        return s_out, off


BORE_NEAR_M = 8.0       # m: a tunnel line whose points lie this close to the axis (median) is one of the bore's lanes
BIN_M = 20
KINDS = ("walls_in_lane", "other_in_lane", "ground_in_lane", "roofless", "step", "hole", "double_owner", "off_truth")


def _parse_dump(path):
    """{kind: [(x, y, line)]} of the audit's flagged cells, and the samples [(line, x, y, level, z)]."""
    flags, samples = defaultdict(list), []
    for row in path.read_text().splitlines():
        f = row.split()
        if not f:
            continue
        line = int(f[f.index("line") + 1]) if "line" in f else -1
        if f[0] == "EN":
            ks = row.split("kinds", 1)[1].replace(" ", "").strip("[]").split(",")
            flags["walls_in_lane" if "4" in ks else "other_in_lane"].append((float(f[1]), float(f[2]), line))
        elif f[0] == "DO":
            flags["double_owner"].append((float(f[1]), float(f[2]), line))
        elif f[0] == "STEP":
            flags["step"].append((float(f[1]), float(f[2]), line))
        elif f[0] == "TE":
            flags["ground_in_lane"].append((float(f[1]), float(f[2]), line))
        elif f[0] == "RF":
            flags["roofless"].append((float(f[1]), float(f[2]), line))
        elif f[0] == "SA":
            z = float(f[7]) if f[7] != "NaN" else math.nan
            samples.append((int(f[1]), float(f[4]), float(f[5]), int(f[6]), z))
    return flags, samples


def report_state(site, state, axis):
    """The bore's lanes only (tunnel lines along the axis, and the between-lanes gauges between two of them): per
    BIN_M of the axis and per lane class ("left", "right": the carriageways either side of the axis; "between": the
    gauges), the cells flagged (walls_in_lane, other_in_lane, double_owner, step, hole) and the cells whose floor lies
    over 0.5 m off the axis's measured height (off_truth), out of the lane cells (cells)."""
    d = site["dir"]
    import checks
    rows = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    truth = []
    for p in pins(site):
        tdir = d / f"tiles-{state}" / p["id"]
        dump = tdir / "audit.dump"
        if not dump.exists():
            continue
        lines = json.loads((tdir / "audit.lines.json").read_text())
        frame = geodesy.EnuFrame(p["lat"], p["lon"])
        if site.get("lanes"):
            bore = {i for i, line in enumerate(lines) if line.get("site") or (line.get("pair") and line["id"].startswith(
                "pair:site:"))}
        else:
            ctx = checks.read(tdir / (p["key"] + ".source.json"))
            src = list(ctx["lines"]) + pair_lanes(ctx["lines"])
            bore = set()
            for i, line in enumerate(src):
                if line.get("level") != "tunnel" or line.get("pair"):
                    continue
                xy = np.asarray(line["xy"], np.float64)
                la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
                _s, off = axis.place(np.asarray(la), np.asarray(lo))
                if np.median(np.abs(off)) <= BORE_NEAR_M:
                    bore.add(i)
            ids = {src[i]["id"] for i in bore}
            for i, line in enumerate(src):
                if line.get("pair") and all(x in ids for x in line["id"][5:].split("|")):
                    bore.add(i)
        flags, samples = _parse_dump(dump)
        pts, what = [], []
        for kind, cells in flags.items():
            for x, y, li in cells:
                if li in bore:
                    pts.append((x, y))
                    what.append((kind, lines[li].get("pair", False), None, li))
        for li, x, y, _level, z in samples:
            if li not in bore:
                continue
            pts.append((x, y))
            what.append(("hole" if not math.isfinite(z) else "cell", lines[li].get("pair", False), z, li))
        if not pts:
            continue
        P = np.asarray(pts, np.float64)
        la, lo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], 0.0)
        s, off = axis.place(np.asarray(la), np.asarray(lo))
        zt = None
        if axis.h is not None:
            zt = np.asarray(frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), axis.h(s))[2], np.float64)
        for k, ((kind, pair, z, li), si, oi) in enumerate(zip(what, s, off)):
            if si < 0 or si > axis.length:
                continue
            if pair:
                cls = "between"
            elif lines[li].get("site"):
                cls = lines[li]["id"].split(":", 1)[1]
            else:
                cls = "left" if oi > 0 else "right"
            b = int(si // BIN_M) * BIN_M
            if kind == "cell":
                rows[b][cls]["cells"] += 1
                if zt is not None and not pair:
                    dz = z - float(zt[k])
                    truth.append((float(si), cls, dz))
                    if abs(dz) > 0.5:
                        rows[b][cls]["off_truth"] += 1
                continue
            if kind == "hole":
                rows[b][cls]["cells"] += 1
            rows[b][cls][kind] += 1
    return rows, truth


def report(site, states):
    axis = Axis(site["axis"])
    out = {}
    for st in states:
        rows, truth = report_state(site, st, axis)
        tot = defaultdict(lambda: defaultdict(int))
        for _b, v in rows.items():
            for cls, m in v.items():
                for k, n in m.items():
                    tot[cls][k] += n
        tv = np.asarray([t[2] for t in truth]) if truth else np.zeros(0)
        out[st] = dict(totals={c: dict(m) for c, m in tot.items()},
                       bins={str(b): {c: dict(m) for c, m in v.items()} for b, v in sorted(rows.items())},
                       truth=dict(n=int(len(tv)), median_m=round(float(np.median(tv)), 3) if len(tv) else None,
                                  p95_abs_m=round(float(np.percentile(np.abs(tv), 95)), 3) if len(tv) else None))
        print("== %s: truth n %d, median %s m, p95 |dz| %s m" % (st, out[st]["truth"]["n"], out[st]["truth"]["median_m"],
                                                               out[st]["truth"]["p95_abs_m"]))
        for cls in sorted(tot, key=lambda c: (c == "between", c)):
            m = tot.get(cls, {})
            print("   %-8s cells %6d  %s" % (cls, m.get("cells", 0), "  ".join("%s %d" % (k, m.get(k, 0)) for k in KINDS)))
    (site["dir"] / "report.json").write_text(json.dumps(out, indent=1))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=("tiles", "warm", "build", "probe", "measure", "report"))
    ap.add_argument("site")
    ap.add_argument("--tree", help="the worktree (code state) to build with")
    ap.add_argument("--state", action="append", default=[])
    a = ap.parse_args()
    site = site_of(a.site)
    site["dir"].mkdir(parents=True, exist_ok=True)
    if a.cmd == "tiles":
        for p in pins(site):
            print(p["id"], p["key"], round(p["lat"], 6), round(p["lon"], 6))
        print(len(pins(site)), "tiles")
    elif a.cmd == "warm":
        warm(site, a.tree)
    elif a.cmd == "build":
        build_state(site, a.state[0], a.tree)
    elif a.cmd == "probe":
        probe_state(site, a.state[0], a.tree)
    elif a.cmd == "measure":
        measure_state(site, a.state[0])
    else:
        report(site, a.state or ["A", "B", "C"])


if __name__ == "__main__":
    main()
