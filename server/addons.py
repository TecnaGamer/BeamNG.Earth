"""Add-ons: third-party mods and tools for BeamNG.Earth, browsed and installed from the in-game Add-ons page (the user,
2026-11-05: "add a add-ons page that people can browse, install, run, and manage 3rd party addons for beamng.earth ...
use the git readme to load an in game description ... git releases to download the latest version and also display a
change log using previous releases"). docs/ADDONS.md has the design, the add-on authors' side and the modding API.

The registry (data/addons.json, REGISTRY_PATH): the curated list. Only its repos can be installed; nothing of an
add-on's is bundled with BeamNG.Earth (their licences forbid redistribution): the player's own server downloads each
from its author's GitHub releases. Per entry: id, name, author, repo, kind, summary, licence, asset (a pattern for
the release file), and for kind "beamng-mod" the mod's zip name in the game's mods folder (modFile), older names it
replaces (oldFiles) and its UI apps (uiApps, a hint for the player). trust: "latest" (any release of the author's) or
"reviewed" (only the files listed in reviewed [{tag, sha256}]); a reviewed file shows a badge either way. A copy on
the BeamNG.Earth-data repo (REGISTRY_URL) replaces the bundled one without a release.

GitHub (api.github.com, 60 requests an hour per address without a token): an add-on's releases (RELEASES_N, one
request) when the page lists the add-ons, its README (one more) when the player opens it; each kept on disk
(GitHubCache: TTL_S, then asked again with If-None-Match, a 304 is the same answer; the copy on disk when GitHub does
not answer, so the page works offline). Under netfetch's api.github.com policy in a lane of its own ("addons"): the
update check keeps its own day's budget.

The text (README, release notes): Markdown to safe HTML in the Rust kernel (fast.md_html, native mdhtml.rs): only
our tags, every link an index into a list of https URLs. open_page opens a URL only if it is in an add-on's text or
under a registry repo (openable).

Installing a "beamng-mod" (the game watches its mods folder; replacing a mod's files under a running game broke its UI
apps, 2026-10-24): the server only downloads, into <mods>/<modFile>.bnge-part, checks the size and the SHA-256 digest
GitHub publishes for the asset and that it is a readable zip, then renames it to <modFile>.bnge-tmp (a name the
game's mod manager ignores) and tells the mod (addon_apply). The mod does the rest through the game's own mod manager
(addons.lua: core_modmanager.updateZipMod, the call the game's own downloader uses, or a rename that its file watcher
mounts), so the game mounts the new file itself. Enable / disable / uninstall are the mod manager's too (the mod
sends addon_applied after each, logged here). Never while a BeamMP server is joined (it turns mods off), and only
after the player agreed to that add-on (consent, the page's dialog naming the author).

Kind "companion" (a program beside the game, Live GPS): its release's Python file installed into <data>/addons/<id>/
and run by BeamNG.Earth's own Python with the companion feed's address (addonrun.Runner: Start / Stop, "Start with
BeamNG.Earth"); its entry's run: {args, page (its local web page, opened from the card), port, lan (it serves the
player's Wi-Fi: a phone)}. Kind "link": README, changelog and downloads only.

The local list (<data>/addons/local.json, LOCAL_NAME; same format, entries with local: {file, tag}): add-ons from a
file on this computer, shown with a Local badge, an entry with a registry id in that one's place (a changed copy tried
before its author releases it). Only this computer's own file says so: nothing from the network adds local entries.

Add from GitHub (the user, 2026-11-07: "add an option where people can paste github repos to install any addon"): the
player pastes owner/name or a github.com link; its latest release decides what it is (a .zip: a game mod, checked as
any; a .py: a program beside the game; else downloads only) and the entry goes into the local list with added: true
(a badge, the notice says nobody has looked at it, "Remove from my list"). Only the player's own paste adds one.

Options (the user, 2026-11-07: "add an options tab that addons can make use of, for example ... the port"): an
add-on declares them (its entry's options, a game mod's beamngEarth_addon.json in its zip, a pasted repository's at
its root: parse_options): [{key, label, type: int | bool | text | choice, default, min, max, choices, help, arg, env}].
The page's Options tab shows them; the values are kept in <data>/addons/<id>/options.json. A program beside the game
gets each as its arg (--port 47800; a bool: the arg alone when on) and / or env (BNE_* only), and its run.page may name
them ({port}); saving restarts it when it runs. A game mod reads them from the modding API (beamngEarth_api.addonOptions).

A game mod's code (addonrun.mod_code: its own GE extensions and modules, whether it has UI files) goes with every
hand-over to the mod, which unloads it before an update / turning off / removing and reloads the UI after, so a change
applies without restarting the game (the game's own files a mod replaces still need a restart: said on the page).
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.request
import zipfile

import addonrun
import netfetch

log = logging.getLogger("addons")

REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "addons.json")
# the online copy (the user, 2026-11-09, after cancelling the 1.9.0 build to add an add-on: add-ons added and removed
# without a release): a plain file in the BeamNG.Earth-data repo (its Pages site is rebuilt only by the weekly data
# workflow), read through raw.githubusercontent.com (netfetch keeps it up to 6 h, GitHubCache TTL_S on top); the
# bundled list when it can't be read ("" = the bundled one only)
REGISTRY_URL = "https://raw.githubusercontent.com/TecnaGamer/BeamNG.Earth-data/main/addons.json"
REGISTRY_FORMAT = 1
KINDS = ("beamng-mod", "companion", "link")
API = "https://api.github.com/repos/"
LANE = "addons"
RETRIES = 2                 # a GitHub request that failed (5xx, 429, a timeout, no answer) asked again this often ...
RETRY_S = 2.0               # ... after this many seconds times its attempt
TTL_S = 3600.0              # a page from GitHub kept this long before it is asked again (conditionally)
RELEASES_N = 8              # the releases asked for (the changelog; Trucking's bodies are ~66 KB each)
README_MAX = 512 * 1024     # bytes of a README / a release's notes rendered at most
HTML_MAX = 256 * 1024
PART, TMP = ".bnge-part", ".bnge-tmp"     # the download, then the checked file the mod hands the mod manager
STALE_S = 24 * 3600.0       # leftovers of an interrupted install older than this are removed
CHUNK = 1 << 16
DL_MAX = 512 << 20          # an asset this large is refused

_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_ZIP = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.zip")
_SHA = re.compile(r"[0-9a-f]{64}")
_APP = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ORIGIN = re.compile(r"http://(?:127\.0\.0\.1|localhost):[0-9]{2,5}")
_PAGE = re.compile(r"http://(?:127\.0\.0\.1|localhost):(?:[0-9]{2,5}|\{[a-z][A-Za-z0-9_]{0,30}\})(?:/[A-Za-z0-9._~/-]{0,100})?")
_ARG = re.compile(r"--?[A-Za-z0-9][A-Za-z0-9-]{0,40}")
_OPT_KEY = re.compile(r"[a-z][A-Za-z0-9_]{0,30}")
_OPT_ENV = re.compile(r"BNE_[A-Z0-9_]{1,40}")
OPT_TYPES = ("int", "bool", "text", "choice")
OPTIONS_MAX = 16
OPTIONS_FILE = "options.json"
MANIFEST = "beamngEarth_addon.json"     # an add-on's own declaration (a mod zip's top, a repository's root)
LOCAL_NAME = "local.json"
_TEXT_MAX = {"name": 80, "author": 40, "summary": 400, "licence": 300, "note": 600}


class AddonError(RuntimeError):
    """An add-on action could not be done (the message says why, for the player)."""


# --- the registry -----------------------------------------------------------------------------------------------

def parse_registry(data, local=False):
    """The valid entries of a registry document (a dict), in order; an invalid entry is left out (logged). local: the
    local list (entries may name a file on this computer)."""
    if not isinstance(data, dict) or data.get("format") != REGISTRY_FORMAT or not isinstance(data.get("addons"), list):
        raise ValueError("not an add-on registry of format %d" % REGISTRY_FORMAT)
    out, seen = [], set()
    for raw in data["addons"]:
        try:
            e = _entry(raw, local)
        except (ValueError, TypeError, re.error) as err:
            log.warning("add-ons: registry entry %r left out: %s", raw.get("id") if isinstance(raw, dict) else raw, err)
            continue
        if e["id"] in seen:
            log.warning("add-ons: registry entry %r twice: the second left out", e["id"])
            continue
        seen.add(e["id"])
        out.append(e)
    return out


def _text(raw, key, need=True):
    v = raw.get(key, "")
    if not isinstance(v, str) or (need and not v.strip()) or len(v) > _TEXT_MAX.get(key, 200):
        raise ValueError(f"bad {key}")
    return v.strip()


def _entry(raw, local=False):
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    e = {"id": raw.get("id"), "repo": raw.get("repo"), "kind": raw.get("kind")}
    if not isinstance(e["id"], str) or not _ID.fullmatch(e["id"]):
        raise ValueError("bad id")
    if not isinstance(e["repo"], str) or not _REPO.fullmatch(e["repo"]) or ".." in e["repo"]:
        raise ValueError("bad repo")
    if e["kind"] not in KINDS:
        raise ValueError("bad kind")
    for k in ("name", "author", "summary", "licence"):
        e[k] = _text(raw, k)
    e["note"] = _text(raw, "note", need=False)
    pat = raw.get("asset", "")
    if not isinstance(pat, str) or len(pat) > 200 or (e["kind"] != "link" and not pat):
        raise ValueError("bad asset pattern")
    e["asset"] = re.compile(pat) if pat else None
    e["links"] = []
    for ln in raw.get("links") or ():
        if not (isinstance(ln, dict) and isinstance(ln.get("label"), str) and 0 < len(ln["label"]) <= 60
                and isinstance(ln.get("asset"), str) and len(ln["asset"]) <= 200):
            raise ValueError("bad link")
        e["links"].append({"label": ln["label"], "asset": re.compile(ln["asset"])})
    e["modFile"], e["oldFiles"], e["uiApps"] = None, [], []
    if e["kind"] == "beamng-mod":
        mf = raw.get("modFile")
        if not isinstance(mf, str) or not _ZIP.fullmatch(mf):
            raise ValueError("bad modFile")
        e["modFile"] = mf
        for of in raw.get("oldFiles") or ():
            if not isinstance(of, str) or not _ZIP.fullmatch(of) or of.lower() == mf.lower():
                raise ValueError("bad oldFiles")
            e["oldFiles"].append(of)
        for app in raw.get("uiApps") or ():
            if not isinstance(app, str) or not _APP.fullmatch(app):
                raise ValueError("bad uiApps")
            e["uiApps"].append(app)
    e["trust"] = raw.get("trust", "latest")
    if e["trust"] not in ("latest", "reviewed"):
        raise ValueError("bad trust")
    e["reviewed"] = {}
    for r in raw.get("reviewed") or ():
        if not (isinstance(r, dict) and isinstance(r.get("tag"), str) and isinstance(r.get("sha256"), str)
                and _SHA.fullmatch(r["sha256"])):
            raise ValueError("bad reviewed")
        e["reviewed"][r["sha256"]] = r["tag"][:60]
    e["feedOrigins"] = []           # its own local web page reads the companion feed (companionfeed.py CORS)
    for o in raw.get("feedOrigins") or ():
        if not isinstance(o, str) or not _ORIGIN.fullmatch(o):
            raise ValueError("bad feedOrigins")
        e["feedOrigins"].append(o)
    e["options"] = parse_options(raw.get("options"))
    e["minEarth"] = raw.get("minEarth") or ""
    if not isinstance(e["minEarth"], str):
        raise ValueError("bad minEarth")
    e["run"] = None
    if e["kind"] == "companion" and raw.get("run") is not None:     # (none: downloads only, as a link)
        run = raw["run"]
        if not isinstance(run, dict):
            raise ValueError("bad run")
        args = run.get("args") or []
        if not isinstance(args, list) or len(args) > 8 or not all(isinstance(a, str) and _ARG.fullmatch(a) for a in args):
            raise ValueError("bad run args")
        page = run.get("page") or ""
        if page and not (isinstance(page, str) and _PAGE.fullmatch(page)):
            raise ValueError("bad run page")
        port = run.get("port")
        if port is not None and not (isinstance(port, int) and 1024 <= port <= 65535):
            raise ValueError("bad run port")
        e["run"] = {"args": list(args), "page": page, "port": port, "lan": run.get("lan") is True}
    e["added"] = raw.get("added") is True
    if e["added"] and not local:
        raise ValueError("added entries only in the local list")
    e["local"] = None
    if raw.get("local") is not None:
        lo = raw["local"]
        if not local:
            raise ValueError("local entries only in the local list")
        if not (isinstance(lo, dict) and isinstance(lo.get("file"), str) and os.path.isabs(lo["file"])):
            raise ValueError("bad local file")
        tag = lo.get("tag") or "local"
        if not isinstance(tag, str) or len(tag) > 60:
            raise ValueError("bad local tag")
        e["local"] = {"file": lo["file"], "tag": tag}
    return e


def parse_options(raw):
    """An add-on's declared options, checked (ValueError): [{key, label, type, default, min, max, choices, help, arg,
    env}]; none -> []."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > OPTIONS_MAX:
        raise ValueError("bad options")
    out, keys = [], set()
    for o in raw:
        if not isinstance(o, dict) or not isinstance(o.get("key"), str) or not _OPT_KEY.fullmatch(o["key"]):
            raise ValueError("bad option key")
        if o["key"] in keys:
            raise ValueError("option key twice")
        keys.add(o["key"])
        t = o.get("type")
        if t not in OPT_TYPES:
            raise ValueError("bad option type")
        label = o.get("label") or o["key"]
        help_ = o.get("help") or ""
        if not isinstance(label, str) or len(label) > 60 or not isinstance(help_, str) or len(help_) > 300:
            raise ValueError("bad option label / help")
        opt = {"key": o["key"], "label": label, "type": t, "help": help_, "arg": None, "env": None}
        if o.get("arg") is not None:
            if not isinstance(o["arg"], str) or not _ARG.fullmatch(o["arg"]):
                raise ValueError("bad option arg")
            opt["arg"] = o["arg"]
        if o.get("env") is not None:
            if not isinstance(o["env"], str) or not _OPT_ENV.fullmatch(o["env"]):
                raise ValueError("bad option env (BNE_ names only)")
            opt["env"] = o["env"]
        if t == "int":
            lo, hi = o.get("min", -(1 << 31)), o.get("max", 1 << 31)
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (lo, hi)) or lo > hi:
                raise ValueError("bad option range")
            opt.update(min=lo, max=hi)
        elif t == "choice":
            ch = o.get("choices")
            if not (isinstance(ch, list) and 0 < len(ch) <= 20 and all(isinstance(c, str) and 0 < len(c) <= 60 for c in ch)):
                raise ValueError("bad option choices")
            opt["choices"] = list(ch)
        elif t == "text":
            opt["maxLength"] = 200
        ok, default = _opt_value(opt, o.get("default", {"int": opt.get("min", 0) if t == "int" else 0, "bool": False,
                                                       "text": "", "choice": (o.get("choices") or [""])[0]}[t]))
        if not ok:
            raise ValueError("bad option default")
        opt["default"] = default
        out.append(opt)
    return out


def _opt_value(opt, v):
    """(ok, the value as its type) of a value for an option."""
    t = opt["type"]
    if t == "int":
        if isinstance(v, bool):
            return False, None
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return False, None
        if isinstance(v, float) and v != iv:
            return False, None
        return (opt["min"] <= iv <= opt["max"]), iv
    if t == "bool":
        return isinstance(v, bool), v
    if t == "text":
        return (isinstance(v, str) and len(v) <= 200 and not any(c in v for c in "\0\n\r")), v
    return (isinstance(v, str) and v in opt["choices"]), v


def load_registry(path=REGISTRY_PATH):
    with open(path, encoding="utf-8") as f:
        return parse_registry(json.load(f))


def feed_origins(path=REGISTRY_PATH):
    """The web origins the companion feed answers (the registry's feedOrigins); none when the registry is unreadable."""
    try:
        return sorted({o for e in load_registry(path) for o in e["feedOrigins"]})
    except (OSError, ValueError):
        return []


# --- GitHub, kept on disk ---------------------------------------------------------------------------------------

class GitHubCache:
    """GitHub API pages kept in folder (one JSON file each: url, etag, at, body), asked again after TTL_S with
    If-None-Match. get() -> (body text, age s, stale): stale when GitHub did not answer and the copy on disk stands
    in; raises AddonError when there is neither."""

    def __init__(self, folder, net, clock=time.time):
        self.folder, self.net, self.clock = folder, net, clock
        self.lock = threading.Lock()

    def _path(self, url):
        return os.path.join(self.folder, hashlib.sha1(url.encode()).hexdigest()[:20] + ".json")

    def _read(self, url):
        try:
            with open(self._path(url), encoding="utf-8") as f:
                d = json.load(f)
            if d.get("url") == url and isinstance(d.get("body"), str):
                return d
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _write(self, d):
        os.makedirs(self.folder, exist_ok=True)
        p = self._path(d["url"])
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, p)

    def get(self, url, accept="application/vnd.github+json", fresh=False):
        with self.lock:
            have = self._read(url)
        now = self.clock()
        if have and not fresh and 0 <= now - have.get("at", 0) < TTL_S:
            return have["body"], now - have["at"], False
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if have and have.get("etag"):
            headers["If-None-Match"] = have["etag"]
        try:
            r = None
            for attempt in range(RETRIES + 1):
                try:
                    r = self.net.get(url, kind="demand", headers=headers, lane=LANE)
                    break
                except netfetch.NotFound:
                    raise
                except (netfetch.FetchError, OSError) as e:
                    # (GitHub's API answered 5xx or took ~10 s a request for a few minutes, 2026-11-07: the page said
                    # "GitHub did not answer" on its first open, nothing on disk yet; a moment later every request
                    # came back in 0.3 s)
                    status = getattr(e, "status", None)
                    if attempt == RETRIES or (status is not None and status < 500 and status != 429):
                        raise
                    time.sleep(RETRY_S * (attempt + 1))
        except (netfetch.FetchError, OSError) as e:
            if have:
                log.info("add-ons: %s: %s; the copy on disk (%.0f h old)", url, e, (now - have.get("at", 0)) / 3600)
                return have["body"], now - have.get("at", 0), True
            raise AddonError(f"GitHub did not answer ({e})") from e
        if r.status == 304 and have:
            have["at"] = now
        else:
            have = {"url": url, "etag": r.headers.get("etag", ""), "at": now,
                    "body": r.body.decode("utf-8", "replace")}
        with self.lock:
            try:
                self._write(have)
            except OSError as e:
                log.debug("add-ons: cache write: %s", e)
        return have["body"], 0.0, False


# --- releases ---------------------------------------------------------------------------------------------------

def _asset(release, pattern, repo):
    """{name, url, size, sha256} of the release's first asset matching pattern from repo's own downloads with a
    SHA-256 digest, or None."""
    prefix = f"https://github.com/{repo}/releases/download/"
    for a in release.get("assets") or ():
        if not isinstance(a, dict):
            continue
        name, url, digest = str(a.get("name") or ""), str(a.get("browser_download_url") or ""), str(a.get("digest") or "")
        if "/" in name or "\\" in name or not pattern.search(name) or not url.startswith(prefix):
            continue
        if not digest.startswith("sha256:") or not _SHA.fullmatch(digest[7:].lower()):
            continue
        size = a.get("size")
        return {"name": name, "url": url, "size": int(size) if isinstance(size, int) and size > 0 else 0,
                "sha256": digest[7:].lower()}
    return None


def releases(entry, body_text):
    """The add-on's releases (newest first, drafts left out): [{tag, name, date, prerelease, body, asset, links}],
    asset: the file to install (see _asset) or None; links: [{label, url, size}] of the entry's extra downloads."""
    try:
        got = json.loads(body_text)
    except ValueError:
        return []
    out = []
    for r in got if isinstance(got, list) else ():
        if not isinstance(r, dict) or r.get("draft"):
            continue
        tag = str(r.get("tag_name") or "")[:60]
        if not tag:
            continue
        a = _asset(r, entry["asset"], entry["repo"]) if entry["asset"] is not None else None
        links = []
        for ln in entry["links"]:
            la = _asset(r, ln["asset"], entry["repo"])
            if la:
                links.append({"label": ln["label"], "url": la["url"], "size": la["size"], "name": la["name"]})
        out.append({"tag": tag, "name": str(r.get("name") or tag)[:120], "date": str(r.get("published_at") or "")[:20],
                    "prerelease": bool(r.get("prerelease")), "body": str(r.get("body") or "")[:README_MAX],
                    "asset": a, "links": links})
    return out


def offered(entry, rels):
    """The release the Install / Update button takes: the newest that is not a pre-release and has the file (with
    trust "reviewed": a reviewed file only), or None."""
    for r in rels:
        if r["prerelease"] or not r["asset"]:
            continue
        if entry["trust"] == "reviewed" and r["asset"]["sha256"] not in entry["reviewed"]:
            continue
        return r
    return None


# --- the game's mods folder -------------------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mod_name(file):
    """The game's mod manager's name for a zip in mods/ (core_modmanager getModNameFromPath: lower case, no .zip)."""
    return file.lower()[:-4] if file.lower().endswith(".zip") else file.lower()


def _db_active(user_dir, name):
    """The mod's 'active' in the game's mods/db.json (True / False), or None when it has no entry."""
    try:
        with open(os.path.join(user_dir, "mods", "db.json"), encoding="utf-8") as f:
            db = json.load(f)
        e = db.get("mods", {}).get(name)
        return None if not isinstance(e, dict) else e.get("active") is not False
    except (OSError, ValueError, AttributeError):
        return None


class Addons:
    """The registry, GitHub's pages for it and the add-ons installed in the game's user folder (user_dir)."""

    def __init__(self, user_dir, cache_dir, net, version="", registry=None, md=None, clock=time.time, data_dir=None,
                 feed_url="", game_dir=None):
        self.user_dir = user_dir
        self.mods = os.path.join(user_dir, "mods")
        self.net = net
        self.version = version or ""
        self.data = os.path.join(data_dir or cache_dir, "addons")     # companions' files, the local list
        self.game_dir = game_dir if game_dir and os.path.isdir(game_dir) else None
        self.runner = addonrun.Runner(self.data, feed_url, clock=clock)
        self._code = {}                 # sha256 -> mod_code of an installed mod zip
        self.bundled = registry if registry is not None else load_registry()
        self.entries = self._merge(self.bundled)
        self.github = GitHubCache(os.path.join(cache_dir, "addons"), net, clock=clock)
        self.md = md                    # (text, base, fold) -> (html, links); fast.md_html by default
        self._links = set()             # every URL an add-on's text showed (openable)
        self._hashes = {}               # path -> ((size, mtime_ns), sha256)
        self._busy = set()              # ids being downloaded
        self.lock = threading.Lock()
        self.clean_stale()

    def local_path(self):
        return os.path.join(self.data, LOCAL_NAME)

    def _merge(self, entries):
        """The registry's entries with the local list's (a local one with a registry id in that one's place)."""
        try:
            with open(self.local_path(), encoding="utf-8") as f:
                local = parse_registry(json.load(f), local=True)
        except FileNotFoundError:
            local = []
        except (OSError, ValueError) as e:
            log.warning("add-ons: the local list %s: %s", self.local_path(), e)
            local = []
        by = {e["id"]: e for e in local}
        out = [by.pop(e["id"], e) for e in entries]
        return out + list(by.values())

    def entry(self, aid):
        for e in self.entries:
            if e["id"] == aid:
                return e
        raise AddonError(f"no add-on {aid!r} in the list")

    def _render(self, text, base, fold=False):
        md = self.md
        if md is None:
            import fast
            md = fast.md_html
        html, links = md(text[:README_MAX], base, fold)
        with self.lock:
            self._links.update(links)
        return html, links

    def openable(self, url):
        """Whether the page may open url in the browser: a link of an add-on's text, a page of a registry repo, or a
        companion's own local page (openable_local)."""
        if isinstance(url, str) and url.startswith("http://"):
            return self.openable_local(url)
        if not isinstance(url, str) or not url.startswith("https://") or any(c in url for c in ' "\'<>`\\\n\r'):
            return False
        if any(url == f"https://github.com/{e['repo']}" or url.startswith(f"https://github.com/{e['repo']}/")
               for e in self.entries):
            return True
        with self.lock:
            return url in self._links

    def _releases(self, e, fresh=False):
        """(releases, age, stale): a local entry's file first (the one Install takes), then GitHub's (the changelog;
        none when GitHub does not answer)."""
        lo = None
        if e["local"]:
            p = e["local"]["file"]
            if os.path.isfile(p):
                st = os.stat(p)
                lo = {"tag": e["local"]["tag"], "name": f"{e['local']['tag']} (local file)",
                      "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)), "prerelease": False,
                      "body": f"A file on this computer: `{p}`", "links": [], "local": True,
                      "asset": {"name": os.path.basename(p), "url": "", "size": st.st_size, "sha256": self._hash(p),
                                "path": p}}
        try:
            body, age, stale = self.github.get(f"{API}{e['repo']}/releases?per_page={RELEASES_N}", fresh=fresh)
            rels = releases(e, body)
        except AddonError:
            if lo is None:
                raise
            rels, age, stale = [], None, True
        return ([lo] if lo else []) + rels, age, stale

    # --- what is installed ---
    def _hash(self, path):
        st = os.stat(path)
        key = (st.st_size, st.st_mtime_ns)
        got = self._hashes.get(path)
        if got and got[0] == key:
            return got[1]
        h = sha256_file(path)
        self._hashes[path] = (key, h)
        return h

    def installed(self, e, rels=()):
        """{installed, file, version, sha256, active, oldFiles} of a beamng-mod in the mods folder; version: the tag of
        the release whose file it is ("" when it is none of the listed ones, e.g. one installed by hand)."""
        if e["kind"] != "beamng-mod":
            return {"installed": False}
        path = os.path.join(self.mods, e["modFile"])
        old = [f for f in e["oldFiles"] if os.path.isfile(os.path.join(self.mods, f))]
        if not os.path.isfile(path):
            return {"installed": False, "file": e["modFile"], "oldFiles": old}
        try:
            sha = self._hash(path)
        except OSError:
            sha = ""
        version = next((r["tag"] for r in rels if r["asset"] and r["asset"]["sha256"] == sha), "")
        version = version or e["reviewed"].get(sha, "")
        return {"installed": True, "file": e["modFile"], "version": version, "sha256": sha,
                "active": _db_active(self.user_dir, mod_name(e["modFile"])), "oldFiles": old}

    def card(self, e, fresh=False):
        """The add-on as the page's list shows it."""
        c = {"id": e["id"], "name": e["name"], "author": e["author"], "repo": e["repo"], "kind": e["kind"],
             "summary": e["summary"], "licence": e["licence"], "note": e["note"], "url": f"https://github.com/{e['repo']}",
             "uiApps": list(e["uiApps"]), "trust": e["trust"], "file": e["modFile"] or "", "oldFiles": [],
             "installed": False, "version": "", "latest": "", "latestDate": "", "size": 0, "update": False,
             "reviewed": False, "installedReviewed": False, "canInstall": False, "busy": e["id"] in self._busy,
             "error": "", "age": None, "stale": False, "tooNew": False, "local": bool(e["local"]),
             "running": False, "autostart": False, "page": "", "lanPage": "", "code": None, "added": e["added"],
             "options": []}
        try:
            rels, c["age"], c["stale"] = self._releases(e, fresh)
        except AddonError as err:
            rels = []
            c["error"] = str(err)
        best = offered(e, rels)
        if best:
            c.update(latest=best["tag"], latestDate=best["date"], size=best["asset"]["size"],
                     reviewed=best["asset"]["sha256"] in e["reviewed"])
        if e["kind"] == "companion":
            inst = self.runner.info(e["id"])
            c.update(installed=inst["installed"], running=inst["running"], autostart=inst["autostart"],
                     version=inst.get("version", ""), canInstall=bool(best) and e["run"] is not None)
            if inst["installed"]:
                c["update"] = bool(best) and best["asset"]["sha256"] != inst.get("sha256")
                if inst.get("exit") not in (None, 0) and not inst["running"]:
                    c["exitCode"] = inst["exit"]
                    c["logTail"] = self.runner.log_tail(e["id"], 6)
            run = e["run"] or {}
            c["page"] = self.page(e)
            if run.get("lan") and c["page"]:
                c["lanPage"] = c["page"].replace("127.0.0.1", _lan_ip()).replace("localhost", _lan_ip())
        if e["kind"] == "beamng-mod":
            inst = self.installed(e, rels)
            c.update(installed=inst["installed"], oldFiles=inst.get("oldFiles", []))
            if inst["installed"]:
                c.update(version=inst["version"] or "unknown", active=inst["active"],
                         installedReviewed=inst["sha256"] in e["reviewed"])
                c["update"] = bool(best) and best["asset"]["sha256"] != inst["sha256"]
                c["code"] = self.code(os.path.join(self.mods, e["modFile"]), inst["sha256"])
            if e["minEarth"] and self.version:
                import updates
                c["tooNew"] = updates.newer(e["minEarth"], self.version)
            c["canInstall"] = bool(best) and not c["tooNew"]
        vals = self.values(e)
        c["options"] = [dict(o, value=vals[o["key"]]) for o in self.options_of(e)]
        return c

    def refresh_registry(self, fresh=False):
        """The Pages copy of the registry instead of the bundled one, when REGISTRY_URL is set and it answers with a
        valid one (kept on disk like GitHub's pages); else the entries stay as they are."""
        if not REGISTRY_URL:
            return
        try:
            body, _, _ = self.github.get(REGISTRY_URL, accept="application/json", fresh=fresh)
            doc = json.loads(body)
            got = parse_registry(doc)
        except (AddonError, ValueError) as e:
            log.info("add-ons: the registry on %s: %s; the bundled one stays", REGISTRY_URL, e)
            return
        # (an empty list is an answer: every add-on taken off; entries that are all invalid are not, a newer format's)
        if got or not doc["addons"]:
            self.bundled = got

    def cards(self, fresh=False):
        self.refresh_registry(fresh)
        self.entries = self._merge(self.bundled)       # (the local list read again: a file just added shows)
        return [self.card(e, fresh) for e in self.entries]

    def code(self, path, sha):
        """addonrun.mod_code of a mod zip, kept by its SHA-256."""
        got = self._code.get(sha)
        if got is None:
            got = self._code[sha] = addonrun.mod_code(path, self.game_dir)
        return got

    def detail(self, aid, fresh=False):
        """The add-on's page: its card, the README and the changelog as safe HTML (with their link lists)."""
        e = self.entry(aid)
        out = {"id": aid, "card": self.card(e, fresh), "readme": "", "readmeLinks": [], "releases": [], "error": ""}
        base = f"https://github.com/{e['repo']}/blob/HEAD/README.md"
        try:
            text, _, _ = self.github.get(f"{API}{e['repo']}/readme", accept="application/vnd.github.raw+json",
                                         fresh=fresh)
            out["readme"], out["readmeLinks"] = self._render(text, base)
        except AddonError as err:
            out["error"] = str(err)
        try:
            rels, _, _ = self._releases(e)
        except AddonError:
            rels = []
        for r in rels:
            html, links = self._render(r["body"], f"https://github.com/{e['repo']}/releases/tag/{r['tag']}", True)
            downloads = list(r["links"])
            # (its file, opened in the browser: a link, or a program not run from here: no run options)
            if (e["kind"] == "link" or (e["kind"] == "companion" and e["run"] is None)) and r["asset"] \
                    and r["asset"]["url"]:
                a = r["asset"]
                downloads.insert(0, {"label": f"Download {a['name']}", "url": a["url"], "size": a["size"], "name": a["name"]})
            out["releases"].append({"tag": r["tag"], "name": r["name"], "date": r["date"],
                                    "prerelease": r["prerelease"], "html": html, "links": links,
                                    "size": r["asset"]["size"] if r["asset"] else 0,
                                    "reviewed": bool(r["asset"]) and r["asset"]["sha256"] in e["reviewed"],
                                    "downloads": downloads})
        return out

    # --- install ---
    def clean_stale(self):
        """Leftovers of an interrupted install (.bnge-part / .bnge-tmp older than STALE_S) removed from mods/."""
        try:
            names = os.listdir(self.mods)
        except OSError:
            return
        now = time.time()
        for n in names:
            if n.endswith((PART, TMP)):
                p = os.path.join(self.mods, n)
                try:
                    if now - os.path.getmtime(p) > STALE_S:
                        os.remove(p)
                except OSError:
                    pass

    def stage(self, aid, consent=False, mp=False, progress=None):
        """Download the release the page offers for add-on aid into the mods folder and check it. Returns the
        addon_apply message for the mod ({id, op, file, tmp, old, tag, active, uiApps}). Raises AddonError."""
        e = self.entry(aid)
        if e["kind"] == "companion":
            return self.install_companion(aid, consent, progress)
        if e["kind"] != "beamng-mod":
            raise AddonError("this add-on is not installed from here: see its page")
        if mp:
            raise AddonError("not while a BeamMP server is joined (it turns mods off): leave the server first")
        if not consent:
            raise AddonError("the add-on's notice was not agreed to")
        with self.lock:
            if aid in self._busy:
                raise AddonError("already downloading")
            self._busy.add(aid)
        try:
            rels, _, _ = self._releases(e, fresh=True)
            best = offered(e, rels)
            if best is None:
                raise AddonError("no release with the add-on's file was found")
            if e["minEarth"] and self.version:
                import updates
                if updates.newer(e["minEarth"], self.version):
                    raise AddonError(f"needs BeamNG.Earth {e['minEarth']} or newer")
            a = best["asset"]
            if a["size"] > DL_MAX:
                raise AddonError("the file is too large")
            inst = self.installed(e, rels)
            if inst["installed"] and inst.get("sha256") == a["sha256"]:
                raise AddonError("already up to date")
            os.makedirs(self.mods, exist_ok=True)
            part = os.path.join(self.mods, e["modFile"] + PART)
            tmp = os.path.join(self.mods, e["modFile"] + TMP)
            self._fetch(a, part, progress)
            why = check_mod_zip(part)
            if why:
                _remove(part)
                raise AddonError(why)
            os.replace(part, tmp)
            op = "update" if inst["installed"] else "install"
            log.info("add-ons: %s %s %s (%s, %d bytes, sha256 %s) staged as %s", op, aid, best["tag"], a["name"],
                     a["size"], a["sha256"][:12], os.path.basename(tmp))
            old_code = self.code(os.path.join(self.mods, e["modFile"]), inst["sha256"]) if inst["installed"] else None
            msg = {"type": "addon_apply", "id": aid, "op": op, "file": e["modFile"], "tmp": e["modFile"] + TMP,
                   "old": inst.get("oldFiles", []), "tag": best["tag"],
                   "active": inst.get("active") is not False, "uiApps": list(e["uiApps"]),
                   "code": addonrun.mod_code(tmp, self.game_dir)}
            if old_code is not None:        # (the installed copy's code: unloaded before the update)
                msg["oldCode"] = old_code
            return msg
        finally:
            with self.lock:
                self._busy.discard(aid)

    def _fetch(self, a, dest, progress):
        """The release's file to dest, checked against its SHA-256 (GitHub's digest, or a local file's own hash)."""
        if a.get("path"):
            import shutil
            try:
                shutil.copyfile(a["path"], dest)
            except OSError as err:
                _remove(dest)
                raise AddonError(f"could not copy the local file ({err})") from err
        else:
            self._download(a["url"], dest, a["size"], progress)
        if (a["size"] and os.path.getsize(dest) != a["size"]) or sha256_file(dest) != a["sha256"]:
            _remove(dest)
            raise AddonError("the file did not match its checksum (deleted)")

    # --- companions (a program beside the game: addonrun.Runner) ---
    def install_companion(self, aid, consent=False, progress=None):
        """Install / update a companion: its release's Python file into <data>/addons/<id>/ (a running copy is
        stopped first and started again after). Returns None (no hand-over to the mod). Raises AddonError."""
        e = self.entry(aid)
        if e["run"] is None:
            raise AddonError("this program is not installed from here yet: see its page")
        if not consent:
            raise AddonError("the add-on's notice was not agreed to")
        with self.lock:
            if aid in self._busy:
                raise AddonError("already downloading")
            self._busy.add(aid)
        try:
            rels, _, _ = self._releases(e, fresh=True)
            best = offered(e, rels)
            if best is None:
                raise AddonError("no release with the add-on's file was found")
            a = best["asset"]
            if not addonrun._SAFE.fullmatch(a["name"]):
                raise AddonError(f"not a Python file: {a['name']}")
            if a["size"] > DL_MAX:
                raise AddonError("the file is too large")
            inst = self.runner.installed(aid)
            if inst and inst.get("sha256") == a["sha256"]:
                raise AddonError("already up to date")
            d = self.runner.dir(aid)
            os.makedirs(d, exist_ok=True)
            part = os.path.join(d, a["name"] + PART)
            self._fetch(a, part, progress)
            was = self.runner.running(aid)
            self.runner.stop(aid)
            if inst and inst["file"] != a["name"]:
                _remove(os.path.join(d, inst["file"]))
            os.replace(part, os.path.join(d, a["name"]))
            self.runner.save_state(aid, file=a["name"], tag=best["tag"], sha256=a["sha256"],
                                   local=bool(best.get("local")))
            log.info("add-ons: %s %s %s (%s, %d bytes, sha256 %s) in %s", "update" if inst else "install", aid,
                     best["tag"], a["name"], a["size"], a["sha256"][:12], d)
            if was:
                self.runner.start(aid, *self.run_args(e))
            return None
        finally:
            with self.lock:
                self._busy.discard(aid)

    def run_op(self, aid, op):
        """A companion's Start / Stop / Remove / autostart on / off. Raises AddonError."""
        e = self.entry(aid)
        if e["kind"] != "companion":
            raise AddonError("not a program beside the game")
        try:
            if op == "start":
                self.runner.start(aid, *self.run_args(e))
            elif op == "stop":
                self.runner.stop(aid)
            elif op == "uninstall":
                self.runner.remove(aid)
                log.info("add-ons: %s removed", aid)
            elif op in ("autostart_on", "autostart_off"):
                if not self.runner.installed(aid):
                    raise AddonError("not installed")
                self.runner.save_state(aid, autostart=op == "autostart_on")
            else:
                raise AddonError("unknown action")
        except RuntimeError as err:
            raise AddonError(str(err)) from err

    def autostart(self):
        """The companions set to start with BeamNG.Earth, started (the server's start)."""
        for e in self.entries:
            if e["kind"] == "companion" and (self.runner.state(e["id"]) or {}).get("autostart") is True:
                try:
                    self.runner.start(e["id"], *self.run_args(e))
                except RuntimeError as err:
                    log.warning("add-ons: %s did not start: %s", e["id"], err)

    # --- options ---
    def options_of(self, e):
        """The add-on's options: its entry's, else an installed game mod's own beamngEarth_addon.json."""
        if e["options"]:
            return e["options"]
        if e["kind"] == "beamng-mod":
            inst = self.installed(e)
            if inst["installed"]:
                man = (self.code(os.path.join(self.mods, e["modFile"]), inst["sha256"]) or {}).get("manifest") or {}
                try:
                    return parse_options(man.get("options"))
                except ValueError as err:
                    log.info("add-ons: %s's own options left out: %s", e["id"], err)
        return []

    def values(self, e):
        """{key: value} of the add-on's options: the saved ones that are still valid, else the defaults."""
        saved = {}
        try:
            with open(os.path.join(self.runner.dir(e["id"]), OPTIONS_FILE), encoding="utf-8") as f:
                got = json.load(f)
            saved = got if isinstance(got, dict) else {}
        except (OSError, ValueError):
            pass
        out = {}
        for o in self.options_of(e):
            ok, v = _opt_value(o, saved.get(o["key"], o["default"])) if o["key"] in saved else (True, o["default"])
            out[o["key"]] = v if ok else o["default"]
        return out

    def set_options(self, aid, values):
        """Save the player's values (each checked against its option). True when a running program was restarted
        for them. Raises AddonError."""
        e = self.entry(aid)
        opts = self.options_of(e)
        if not opts:
            raise AddonError("this add-on has no options")
        if not isinstance(values, dict):
            raise AddonError("no values")
        cur = self.values(e)
        for o in opts:
            if o["key"] in values:
                ok, v = _opt_value(o, values[o["key"]])
                if not ok:
                    raise AddonError(f"{o['label']}: not a valid value")
                cur[o["key"]] = v
        d = self.runner.dir(aid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, OPTIONS_FILE + ".tmp"), "w", encoding="utf-8") as f:
            json.dump(cur, f)
        os.replace(os.path.join(d, OPTIONS_FILE + ".tmp"), os.path.join(d, OPTIONS_FILE))
        log.info("add-ons: %s options saved: %s", aid, cur)
        if e["kind"] == "companion" and self.runner.running(aid):
            self.runner.stop(aid)
            self.runner.start(aid, *self.run_args(e))
            return True
        return False

    def run_args(self, e):
        """(args, env) a program beside the game starts with: its run args, then its options' (arg value / env)."""
        args, env = list((e["run"] or {}).get("args", ())), {}
        vals = self.values(e)
        for o in self.options_of(e):
            v = vals[o["key"]]
            if o["arg"]:
                if o["type"] == "bool":
                    if v:
                        args.append(o["arg"])
                elif v != "":
                    args += [o["arg"], str(v)]
            if o["env"]:
                env[o["env"]] = ("1" if v else "0") if o["type"] == "bool" else str(v)
        return args, env

    def page(self, e):
        """The program's own page with its options' values in it ({port}), or "" (none, or not a local page then)."""
        page = (e["run"] or {}).get("page", "")
        if not page:
            return ""
        vals = self.values(e)
        out = re.sub(r"\{([a-z][A-Za-z0-9_]{0,30})\}", lambda m: str(vals.get(m.group(1), m.group(0))), page)
        return out if _PAGE.fullmatch(out) and "{" not in out else ""

    # --- Add from GitHub ---
    def add_repo(self, text):
        """Add a GitHub repo the player pasted to the local list (added: true). Returns its id. Raises AddonError."""
        repo = parse_repo(text)
        try:
            body, _, _ = self.github.get(f"{API}{repo}", fresh=True)
            info = json.loads(body)
        except (AddonError, ValueError) as err:
            raise AddonError(f"could not read {repo} on GitHub ({err})") from err
        if not isinstance(info, dict) or not isinstance(info.get("full_name"), str):
            raise AddonError(f"no repository {repo} on GitHub")
        repo = info["full_name"]
        if not _REPO.fullmatch(repo):
            raise AddonError("an unusual repository name")
        for e in self.entries:          # (already on the list: that one)
            if e["repo"].lower() == repo.lower():
                return e["id"]
        try:
            body, _, _ = self.github.get(f"{API}{repo}/releases?per_page={RELEASES_N}", fresh=True)
            rels = json.loads(body)
        except (AddonError, ValueError) as err:
            raise AddonError(f"could not read its releases ({err})") from err
        names = []
        for r in rels if isinstance(rels, list) else ():
            if isinstance(r, dict) and not r.get("draft") and not r.get("prerelease"):
                names = [str(a.get("name") or "") for a in r.get("assets") or () if isinstance(a, dict)]
                break
        if not names:
            raise AddonError("it has no release with files to install (only repositories with GitHub releases work)")
        aid = _repo_id(repo, {e["id"] for e in self.entries})
        owner, name = repo.split("/", 1)
        lic = info.get("license") if isinstance(info.get("license"), dict) else {}
        raw = {"id": aid, "name": name[:80], "author": owner[:40], "repo": repo, "added": True,
               "summary": (str(info.get("description") or "") or f"{name} from github.com/{repo}")[:400],
               "licence": (str(lic.get("name") or "") or "No licence named in the repository")[:300],
               "trust": "latest"}
        if any(n.lower().endswith(".zip") for n in names):
            raw.update(kind="beamng-mod", asset=r"\.zip$", modFile=f"{aid}.zip")
        elif any(addonrun._SAFE.fullmatch(n) for n in names):
            raw.update(kind="companion", asset=r"\.py$", run={"args": []})
        else:
            raw.update(kind="link", asset="")
        try:            # (the author's own declaration at the repository's root: its options, a program's run)
            text, _, _ = self.github.get(f"{API}{repo}/contents/{MANIFEST}", accept="application/vnd.github.raw+json",
                                         fresh=True)
            man = json.loads(text) if len(text) <= 64 * 1024 else {}
        except (AddonError, ValueError):
            man = {}
        if isinstance(man, dict):
            if man.get("options") is not None:
                try:
                    parse_options(man["options"])
                    raw["options"] = man["options"]     # (kept as written: _entry parses it again)
                except (ValueError, TypeError) as err:
                    log.info("add-ons: %s's %s options left out: %s", repo, MANIFEST, err)
            if raw["kind"] == "companion" and isinstance(man.get("run"), dict):
                raw["run"] = {k: man["run"][k] for k in ("args", "page", "port", "lan") if k in man["run"]}
        try:
            _entry(raw, local=True)              # (the same checks as any entry)
        except ValueError:
            raw.pop("options", None)
            raw["run"] = {"args": []} if raw["kind"] == "companion" else raw.get("run")
            if raw.get("run") is None:
                raw.pop("run", None)
            _entry(raw, local=True)
        doc = self._read_local()
        doc["addons"] = [a for a in doc["addons"] if a.get("repo", "").lower() != repo.lower()] + [raw]
        self._write_local(doc)
        self.entries = self._merge(self.bundled)
        log.info("add-ons: %s added from github.com/%s (%s)", aid, repo, raw["kind"])
        return aid

    def forget(self, aid):
        """An entry the player added taken off the local list (not while installed). Raises AddonError."""
        e = self.entry(aid)
        if not e["added"]:
            raise AddonError("only add-ons you added can be removed from the list")
        if (e["kind"] == "beamng-mod" and self.installed(e)["installed"]) or \
                (e["kind"] == "companion" and self.runner.installed(aid)):
            raise AddonError("remove it first")
        doc = self._read_local()
        doc["addons"] = [a for a in doc["addons"] if a.get("id") != aid]
        self._write_local(doc)
        self.entries = self._merge(self.bundled)
        log.info("add-ons: %s taken off the list", aid)

    def _read_local(self):
        try:
            with open(self.local_path(), encoding="utf-8") as f:
                doc = json.load(f)
            if isinstance(doc, dict) and isinstance(doc.get("addons"), list):
                return doc
        except (OSError, ValueError):
            pass
        return {"format": REGISTRY_FORMAT, "addons": []}

    def _write_local(self, doc):
        doc.setdefault("format", REGISTRY_FORMAT)
        p = self.local_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        os.replace(p + ".tmp", p)

    def openable_local(self, url):
        """A companion's own local web page (its entry's run.page, any path under its origin)."""
        if not isinstance(url, str) or not url.startswith("http://"):
            return False
        for e in self.entries:
            page = self.page(e) if e["kind"] == "companion" else ""
            if page:
                origin = "/".join(page.split("/")[:3])
                if (url == origin or url.startswith(origin + "/")) and not any(c in url for c in ' "\'<>`\\\n\r'):
                    return True
        return False

    def _download(self, url, dest, size, progress):
        try:
            with self.net.session(url, kind="demand", lane=LANE) as s:
                req = urllib.request.Request(url, headers=s.headers)
                with _urlopen(req) as resp:
                    if not resp.geturl().startswith("https://"):
                        raise AddonError("the download was redirected off https")
                    s.status = resp.status
                    done = 0
                    with open(dest, "wb") as f:
                        while True:
                            chunk = resp.read(CHUNK)
                            if not chunk:
                                break
                            done += len(chunk)
                            if done > DL_MAX:
                                raise AddonError("the file is too large")
                            f.write(chunk)
                            s.nbytes = done
                            if progress:
                                progress(done, size)
        except (netfetch.FetchError, OSError) as e:
            _remove(dest)
            raise AddonError(f"the download failed ({e})") from e
        except AddonError:
            _remove(dest)
            raise

    def applied(self, aid, op, ok, error=""):
        """The mod's answer after it handed the file to the game's mod manager (or enabled / disabled / removed the
        mod): logged; a leftover .bnge-tmp removed."""
        e = self.entry(aid)
        log.info("add-ons: %s %s in the game: %s%s", op, aid, "done" if ok else "failed", f" ({error})" if error else "")
        if e["kind"] == "beamng-mod" and (op == "uninstall" or not ok):
            _remove(os.path.join(self.mods, e["modFile"] + TMP))


def _urlopen(req):
    import ioprofiler
    return ioprofiler.urlopen(req, timeout=60, context=netfetch.windows_ssl_context())


MOD_TOP = ("lua/", "ui/", "scripts/", "vehicles/", "levels/", "art/", "gameplay/", "settings/", "mods_data/",
           "campaigns/", "core/", "flowEditor/", "locales/")


def check_mod_zip(path):
    """Why the file is not a BeamNG mod zip ("" when it is one): a readable zip (CRCs checked), no absolute or ..
    names, and the game's folders at its top (a zip of a folder of the mod would not load)."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if z.testzip() is not None:
                return "the downloaded file is a damaged zip (deleted)"
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        return "the downloaded file is not a zip (deleted)"
    if not names:
        return "the downloaded zip is empty (deleted)"
    for n in names:
        if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/") or ":" in n:
            return "the downloaded zip has unsafe file names (deleted)"
    if not any(n.replace("\\", "/").startswith(MOD_TOP) for n in names):
        return "the downloaded zip is not laid out as a BeamNG mod (deleted)"
    return ""


_URL = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})(?:[/?#].*)?")


def parse_repo(text):
    """owner/name of a pasted repository (owner/name, or a github.com link to it or to a page in it). AddonError."""
    t = (text or "").strip() if isinstance(text, str) else ""
    m = _URL.fullmatch(t)
    repo = f"{m.group(1)}/{m.group(2)}" if m else t
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _REPO.fullmatch(repo) or ".." in repo:
        raise AddonError("paste a GitHub repository: owner/name or its github.com link")
    return repo


def _repo_id(repo, taken):
    """A list id for a pasted repository: its name in lower case ([a-z0-9-]), made unique."""
    base = re.sub(r"[^a-z0-9]+", "-", repo.split("/", 1)[1].lower()).strip("-")[:30] or "addon"
    if not base[0].isalnum():
        base = "a" + base
    base = base if len(base) >= 2 else base + "-x"
    aid, n = base, 2
    while aid in taken:
        aid, n = f"{base}-{n}", n + 1
    return aid


def _lan_ip():
    """This computer's address on its network (the one a phone on the same Wi-Fi reaches), or 127.0.0.1."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))          # (no packet is sent: it only picks the outgoing interface)
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _remove(p):
    try:
        os.remove(p)
    except OSError:
        pass
