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
the BeamNG.Earth-data Pages site (REGISTRY_URL, off until it exists) can replace the bundled one without a release.

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

Kind "companion" (a program beside the game, LiveGPS) and "link": shown with their README, changelog and download
links; installing them is not done yet (docs/ADDONS.md phase 3).
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

import netfetch

log = logging.getLogger("addons")

REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "addons.json")
REGISTRY_URL = ""           # the BeamNG.Earth-data Pages copy, once it is published there ("" = the bundled one only)
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
_TEXT_MAX = {"name": 80, "author": 40, "summary": 400, "licence": 300, "note": 600}


class AddonError(RuntimeError):
    """An add-on action could not be done (the message says why, for the player)."""


# --- the registry -----------------------------------------------------------------------------------------------

def parse_registry(data):
    """The valid entries of a registry document (a dict), in order; an invalid entry is left out (logged)."""
    if not isinstance(data, dict) or data.get("format") != REGISTRY_FORMAT or not isinstance(data.get("addons"), list):
        raise ValueError("not an add-on registry of format %d" % REGISTRY_FORMAT)
    out, seen = [], set()
    for raw in data["addons"]:
        try:
            e = _entry(raw)
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


def _entry(raw):
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
    e["minEarth"] = raw.get("minEarth") or ""
    if not isinstance(e["minEarth"], str):
        raise ValueError("bad minEarth")
    return e


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

    def __init__(self, user_dir, cache_dir, net, version="", registry=None, md=None, clock=time.time):
        self.user_dir = user_dir
        self.mods = os.path.join(user_dir, "mods")
        self.net = net
        self.version = version or ""
        self.entries = registry if registry is not None else load_registry()
        self.github = GitHubCache(os.path.join(cache_dir, "addons"), net, clock=clock)
        self.md = md                    # (text, base, fold) -> (html, links); fast.md_html by default
        self._links = set()             # every URL an add-on's text showed (openable)
        self._hashes = {}               # path -> ((size, mtime_ns), sha256)
        self._busy = set()              # ids being downloaded
        self.lock = threading.Lock()
        self.clean_stale()

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
        """Whether the page may open url in the browser: a link of an add-on's text, or a page of a registry repo."""
        if not isinstance(url, str) or not url.startswith("https://") or any(c in url for c in ' "\'<>`\\\n\r'):
            return False
        if any(url == f"https://github.com/{e['repo']}" or url.startswith(f"https://github.com/{e['repo']}/")
               for e in self.entries):
            return True
        with self.lock:
            return url in self._links

    def _releases(self, e, fresh=False):
        body, age, stale = self.github.get(f"{API}{e['repo']}/releases?per_page={RELEASES_N}", fresh=fresh)
        return releases(e, body), age, stale

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
             "error": "", "age": None, "stale": False, "tooNew": False}
        try:
            rels, c["age"], c["stale"] = self._releases(e, fresh)
        except AddonError as err:
            rels = []
            c["error"] = str(err)
        best = offered(e, rels)
        if best:
            c.update(latest=best["tag"], latestDate=best["date"], size=best["asset"]["size"],
                     reviewed=best["asset"]["sha256"] in e["reviewed"])
        if e["kind"] == "beamng-mod":
            inst = self.installed(e, rels)
            c.update(installed=inst["installed"], oldFiles=inst.get("oldFiles", []))
            if inst["installed"]:
                c.update(version=inst["version"] or "unknown", active=inst["active"],
                         installedReviewed=inst["sha256"] in e["reviewed"])
                c["update"] = bool(best) and best["asset"]["sha256"] != inst["sha256"]
            if e["minEarth"] and self.version:
                import updates
                c["tooNew"] = updates.newer(e["minEarth"], self.version)
            c["canInstall"] = bool(best) and not c["tooNew"]
        return c

    def refresh_registry(self, fresh=False):
        """The Pages copy of the registry instead of the bundled one, when REGISTRY_URL is set and it answers with a
        valid one (kept on disk like GitHub's pages); else the entries stay as they are."""
        if not REGISTRY_URL:
            return
        try:
            body, _, _ = self.github.get(REGISTRY_URL, accept="application/json", fresh=fresh)
            got = parse_registry(json.loads(body))
        except (AddonError, ValueError) as e:
            log.info("add-ons: the registry on %s: %s; the bundled one stays", REGISTRY_URL, e)
            return
        if got:
            self.entries = got

    def cards(self, fresh=False):
        self.refresh_registry(fresh)
        return [self.card(e, fresh) for e in self.entries]

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
            if e["kind"] != "beamng-mod" and r["asset"]:     # (a program beside the game: its file, opened in the browser)
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
            self._download(a["url"], part, a["size"], progress)
            if (a["size"] and os.path.getsize(part) != a["size"]) or sha256_file(part) != a["sha256"]:
                _remove(part)
                raise AddonError("the download did not match GitHub's checksum (deleted)")
            why = check_mod_zip(part)
            if why:
                _remove(part)
                raise AddonError(why)
            os.replace(part, tmp)
            op = "update" if inst["installed"] else "install"
            log.info("add-ons: %s %s %s (%s, %d bytes, sha256 %s) staged as %s", op, aid, best["tag"], a["name"],
                     a["size"], a["sha256"][:12], os.path.basename(tmp))
            return {"type": "addon_apply", "id": aid, "op": op, "file": e["modFile"], "tmp": e["modFile"] + TMP,
                    "old": inst.get("oldFiles", []), "tag": best["tag"],
                    "active": inst.get("active") is not False, "uiApps": list(e["uiApps"])}
        finally:
            with self.lock:
                self._busy.discard(aid)

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


def _remove(p):
    try:
        os.remove(p)
    except OSError:
        pass
