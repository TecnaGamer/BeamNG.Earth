#!/usr/bin/env python3
"""The junctions page of an earth_eval run against upstream: the results table, the junctions that timed out or got worse, the
build times, and per junction its row, its 3 x 2 picture, its close-up change and the after side's interchange steps' timing.

    python server/evals/junction_page.py --run <earth_eval out> --out <page dir> --commit 8cb21280 \
        --note "upstream dev merged, earthfast 640; run with --jobs 4, BNE_SHAPES=1"

<page dir>/index.html and <page dir>/img/ (the run's pictures): one self-contained page to publish. The run is earth_eval's
--out with --labels upstream,now (its report.md, report.json and <junction>/after/view.log), and earth_eval's own output
saved to a file (--log; default <run>/eval.log: earth_eval.py ... > <run>/eval.log 2>&1).
"""
import argparse
import datetime
import html
import os
import re
import shutil
from collections import OrderedDict

STEPS = ["paths", "extent", "order", "terrain", "datum", "heights"]
STEP_RE = re.compile(r"ix step: (\w+) key (\S+) pieces (\d+) crossings (\d+) ([\d.]+) s (\w+)(.*)$")
FAIL_RE = re.compile(r"ix step: (\w+) key (\S+) failed: (.*)$")
STYLE = """
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
EXPLAIN = ("Each picture has the upstream views on top and the latest views underneath: the area 1 km round from the south-west "
           "(the ground that rose red, fell blue), the close-up from above and from the south-west. Cyan boxes are 4 m trucks on the "
           "roads, violet ones 4.3 m wagons on the tracks; a box is yellow or red where it hits Earth's geometry (terrain included).")
TIMING_NOTE = ("Earth's own build of the place (the block and its road tiles); the solver passes' time is what the interchange shapes "
               "and the road laws logged, summed over their cells and worker processes (CPU time: it can exceed the wall time). "
               "Junctions built side by side (--jobs above 1) have times not comparable to a --jobs 1 run. Comparable when both "
               "sides are built in the same run, one junction at a time (--jobs 1), with warm caches. A side reused from an "
               "earlier run has the timing of that run.")


def tables(path):
    tabs, cur = [], []
    with open(path, encoding="utf-8") as f:
        for line in f.read().split("\n"):
            if line.startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if all(re.fullmatch(r"-+", c) for c in cells):
                    continue
                cur.append(cells)
            elif cur:
                tabs.append(cur)
                cur = []
    if cur:
        tabs.append(cur)
    return tabs


def md(s):
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(s, quote=False))


def find_tab(report, first, second):
    return [t for t in tables(report) if t[0][0] == first and t[0][1] == second][0]


def log_lines(log):
    with open(log, encoding="utf-8", errors="replace") as f:
        return f.read().split("\n")


def timeout_sides(lines, n):
    """The side(s) that timed out: eval.log's line '[name] timeout after N s (...): <side>: stopped ...'"""
    hits = []
    for line in lines:
        m = re.match(r"\[(\w+)\] timeout after ([\d.]+) s .*?: (before|after): ", line)
        if m and m.group(1) == n:
            hits.append((m.group(3), round(float(m.group(2)))))
    return hits


def step_stats(run, n):
    """Per interchange step, from the after side's view.log ('ix step:' lines, one per interchange per step)."""
    p = os.path.join(run, n, "after", "view.log")
    if not os.path.isfile(p):
        return None
    st = OrderedDict((s, {"n": 0, "total": 0.0, "slow": None, "failed": []}) for s in STEPS)
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            fl = FAIL_RE.search(line)
            if fl:      # a step that failed before it measured (no pieces, no seconds): the reason is the text before ' @ '
                d = st.setdefault(fl.group(1), {"n": 0, "total": 0.0, "slow": None, "failed": []})
                d["n"] += 1
                d["failed"].append(fl.group(3).split(" @ ")[0].strip(" :-"))
                continue
            m = STEP_RE.search(line)
            if not m:
                continue
            step, _key, pieces, crossings, secs, status, rest = m.groups()
            d = st.setdefault(step, {"n": 0, "total": 0.0, "slow": None, "failed": []})
            secs = float(secs)
            d["n"] += 1
            d["total"] += secs
            if d["slow"] is None or secs > d["slow"][0]:
                d["slow"] = (secs, int(pieces), int(crossings))
            if status != "ok":
                d["failed"].append(rest.split(" @ ")[0].strip(" :-") or status)
    return st


def build(run, out, commit, note, log):
    """Write <out>/index.html and <out>/img/; returns (built, timed out, pictures copied)."""
    report = os.path.join(run, "report.md")
    res_tab = find_tab(report, "junction", "result")
    build_tab = find_tab(report, "junction", "before (s)")
    hdr, rows = res_tab[0], res_tab[1:]
    names = [r[0] for r in rows]
    lines = log_lines(log)
    run_date = datetime.datetime.fromtimestamp(os.path.getmtime(os.path.join(run, "report.json"))).strftime("%Y-%m-%d")

    timeout = {}
    for r in rows:
        if re.search(r"timeout", r[1], re.I):
            m = re.search(r"timeout[^0-9]*(\d+) s", r[1], re.I)
            timeout[r[0]] = int(m.group(1)) if m else None
    reason = {r[0]: r[1] for r in rows if r[1].startswith("error") and r[0] not in timeout}
    sides = {n: timeout_sides(lines, n) for n in timeout}
    built = [n for n in names if n not in timeout and n not in reason]

    os.makedirs(os.path.join(out, "img"), exist_ok=True)
    files = []
    for n in built:
        for f in (n + ".png", n + "_diff.png"):
            src = os.path.join(run, f)
            if os.path.isfile(src):
                shutil.copy(src, os.path.join(out, "img", f))
                files.append(f)

    def result_cell(r):
        n = r[0]
        if n in reason:
            return "no before side captured"
        if n in timeout:
            hits = sides.get(n) or []
            side = ", ".join(s for s, _ in hits) if hits else "side not in log"
            return "timeout after %s s (%s)" % (timeout[n] if timeout[n] is not None else "?", side)
        return r[1]

    def row_cells(r, name_link=False):
        cells = []
        for i, c in enumerate(r):
            if i == 1:
                c = result_cell(r)
            if i == 0 and name_link:
                cells.append('<td><a href="#%s">%s</a></td>' % (c, html.escape(c)))
            elif i == 1 and ("worse" in c or "timeout" in c or c.startswith("error")):
                cells.append('<td class="bad">%s</td>' % md(c))
            else:
                cells.append("<td>%s</td>" % md(c))
        return "".join(cells)

    head = "".join("<th>%s</th>" % html.escape(h) for h in hdr)

    def table_html(rows_, link=False):
        body = "".join("<tr>%s</tr>" % row_cells(r, link) for r in rows_)
        return '<div class="wrap"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (head, body)

    bh, brows, build_rows = build_tab[0], [], {}
    for r in build_tab[1:]:
        build_rows[r[0].strip("*")] = r
        brows.append("<tr>%s</tr>" % "".join(
            ("<td><b>%s</b></td>" if r[0].startswith("**") else "<td>%s</td>") % md(c) for c in r))
    timing = ('<div class="wrap"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
              % ("".join("<th>%s</th>" % html.escape(h) for h in bh), "".join(brows)))

    after_s = {}
    for line in lines:
        m = re.match(r"\[(\w+)\] after: ([\d.]+) s\s*$", line)
        if m:
            after_s[m.group(1)] = round(float(m.group(2)))

    def timing_html(n):
        st = step_stats(run, n)
        parts = []
        if st is None or all(d["n"] == 0 for d in st.values()):
            parts.append('<p class="mute">no per-step timing in this build’s logs</p>')
        else:
            th = ("<th>step</th><th>interchanges</th><th>total s</th><th>slowest s</th>"
                  "<th>slowest: pieces / crossings</th><th>failed</th><th>failure reason</th>")
            body = []
            for s, d in st.items():
                if d["n"] == 0 and not d["failed"]:
                    body.append("<tr><td>%s</td><td>0</td><td>-</td><td>-</td><td>-</td><td>0</td><td>-</td></tr>" % s)
                    continue
                slow = ("%.2f s, %d / %d" % d["slow"]) if d["slow"] else "-"
                reasons = "; ".join(sorted(set(d["failed"]))) if d["failed"] else "-"
                cls = ' class="bad"' if d["failed"] else ""
                body.append("<tr><td>%s</td><td>%d</td><td>%.2f</td><td>%s</td><td>%s</td><td%s>%d</td><td>%s</td></tr>"
                            % (s, d["n"], d["total"], "%.2f" % d["slow"][0] if d["slow"] else "-", slow,
                               cls, len(d["failed"]), html.escape(reasons)))
            parts.append('<div class="wrap"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
                         % (th, "".join(body)))
        if after_s.get(n) is not None:
            parts.append('<p class="mute">Total after build: %s s (eval.log).</p>' % after_s[n])
        return "".join(parts)

    worse = [r for r in rows if "worse" in r[1]]
    worse_list = ("<ul>" + "".join(
        '<li><a href="#%s">%s</a>: vehicles hitting %s, crossings cutting %s, cutting in 1 km %s</li>'
        % (r[0], r[0], md(r[7]), md(r[8]), md(r[9])) for r in worse) + "</ul>") if worse else "<p>None.</p>"
    timeout_list = ("<ul>" + "".join('<li><a href="#%s">%s</a>: %s</li>'
                                      % (n, n, html.escape(result_cell(rows[names.index(n)]))) for n in timeout)
                    + "</ul>") if timeout else "<p>None.</p>"

    sections = []
    for r in rows:
        n = r[0]
        if n not in built:
            continue        # timed out or no before side: no picture, no detail section (its row in the table says so)
        body = ('<img src="img/%s.png" alt="%s: upstream views on top, latest underneath" loading="lazy">'
                '<img class="diff" src="img/%s_diff.png" alt="%s close-up change" loading="lazy">') % (n, n, n, n)
        one_row = '<div class="wrap"><table><thead><tr>%s</tr></thead><tbody><tr>%s</tr></tbody></table></div>' % (
            head, row_cells(r))
        sections.append('<section id="%s"><h2>%s</h2>%s%s<h3>Pipeline step timing (after side)</h3>%s</section>'
                        % (n, n, one_row, body, timing_html(n)))

    title = "Earth junctions: latest against upstream"
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{STYLE}</style></head><body><main>
<h1>{title}</h1>
<p class="mute">BeamNG.Earth <code>{html.escape(commit)}</code> against upstream{': ' + html.escape(note) if note else ''}. All {len(rows)} junctions of the run. Run on {run_date}.</p>
<p class="mute">What each picture shows: {html.escape(EXPLAIN)}</p>
<h2>Results: all {len(rows)} junctions</h2>
{table_html(rows, link=True)}
<h2>Timed out</h2>
{timeout_list}
<h2>Worse than upstream</h2>
{worse_list}
<h2>Build time</h2>
{timing}
<p class="mute">{html.escape(TIMING_NOTE)}</p>
<h2>Junctions</h2>
<p class="mute">A timed-out junction has no picture and no detail section. Each detail section lists the pipeline steps from the after side's view.log: one line per interchange and step.</p>
{chr(10).join(sections)}
</main></body></html>
"""
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    return built, timeout, files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="earth_eval's --out of the run")
    ap.add_argument("--out", required=True, help="the page's folder (index.html, img/)")
    ap.add_argument("--commit", required=True, help="the after tree's commit, as the page names it")
    ap.add_argument("--note", default="", help="what the run was: the merges in it, its --jobs, its limits")
    ap.add_argument("--log", help="earth_eval's output saved to a file (default <run>/eval.log)")
    a = ap.parse_args(argv)
    built, timeout, files = build(a.run, a.out, a.commit, a.note, a.log or os.path.join(a.run, "eval.log"))
    print(f"{len(built)} built, {len(timeout)} timed out, {len(files)} pictures copied: {os.path.join(a.out, 'index.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
