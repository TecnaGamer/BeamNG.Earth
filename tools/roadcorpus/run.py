"""run.py: the road revamp corpus scorecard (plan .claude/work/road-revamp-plan-2026-11-16.md section 5, phase R0).

  run.py build --label L [--server DIR | --rev GITREV] [--set top10|top20|claimed|bisect|standard|all | --places a,b]
               [--root ~/bnge-tests/roadcorpus] [--online] [--jobs 1] [--env BNE_X=v ...]
      Score every place of the set with probe.py against one build of the server code and write
      ROOT/results/L/<place>.json (deterministic), <place>.timing.json, scorecard.json and summary.md.
      --rev exports that git revision's server/ (its own earthfast binaries are committed) into
      ROOT/trees/<sha>/server, so any commit can be scored with today's probes.
      Data: the first build of a place downloads into a scratch cache and keeps the downloaded data (dem, mapdata,
      pagesdata, roadz) as ROOT/seed/<place>; every later build of that place starts from a reflink copy of the seed,
      offline, so all versions see the same data (no weekly-manifest drift between baseline and dev). A build that
      fails offline (an old revision wants files the seed has not got) runs again online in its copy (noted in
      timing as "online": its data may differ). Nothing touches the player's ~/.cache/beamng-earth.
  run.py compare ROOT/results/A ROOT/results/B
      Per place and category: blockers / defects A -> B, and whether the place JSONs are byte-identical.
  run.py same ROOT/results/A ROOT/results/B
      Exit 0 when every place JSON in both is byte-identical (the harness's determinism check).

Data eras: a revision that reads the downloaded data in another layout (8031bac3, 2026-11-11, moved the OSM road
tag archive to parts on the tile host: older code finds no raceways, no OSM lane / tag data in a newer seed, silently)
must not be scored offline on a newer seed. run.era_of(REV) names the era; build --rev and revbisect.py keeps one seed per era
(seed-<era>/), each made by an online run of that era's first revision scored.

Everything runs under nice -n 15, one place at a time by default; the place JSONs hold no times (those go to
.timing.json), so two runs of the same code on the same seed give byte-identical files."""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
DATA_DIRS = ("dem", "mapdata", "mapdata-mirror", "pagesdata", "roadz")
# the categories shown in the summary: (scorecard key, label)
CATS = (("clearance", "deck on road"), ("deck_deck", "stack"), ("end_step", "bridge end"), ("overlap", "deck overlap"),
        ("steep_deck", "steep deck"), ("unbridged", "unbridged"), ("rail_block", "rail block"), ("spikes", "spikes"), ("humps", "junction humps"),
        ("widths", "width jumps"), ("tunnels", "tunnels"), ("missing_deck", "missing deck"), ("track", "racetrack"), ("ride", "ride"))
# extra columns: (label, scorecard key, field)
EXTRA = (("wavy decks", "deck_shape", "wavy"), ("decks on ground", "deck_shape", "on_ground"),
         ("rail beds > 3 m", "rail_bed", "tall3"), ("unpaved m", "unpaved_m", None))


def places(sel_set=None, ids=None):
    with open(os.path.join(HERE, "places.json")) as f:
        ps = json.load(f)["places"]
    if ids:
        want = ids.split(",")
        by = {p["id"]: p for p in ps}
        missing = [i for i in want if i not in by]
        if missing:
            sys.exit(f"unknown places: {missing}")
        return [by[i] for i in want]
    if sel_set and sel_set != "all":
        return [p for p in ps if sel_set in p["sets"]]
    return ps


SWITCHES = {}       # build --env K=V: the only BNE_* switches the probe sees (an experimental switch scored)


def clean_env():
    """The probe's environment: no BNE_* / BNGE_* switches from the caller (they change the output) but build --env's,
    no plan run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BNE_", "BNGE_"))}
    env.update(SWITCHES)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def export_rev(root, rev):
    sha = subprocess.check_output(["git", "-C", REPO, "rev-parse", "--verify", rev + "^{commit}"], text=True).strip()
    dst = os.path.join(root, "trees", sha[:12])
    if not os.path.isdir(os.path.join(dst, "server")):
        tmp = dst + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        arch = subprocess.Popen(["git", "-C", REPO, "archive", sha, "server"], stdout=subprocess.PIPE)
        subprocess.check_call(["tar", "-x", "-C", tmp], stdin=arch.stdout)
        if arch.wait() != 0:
            sys.exit(f"git archive {sha} failed")
        os.rename(tmp, dst)
    return os.path.join(dst, "server"), sha


# (boundary commit, era of the revisions without it): newest first
ERAS = (("8031bac3", "pre-8031bac3"),)


def era_of(rev):
    """The data era of a revision: None (today's layout) or the first era boundary it lacks."""
    for sha, tag in ERAS:
        if subprocess.call(["git", "-C", REPO, "merge-base", "--is-ancestor", sha, rev]) != 0:
            return tag
    return None


def copy_tree(src, dst):
    subprocess.check_call(["cp", "-a", "--reflink=auto", src, dst])


PROBE = ["road"]     # build --probe: "road" (probe.py) or "struct" (tools/probes/structprobe.py, the structures track)
SEED_FROM = [None]  # build --seed-from ROOT2: a place without a seed here starts from ROOT2's (online: grown, kept)


def _complete(js):
    """A structprobe result there, with no tile or cell it could not build."""
    try:
        with open(js) as f:
            return not json.load(f).get("errors")
    except (OSError, ValueError):
        return False


def run_place(p, server, root, label, online, checks=None, era=None):
    out = os.path.join(root, "results", label)
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    for d in (out, os.path.join(root, "seed"), os.path.join(root, "work")):     # (before anything is made there)
        rd = os.path.realpath(d)
        if rd == real or rd.startswith(real + os.sep):
            sys.exit(f"refused: {d} resolves into the player's cache")
    os.makedirs(out, exist_ok=True)
    seed = os.path.join(root, "seed" if not era else "seed-" + era, p["id"])
    work = os.path.join(root, "work", label, p["id"])
    for d in (seed, work, out):         # (never through a link into the player's cache)
        rd = os.path.realpath(d)
        if rd == real or rd.startswith(real + os.sep):
            sys.exit(f"refused: {d} resolves into the player's cache")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    have_seed = os.path.isdir(seed) and not online
    if have_seed:
        for d in DATA_DIRS:
            if os.path.isdir(os.path.join(seed, d)):
                copy_tree(os.path.join(seed, d), work)
    elif not online and SEED_FROM[0] and not era:
        other = os.path.join(os.path.expanduser(SEED_FROM[0]), "seed", p.get("seed_from", p["id"]))
        if os.path.isdir(other):        # (its data as a start; what this place needs more is downloaded and kept)
            for d in DATA_DIRS:
                if os.path.isdir(os.path.join(other, d)):
                    copy_tree(os.path.join(other, d), work)
    js, tj, log = (os.path.join(out, p["id"] + ext) for ext in (".json", ".timing.json", ".log"))
    for f_ in (js, tj, log, js + ".tmp"):   # (no earlier run's result left to be scored as this one's; no link)
        if os.path.lexists(f_):
            os.remove(f_)
    shutil.rmtree(os.path.join(out, p["id"] + ".structures"), ignore_errors=True)
    if PROBE[0] == "struct":        # (today's checkout's probe, scoring the server given: --server)
        base = ["nice", "-n", "15", sys.executable, os.path.join(REPO, "tools", "probes", "structprobe.py"),
                repr(p["lat"]), repr(p["lon"]), repr(float(p.get("struct_r", p["r"]))), "--server", server,
                "--cache", work, "--json", js, "--timing", tj]
    else:
        base = ["nice", "-n", "15", sys.executable, os.path.join(HERE, "probe.py"), "--server", server, "--cache",
                work, "--lat", repr(p["lat"]), "--lon", repr(p["lon"]), "--r", repr(float(p["r"])), "--json", js,
                "--timing", tj] + (["--checks", checks] if checks else [])
    mode = "offline" if have_seed else "online"
    with open(log, "w") as lf:
        rc = subprocess.call(base + (["--offline"] if have_seed else []), stdout=lf, stderr=subprocess.STDOUT,
                             env=clean_env(), cwd=work)
    if rc == 0 and have_seed and os.path.exists(js):
        with open(js) as f:
            errs = json.load(f).get("errors", [])
        if any("offline" in e for e in errs) or (PROBE[0] == "struct" and errs):     # (a probe wanted data the seed has not got: fetched, seed grown)
            mode = "online-after-offline-missing"
            with open(log, "a") as lf:
                lf.write("\n== offline data missing; online ==\n")
                lf.flush()
                rc = subprocess.call(base, stdout=lf, stderr=subprocess.STDOUT, env=clean_env(), cwd=work)
            if rc == 0 and (PROBE[0] != "struct" or _complete(js)):
                for d in DATA_DIRS:
                    if os.path.isdir(os.path.join(work, d)):
                        copy_tree(os.path.join(work, d), seed)
    if rc != 0 and have_seed:
        mode = "online-after-offline-failed"
        with open(log, "a") as lf:
            lf.write("\n== offline failed; online ==\n")
            lf.flush()
            rc = subprocess.call(base, stdout=lf, stderr=subprocess.STDOUT, env=clean_env(), cwd=work)
        if rc == 0 and PROBE[0] == "struct" and _complete(js):    # (the seed grown by what the online run fetched)
            for d in DATA_DIRS:
                if os.path.isdir(os.path.join(work, d)):
                    copy_tree(os.path.join(work, d), seed)
    if rc == 0 and PROBE[0] == "struct" and not _complete(js):
        rc = 3                          # (tiles or cells it could not build: an incomplete place, failed)
    if rc == 0 and not os.path.isdir(seed) and mode == "online":
        tmp = seed + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        for d in DATA_DIRS:
            if os.path.isdir(os.path.join(work, d)):
                copy_tree(os.path.join(work, d), tmp)
        os.rename(tmp, seed)
    if os.path.exists(tj):
        with open(tj) as f:
            t = json.load(f)
        t["mode"] = mode
        t["rc"] = rc
        with open(tj, "w") as f:
            json.dump(t, f, sort_keys=True)
            f.write("\n")
    elif rc != 0:
        with open(tj, "w") as f:
            json.dump({"mode": mode, "rc": rc}, f, sort_keys=True)
            f.write("\n")
    shadow = os.path.join(work, "debug", "structures")     # (BNE_DECK_STRUCT=shadow's chain JSONs: kept with the results)
    if os.path.isdir(shadow):
        dst = os.path.join(out, p["id"] + ".structures")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(shadow, dst)
    shutil.rmtree(work, ignore_errors=True)
    return rc


def load(dirn):
    got = {}
    for n in sorted(os.listdir(dirn)):
        if n.endswith(".json") and not n.endswith(".timing.json") and n != "scorecard.json":
            with open(os.path.join(dirn, n)) as f:
                got[n[:-5]] = json.load(f)
    return got


def cell(sm, key):
    v = sm.get(key)
    if not isinstance(v, dict):
        return "-"
    return f"{v.get('blocker', 0)}/{v.get('defect', 0)}"


def extra(sm, key, field):
    v = sm.get(key)
    if v is None:
        return "-"
    return str(v.get(field) if field else v)


def write_scorecard(dirn, meta):
    res = load(dirn)
    by = {p["id"]: p for p in places()}
    card = {"meta": meta, "places": {}}
    lines = [f"# Road corpus scorecard: {meta.get('label')}", "",
             f"Server: `{meta.get('server')}`" + (f", revision {meta['rev']}" if meta.get("rev") else ""), "",
             "Cells are blockers/defects (definitions: tools/roadcorpus/probe.py summarise).", "",
             "| place | " + " | ".join(lbl for _k, lbl in CATS) + " | " + " | ".join(e[0] for e in EXTRA)
             + " | errors |",
             "|---|" + "---|" * (len(CATS) + len(EXTRA) + 1)]
    tot = {k: [0, 0] for k, _l in CATS}
    for pid, r in sorted(res.items()):
        sm = r.get("summary", {})
        card["places"][pid] = sm
        for k, _l in CATS:
            v = sm.get(k)
            if isinstance(v, dict):
                tot[k][0] += v.get("blocker", 0)
                tot[k][1] += v.get("defect", 0)
        name = by.get(pid, {}).get("name", pid)
        lines.append(f"| {name} | " + " | ".join(cell(sm, k) for k, _l in CATS) + " | "
                     + " | ".join(extra(sm, k, fl) for _l, k, fl in EXTRA) + f" | {sm.get('errors', 0)} |")
    lines.append("| **total** | " + " | ".join(f"**{tot[k][0]}/{tot[k][1]}**" for k, _l in CATS) + " |"
                 + " |" * (len(EXTRA) + 1))
    card["totals"] = {k: {"blocker": v[0], "defect": v[1]} for k, v in tot.items()}
    with open(os.path.join(dirn, "scorecard.json"), "w") as f:
        json.dump(card, f, sort_keys=True, indent=1)
        f.write("\n")
    with open(os.path.join(dirn, "summary.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return lines


def cmd_build(a):
    root = os.path.expanduser(a.root)
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    if os.path.realpath(root).startswith(real):
        sys.exit("refused: inside the player's cache")
    rev = era = None
    if a.rev:
        server, rev = export_rev(root, a.rev)
        era = era_of(rev)
    else:
        server = os.path.realpath(a.server or os.path.join(REPO, "server"))
    PROBE[0] = a.probe
    SEED_FROM[0] = a.seed_from
    if a.probe == "struct":
        if era and a.seed_from:
            sys.exit("--seed-from with a revision of another data era: its seeds are today's layout")
        return cmd_build_struct(a, server, rev, root, era)
    for kv in a.env:
        k, _, v = kv.partition("=")
        if not k.startswith("BNE_"):
            sys.exit(f"--env {kv}: only BNE_* build switches")
        SWITCHES[k] = v
    ps = places(a.set, a.places)
    fails = []
    for k, p in enumerate(ps):
        print(f"[{k + 1}/{len(ps)}] {p['id']} ...", flush=True)
        rc = run_place(p, server, root, a.label, a.online, a.checks, era)
        if rc != 0:
            fails.append(p["id"])
            print(f"  FAILED rc={rc} (see results/{a.label}/{p['id']}.log)", flush=True)
    meta = {"label": a.label, "server": server, "rev": rev, "env": sorted(SWITCHES.items())}
    for ln in write_scorecard(os.path.join(root, "results", a.label), meta):
        print(ln)
    if fails:
        print("failed:", ",".join(fails))
        sys.exit(1)


def write_struct_card(dirn, meta, ran=None, failed=()):
    """The structures scorecard (tools/probes/structprobe.py's summaries per place): scorecard.json and summary.md.
    ran: the places this build ran (only their results are scored, never an older run's file left in the folder);
    failed: those that failed (listed apart, their partial results not scored)."""
    res = {k: v for k, v in load(dirn).items()
           if (ran is None or k in ran) and k not in set(failed)}
    by = {p["id"]: p for p in places()}
    keys = ("decks", "overlap_m2", "hole_m2", "parapet_in_cw_m", "seam_step_n", "seam_step_max", "twin_pairs",
            "twin_dz_max", "skew_ends", "skew_err_max", "groups", "grouped")
    card = {"meta": meta, "places": {}}
    lines = [f"# Structures scorecard: {meta.get('label')}", "",
             f"Server: `{meta.get('server')}`" + (f", revision {meta['rev']}" if meta.get("rev") else ""), "",
             "Definitions: tools/probes/structprobe.py. Targets (design): overlap 0, hole 0, parapet in carriageway 0, "
             "seam steps > 5 cm 0, twin dz <= 2 cm, skew error <= 3 degrees.", "",
             "| place | " + " | ".join(keys) + " | errors |", "|---|" + "---|" * (len(keys) + 1)]
    tot = {k: 0.0 for k in ("overlap_m2", "hole_m2", "parapet_in_cw_m", "seam_step_n")}
    for pid, r in sorted(res.items()):
        sm = dict(r.get("summary", {}))
        sm["errors"] = len(r.get("errors", []))
        card["places"][pid] = sm
        for k in tot:
            tot[k] += float(sm.get(k, 0) or 0)
        lines.append(f"| {by.get(pid, {}).get('name', pid)} | " + " | ".join(str(sm.get(k, "-")) for k in keys)
                     + f" | {sm['errors']} |")
    card["totals"] = {k: round(v, 2) for k, v in tot.items()}
    card["failed"] = sorted(failed)
    if failed:
        lines.append(f"Failed (not scored): {', '.join(sorted(failed))}")
    lines.append("| **total (overlap, hole, parapet, seams)** | " + " | ".join(
        f"**{round(tot[k], 2)}**" for k in ("overlap_m2", "hole_m2", "parapet_in_cw_m", "seam_step_n")) + " |")
    with open(os.path.join(dirn, "scorecard.json"), "w") as f:
        json.dump(card, f, sort_keys=True, indent=1)
        f.write("\n")
    with open(os.path.join(dirn, "summary.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return lines


def cmd_build_struct(a, server, rev, root, era=None):
    for kv in a.env:
        k, _, v = kv.partition("=")
        if not k.startswith("BNE_"):
            sys.exit(f"--env {kv}: only BNE_* build switches")
        SWITCHES[k] = v
    ps = places(a.set, a.places)
    fails = []
    for k, p in enumerate(ps):
        print(f"[{k + 1}/{len(ps)}] {p['id']} ...", flush=True)
        rc = run_place(p, server, root, a.label, a.online, era=era)
        if rc != 0:
            fails.append(p["id"])
            print(f"  FAILED rc={rc} (see results/{a.label}/{p['id']}.log)", flush=True)
    meta = {"label": a.label, "server": server, "rev": rev, "env": sorted(SWITCHES.items()), "probe": "struct"}
    for ln in write_struct_card(os.path.join(root, "results", a.label), meta, {p["id"] for p in ps}, fails):
        print(ln)
    if fails:
        print("failed:", ",".join(fails))
        sys.exit(1)


def cmd_compare(a):
    A, B = load(a.a), load(a.b)
    by = {p["id"]: p for p in places()}
    print("| place | same | " + " | ".join(lbl for _k, lbl in CATS) + " | " + " | ".join(e[0] for e in EXTRA) + " |")
    print("|---|---|" + "---|" * (len(CATS) + len(EXTRA)))
    for pid in sorted(set(A) | set(B)):
        ra, rb = A.get(pid), B.get(pid)
        if ra is None or rb is None:
            print(f"| {pid} | missing in {'A' if ra is None else 'B'} |")
            continue
        same = json.dumps(ra, sort_keys=True) == json.dumps(rb, sort_keys=True)
        cells = []
        for k, _l in CATS:
            ca, cb = cell(ra.get("summary", {}), k), cell(rb.get("summary", {}), k)
            cells.append(ca if ca == cb else f"{ca} -> **{cb}**")
        for _l, k, fl in EXTRA:
            ca, cb = extra(ra.get("summary", {}), k, fl), extra(rb.get("summary", {}), k, fl)
            cells.append(ca if ca == cb else f"{ca} -> **{cb}**")
        print(f"| {by.get(pid, {}).get('name', pid)} | {'yes' if same else 'no'} | " + " | ".join(cells) + " |")


def cmd_same(a):
    A, B = sorted(os.listdir(a.a)), sorted(os.listdir(a.b))
    bad = []
    for n in sorted(set(A) | set(B)):
        if not n.endswith(".json") or n.endswith(".timing.json") or n == "scorecard.json":
            continue
        pa, pb = os.path.join(a.a, n), os.path.join(a.b, n)
        if not (os.path.exists(pa) and os.path.exists(pb)) or open(pa, "rb").read() != open(pb, "rb").read():
            bad.append(n)
    print("identical" if not bad else "differ: " + ", ".join(bad))
    sys.exit(1 if bad else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--label", required=True)
    b.add_argument("--server")
    b.add_argument("--rev")
    b.add_argument("--set", default="top10")
    b.add_argument("--places")
    b.add_argument("--root", default="~/bnge-tests/roadcorpus")
    b.add_argument("--online", action="store_true", help="ignore the seeds (download again)")
    b.add_argument("--checks", help="probe.py --checks (a narrow probe for bisects), default all")
    b.add_argument("--env", action="append", default=[], help="K=V: a BNE_* build switch the probe runs with")
    b.add_argument("--probe", choices=("road", "struct"), default="road",
                   help="struct: tools/probes/structprobe.py (the structures track; --set structures, --root "
                        "~/bnge-tests/structures)")
    b.add_argument("--seed-from", help="ROOT2: a place with no seed here starts from ROOT2/seed/<seed_from or id>")
    b.set_defaults(fn=cmd_build)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.set_defaults(fn=cmd_compare)
    s = sub.add_parser("same")
    s.add_argument("a")
    s.add_argument("b")
    s.set_defaults(fn=cmd_same)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
