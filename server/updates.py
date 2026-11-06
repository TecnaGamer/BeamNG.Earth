"""Update check (the user, 2026-10-15: "add an update system where the mod will check GitHub for any updates").

The project's latest published main release on GitHub is compared with this copy's VERSION; when it is newer the server tells the
mod ('update' message), which says so once in game and on its settings page, with the link to the download.
This uses the same release endpoint as the one-click downloader, so a version committed before a failed or
cancelled release build is not advertised. Development builds continue to use the releases list.

    newer("1.4.1-preview", "1.4.0-preview") -> True
    check(local_version, net) -> {"current", "latest", "url"} or None (up to date, offline, no VERSION there)

Development builds (the user, 2026-10-30: "every push to dev would create a new pre-release"): the release workflow
builds each push to the dev branch as the GitHub pre-release v<VERSION>.dev.<n> ("1.7.2-beta.dev.37": newer than
1.7.2-beta, older than 1.7.3-beta). A copy that is one asks GitHub's releases list instead (pre-releases included),
so it is told of the next development build or the next beta, whichever is newer.
"""
import json
import os
import re
import sys
import threading
import time

import netfetch

REPO = "TecnaGamer/BeamNG.Earth"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE_URL = f"https://github.com/{REPO}/releases"      # the downloads (.exe / AppImage)
# the folder with VERSION: above server/, or the packaged app's bundle (launcher.py frozen by PyInstaller, where
# every module sits at the bundle's top)
ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def local_version(root=ROOT):
    """This copy's VERSION (the folder above server/), or None."""
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            v = f.read().strip()
        return v or None
    except OSError:
        return None


def parse(v):
    """(major, minor, patch, release) of a version like "1.4.0-preview" ("-..." is a pre-release: below the same
    numbers without it; its dot-separated parts compared one by one, numbers as numbers, as semver: "beta" <
    "beta.dev.9" < "beta.dev.10"); None when it is not one."""
    m = re.fullmatch(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?\s*", str(v or ""))
    if not m:
        return None
    nums = tuple(int(g or 0) for g in m.groups()[:3])
    if not m.group(4):
        return nums + ((1, ()),)
    ids = tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in m.group(4).split("."))
    return nums + ((0, ids),)


def is_dev(v):
    """Is version v a development build (a pre-release of the dev branch: "...dev.<n>")?"""
    p = parse(v)
    return p is not None and any(i[2] == "dev" for i in p[3][1])


def newer(latest, current):
    """Is version latest newer than current? (False when either is not a version.)"""
    a, b = parse(latest), parse(current)
    return a is not None and b is not None and a > b


def check(current, net=None):
    """The update GitHub has for version current, or None. Blocking (a worker thread)."""
    if not current:
        return None
    net = net if net is not None else netfetch.default_fetcher()
    if is_dev(current):
        rel = newest_release(net)
        if rel is None or not newer(rel[0], current):
            return None
        return {"current": current, "latest": rel[0], "url": rel[1], "dev": is_dev(rel[0])}
    try:
        raw = net.get(LATEST_API, kind="background", headers={"Accept": "application/vnd.github+json"})
        release = json.loads(raw.body.decode("utf-8", "replace"))
    except (netfetch.FetchError, OSError, ValueError):
        return None
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        return None
    latest = str(release.get("tag_name") or "").removeprefix("v")
    url = str(release.get("html_url") or "")
    if is_dev(latest) or not newer(latest, current) or url != f"{PAGE_URL}/tag/v{latest}":
        return None
    return {"current": current, "latest": latest, "url": url}


RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
RELEASES_TTL_S = 3 * 3600.0     # the releases list kept this long (api.github.com: a few requests a day, netfetch)
_releases = [0.0, None]
_releases_lock = threading.Lock()


def releases(net, fresh=False):
    """GitHub's releases of the project (newest first, pre-releases included, drafts not), kept RELEASES_TTL_S; None
    when GitHub did not answer."""
    import json
    with _releases_lock:
        if not fresh and _releases[1] is not None and time.monotonic() - _releases[0] < RELEASES_TTL_S:
            return _releases[1]
    try:
        r = net.get(RELEASES_API, kind="demand", headers={"Accept": "application/vnd.github+json"})
        got = json.loads(r.body.decode("utf-8", "replace"))
    except (netfetch.FetchError, OSError, ValueError):
        return None
    if not isinstance(got, list):
        return None
    got = [x for x in got if isinstance(x, dict) and not x.get("draft")]
    with _releases_lock:
        _releases[:] = [time.monotonic(), got]
    return got


def newest_release(net, fresh=False):
    """(version, page url, release) of the newest release or pre-release on GitHub, or None."""
    best = None
    for x in releases(net, fresh) or ():
        v = str(x.get("tag_name") or "").lstrip("v")
        url = str(x.get("html_url") or "")
        if parse(v) is None or not url.startswith(f"https://github.com/{REPO}/"):
            continue
        if best is None or newer(v, best[0]):
            best = (v, url, x)
    return best


# The mod's update notice asks the server to open the releases page: the game's openWebBrowser does nothing in the
# native Linux build (the user, 2026-10-18: "i clicked open releases page but it never opened my browser"). Only this
# project's GitHub pages are opened (a message from the mod cannot open anything else).
OPEN_PREFIX = f"https://github.com/{REPO}/"
# ... and the settings page's About links (the game's openWebBrowser refuses every site outside its own domain filter:
# "unable to open webpage due to domain filter", 2026-11-05): the project's Discord and Ko-fi, exactly, and a
# contributor's GitHub profile (github.com/<login>, nothing after it)
OPEN_EXACT = ("https://discord.gg/tgf", "https://ko-fi.com/tecnagamer", f"https://github.com/{REPO}")
_PROFILE = re.compile(r"https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


def openable(url):
    """Whether open_page may open url: this project's GitHub pages, OPEN_EXACT, or a GitHub user's profile."""
    if not isinstance(url, str) or any(c in url for c in ' "\'<>`\\\n\r'):
        return False
    return url.startswith(OPEN_PREFIX) or url in OPEN_EXACT or _PROFILE.fullmatch(url) is not None


def open_page(url, popen=None):
    """Open url (openable: this project's pages and the About page's links) in the player's browser, without waiting.
    Returns True if a browser was asked. On Linux xdg-open runs with the packaged app's library and Python paths taken out of its
    environment (the AppImage's own libraries broke the browser it starts)."""
    import subprocess
    if not openable(url):
        return False
    popen = popen or subprocess.Popen
    try:
        if sys.platform == "win32":
            os.startfile(url)       # noqa: S606 (the default browser)
            return True
        env = {k: v for k, v in os.environ.items()
               if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH", "APPDIR", "APPIMAGE", "ARGV0")}
        cmd = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
        popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except (OSError, ValueError):
        return False
