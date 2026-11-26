"""One page of an Earth eval run (server/evals/earth_eval.py): every junction's upstream views next to the latest ones, the vehicle
hits and mesh cuts of each side, the junctions with something still hitting first; first of all Earth's build time
upstream against latest (what the new passes cost). Only that run: no history.

    python server/evals/eval_page.py --run <earth_eval out> --out <page dir>

<page dir>/index.html and <page dir>/img/ (each junction's picture as JPEG, its close-up change as PNG): published as they
are (an artifact from files, the same artifact republished after every run, its files replaced).
"""
import argparse
import datetime
import html
import json
import os
import shutil
import subprocess
import sys

CHECK_NAMES = (("vehicles_hit", "vehicles hitting"), ("crossings_cut", "crossings cutting"),
               ("overview_crossings_cut", "cutting in 1 km"))


def tree_rev(path):
    """(short commit, subject) of the Earth tree at path, or ("?", "") when it is not a git checkout."""
    try:
        out = subprocess.run(["git", "-C", path, "log", "-1", "--format=%h%x00%s%x00%cs"], capture_output=True,
                             text=True, encoding="utf-8", check=True).stdout.strip().split("\x00")
        return out[0], out[1][:90], out[2]
    except (OSError, subprocess.CalledProcessError, IndexError):
        return "?", "", ""


def counts(r):
    """{check: (upstream, latest)} of a junction's report row."""
    ck = r.get("checks", {})
    return {k: tuple(ck.get(k, [None, None])) for k, _n in CHECK_NAMES}


def badness(r):
    """Sort key: the latest side's hits and cuts, most first; errors first of all."""
    if r.get("error"):
        return (-1, 0, r["name"])
    c = counts(r)
    return (0, -sum((c[k][1] or 0) for k, _n in CHECK_NAMES), r["name"])


def cell(b, a):
    if b is None and a is None:
        return "<td>-</td>"
    cls = "bad" if (a or 0) > (b or 0) else "good" if (a or 0) < (b or 0) else ""
    hot = " hot" if (a or 0) > 0 else ""
    return f'<td class="{cls}{hot}">{b} &rarr; <b>{a}</b></td>'


CSS = """
:root{--bg:#fafaf8;--fg:#1d1d1b;--mute:#6b6b66;--line:#e2e1dc;--bad:#b3261e;--good:#1b6e3a;--hot:#fff4d6}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b1a;--fg:#ecebe6;--mute:#a09f99;--line:#3a3936;--bad:#ff8a80;
--good:#7fd99a;--hot:#3d3420}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:1500px;margin:0 auto;padding:24px 20px 60px}h1{font-size:22px;margin:0 0 6px}
h2{font-size:17px;margin:36px 0 4px}.mute{color:var(--mute)}code{font-size:13px}
table{border-collapse:collapse;width:100%;margin:16px 0;font-size:14px}th,td{padding:6px 8px;border-bottom:1px solid
var(--line);text-align:left;white-space:nowrap}th{font-weight:600}td.hot{background:var(--hot)}td.bad b{color:var(--bad)}
td.good b{color:var(--good)}a{color:inherit}img{width:100%;height:auto;border:1px solid var(--line);border-radius:4px;
display:block;margin-top:8px}.diff{max-width:900px}.wrap{overflow-x:auto}
"""


def build(run, out):
    with open(os.path.join(run, "report.json"), encoding="utf-8") as f:
        rep = json.load(f)
    results = sorted(rep["junctions"], key=badness)
    img = os.path.join(out, "img")
    shutil.rmtree(img, ignore_errors=True)
    os.makedirs(img)
    from PIL import Image
    for r in results:
        src = os.path.join(run, f"{r['name']}.png")
        if os.path.isfile(src):
            Image.open(src).convert("RGB").save(os.path.join(img, f"{r['name']}.jpg"), quality=85, optimize=True)
        d = os.path.join(run, f"{r['name']}_diff.png")
        if os.path.isfile(d):
            shutil.copyfile(d, os.path.join(img, f"{r['name']}_diff.png"))
    up, la = tree_rev(rep["before"]), tree_rev(rep["after"])
    when = datetime.datetime.fromtimestamp(os.path.getmtime(os.path.join(run, "report.json"))).strftime("%Y-%m-%d %H:%M")
    tot = {k: [sum((counts(r)[k][i] or 0) for r in results) for i in (0, 1)] for k, _n in CHECK_NAMES}
    e = html.escape
    rows = []
    for r in results:
        n = r["name"]
        if r.get("error"):
            rows.append(f'<tr><td><a href="#{e(n)}">{e(n)}</a></td><td colspan="4">error: {e(r["error"])}</td></tr>')
            continue
        c = counts(r)
        verdict = ("worse, " if r.get("worse") else "") + ("changed" if r.get("changed") else r.get("verdict", ""))
        rows.append(f'<tr><td><a href="#{e(n)}">{e(n)}</a></td>' + "".join(cell(*c[k]) for k, _n in CHECK_NAMES)
                    + f"<td>{e(verdict)}</td></tr>")
    trows, tb, ta, ts = [], 0.0, 0.0, 0.0
    for r in sorted(results, key=lambda r: r["name"]):
        t = r.get("timing") or {}
        b, a_ = (t.get("before") or {}).get("build_s"), (t.get("after") or {}).get("build_s")
        s = (t.get("after") or {}).get("solver_s")
        if not (b and a_):
            continue
        tb, ta, ts = tb + b, ta + a_, ts + (s or 0.0)
        ch = 100.0 * (a_ - b) / b
        trows.append(f'<tr><td>{e(r["name"])}</td><td>{b:.0f} s</td><td><b>{a_:.0f} s</b></td>'
                     f'<td class="{"bad" if ch > 10 else "good" if ch < -10 else ""}"><b>{ch:+.0f} %</b></td>'
                     f'<td>{(s or 0.0):.0f} s</td></tr>')
    if trows:
        ch = 100.0 * (ta - tb) / tb
        timing = (f'<h2>Build time: upstream &rarr; latest</h2><p><b>{tb:.0f} s &rarr; {ta:.0f} s ({ch:+.0f} %)</b> over '
                  f'{len(trows)} junctions; the new solver passes (interchange shapes, road laws) logged {ts:.0f} s '
                  f'of CPU time.</p><div class="wrap"><table><thead><tr><th>junction</th><th>upstream</th>'
                  f'<th>latest</th><th>change</th><th>solver passes (CPU)</th></tr></thead><tbody>'
                  f'{"".join(trows)}</tbody></table></div><p class="mute">Earth\'s own build of each place (its terrain '
                  f'block and road tiles), both sides built in the same run on the same machine; the solver time is '
                  f'what those passes logged, summed over their cells and worker processes (CPU time: it can exceed the '
                  f'wall time of the build where the cells are planned in parallel). Built one junction at a time, after a '
                  f'run that warmed the caches of both sides, for comparable numbers.</p>')
    else:
        timing = '<p class="mute">No build times in this run (a side reused from an earlier run has none).</p>'
    secs = []
    for r in results:
        n = r["name"]
        c = counts(r)
        line = ", ".join(f"{name} {c[k][0]} &rarr; {c[k][1]}" for k, name in CHECK_NAMES if c[k] != (None, None))
        pic = (f'<img src="img/{e(n)}.jpg" alt="{e(n)}: upstream views on top, latest underneath" loading="lazy">'
               if os.path.isfile(os.path.join(img, f"{n}.jpg")) else '<p class="mute">no picture</p>')
        diff = (f'<details><summary>close-up change (red rose, blue fell, grey now something else)</summary>'
                f'<img class="diff" src="img/{e(n)}_diff.png" alt="{e(n)} close-up change" loading="lazy"></details>'
                if os.path.isfile(os.path.join(img, f"{n}_diff.png")) else "")
        secs.append(f'<section id="{e(n)}"><h2>{e(n)}</h2><div class="mute">{e(r.get("what", ""))}'
                    f'{"<br>" + line if line else ""}</div>{pic}{diff}</section>')
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Earth junctions: latest against upstream</title><style>{CSS}</style></head><body><main>
<h1>Earth junctions: latest against upstream</h1>
<p class="mute">Latest eval run, {e(when)}. Each picture has the upstream views on top and the latest views underneath:
the area 1 km round from the south-west (the ground that rose red, fell blue), the close-up from above and from the
south-west. Cyan boxes are 4 m trucks on the roads, violet ones 4.3 m wagons on the tracks; a box is yellow or red where
it hits Earth's geometry (terrain included).</p>
<p>upstream: <code>{e(up[0])}</code> {e(up[1])} <span class="mute">{e(up[2])}</span><br>
latest: <code>{e(la[0])}</code> {e(la[1])} <span class="mute">{e(la[2])}</span></p>
{timing}
<div class="wrap"><table><thead><tr><th>junction ({len(results)})</th>""" + "".join(
        f"<th>{name}<br><span class=mute>{tot[k][0]} &rarr; {tot[k][1]}</span></th>" for k, name in CHECK_NAMES) + \
        f"""<th>result</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="mute">upstream &rarr; latest. Junctions with the most left hitting first.</p>
{''.join(secs)}
</main></body></html>
"""
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)
    return os.path.join(out, "index.html")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="an earth_eval.py --out (its report.json and pictures)")
    ap.add_argument("--out", required=True, help="the page's folder")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    print(build(os.path.abspath(a.run), os.path.abspath(a.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
