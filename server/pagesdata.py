"""BeamNG.Earth-data's GitHub Pages: a dataset's manifest (the build in use) pinned on disk, the primitives that read,
pin and fetch it, and the server run's one decision of each dataset's build (the session).

A dataset (rail, lanes, signs, regional...) publishes <PAGES>/<name>/manifest.json beside its shards; its "built"
names the build the shards on disk belong to, and its "data" (data_url) the https folder the build's files are served
from instead (the tile host, R2 behind Cloudflare's cache, data/<dataset>/<key>/, never overwritten), or none: beside the
manifest on Pages.

One build per server run (2026-11-24, ~/bnge-tests/manifestaudit/REPORT.md R1-R6): every manifest is decided once per
run and never replaced while the run uses it. Before, a pin older than MANIFEST_TTL_S was used at once and refreshed in
the background, and the refresh's build taken mid-run, in each process at its own time: the first minutes of almost
every session planned cells on two builds side by side (their plans in one run folder, tiles keyed on the newer),
filed Pages' newer shards under the older build, and re-joined shared layers column by column. Now:
- the server's main process decides each weekly dataset before its worker processes start (MapData.freeze_manifests):
  the manifest pinned on disk, whatever its age; without one (a first run) one fetched then, FETCH_WAIT_S at most, else
  "none" (no data) for the whole run. A decision is a file in the run's session folder (SESSION_ENV, handed to every
  worker process, respawned and elastic ones too, by the environment they inherit); the first decision of a dataset
  wins, so a dataset first asked in a worker (lazily, e.g. a roof folder) is still one build in every process;
- a pin older than MANIFEST_TTL_S is fetched again in the background by the process that decided it, and pinned for
  the NEXT start only (stale-if-error: a failure faults no build, netfetch.stale_ok; asked again after RETRY_S,
  doubling per failure in a row up to MANIFEST_TTL_S: netfetch.Backoff);
- a build's files come from its "data" host (build-addressed); Pages (no "data", or the data host failing) only while
  Pages' own manifest names the session's build and the file comes from the same Pages deploy as that manifest
  (Manifest.fetch: else OtherBuild, the file not had in this run);
- a process without a session folder (tools, tests, a server's helper started on its own) decides once per process;
- a pinned session (seed_session: tools/build_cip.py --pins, the CIP planet build's shards, 2026-11-24) holds exactly
  the decisions it was seeded with, made days before in another job: a dataset not in it is "none" for the run in
  every process (never decided from the disk or the network), and no pin is fetched again.

(2026-11-09, the night of 1.9.0: GitHub answered 504s for hours; every road worker process asked each dataset's
manifest for itself, 2 tries x 20 s, two at a time, and dropped the data it had on disk once a day's manifest was due:
world gen hung at 15 % for players. Now only the deciding process asks, once.)

rail tags, lane tags, sign tags (railtags.Shards), the OSM tag / year / colour / area / tall-building archives
(osmtagshards, osmyears ...), New York's shards (nycshards: session_take) and the regional scenery (regionaldata) read
their manifests and fetch their files through the one state machine here.
"""
import json
import logging
import os
import threading
import time

import netfetch

log = logging.getLogger("earthd.pagesdata")

PAGES = os.environ.get("BNE_DATA_PAGES", "https://tecnagamer.github.io/BeamNG.Earth-data")

MANIFEST_TTL_S = 24 * 3600      # a pin older than this is fetched again in the background (for the next start)
RETRY_S = 3600                  # that fetch failing: asked again after this long (doubling)
FETCH_WAIT_S = 90.0             # s a first run waits for a manifest none is pinned of (then "none" for the run)
SESSION_ENV = "BNGE_PAGES_SESSION"  # the server run's folder of decided manifests (earthd, before its workers start)
SESSION_KEEP_S = 7 * 86400      # other runs' session folders untouched this long are removed (start_session)
PAGES_CHECK_S = 60.0            # s a look at Pages' manifest (Manifest.fetch's Pages fallback) is trusted
SAME_DEPLOY_S = 1800.0          # s between two files' Last-Modified on Pages within which they are one deploy


def read_pin(d):
    """(the manifest pinned in folder d, its age s), or (None, None)."""
    try:
        path = os.path.join(d, "manifest.json")
        age = time.time() - os.path.getmtime(path)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), age
    except (OSError, TypeError, ValueError):
        return None, None


def data_url(m):
    """A dataset manifest's "data": the https folder its build's files are served from (the data repo's workflow puts
    them on the project's tile host, R2 behind Cloudflare's cache, data/<dataset>/<build>/, the same paths as on
    Pages), or None: the files beside the manifest on Pages. (2026-11-11: ~1.15 TB a month of players' reads against
    Pages' 100 GB soft limit.)"""
    d = m.get("data") if isinstance(m, dict) else None
    return d.rstrip("/") if isinstance(d, str) and d.startswith("https://") and len(d) > 8 else None


def fetch_data(fetcher, data, pages, rel, kind="demand"):
    """rel's bytes from a dataset's data host (data: data_url's, or None) else its Pages folder (pages). The data
    host failing (not a 404: that answers for the build) is asked of Pages as well, while Pages still has the files
    (old clients read them there); that failing too raises the data host's error, never a NotFound (no "none
    there" from a copy that may be gone). Not bound to a build: only for files checked against their manifest's entry
    (nycshards: each shard's sha256); a dataset's Manifest.fetch reads Pages only while Pages serves the run's build."""
    if not data:
        return fetcher.get(f"{pages}/{rel}", kind=kind).body
    try:
        return fetcher.get(f"{data}/{rel}", kind=kind).body
    except (netfetch.NotFound, netfetch.Deferred):
        raise
    except Exception as e:
        first = e
    try:
        return fetcher.get(f"{pages}/{rel}", kind=kind).body
    except Exception:
        raise first from None


def write_atomic(path, body):
    """body to path whole: a temporary file of this process and thread's own, renamed over it. Several fetching the
    same file at once (the road processes' first tiles in Germany on a cold cache) each write a whole one; a shared
    temporary name had one's rename fail and another read a part of it. A rename refused with path there (Windows:
    another process holds it open) keeps that copy, another fetch's of the same bytes; a write that fails (a full disk)
    leaves no temporary file behind."""
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(body)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if not os.path.exists(path):
            raise


def fetch_pin(fetcher, base, d, raw=False):
    """The manifest of base (a dict; raw: (dict, its text, its Last-Modified or None)), fetched and pinned in folder d
    (None: not kept); raises when it cannot be had."""
    r = fetcher.get(f"{base}/manifest.json", kind="demand")
    body = r.body
    text = body.decode("utf-8")
    got = json.loads(text)
    if not isinstance(got, dict):
        raise ValueError("the manifest is not an object")
    if d:
        os.makedirs(d, exist_ok=True)
        write_atomic(os.path.join(d, "manifest.json"), body)
    return (got, text, _last_modified(r)) if raw else got


def read_pin_text(d):
    """(the text of the manifest pinned in folder d, its age s), or (None, None) (none, or not a JSON object)."""
    try:
        path = os.path.join(d, "manifest.json")
        age = time.time() - os.path.getmtime(path)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if not isinstance(json.loads(text), dict):
            return None, None
        return text, age
    except (OSError, TypeError, ValueError):
        return None, None


def _last_modified(resp):
    import email.utils
    try:
        v = (getattr(resp, "headers", None) or {}).get("last-modified")
        return email.utils.parsedate_to_datetime(v).timestamp() if v else None
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def build_of(manifest):
    """The build ("built") a manifest names, or None."""
    return manifest.get("built") if isinstance(manifest, dict) else None


class OtherBuild(netfetch.FetchFailed):
    """Pages serves another build than the session's (or cannot say which): its file is not had in this run (a fault,
    as any file not had: what is built without it is not kept)."""


# --- the session: one decision per dataset per server run ------------------------------------------------------------
_PROC = {}                      # (session folder or cache folder, ident) -> manifest body (str) or None: decided
_PROC_LOCK = threading.Lock()


def start_session(cache_dir):
    """earthd's main process, before its worker processes start: a session folder of this run's own in the environment
    they inherit (SESSION_ENV); other runs' folders untouched for SESSION_KEEP_S removed. The folder."""
    import shutil
    root = os.path.join(cache_dir, "pagesdata", "session")
    now = time.time()
    try:
        for n in os.listdir(root):
            p = os.path.join(root, n)
            try:
                if now - os.path.getmtime(p) > SESSION_KEEP_S:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass
    d = os.path.join(root, f"{os.getpid()}_{time.time_ns()}")
    os.makedirs(d, exist_ok=True)
    os.environ[SESSION_ENV] = d
    _OWNER[:] = [d, os.getpid()]
    return d


_OWNER = []                     # [session folder, pid] of the process that started the session (start_session)


def session_owner(cache_dir):
    """Is this process the one that started cache_dir's session (the server's main process: it fetches the stale pins
    again for the next start, whoever decided them)?"""
    d = session_dir(cache_dir)
    return d is not None and _OWNER == [d, os.getpid()]


def ensure_session(cache_dir):
    """A tool or helper that starts worker processes (build_decisions, bake_area, the BeamMP share helper): the session
    it inherited when it is cache_dir's (a server's helper: the server's builds), else one of its own. The folder."""
    return session_dir(cache_dir) or start_session(cache_dir)


def session_dir(cache_dir):
    """The run's session folder when it is cache_dir's (SESSION_ENV), else None (decided per process)."""
    d = os.environ.get(SESSION_ENV)
    if not d or not cache_dir:
        return None
    try:
        if os.path.normcase(os.path.abspath(os.path.dirname(os.path.dirname(d)))) != \
                os.path.normcase(os.path.abspath(os.path.join(cache_dir, "pagesdata"))):
            return None
    except (TypeError, ValueError):
        return None
    return d


def _entry(sdir, ident):
    import hashlib
    import re
    name = re.sub(r"[^0-9A-Za-z_.-]+", "_", ident.split("|", 1)[0])[:40]
    return os.path.join(sdir, f"{name}-{hashlib.sha256(ident.encode('utf-8')).hexdigest()[:16]}.json")


def _read_entry(path, ident):
    """(True, body) of a decision file for ident, (False, None) none (or not whole yet)."""
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return False, None
    if not isinstance(got, dict) or got.get("ident") != ident:
        return False, None
    body = got.get("body")
    return True, body if isinstance(body, str) else None


def session_get(cache_dir, ident):
    """(True, body) when dataset ident's manifest is decided for this run (body: its text, None: no data this run), else
    (False, None). ident: the dataset's name and base URL ("<name>|<base>")."""
    sdir = session_dir(cache_dir)
    k = (sdir or cache_dir, ident)
    with _PROC_LOCK:
        if k in _PROC:
            return True, _PROC[k]
    if sdir is None:
        return False, None
    found, body = _read_entry(_entry(sdir, ident), ident)
    if not found and session_pinned(cache_dir) is not None:
        # (a pinned run: what the pin set does not name is no data, in every process, never this run's own decision)
        log.warning("pagesdata %s: not in the run's pin set: no data this run", ident)
        body, _ = session_put(cache_dir, ident, None, "unpinned")
        return True, body
    if found:
        with _PROC_LOCK:
            body = _PROC.setdefault(k, body)
    return found, body


PINS_NAME = "_pins.json"        # in a session folder: the run is pinned (seed_session); its idents
_PINNED = {}                    # session folder -> frozenset of the pinned idents, or None (not pinned)


def session_pinned(cache_dir):
    """The idents a pinned session (seed_session) was seeded with (a frozenset), or None: not a pinned session (or
    no session)."""
    sdir = session_dir(cache_dir)
    if sdir is None:
        return None
    with _PROC_LOCK:
        if sdir in _PINNED:
            return _PINNED[sdir]
    try:
        with open(os.path.join(sdir, PINS_NAME), encoding="utf-8") as fh:
            got = frozenset(str(i) for i in json.load(fh)["idents"])
    except (OSError, ValueError, KeyError, TypeError):
        got = None
    with _PROC_LOCK:
        return _PINNED.setdefault(sdir, got)


def seed_session(cache_dir, datasets):
    """A pinned run (tools/build_cip.py --pins): a session folder of its own (start_session, in the environment its
    worker processes inherit) holding exactly the decisions datasets gives ({ident: manifest text, or None: no data}),
    marked pinned: every process of the run reads those builds, a dataset not named is none for the run (session_get),
    and no pin is fetched again (Manifest._adopt). Raises when a decision could not be recorded as given. The folder."""
    d = start_session(cache_dir)
    for ident in sorted(datasets):
        body = datasets[ident]
        if body is not None and not isinstance(body, str):
            raise ValueError(f"pin {ident}: not a manifest's text")
        got, _won = session_put(cache_dir, ident, body, "pins")
        if got != body or _read_entry(_entry(d, ident), ident) != (True, body):
            raise RuntimeError(f"pin {ident}: the session holds another decision")
    write_atomic(os.path.join(d, PINS_NAME), json.dumps({"idents": sorted(datasets)}).encode("utf-8"))
    with _PROC_LOCK:
        _PINNED.pop(d, None)
    return d


def session_entries(cache_dir):
    """{ident: manifest text or None} of every decision in the run's session folder (the pin set a plan records:
    tools/build_cip.py), {} without a session."""
    sdir = session_dir(cache_dir)
    out = {}
    if sdir is None:
        return out
    try:
        names = sorted(os.listdir(sdir))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json") or n == PINS_NAME:
            continue
        try:
            with open(os.path.join(sdir, n), encoding="utf-8") as fh:
                got = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(got, dict) and isinstance(got.get("ident"), str) and _entry(sdir, got["ident"]) == \
                os.path.join(sdir, n):
            body = got.get("body")
            out[got["ident"]] = body if isinstance(body, str) else None
    return out


def session_put(cache_dir, ident, body, who=""):
    """Decide dataset ident's manifest for this run (body: its text, or None: no data this run) unless another thread
    or process decided it first. (the decision in use, whether it is this call's): the first one wins, in every process
    of the run."""
    sdir = session_dir(cache_dir)
    k = (sdir or cache_dir, ident)
    won = True
    if sdir is not None:
        path = _entry(sdir, ident)
        rec = {"ident": ident, "body": body, "pid": os.getpid(), "by": who, "at": time.time()}
        tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.part"
        try:
            os.makedirs(sdir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh)
            try:
                if os.name == "nt":
                    os.rename(tmp, path)        # (Windows: refused when the file is there, the first one wins)
                else:
                    os.link(tmp, path)          # (likewise: a link never replaces a file)
            except FileExistsError:
                won = False
            except OSError:                     # (no hard links on this file system: created exclusively, written)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(rec, fh)
                except FileExistsError:
                    won = False
        except OSError as e:                    # (the folder unwritable: this process's own decision)
            log.warning("pagesdata session %s: %s: %s", ident, type(e).__name__, e)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not won:
            for _ in range(100):                # (the winner's file; one written in place may be a moment from whole)
                found, got = _read_entry(path, ident)
                if found:
                    body = got
                    break
                time.sleep(0.02)
    with _PROC_LOCK:
        if k in _PROC:
            return _PROC[k], False
        _PROC[k] = body
        return body, won


def session_reset():
    """Forget this process's decisions (tests)."""
    with _PROC_LOCK:
        _PROC.clear()
        _PINNED.clear()


def session_take(cache_dir, ident, decide, who=""):
    """Dataset ident's manifest text for this run (None: no data this run): the run's decision, else decide() -> text or
    None, recorded (session_put: the first decision in any process of the run wins). (body, mine): mine when this call
    decided it."""
    found, body = session_get(cache_dir, ident)
    if found:
        return body, False
    return session_put(cache_dir, ident, decide(), who)


class Manifest:
    """The manifest in use of one BeamNG.Earth-data Pages dataset (name: its folder under PAGES), pinned in
    <cache_dir>/pagesdata/<name>, decided once for the server run (the session: see the module docstring) and never
    replaced while the run uses it.

    current() is the manifest in use (deciding it at the first ask); known()/build() its build (the tiles' cache keys;
    known() decides from the session or the pin without asking the network, None while neither has one). fetch(rel)
    reads a file of the build from the manifest's "data" host (data/None in self.data, data_url), else Pages while Pages
    serves the same build (OtherBuild otherwise). key (build_of) is what "another build" means for a dataset; on_change
    runs (under self.lock) when the build is taken, so nothing decoded before it is used. local (() -> folder) reads
    that folder's manifest.json instead of Pages, once. recheck is kept for its callers: every manifest is frozen for
    the run now (regionaldata asked for it with recheck=False; the background refresh replaced it anyway, R6)."""

    def __init__(self, cache_dir, fetcher, name, *, base=None, ttl=MANIFEST_TTL_S, retry_s=RETRY_S,
                 wait_s=FETCH_WAIT_S, key=build_of, on_change=None, local=None, recheck=True):
        self.name = name
        self.cache_dir = cache_dir
        self.base = base or f"{PAGES}/{name}"
        self.dir = os.path.join(cache_dir, "pagesdata", name) if cache_dir else None
        self.fetcher = fetcher
        self.ttl, self.retry_s, self.wait_s = ttl, retry_s, wait_s
        self.key = key
        self.on_change = on_change
        self.local = local          # () -> local folder whose manifest.json is read instead of Pages (None: Pages)
        self.recheck = recheck      # (not used: every manifest is the run's once in use)
        self.lock = threading.Lock()
        self.manifest = None        # the manifest in use
        self.built = None           # its key (the build), what cache keys are made from
        self.data = None            # its "data" host (data_url), None: the files beside the manifest on Pages
        self.decided = False        # the run's manifest is taken (for the rest of the process)
        self.mine = False           # this process decided it: it fetches the pin again for the next start
        self.refresh_at = None      # (mine) wall time the pin is fetched again, None: not due
        self.down_at = 0.0          # the last failed first fetch
        self.backoff = netfetch.Backoff(retry_s, max(retry_s, ttl))   # the pin's refresh failing: asked again
        self.fetching = None        # threading.Event while a manifest fetch runs (the others wait for its answer)
        self.missing = False        # the first fetch was a 404 (not published), not a failure
        # a first fetch that failed (none for the run) faults the asking thread's build: what is built without the data
        # is not kept under keys that do not name it; False for a dataset whose "none" is in the keys (osmyears)
        self.fault_none = True
        # Pages read whatever build it serves (Manifest.fetch): a dataset whose files are kept without a build and in no
        # key (lod2roofs' static roofs), where the build check would only drop data
        self.any_build = False
        self._pages_seen = (-1e18, None, None)      # (monotonic s, Pages' build, its Last-Modified): Manifest.fetch

    def ident(self):
        """The dataset's name in the session: its name and folder URL."""
        return f"{self.name}|{self.base}"

    def known(self, recheck_s=None):
        """The build in use as far as known without asking the network: the run's decision, else the one pinned on
        disk (decided then), else None (none pinned yet). (The tiles built in a run's first minutes had other keys
        than the same tiles built later, in the revisit cache: a warm drive rebuilt and stored again ~1.5 GB of far
        tiles, 2026-11-04.) recheck_s: with none pinned yet, the folder is looked at again at most this often (None:
        once; another process pins it a few seconds into a first run, 2026-11-13)."""
        if self.decided or self.built is not None:
            return self.built
        now = time.monotonic()
        last = getattr(self, "_pin_at", None)
        if last is not None and (recheck_s is None or now - last <= recheck_s):
            return None
        self._pin_at = now
        self._decide(network=False)
        return self.built

    def current(self):
        """The manifest in use (a dict), or None (no data this run).

        Decided at the first ask (_decide): the run's decision when one is made (by the main process before the
        workers start, or another process), else the manifest pinned on disk whatever its age, else (a first run) one
        fetched now by one thread while the others wait, wait_s at most, else none for the run. A pin older than ttl
        is fetched again in the background by the process that decided it, for the next start (stale-if-error).
        A local folder's manifest is read once, before Pages, and kept."""
        local = self.local() if self.local is not None else None
        if local is not None:
            with self.lock:
                if self.manifest is not None:
                    return self.manifest
            try:
                with open(os.path.join(local, "manifest.json"), encoding="utf-8") as fh:
                    got = json.load(fh)
            except (OSError, ValueError) as e:
                log.warning("%s: %s: %s", self.name, local, e)
                got = None
            return self._take(got)
        if not self.decided:
            if self.fetcher is None:
                return None
            self._decide(network=True)
        if self.refresh_at is not None:
            self._maybe_refresh()
        return self.manifest

    def _adopt(self, text, mine, age=None):
        """Take the run's decision (text: the manifest's, None: no data this run) unless one is taken already; mine:
        this process decided it, from a pin age s old (None: fetched now or no pin): it fetches the pin again once
        it is older than ttl."""
        m = None
        if text is not None:
            try:
                m = json.loads(text)
            except ValueError:
                m = None
            if not isinstance(m, dict):
                m = None
        if session_pinned(self.cache_dir) is not None:
            mine = False                # (a pinned run's builds were decided elsewhere: no pin fetched again here)
        elif not mine and m is not None and self.dir and session_owner(self.cache_dir):
            mine = True                 # (the session's main process refreshes every stale pin, whoever decided it)
            try:
                age = time.time() - os.path.getmtime(os.path.join(self.dir, "manifest.json"))
            except OSError:
                age = None
        with self.lock:
            if self.decided:
                return self.manifest
            key = self.key(m) if m is not None else None
            changed = key != self.built
            self.manifest, self.built, self.data = m, key, data_url(m)
            self.decided, self.mine = True, bool(mine)
            if mine and m is not None and self.dir and self.fetcher is not None:
                self.refresh_at = time.time() + max(0.0, self.ttl - (age or 0.0))
            if changed:
                self._changed()
            return self.manifest

    def _decide(self, network):
        """The run's manifest taken (and decided, when no process has): the session's, the pin's, else (network) a
        fetched one. network false: nothing taken when neither the session nor the disk has one."""
        ident = self.ident()
        found, text = session_get(self.cache_dir, ident)
        if found:
            return self._adopt(text, False)
        text, age = read_pin_text(self.dir) if self.dir else (None, None)
        if text is not None:
            got, mine = session_put(self.cache_dir, ident, text, self.name)
            return self._adopt(got, mine, age)
        if not network:
            return None
        with self.lock:
            if self.decided:
                return self.manifest
            ev = self.fetching
            mine = ev is None
            if mine:
                ev = self.fetching = threading.Event()
        if not mine:                # (another thread's fetch: its answer, else none for the run)
            ev.wait(self.wait_s)
            with self.lock:
                if self.decided:
                    return self.manifest
            got, _ = session_put(self.cache_dir, ident, None, self.name)
            return self._adopt(got, False)
        out = {}

        def fetch():
            try:
                with netfetch.stale_ok(True):   # (the caller's thread faults when nothing came: below)
                    got, out["text"], out["lm"] = fetch_pin(self.fetcher, self.base, self.dir, raw=True)
                    out["at"], out["built"] = time.monotonic(), self.key(got)
            except Exception as e:
                out["error"] = e
        try:
            t = threading.Thread(target=fetch, name=f"{self.name}-manifest", daemon=True)
            t.start()
            t.join(self.wait_s)     # (a slow answer: none for the run; it is still pinned for the next start)
            text, e = out.get("text"), out.get("error")
            if text is None:
                with self.lock:
                    self.down_at = time.time()
                    self.missing = isinstance(e, netfetch.NotFound)
                if not self.missing and self.fault_none:    # (not published is no fault; a failure is a missing
                    # input, as before)
                    import revisit
                    revisit.fault()
                log.info("%s manifest: none for this run (%s)", self.name,
                         f"{type(e).__name__}: {e}" if e is not None else f"no answer in {self.wait_s:.0f} s")
            got, won = session_put(self.cache_dir, ident, text, self.name)
            if text is not None:            # (what Pages serves, as just seen: Manifest.fetch's look)
                with self.lock:
                    self._pages_seen = (out["at"], out["built"], out["lm"])
            return self._adopt(got, won and text is not None and got == text, 0.0)
        finally:
            with self.lock:
                ev, self.fetching = self.fetching, None
            if ev is not None:
                ev.set()

    def _take(self, got):
        """Take a manifest read without the network (None: not available): the one in use."""
        with self.lock:
            if got is None:
                self.down_at = time.time()
            else:
                key = self.key(got)
                changed = key != self.built
                self.manifest, self.built, self.data = got, key, data_url(got)
                if changed:
                    self._changed()
            return self.manifest

    def fetch(self, rel, kind="demand"):
        """The bytes of the build's file rel (path in the dataset's folder): from the manifest's "data" host, else (or
        that failing, not a 404: that answers for the build) Pages, while Pages serves this run's build (_from_pages);
        both failing raises the data host's error, never a NotFound (no "none there" from a copy that may be
        another). Ask current()/build() first: self.data is what the manifest in use names, None before one is read."""
        data = self.data
        if not data:
            return self._from_pages(rel, kind)
        try:
            return self.fetcher.get(f"{data}/{rel}", kind=kind).body
        except (netfetch.NotFound, netfetch.Deferred):
            raise
        except Exception as e:
            first = e
        try:
            return self._from_pages(rel, kind)
        except Exception:
            raise first from None

    def _pages_look(self, kind, force=False):
        """(Pages' build of the dataset now, its manifest's Last-Modified), each None when not known; looked at again
        after PAGES_CHECK_S (force: now). Not pinned: only compared."""
        now = time.monotonic()
        with self.lock:
            at, b, lm = self._pages_seen
        if force or now - at >= PAGES_CHECK_S:
            try:
                r = self.fetcher.get(f"{self.base}/manifest.json", kind=kind)
                got = json.loads(r.body.decode("utf-8"))
                b, lm = (self.key(got) if isinstance(got, dict) else None), _last_modified(r)
            except Exception as e:
                log.debug("%s: Pages' manifest: %s: %s", self.name, type(e).__name__, e)
                b, lm = None, None
            with self.lock:
                self._pages_seen = (now, b, lm)
        return b, lm

    def _from_pages(self, rel, kind):
        """rel's bytes from the dataset's Pages folder, when Pages' manifest names this run's build and the file is of
        the same Pages deploy as that manifest (their Last-Modified within SAME_DEPLOY_S, when both say). Else
        OtherBuild: Pages' files are of whatever build it serves now, and kept under this run's they mixed two builds
        under one key (R3: a stale pin in use while Pages served the next week's)."""
        url = f"{self.base}/{rel}"
        if self.any_build:
            return self.fetcher.get(url, kind=kind).body
        b, lm = self._pages_look(kind)
        if b is None or b != self.built:
            raise OtherBuild(f"{self.name}: Pages serves build {b!r}, this run reads {self.built!r}: {rel} not "
                             "read there", url, netfetch.host_of(url))
        r = self.fetcher.get(url, kind=kind)
        ls = _last_modified(r)
        if lm is not None and ls is not None and abs(ls - lm) > SAME_DEPLOY_S:
            b, lm = self._pages_look(kind, force=True)
            if b != self.built or lm is None or abs(ls - lm) > SAME_DEPLOY_S:
                raise OtherBuild(f"{self.name}: {rel} is of another Pages deploy than its manifest's ({b!r}, this run "
                                 f"reads {self.built!r})", url, netfetch.host_of(url))
        return r.body

    def build(self):
        """The build in use ("built" of its manifest), or None (no data this run)."""
        self.current()
        return self.built

    def _changed(self):
        """The build in use was taken (called under self.lock): drop what was decoded before it."""
        if self.on_change is not None:
            self.on_change()

    def _maybe_refresh(self):
        """This process decided the run's manifest from a pin older than ttl: fetch it again in the background, one
        fetch at a time, after the backoff when one failed. The answer is pinned for the next start; this run keeps
        its manifest."""
        now = time.time()
        with self.lock:
            if self.refresh_at is None or now < self.refresh_at or self.fetching is not None \
                    or not self.backoff.due(now):
                return
            self.fetching = threading.Event()
            self.refresh_at = None
        threading.Thread(target=self._refresh, name=f"{self.name}-manifest", daemon=True).start()

    def _refresh(self):
        """Fetch the manifest and pin it for the next start (this thread holds self.fetching). A failure is
        stale-if-error: it faults no build (netfetch.stale_ok), is logged once, and is asked again after
        self.backoff."""
        now = time.time()
        try:
            try:
                with netfetch.stale_ok(True):
                    got = fetch_pin(self.fetcher, self.base, self.dir)
            except Exception as e:      # (offline, not published any more: the pin stays)
                with self.lock:
                    netfetch.refresh_failed(log, self.backoff, now, f"{self.name} manifest", e)
                    self.refresh_at = self.backoff.until
                return
            with self.lock:
                self.backoff.ok()
                self.refresh_at = now + self.ttl
                b = self.key(got)
            if b != self.built:
                log.info("%s: build %s pinned for the next start (this run keeps %s)", self.name, b, self.built)
        finally:
            with self.lock:
                ev, self.fetching = self.fetching, None
            if ev is not None:
                ev.set()
