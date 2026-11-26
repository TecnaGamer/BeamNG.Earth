"""roadscan's browsable report: one HTML page beside a scan (SCAN/report/index.html): the issue counts by kind and
severity, the ranked issues with mileposts and map links, plan views (the finest aerial | the built scene, same
scale, north up, the issue ringed) for the top N, and with a second state the before / after comparison.

    python3 tools/roadscan/report.py SCAN_DIR [--against SCAN_DIR] [--top 30] [--no-aerial] [--title T]

The aerial comes from tools/ortho_hi.py (network, cached under SCAN/report/aerial); the built view from
`roadscan plan`."""
import argparse
import html
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1]
PX = 480
SEV = {1: "minor", 2: "major", 3: "critical"}


def _read(p):
    return json.loads(Path(p).read_text())


def views(scan, issue, exe, aerial):
    """(aerial | built) JPEG path for one issue, made once."""
    from PIL import Image
    img = scan / "report" / "img"
    img.mkdir(parents=True, exist_ok=True)
    lat, lon = issue["lat"], issue["lon"]
    b = issue["box"]
    ext = max(abs(b[2] - b[0]) * 111320.0, abs(b[3] - b[1]) * 111320.0 * 0.7)
    half = max(30.0, min(120.0, ext / 2 + 20.0))
    name = f"{issue['rank']:04d}_{lat:.6f}_{lon:.6f}_{int(half)}"
    out = img / (name + ".jpg")
    if out.exists():
        return out, half
    ppm = img / (name + ".ppm")
    subprocess.run([str(exe), "plan", str(scan / "index.bin"), repr(lat), repr(lon), repr(half), str(PX), str(ppm),
                    f"{lat!r},{lon!r}"], check=True, stdin=subprocess.DEVNULL)
    built = Image.open(ppm).convert("RGB")
    ppm.unlink()
    panes = [built]
    if aerial:
        ad = scan / "report" / "aerial"
        ad.mkdir(parents=True, exist_ok=True)
        af = ad / f"{lat:.6f}_{lon:.6f}_{int(half)}.jpg"
        if not af.exists():
            env = dict(os.environ, HOME=str(scan / "report" / "net"), XDG_CACHE_HOME=str(scan / "report" / "net" / "xdg"))
            (scan / "report" / "net").mkdir(parents=True, exist_ok=True)
            subprocess.run([sys.executable, str(TREE / "tools" / "ortho_hi.py"), repr(lat), repr(lon), repr(half), str(af),
                            "auto", str(PX)], env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
        if af.exists():
            a = Image.open(af).convert("RGB").resize((PX, PX))
            from PIL import ImageDraw
            d = ImageDraw.Draw(a)
            r = 6
            d.ellipse([PX / 2 - r, PX / 2 - r, PX / 2 + r, PX / 2 + r], outline=(255, 0, 255), width=2)
            panes.insert(0, a)
    sheet = Image.new("RGB", (PX * len(panes) + 8 * (len(panes) - 1), PX), (255, 255, 255))
    for k, p in enumerate(panes):
        sheet.paste(p, (k * (PX + 8), 0))
    sheet.save(out, quality=88)
    return out, half


def write(scan, corridor=None, against=None, top=30, aerial=True, exe=None, title=None):
    scan = Path(scan)
    data = _read(scan / "issues.json")
    issues = data["issues"]
    rep = scan / "report"
    rep.mkdir(parents=True, exist_ok=True)
    e = html.escape
    title = title or scan.name
    h = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
         f"<title>{e(title)}</title><style>"
         "body{font:14px system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}"
         "table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #ccc;padding:3px 6px;text-align:left}"
         "th{background:#eee}.s3{background:#f8d0d0}.s2{background:#fbe8c8}.s1{background:#f4f4dc}"
         ".card{margin:14px 0;padding:8px;background:#fff;border:1px solid #ddd}img{max-width:100%}"
         "code{background:#eee;padding:0 3px}</style></head><body>"]
    h.append(f"<h1>{e(title)}</h1>")
    miles = data.get("route_miles")
    if corridor:
        h.append(f"<p>Corridor <b>{e(corridor['name'])}</b>: {corridor['miles']} miles of road, "
                 f"{len(corridor['tiles'])} geotiles; {data['tiles']} scanned.</p>")
    elif miles:
        h.append(f"<p>{miles:.1f} miles, {data['tiles']} tiles scanned.</p>")
    else:
        h.append(f"<p>{data['tiles']} tiles scanned.</p>")
    h.append(f"<p>{data['findings']} located findings, {len(issues)} issues (scanner version {data['scan_version']}). "
             "Severity: <span class=s3>critical</span> (a car is stopped, falls or is thrown), <span class=s2>major</span> "
             "(felt or seen at once), <span class=s1>minor</span>.</p>")
    h.append("<h2>By kind</h2><table><tr><th>kind</th><th>critical</th><th>major</th><th>minor</th></tr>")
    for k, c in sorted(data["by_kind"].items(), key=lambda kv: (-kv[1]["critical"], -kv[1]["major"], kv[0])):
        h.append(f"<tr><td>{e(k)}</td><td>{c['critical']}</td><td>{c['major']}</td><td>{c['minor']}</td></tr>")
    h.append("</table>")
    if against is not None:
        cmp_dir = rep / "compare"
        r = subprocess.run([str(exe), "compare", str(against), str(scan), str(cmp_dir)], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        if (cmp_dir / "compare.json").exists():
            cj = _read(cmp_dir / "compare.json")
            h.append(f"<h2>Against {e(Path(against).name)}</h2><p>{cj['tiles_both']} tiles in both; "
                     f"<b>{cj['regressions']}</b> new or worse issues of severity major or critical.</p>")
            h.append("<table><tr><th>status</th><th>kind</th><th>sev</th><th>before</th><th>after</th><th>where</th></tr>")
            rows = [p for p in cj["pairs"] if p["status"] in ("new", "worse", "gone", "better")]
            rows.sort(key=lambda p: (["new", "worse", "better", "gone"].index(p["status"]), -(p["b"] or p["a"])["severity"]))
            for p in rows[:400]:
                x = p["b"] or p["a"]
                h.append(f"<tr class=s{x['severity']}><td>{p['status']}</td><td>{e(x['kind'])}</td><td>{x['severity']}</td>"
                         f"<td>{p['a']['magnitude'] if p['a'] else ''}</td><td>{p['b']['magnitude'] if p['b'] else ''}</td>"
                         f"<td>{x['lat']:.6f},{x['lon']:.6f}</td></tr>")
            h.append("</table>")
        else:
            h.append(f"<p>Comparison failed: {e(r.stderr[-500:])}</p>")
    h.append(f"<h2>Worst {min(top, len(issues))}</h2><p>Left: the finest public aerial; right: the built scene "
             "(terrain by height, holes red; roads grey, sidewalks light, markings white, deck tops orange over their "
             "sides brown, rails purple, barriers red-brown, tunnels translucent blue; mapped centre lines: ground cyan, "
             "bridge yellow, tunnel magenta dashed). The issue is ringed in magenta.</p>")
    for i in issues[:top]:
        try:
            img, half = views(scan, i, exe, aerial)
            pic = f"<img src='{e(os.path.relpath(img, rep))}' alt='plan view'><br>{2 * half * 3.28084:.0f} ft across"
        except Exception as ex:      # (one view's failure keeps the page)
            pic = f"(no view: {e(str(ex))[:200]})"
        h.append(card(i, pic))
    h.append("<h2>All issues</h2><table><tr><th>#</th><th>sev</th><th>kind</th><th>magnitude</th><th>cells</th>"
             "<th>mile</th><th>class</th><th>level</th><th>where</th></tr>")
    for i in issues[:3000]:
        h.append(f"<tr class=s{i['severity']}><td>{i['rank']}</td><td>{SEV[i['severity']]}</td><td>{e(i['kind'])}</td>"
                 f"<td>{i['magnitude']} {e(i['unit'])}</td><td>{i['cells']}</td><td>{mile(i)}</td><td>{e(i['cls'])}</td>"
                 f"<td>{['ground', 'bridge', 'tunnel'][min(i['level'], 2)]}</td><td>{maplink(i)}</td></tr>")
    h.append("</table></body></html>")
    page = rep / "index.html"
    page.write_text("\n".join(h))
    return page


def mile(i):
    return "" if i.get("mile") is None else f"{i['mile']:.2f}" + ("" if i.get("on_route", True) else " (off)")


def maplink(i):
    u = f"https://www.google.com/maps/@{i['lat']:.6f},{i['lon']:.6f},80m/data=!3m1!1e3"
    return f"<a href='{u}'>{i['lat']:.6f},{i['lon']:.6f}</a>"


def card(i, pic):
    e = html.escape
    return (f"<div class='card s{i['severity']}'><b>#{i['rank']} {SEV[i['severity']]}: {e(i['kind'])}</b> "
            f"{i['magnitude']} {e(i['unit'])}, {i['cells']} cells; {e(i['what'])}<br>"
            f"{maplink(i)} mile {mile(i)}, {e(i['cls'])} {['ground', 'bridge', 'tunnel'][min(i['level'], 2)]}, "
            f"tile {e(i['tile'])}, way {e(i['line'])}<br>{pic}</div>")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scan", type=Path)
    ap.add_argument("--against", type=Path)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--no-aerial", action="store_true")
    ap.add_argument("--title")
    a = ap.parse_args()
    sys.path.insert(0, str(HERE))
    import roadscan
    print(write(a.scan, None, a.against, a.top, not a.no_aerial, roadscan.scanner(2), a.title))
