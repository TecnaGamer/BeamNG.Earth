#!/usr/bin/env python3
"""The in-game reports (mod report.lua): Ctrl+Shift+E a report (map issue, performance issue, bug,
idea), Ctrl+Shift+Y the 30 s performance profiler. Each report folder has report.json, screenshot.jpg,
hardware.json and issue.md (a ready-to-post GitHub issue).

  python3 tools/reports.py [list]       one line per report (newest last)
  python3 tools/reports.py show N       the Nth report (1-based, from the list) in detail
  python3 tools/reports.py since DATE   reports at or after 'YYYY-MM-DD HH:MM'
  python3 tools/reports.py issue [N]    post report N (default: the newest) as a GitHub issue: opens
                                        issue.md in your editor, then the new-issue page with the edited
                                        text, and the report folder (drag screenshot.jpg into the issue)

  --root DIR    the reports folder (default: found from the BeamNG.drive user folder; or set BNGE_REPORTS)
"""
import json
import os
import shlex
import subprocess
import sys
import urllib.parse
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
# Maintainer configuration: target GitHub repo for tickets ("Madderose/BeamNG.Earth" on fork, "TecnaGamer/BeamNG.Earth" upstream)
REPO = os.environ.get("BEAMNG_EARTH_ISSUE_REPO", "Madderose/BeamNG.Earth")
URL_MAX = 7000          # characters of the whole new-issue URL (GitHub refuses ~8 KB)
TRUNCATED = "\n\n_(Truncated: paste the rest from issue.md in the report folder.)_"

# report kind -> (name, GitHub label); mirrors mod issuefmt.lua. 'note' / 'bench': reports before 2026-10-14.
KINDS = {
    "map": ("Map issue", "map"),
    "perf": ("Performance issue", "performance"),
    "bug": ("Bug", "bug"),
    "idea": ("Idea / suggestion", "enhancement"),
    "bench": ("Performance issue", "performance"),
    "profile": ("Performance issue", "performance"),
    "note": ("Note", None),
}


def kind_of(d):
    return KINDS.get(d.get("kind"), (str(d.get("kind")), None))


def user_dirs():
    """BeamNG.drive user folders, best first: the installer's choice, then the usual places."""
    out = []
    try:
        with open(os.path.join(REPO_ROOT, ".beamng_earth.json"), encoding="utf-8") as f:
            ud = json.load(f).get("user_dir")
        if ud:
            out.append(ud)
    except (OSError, ValueError):
        pass
    try:
        sys.path.insert(0, REPO_ROOT)
        import install  # noqa: E402  (install.py: candidate_user_dirs)
        out += install.candidate_user_dirs()
    except Exception:
        out.append(os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current"))
    return out


def find_root(arg=None):
    if arg:
        return arg
    if os.environ.get("BNGE_REPORTS"):
        return os.environ["BNGE_REPORTS"]
    for ud in user_dirs():
        for sub in ("beamngearth", "beamngEarth"):      # the game lowercases new folders
            p = os.path.join(ud, sub, "reports")
            if os.path.isdir(p):
                return p
    return os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current/beamngearth/reports")


def load(root):
    out = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        p = os.path.join(root, name, "report.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError) as e:
                d = {"kind": "?", "note": f"unreadable report.json: {e}"}
            d["_dir"] = os.path.join(root, name)
            out.append(d)
    return out


def line(i, d):
    fps = (d.get("bench") or {}).get("fps") or (d.get("recentFrames") or {}).get("fps")
    loc = f"{d['lat']:.5f},{d['lon']:.5f}" if isinstance(d.get("lat"), (int, float)) else "-"
    text = d.get("title") or (d.get("note") or "").split("\n")[0]
    return f"{i:3d} {d.get('time')} {kind_of(d)[0][:11]:11s} {loc:22s} fps {fps or 0:5.1f}  {text[:60]}"


def show(d):
    keys = ("time", "kind", "title", "lat", "lon", "anchorId", "note", "vehicle", "camera", "recentFrames", "fpsLimit")
    for k in keys:
        if k in d:
            print(f"{k}: {d[k]}")
    b = d.get("bench")
    if b:
        print("profiler:", {k: v for k, v in b.items() if k != "hitches"})
        for h in b.get("hitches", [])[:20]:
            print("  hitch", h)
    m = d.get("engineMetrics") or {}
    top = sorted(((v, k) for k, v in m.items() if isinstance(v, (int, float)) and k not in ("physAvailable", "physUsed", "virtAvailable", "virtUsed")), reverse=True)[:10]
    print("engine (ms):", ", ".join(f"{k} {v:.2f}" for v, k in top))
    mod = d.get("mod") if isinstance(d.get("mod"), dict) else {}
    print("mod:", {k: mod.get(k) for k in ("anchorId", "commits", "sea", "trees", "far")})
    for f in ("screenshot.jpg", "hardware.json", "issue.md"):
        p = os.path.join(d["_dir"], f)
        print(f"{f}: {p}" + ("" if os.path.exists(p) else "  (missing)"))


# --- GitHub issue ---------------------------------------------------------------------------------

def osm_link(lat, lon, zoom=17):
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map={zoom}/{lat:.6f}/{lon:.6f}"


def fallback_issue(d):
    """issue.md for a report saved before the mod wrote one (old notes and benchmarks)."""
    name = kind_of(d)[0]
    title = d.get("title") or (d.get("note") or "").split("\n")[0].strip() or "(short title)"
    out = [f"# [{name}] {title}", "", f"**Kind:** {name}", "", "### What happened", d.get("note") or "_Describe it here._", ""]
    if isinstance(d.get("lat"), (int, float)):
        out.append(f"**Location:** {d['lat']:.6f}, {d['lon']:.6f} ([OpenStreetMap]({osm_link(d['lat'], d['lon'])}))")
    out.append(f"**Time:** {d.get('time')}")
    for label, s in (("Performance (last ~10 s)", d.get("recentFrames")), ("Performance profiler", d.get("bench"))):
        if s and s.get("fps"):
            low = f", 1% low {1000 / s['p99']:.1f} fps" if s.get("p99") else ""
            out.append(f"**{label}:** {s['fps']:.1f} fps avg{low}, worst frame {s.get('maxMs', 0):.0f} ms")
    out += ["", "### Screenshot", "_Attach the screenshot from this report folder: drag `screenshot.jpg` into this box._", ""]
    return "\n".join(out)


def split_issue(text):
    first, _, rest = text.partition("\n")
    return first.lstrip("#").strip(), rest.lstrip("\n")


def issue_url(title, body, label, max_len=URL_MAX, repo=REPO):
    """The new-issue URL; the body is cut (on a character boundary) to keep the URL under max_len.
    Returns (url, truncated)."""
    q = urllib.parse.quote
    base = f"https://github.com/{repo}/issues/new?title={q(title, safe='')}"
    if label:
        base += f"&labels={q(label, safe='')}"
    base += "&body="
    enc = q(body, safe="")
    if len(base) + len(enc) <= max_len:
        return base + enc, False
    room = max_len - len(base) - len(q(TRUNCATED, safe=""))
    lo, hi = 0, len(body)
    while lo < hi:                      # the longest prefix (in characters) whose encoding fits
        mid = (lo + hi + 1) // 2
        if len(q(body[:mid], safe="")) <= room:
            lo = mid
        else:
            hi = mid - 1
    return base + q(body[:lo] + TRUNCATED, safe=""), True


def open_path(path):
    """Open a file or folder with the system's default application (does not wait)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, AttributeError) as e:
        print(f"(could not open {path}: {e})")
        return False


def edit(path):
    """Let the user edit the file: $VISUAL / $EDITOR (waits), else the system editor and Enter."""
    ed = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if ed:
        try:
            subprocess.call(shlex.split(ed, posix=not sys.platform.startswith("win")) + [path])
            return
        except OSError as e:
            print(f"(could not run {ed}: {e})")
    if sys.platform.startswith("win"):
        subprocess.call(["notepad.exe", path])        # waits until Notepad closes
        return
    if sys.platform == "darwin":
        subprocess.call(["open", "-W", "-t", path])   # TextEdit, waits
        return
    open_path(path)
    try:
        input("Edit issue.md, save it, then press Enter here to open GitHub... ")
    except EOFError:
        pass


def issue(d, do_edit=True, browse=True):
    path = os.path.join(d["_dir"], "issue.md")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(fallback_issue(d))
    print(f"issue.md: {path}")
    if do_edit:
        edit(path)
    with open(path, encoding="utf-8") as f:
        title, body = split_issue(f.read())
    url, cut = issue_url(title, body, kind_of(d)[1])
    if cut:
        print("The issue text is long: the page gets the start of it; paste the rest from issue.md.")
    shot = os.path.join(d["_dir"], "screenshot.jpg")
    if browse:
        webbrowser.open(url)
        open_path(d["_dir"])
        if os.path.exists(shot):
            print(f"Drag the screenshot into the issue: {shot}")
    return url


def main(argv):
    root = None
    if "--root" in argv:
        i = argv.index("--root")
        root = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    root = find_root(root)
    reps = load(root)
    cmd = argv[0] if argv else "list"

    def pick(i):
        if not reps:
            sys.exit(f"no reports in {root}")
        n = int(argv[i]) if len(argv) > i else len(reps)
        if not 1 <= n <= len(reps):
            sys.exit(f"no report {n} (1..{len(reps)})")
        return reps[n - 1]

    if cmd == "list":
        if not reps:
            print(f"no reports yet in {root} (in the game: Ctrl+Shift+E a report, Ctrl+Shift+Y the profiler)")
        for i, d in enumerate(reps, 1):
            print(line(i, d))
    elif cmd == "show":
        show(pick(1))
    elif cmd == "since":
        for i, d in enumerate(reps, 1):
            if d.get("time", "") >= argv[1]:
                print(line(i, d))
    elif cmd == "issue":
        issue(pick(1))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
