"""Cached SHA-256 digests of files, keyed by each file's stat (its size, mtime_ns and inode).

The launch's up-to-date checks (is the installed mod still this folder's copy? are the texture
sources the ones the textures were made from?) read whole trees just to hash them: ~240 MB of
files on every `python start.py`, several seconds of disk. A file whose size, modification time
and inode are all as the cache last saw them has the digest the cache recorded — same file, same
content, not read again. Everything else (a real edit, a replaced file, a different machine: the
inode differs) is hashed and the cache updated.

The cache lives in the launcher's data folder (apppaths.data_dir()/hashcache.json), shared by the
processes that hash (install.py, start.py, the app's launcher) and replaced whole (os.replace) by
whoever wrote last: any value in it is a digest a previous run saw of a file that its stat named.
A rewrite inside one clock tick (the file system's timestamp granularity: about 15 ms on Windows,
2 s on FAT) with the same size is the one change the stat cannot see; the mod's files change far
apart in time, and any other change (a different size, a replaced file, a different machine) is
hashed again. No cache (a data folder that cannot be made): every file is read, as before.

Standard library only, like install.py's and modtextures.py's other helpers.
"""
import hashlib
import json
import os

try:
    import apppaths
except ImportError:                       # (run from another folder: a copy of this file beside a test)
    apppaths = None

FILE = "hashcache.json"
READ_CHUNK = 1 << 20                      # (a whole tree's files through 1 MB reads, like tree_hash's)


def _path():
    try:
        return os.path.join(apppaths.data_dir(), FILE)
    except Exception:                     # (no data folder: no cache, digests read every time)
        return None


def _stat_key(st):
    """What says this is still the file the cache read: its size, its mtime_ns and its inode (the
    file index on Windows, the inode on Linux: a replaced file gets a new one even with the same
    size and mtime)."""
    return f"{st.st_size}|{st.st_mtime_ns}|{st.st_ino}|{st.st_dev}"


class Cache:
    """The digests of one hashing pass, loaded whole and written back whole when it was used.
    with statcache.Cache() as cache: digest = cache.digest(path); (an unchanged file is not read)."""

    def __init__(self):
        self.d = {}
        self.dirty = False
        path = _path()
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    # only entries a previous run wrote whole ([key, hexdigest]); files that are
                    # gone drop out here (the prune the save would do, without the stats' cost)
                    self.d = {k: v for k, v in d.items()
                              if isinstance(v, list) and len(v) == 2 and os.path.isfile(k)}
            except (OSError, ValueError):
                pass

    def digest(self, path):
        """The file's SHA-256 (bytes), read only when its stat moved since the cache's last look."""
        st = os.stat(path)
        key = _stat_key(st)
        hit = self.d.get(path)
        if hit is not None and hit[0] == key:
            return bytes.fromhex(hit[1])
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(READ_CHUNK), b""):
                h.update(chunk)
        d = h.digest()
        self.d[path] = [key, d.hex()]
        self.dirty = True
        return d

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if self.dirty:
            self.save()
        return False

    def save(self):
        """The digests back to disk (one file, whole, renamed into place: the next process reads one
        file too), pruned to the files that still exist. Best effort: a data folder that cannot be
        written loses only the cache, never the launch."""
        path = _path()
        if not path:
            return
        out = {k: v for k, v in self.d.items() if os.path.isfile(k)}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, sort_keys=True)
            os.replace(tmp, path)
            self.dirty = False
        except OSError:
            pass
