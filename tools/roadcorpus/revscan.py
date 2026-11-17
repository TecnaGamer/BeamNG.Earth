"""revscan.py: the regressions' timelines (plan phase R0e): every first-parent dev revision of a range scored at a few
places with a narrow probe, so a metric that flips back and forth shows each commit that moved it.

  revscan.py scan --good REV --bad REV --places a,b [--checks end,deck_grade] [--every N] [--root ROOT]
      Scores each first-parent revision of good..bad that changed server/ or native/ (every N-th with --every; the
      good and bad ends always) at every place, on the place's seed for the revision's data era (run.era_of), then
      removes the revision's exported tree (git archive: ~100 MB each). Results: ROOT/results/bisect/<sha12>/<place>.json
      (shared with revbisect.py; kept and reused).
  revscan.py report --good REV --bad REV --place P --metrics end_step.blocker,end_step.worst
      The timeline: one line per revision where any metric changed, with the commit's date and subject.
  revscan.py rows --rev REV --place P --key end
      A place JSON's rows of one probe at a revision (to see which item moved)."""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run  # noqa: E402
import revbisect  # noqa: E402


def revs_of(a):
    revs = revbisect.first_parent(a.good, a.bad)
    good = subprocess.check_output(["git", "-C", run.REPO, "rev-parse", a.good + "^{commit}"], text=True).strip()
    every = max(1, int(getattr(a, "every", 1) or 1))
    keep = [r for k, r in enumerate(revs) if k % every == 0 or k == len(revs) - 1]
    return [good] + keep


def cmd_scan(a):
    root = os.path.expanduser(a.root)
    ps = run.places(ids=a.places)
    revs = revs_of(a)
    for k, sha in enumerate(revs):
        need = [p for p in ps if not os.path.exists(os.path.join(root, "results", "bisect", sha[:12], p["id"] + ".json"))]
        if not need:
            continue
        server, sha = run.export_rev(root, sha)
        era = run.era_of(sha)
        for p in need:
            rc = run.run_place(p, server, root, os.path.join("bisect", sha[:12]), False, a.checks, era)
            print(f"[{k + 1}/{len(revs)}] {sha[:10]} {p['id']} rc={rc}", flush=True)
        shutil.rmtree(os.path.dirname(server), ignore_errors=True)


def cmd_report(a):
    if a.spec:      # place=m1,m2;place2=m3: several timelines
        for part in a.spec.split(";"):
            pl, ms = part.split("=")
            print(f"== {pl}")
            a.place, a.metrics, a.spec = pl, ms, None
            cmd_report(a)
        return
    root = os.path.expanduser(a.root)
    names = a.metrics.split(",")
    prev = None
    for sha in revs_of(a):
        js = os.path.join(root, "results", "bisect", sha[:12], a.place + ".json")
        if not os.path.exists(js):
            continue
        with open(js) as f:
            r = json.load(f)
        vals = [revbisect.metric(r, m) for m in names]
        if vals != prev:
            msg = subprocess.check_output(["git", "-C", run.REPO, "log", "-1", "--format=%ad %s",
                                           "--date=format:%m-%d %H:%M", sha], text=True).strip()
            print(f"{sha[:10]} {' '.join(f'{n}={v}' for n, v in zip(names, vals))}  | {msg[:170]}")
            prev = vals


def cmd_rows(a):
    root = os.path.expanduser(a.root)
    sha = subprocess.check_output(["git", "-C", run.REPO, "rev-parse", a.rev + "^{commit}"], text=True).strip()
    with open(os.path.join(root, "results", "bisect", sha[:12], a.place + ".json")) as f:
        r = json.load(f)
    for row in r.get(a.key, []):
        print(json.dumps(row))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--good", required=True)
    s.add_argument("--bad", required=True)
    s.add_argument("--places", required=True)
    s.add_argument("--checks")
    s.add_argument("--every", type=int, default=1)
    s.set_defaults(fn=cmd_scan)
    r = sub.add_parser("report")
    r.add_argument("--good", required=True)
    r.add_argument("--bad", required=True)
    r.add_argument("--place")
    r.add_argument("--metrics")
    r.add_argument("--spec", help="place=m1,m2;place2=m3 (several timelines)")
    r.add_argument("--every", type=int, default=1)
    r.set_defaults(fn=cmd_report)
    w = sub.add_parser("rows")
    w.add_argument("--rev", required=True)
    w.add_argument("--place", required=True)
    w.add_argument("--key", required=True)
    w.set_defaults(fn=cmd_rows)
    for p in (s, r, w):
        p.add_argument("--root", default="~/bnge-tests/roadcorpus")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
