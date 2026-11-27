"""The species rules' prebuilt maps (ecoregions, leaf type, tree genus; SOURCES.md) shared by the server and its worker
processes: each is decoded once per machine into a .npy beside the others (keyed by its file's SHA-256: a new map is a
new file) and opened memory-mapped read-only, so every process maps the same pages and only the parts read are
resident (the review of PR #1545: ~175 MB a process when each decoded its own copy). A folder that cannot be written,
or a .npy that cannot be read, gives the plain decoded array as before.

    a = staticmaps.mapped(path, decode)     # decode(): the array of the file at path (read only once a machine)
"""
import hashlib
import os
import tempfile

import numpy as np

DIR_ENV = "BNGE_STATICMAPS_DIR"


def folder():
    """Where the decoded maps are kept (BNGE_STATICMAPS_DIR, else a folder in the system's temp folder)."""
    return os.environ.get(DIR_ENV) or os.path.join(tempfile.gettempdir(), "beamng-earth-staticmaps")


def _load(p):
    return np.asarray(np.load(p, mmap_mode="r", allow_pickle=False))    # (a plain view of the mapping)


def mapped(path, decode):
    """decode()'s array for the file at path, memory-mapped from its .npy in folder() (written first by whichever
    process comes first: a temporary file renamed into place); decode() itself when that cannot be done."""
    try:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:16]
        d = folder()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{os.path.basename(path)}-{digest}.npy")
        if not os.path.exists(p):
            a = np.ascontiguousarray(decode())
            tmp = f"{p}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                np.save(f, a, allow_pickle=False)
            try:
                os.replace(tmp, p)
            except OSError:                 # (another process put it there first, and may have it mapped)
                os.remove(tmp)
                if not os.path.exists(p):
                    return a
        return _load(p)
    except (OSError, ValueError):
        return decode()
