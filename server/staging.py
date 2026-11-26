"""Temporary files of the atomic writes into the game's user folder, kept outside it.

The game watches its user folder: every file created or changed there is an onFileChanged event, handled in Lua by
every loaded extension, and the engine's FileWatchers took up to ~1 s in a frame during a 100 mph drive (2026-10-17).
A write as "<path>.tmp" then os.replace() made two events per tile file (3,220 in a 3 km drive, 1,079 of them .tmp).
tmp_for(path) puts the temporary file in "beamngEarth_staging" next to the user folder (the same disk: the rename stays
atomic), so only the finished file shows up. Paths outside a user folder's "temp" (the download cache) keep
"<path>.tmp". Pure but for creating that folder.

replace() is the atomic rename into the game's folder; a destination folder that vanished mid-session is made again
(GitHub #27: a player's session folder under temp/beamngEarth was gone, and every terrain write after it failed).
"""
import asyncio
from contextlib import contextmanager
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import fast

_native = fast.native(557, "session file staging")
STAGING = "beamngEarth_staging"
SWEEP_AGE_S = 3600
WARM_READ = os.environ.get("BNGE_WARM_READ", "1" if os.name == "nt" else "0") != "0"
GAME_WAITS = (0.02, 0.05, 0.1, 0.25, 0.5, 1.0)
REPLACE_WAITS = (0.02, 0.05, 0.1, 0.25, 0.5)
READ_WAITS = (0.02, 0.05, 0.1, 0.25)
log = logging.getLogger("earthd.staging")
_io = ThreadPoolExecutor(1, thread_name_prefix="staging-io")
warm = _native.staging_warm


def staging_dir(path):
    return _native.staging_dir(os.path.abspath(path))


def tmp_for(path):
    return _native.staging_tmp(path, os.path.abspath(path), threading.get_ident())


def _rename(tmp, path, cached):
    game = staging_dir(path) is not None
    waits = REPLACE_WAITS if cached else GAME_WAITS if game else ()
    args = (cached, os.replace, warm if WARM_READ and game else None, log)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _native.staging_replace(tmp, path, waits, *args)
    # Synchronous API used by callers that immediately open the destination. Single try
    # for game files. The streaming paths prepare their files in workers before delivery.
    return _io.submit(_native.staging_replace, tmp, path, waits if cached else (), *args).result()


def replace(tmp, path):
    _rename(tmp, path, False)


@contextmanager
def staged_write(path):
    """Publish a completed write or remove just this writer's private temporary file."""
    tmp = tmp_for(path)
    try:
        yield tmp
        replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("staging could not remove %s after a failed write: %s", tmp, exc)
        raise


def replace_cached(tmp, path):
    return _rename(tmp, path, True)


def read_held(read):
    return _native.staging_read_held(read, READ_WAITS)


DAMAGED_NPZ = (OSError, ValueError, EOFError, KeyError)     # (and zipfile.BadZipFile: read_npz)


def read_npz(path, read, what="cache file"):
    """read(f) of the npz cache file at path (f: np.load's NpzFile), or None when the file is damaged: empty
    (EOFError), cut short (zipfile.BadZipFile), zeroed (ValueError) or missing a member (KeyError), as a crash or power
    loss after its write leaves it. The damaged file is removed so the caller builds it again: unread, it failed every
    ask for good (telemetry 2026-11-08: the far tiles' EOFError, 313k times in a week from three dev builds, BadZipFile
    beside it). Opened with its own handle, closed whatever np.load raises (numpy left a damaged file's open, and
    Windows cannot remove an open file)."""
    import zipfile
    import numpy as np
    try:
        with open(path, "rb") as fh, np.load(fh, allow_pickle=False) as f:
            return read(f)
    except FileNotFoundError:
        return None
    except PermissionError as e:        # (Windows: held by another process, a virus scanner: whole, so kept; built anew)
        log.debug("%s %s: %s", what, path, e)
        return None
    except DAMAGED_NPZ + (zipfile.BadZipFile,) as e:
        log.warning("%s %s damaged (%s: %s): built again", what, path, type(e).__name__, e)
        try:
            os.remove(path)
        except OSError:
            pass
        return None


INLINE_MAX = 1 << 20        # bytes: a session file this small or smaller goes inside its message (inline_json)


def inline_raw(out_dir, vfs_path, max_bytes=INLINE_MAX, digest=False):
    """A worker thread's inline_json: the session file's text as protocol.RawJson (spliced into its message as it is;
    a file with \\u escapes parsed and written again as real UTF-8, as inline_json's message would carry it), or None
    as inline_json. digest: (raw, its bytes' SHA-1) instead (the trackers' same-file check)."""
    import hashlib
    import json
    import protocol
    raw = h = None
    try:
        path = os.path.join(out_dir, os.path.basename(vfs_path))
        with open(path, "rb") as f:
            data = f.read(max_bytes + 1) if not digest else f.read()
        if digest:
            h = hashlib.sha1(data).digest()
        if len(data) <= max_bytes:
            text = data.decode("utf-8").strip()
            if b"\\u" in data:
                text = json.dumps(json.loads(text), separators=(",", ":"), ensure_ascii=False)
            if text[:1] in ("{", "[") and text[-1:] in ("}", "]"):
                raw = protocol.RawJson(text)
    except (OSError, ValueError):
        pass
    return (raw, h) if digest else raw


class Inlined:
    """A tracker build's result with its files' inline data read in the worker (build_inline): the event loop sends
    it without opening or parsing a file. files: {vfs path: RawJson or None}; data: the first file's."""
    __slots__ = ("res", "data", "digest", "files", "prepared")

    def __init__(self, res, data, digest=None, files=None):
        self.prepared = None
        self.res, self.data, self.digest, self.files = res, data, digest, files or {}

    def pool(self, conn, files):
        return self.prepared.claim(conn, files)


def prepare_shape_files(server, files):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _native.prepare_shape_files(server, files)
    return _io.submit(_native.prepare_shape_files, server, files).result()


def prepare_shapes(server, kind, geo, aid, key, job, *args):
    result = job(*args)
    result.prepared = _native.prepare_shapes(server, kind, geo, aid, key, result.res)
    return result


def build_inline(build, out_dir, digest, *args):
    """Worker thread: build(*args), and when it made a file (its result's first item, a vfs path) the file's inline
    data (inline_raw; with its SHA-1 when digest). Returns an Inlined."""
    res = build(*args)
    if res is None or not out_dir:
        return Inlined(res, None)
    if digest:
        raw, h = inline_raw(out_dir, res[0], digest=True)
        return Inlined(res, raw, h, {res[0]: raw})
    raw = inline_raw(out_dir, res[0])
    return Inlined(res, raw, None, {res[0]: raw})


def build_inline_paths(build, out_dir, paths_of, *args):
    """Worker thread: build(*args) and the inline data of each file paths_of(result) names (a list of vfs paths).
    Returns an Inlined."""
    res = build(*args)
    files = {}
    if res is not None and out_dir:
        for p in paths_of(res):
            if p and p not in files:
                files[p] = inline_raw(out_dir, p)
    return Inlined(res, None, None, files)


def inline_from(pre, out_dir, vfs_path):
    """The inline data of a session file: read in the worker (pre: an Inlined naming it), else inline_json here."""
    if pre is not None and vfs_path in pre.files:
        return pre.files[vfs_path]
    return inline_json(out_dir, vfs_path) if out_dir else None


def inline_json(out_dir, vfs_path, max_bytes=INLINE_MAX):
    """The parsed JSON of a session file (vfs_path's name in out_dir), to send inside its message (its "data") so the
    game does not open the file: on Windows the game's first read of a file the server just wrote took 6.8 ms on
    average and up to 552 ms, in the frame (a 4-minute highway drive on a Windows test PC, 2026-10-24: 1,176 tile files
    read; 0.03 ms on Linux). None when it cannot be read or is larger than max_bytes (the mod reads the file then)."""
    import json
    try:
        path = os.path.join(out_dir, os.path.basename(vfs_path))
        if os.path.getsize(path) > max_bytes:
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
