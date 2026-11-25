"""The Overture release an app session reads (the user, 2026-11-18: Overture's monthly releases without an app update).

ONE owner decides, ONCE per app session: the supervisor that starts the world server and starts it again when the
game's settings page asks (launcher.py's RestartingServer, start.py's run_restarting; the BeamMP share helper and the
decisions builder for themselves). It makes a Pick at its start and hands pick.release() to EVERY server it starts in
that session (RELEASE_ENV), so a restart in the middle of a game never changes the release and nothing is handed from
one server to the next. A server started without a supervisor (dev runs) reads RELEASE_ENV when set, else RELEASE.

The decision (Pick): the project's switch file (cdn.json {"overture": {"release": "YYYY-MM-DD.N"}}) names a release;
it is taken when Overture's STAC catalogue lists it and its release catalogue states a schema this build reads
(SUPPORTED_SCHEMAS). All of it within ONE deadline (PICK_S, end to end, in a thread of its own: the server's start
waits at most that, never for the network's own timeouts). No answer by then (offline, a host down or slow): the
release this machine last ran a game session on (LAST_FILE, written by the world server, overture_session) when it is
newer than RELEASE and its whole STAC index is kept on this machine (overture.local_bundle_path); else RELEASE. A
switch without the key, or a release STAC does not list (or of another schema): RELEASE.

No state is shared between processes except LAST_FILE, a hint replaced whole (os.replace) by whoever wrote last: any
value in it is a release a game session ran with its whole index kept, and it is read only when the network gives no
answer. Stdlib only at import (the launcher imports it; netfetch is imported in the pick's own thread).
"""
import gzip
import json
import os
import re
import threading
import time

# The build's own release: the fallback and the default (overture.RELEASE is this; bumped here). Overture removes old
# releases from its buckets after about 60 days: a release that is gone raises overture.ReleaseGone for cells not
# cached yet.
RELEASE = "2026-11-18.0"
RELEASE_ENV = "BNGE_OVERTURE_RELEASE"       # the release a supervisor hands to the server (and its workers inherit)
RELEASE_RE = re.compile(r"20[2-9]\d-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\.\d{1,2}")
# the schema versions (a release catalogue's "schema:version") whose columns this build reads (overture.LAYERS,
# pmtiles): a release of another schema is not taken
SUPPORTED_SCHEMAS = ("2.0.0",)
STAC_ROOT = "https://stac.overturemaps.org"
# (in the data repo itself, read raw: cdn.SWITCH_URL is this)
SWITCH_URL = os.environ.get("BNGE_CDN_SWITCH",
                            "https://raw.githubusercontent.com/TecnaGamer/BeamNG.Earth-data/main/cdn.json")
SWITCH_TTL_S = 3600.0                       # (cdn.SWITCH_TTL_S: the copy on disk the server keeps, read when this fresh)
# the switch file's largest size, on the wire, decoded and on disk (a few hundred bytes in 2026-11): past it the file
# is not read, parsed or kept (no answer: the fallback), so a publisher's mistake or a compromise cannot fill memory
SWITCH_MAX_BYTES = 256 * 1024
PICK_S = 5.0                                # the whole decision's deadline (switch file + STAC), then the fallback
LAST_FILE = "overture_last.json"            # <cache>/cdn/: the release of this machine's last game session


def valid(release):
    return isinstance(release, str) and RELEASE_RE.fullmatch(release) is not None


def order(release):
    """A release id's place in time ("2026-11-18.10" after "2026-11-18.9")."""
    day, _, n = release.partition(".")
    return day, int(n)


def from_env(environ=None):
    """RELEASE_ENV when it is a release id, else None."""
    v = ((environ if environ is not None else os.environ).get(RELEASE_ENV) or "").strip()
    return v if valid(v) else None


def cache_dir_of(extra, default, cwd):
    """The download cache a world server started with options extra in folder cwd uses, by earthd's own rules: the
    last --cache-dir (argparse), else default; the home folder expanded, a relative path from cwd, absolute."""
    path = default
    for i, a in enumerate(extra):
        if a == "--cache-dir" and i + 1 < len(extra):
            path = extra[i + 1]
        elif a.startswith("--cache-dir="):
            path = a.split("=", 1)[1]
    if not path:
        return None
    return os.path.abspath(os.path.join(cwd, os.path.expanduser(path)))


def local_bundle_path(cache_dir, release):
    """The release's whole STAC index kept on this machine (overture.keep_local_bundle), as the shipped one's form."""
    return os.path.join(cache_dir, "cdn", f"overture_stac_{release}.json.gz")


def read_bundle(path, release):
    """{"theme/type": {...}} of a STAC index bundle file of this release, or {} (missing, truncated, another release)."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, EOFError):
        return {}
    if not isinstance(data, dict) or data.get("release") != release:
        return {}
    got = data.get("collections")
    return got if isinstance(got, dict) else {}


def parse_switch(body):
    """The switch file's dict from its bytes, or None (past SWITCH_MAX_BYTES, not JSON, not an object)."""
    if not isinstance(body, (bytes, bytearray, str)) or len(body) > SWITCH_MAX_BYTES:
        return None
    try:
        doc = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def read_switch_file(path):
    """The switch file's copy on disk as a dict, or None (missing, past SWITCH_MAX_BYTES, unreadable): no more than
    one byte past the limit is read."""
    try:
        with open(path, "rb") as fh:
            body = fh.read(SWITCH_MAX_BYTES + 1)
    except OSError:
        return None
    return parse_switch(body)


def get_switch(get):
    """The switch file from the net as a dict through get(url), None when past SWITCH_MAX_BYTES or not an object
    (get's own errors raised)."""
    return parse_switch(get(SWITCH_URL))


def keep_switch(path, doc):
    """The switch file's copy on disk (cdn.switch reads it while fresh), written whole under a writer-unique name; not
    written (the copy before kept) when its form here is past SWITCH_MAX_BYTES (read_switch_file would refuse it: a
    number or an escape can grow when written again)."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        body = json.dumps(doc).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return
    if len(body) > SWITCH_MAX_BYTES:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _switch_doc(get, cache_dir):
    """The switch file: the copy on disk when fresh (cdn.switch keeps it), else from the net (and kept on disk), else
    the copy whatever its age; None when unknown."""
    path = os.path.join(cache_dir, "cdn", "cdn.json") if cache_dir else None
    if path:
        try:
            fresh = time.time() - os.path.getmtime(path) < SWITCH_TTL_S
        except OSError:
            fresh = False
        doc = read_switch_file(path) if fresh else None
        if doc is not None:
            return doc
    try:
        doc = get_switch(get)       # (past SWITCH_MAX_BYTES: None, neither parsed nor kept)
        if doc is not None:
            if path:            # (kept for the server's own cdn.switch: no second fetch at its start)
                keep_switch(path, doc)
            return doc
    except Exception:
        pass
    return read_switch_file(path) if path else None


class Unreachable(Exception):
    """No answer about a release (offline, a host down, a timeout): the fallback decides."""


def check_stac(release, get):
    """(True, schema) when Overture's STAC catalogue lists the release and its catalogue states a supported schema;
    (False, why) when an answer says no (not listed, 404 / 410, another release, an unknown schema).
    Raises Unreachable when there is no answer. get(url) -> body bytes, raising the netfetch errors."""
    import netfetch
    try:
        root = json.loads(get(f"{STAC_ROOT}/catalog.json"))
        listed = {str(ln.get("href")) for ln in root.get("links", ()) if isinstance(ln, dict) and ln.get("rel") == "child"}
        if f"{STAC_ROOT}/{release}/catalog.json" not in listed and f"./{release}/catalog.json" not in listed:
            return False, "not listed in Overture's STAC catalogue"
        cat = json.loads(get(f"{STAC_ROOT}/{release}/catalog.json"))
        if not isinstance(cat, dict) or cat.get("release:version") != release:
            return False, "its STAC catalogue names another release"
        schema = str(cat.get("schema:version"))
    except netfetch.NotFound as e:
        return False, f"not found ({e})"
    except (netfetch.FetchError, OSError) as e:
        raise Unreachable(f"{type(e).__name__}: {e}") from e
    except (ValueError, AttributeError, TypeError) as e:     # (a captive portal's page: no answer, not a no)
        raise Unreachable(f"STAC catalogue unreadable ({type(e).__name__})") from e
    if schema not in SUPPORTED_SCHEMAS:
        return False, f"schema {schema} (this build reads {', '.join(SUPPORTED_SCHEMAS)})"
    return True, schema


def decide_online(get, cache_dir):
    """(release, why) from the switch file and STAC; raises Unreachable when an answer is missing."""
    doc = _switch_doc(get, cache_dir)
    if doc is None:
        raise Unreachable("no switch file")
    on = doc.get("overture")
    rel = on.get("release") if isinstance(on, dict) else None
    if rel is None:
        return RELEASE, "the switch names none"
    if not valid(rel):
        return RELEASE, f"the switch's {rel!r} is not a release id"
    if rel == RELEASE:
        return RELEASE, "the switch names the build's own"
    ok, why = check_stac(rel, get)
    if not ok:
        forget(cache_dir, rel)
        return RELEASE, f"the switch's {rel} not taken: {why}"
    return rel, f"the switch's, schema {why}"


def fallback(cache_dir):
    """(release, why) without the network: the last session's release (LAST_FILE) when newer than RELEASE, of a
    supported schema and with its whole STAC index kept here; else RELEASE."""
    if not cache_dir:
        return RELEASE, "no answer in time; the build's own"
    try:
        with open(os.path.join(cache_dir, "cdn", LAST_FILE), encoding="utf-8") as fh:
            last = json.load(fh)
    except (OSError, ValueError):
        return RELEASE, "no answer in time and no earlier session's release; the build's own"
    rel = last.get("release") if isinstance(last, dict) else None
    if not valid(rel) or rel == RELEASE or order(rel) < order(RELEASE):
        return RELEASE, "no answer in time; the build's own"
    if os.path.exists(_rejected_path(cache_dir, rel)):
        return RELEASE, f"no answer in time; the last session's {rel} was said no to"
    if str(last.get("schema")) not in SUPPORTED_SCHEMAS:
        return RELEASE, f"no answer in time; the last session's {rel} is of another schema"
    if not read_bundle(local_bundle_path(cache_dir, rel), rel):
        return RELEASE, f"no answer in time; the last session's {rel} has no STAC index here"
    return rel, "no answer in time; the last session's (its STAC index kept here)"


REJECTED = "overture_rejected"      # <cache>/cdn/<this>/<release>: an answer said no to it (forget)


def _rejected_path(cache_dir, release):
    return os.path.join(cache_dir, "cdn", REJECTED, release)       # (release: RELEASE_RE's form only)


def forget(cache_dir, release):
    """An answer said no to release (not listed, 404 / 410, another release, a schema this build does not read):
    never a fallback again on this machine. A marker of its own, only ever created (never removed: Overture does not
    publish a removed release again, nor does this build learn another schema), so no other process's write (a
    server's session note, another start's acceptance) can bring the release back as a fallback."""
    if not cache_dir or not valid(release):
        return
    path = _rejected_path(cache_dir, release)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError:
        pass


def _net_get(offline):
    import netfetch
    fetcher = netfetch.Fetcher(offline=offline)
    # (the switch file read no further than its limit, on the wire and decoded: netfetch.TooLarge, no answer)
    return lambda url: fetcher.get(url, kind="demand",
                                   max_bytes=SWITCH_MAX_BYTES if url == SWITCH_URL else None).body


class Pick:
    """The decision, started at once in threads of its own: the network's (the switch file, STAC) and the disk's
    (fallback: LAST_FILE and the kept STAC index). release() takes what was ready by deadline_s after the start:
    the network's answer, else the disk's, else RELEASE; an answer that comes later is never taken (each is stamped
    with when it came). The first release() decides and every later one returns the same (a supervisor's
    restarts). RELEASE_ENV set in the supervisor's own environment (by hand) wins."""

    def __init__(self, cache_dir, offline=False, get=None, deadline_s=PICK_S, environ=None, clock=time.monotonic):
        self.cache_dir, self.clock = cache_dir, clock
        self.deadline = clock() + deadline_s
        self.why = ""
        self._chosen = None
        self._lock = threading.Lock()
        self._online, self._disk = [], []       # [(result or None, when it came)]
        self._given = from_env(environ)
        self._thread = self._disk_thread = None
        self._error = ""
        if self._given:
            return

        def online():
            try:
                import netfetch     # (the pick's own thread: the launcher imports this module, stdlib only)
                # (no answer is no build's missing input: the disk's pick, else RELEASE, decides then; a late failure
                # of this thread must not fault the builds a process that is its own supervisor runs meanwhile)
                with netfetch.stale_ok():
                    got = decide_online(get or _net_get(False), cache_dir)
            except Exception as e:      # (Unreachable, or anything else: the disk's)
                self._error = f"{type(e).__name__}: {e}"
                got = None
            self._online.append((got, clock()))

        def disk():
            try:
                got = fallback(cache_dir)
            except Exception:
                got = None
            self._disk.append((got, clock()))
        if not offline:
            self._thread = threading.Thread(target=online, name="overture-pick", daemon=True)
            self._thread.start()
        self._disk_thread = threading.Thread(target=disk, name="overture-pick-disk", daemon=True)
        self._disk_thread.start()

    def _ready(self, thread, out):
        if thread is not None:
            thread.join(max(0.0, self.deadline - self.clock()))
        if out and out[0][0] is not None and out[0][1] <= self.deadline:
            return out[0][0]
        return None

    def release(self):
        with self._lock:
            if self._chosen is None:
                if self._given:
                    self._chosen, self.why = self._given, f"{RELEASE_ENV} set"
                else:
                    got = self._ready(self._thread, self._online)
                    if got is None:
                        got = self._ready(self._disk_thread, self._disk)
                        if got is not None:
                            got = (got[0], got[1] + (f" ({self._error})" if self._error else ""))
                    self._chosen, self.why = got or (RELEASE, "nothing ready in time; the build's own")
            return self._chosen


def choose_for_process(cache_dir, offline=False):
    """A process that is its own supervisor (the BeamMP share helper, the decisions builder): RELEASE_ENV when set,
    else a Pick's release, put in RELEASE_ENV before anything reads map data or starts workers. The release."""
    rel = from_env()
    if rel is None:
        rel = Pick(cache_dir, offline=offline).release()
        os.environ[RELEASE_ENV] = rel
    return rel


def write_last(cache_dir, release, schema):
    """The world server's note after a game session started on release with its whole STAC index kept here
    (overture_session): replaced whole; any writer's value is a release that ran."""
    if not valid(release):
        return False
    path = os.path.join(cache_dir, "cdn", LAST_FILE)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"release": release, "schema": schema, "time": time.time()}, fh)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
