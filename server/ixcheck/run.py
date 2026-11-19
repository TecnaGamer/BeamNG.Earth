"""python -m ixcheck.run <root> [--json out.json]: every check over a cache directory, a table of what it found per
step and per code, then the first 20 fails. Exit code 0 always: the counts are the output."""
import argparse
import json
import sys

from ixcheck import c1_paths, c2_extent, c3_order, c4_terrain, c5_datum, c6_heights, load

CHECKS = {1: c1_paths, 2: c2_extent, 3: c3_order, 4: c4_terrain, 5: c5_datum, 6: c6_heights}


def check_all(scanned):
    """[(artifact, [Finding])], steps in order, artifacts in key order, each with its parents by step."""
    # (Terrain's piece count is its Heights', a child of it: each Heights names the Terrain it was solved on)
    solved_on = {}
    for h in scanned["steps"].get(6, []):
        t = load.parents_of(h, scanned).get(4)
        if t is not None and getattr(h, "pieces", None) is not None:
            solved_on[t.meta.key] = h
    out = []
    for step in sorted(scanned["steps"]):
        mod = CHECKS.get(step)
        if mod is None:
            continue
        for art in scanned["steps"][step]:
            parents = load.parents_of(art, scanned)
            if step == 4 and art.meta.key in solved_on:
                parents[6] = solved_on[art.meta.key]
            out.append((art, mod.check(art, parents)))
    return out


def report(rows):
    """(the table's lines, the fail lines, the counts as JSON types)."""
    per_step, codes, fails = {}, {}, []
    for art, fs in rows:
        d = per_step.setdefault(art.meta.step, [0, 0, 0])       # artifacts, fails, notes
        d[0] += 1
        for f in fs:
            c = codes.setdefault((art.meta.step, f.code), {"fail": 0, "note": 0})
            c[f.level] += 1
            d[1 if f.level == "fail" else 2] += 1
            if f.level == "fail":
                fails.append(f)
    lines = ["step  artifacts  fails  notes"]
    for step in sorted(per_step):
        d = per_step[step]
        lines.append("%4d  %9d  %5d  %5d" % (step, d[0], d[1], d[2]))
        for (s, code) in sorted(c for c in codes if c[0] == step):
            c = codes[(s, code)]
            lines.append("      %-16s fail %d  note %d" % (code, c["fail"], c["note"]))
    fail_lines = ["step %d  %s: %s" % (f.step, f.code, f.text) for f in fails[:20]]
    counts = {str(step): {code: dict(c) for (s, code), c in sorted(codes.items()) if s == step}
              for step in sorted(per_step)}
    return lines, fail_lines, counts


def _plain(f):
    return {"step": f.step, "level": f.level, "code": f.code, "text": f.text,
            "where": [[float(v) if isinstance(v, float) else int(v) if isinstance(v, int) else v for v in w]
                      for w in f.where]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="checks over an interchange pipeline cache directory")
    ap.add_argument("root", help="the cache root (BNE_IX_CACHE's value)")
    ap.add_argument("--json", dest="json_out", default=None, help="write the findings here as JSON")
    args = ap.parse_args(argv)
    scanned = load.scan(args.root)
    rows = check_all(scanned)
    lines, fail_lines, counts = report(rows)
    print("\n".join(lines))
    if scanned["errors"]:
        for path, why in scanned["errors"]:
            print("did not load: %s (%s)" % (path, why))
    print("")
    print("fails (first 20):")
    print("\n".join(fail_lines) if fail_lines else "  none")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"counts": counts, "findings": [_plain(f) for _a, fs in rows for f in fs]}, fh,
                      indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
