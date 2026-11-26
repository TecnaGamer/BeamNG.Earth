"""Our packed photo copies in the game's terrain texture cache: what we wrote, what goes, and a one-time clean-up.

The work is native/earthfast/src/terrainpacks.rs (earthfast 635, the module's notes say what and why); this is its glue.
- Ledger: one line per pack we write ("<key> <session id> <unix time>") in the server's cache folder, recorded before
  the write; a session's packs go with the session (earthd._base_cache_files), and at a start with no game running
  (earthd._prune_packs' guard) the packs of sessions gone (prune), then the oldest over CAP_BYTES (cap).
- wipe_v2: once (a marker), with no game running: the packs earlier builds leaked, only ours beyond doubt (is_ours) in a
  folder holding nothing else (familiar), none with a photo on disk or a live ledger entry (keep_keys).
A pack folder's entries are never followed through a link: a link at a pack's name is no pack (not measured, read or
deleted).
"""
import logging
import os
import time

import fast

log = logging.getLogger("earthd.terrainpacks")

NATIVE = 635
_CONSTANTS = {"LEDGER", "WIPE_MARK", "CAP_BYTES", "BASE_AO"}


def _ef():
    fast.require(NATIVE, "the terrain pack clean-up (terrainpacks)")
    return fast._ef


def __getattr__(name):
    """LEDGER, WIPE_MARK, CAP_BYTES, BASE_AO: the native module's (read when asked: importing needs no module)."""
    if name in _CONSTANTS:
        return getattr(_ef(), "TPACKS_" + name)
    raise AttributeError(f"module 'terrainpacks' has no attribute {name!r}")


def pack_dir(user_dir):
    return os.path.join(user_dir, "temp", "art", "terrainmaterialcache")


def _debug(notes):
    for note in notes:
        log.debug("%s", note)


class Ledger:
    """The packs we wrote, by key: { key: (sid, unix time) } (the last line of a key wins)."""

    def __init__(self, cache_dir):
        self.path = os.path.join(cache_dir, _ef().TPACKS_LEDGER) if cache_dir else None

    def record(self, key, sid, now=None):
        """A pack about to be written (before the write: a crash in between leaves a line, never a pack unknown)."""
        if not self.path or not key:
            return
        err = _ef().tpacks_record(self.path, f"{key}", f"{sid}", str(int(now if now is not None else time.time())))
        if err:
            log.debug("terrain pack ledger: %s", err)

    def entries(self):
        if not self.path:
            return {}
        return {k: (s, t) for k, s, t in _ef().tpacks_entries(self.path)}

    def keys_of(self, sid):
        return _ef().tpacks_keys_of(self.path, sid) if self.path else []


def prune(ledger, pdir, live_sids):
    """Every recorded pack of a session not in live_sids deleted, its line dropped; lines of packs gone dropped too.
    Returns (files deleted, bytes)."""
    n, freed, notes = _ef().tpacks_prune(ledger.path, os.fspath(pdir), list(live_sids))
    _debug(notes)
    return n, freed


def cap(ledger, pdir, max_bytes=None, protect=()):
    """The recorded packs over max_bytes in all (None: CAP_BYTES) deleted, oldest first (keys in protect kept: the
    session in use). Returns (files deleted, bytes)."""
    n, freed, notes = _ef().tpacks_cap(ledger.path, os.fspath(pdir), max_bytes, list(protect))
    _debug(notes)
    return n, freed


def ours_size(px):
    """The file size of our pack of a px x px photo: the header and every mip's BC7 blocks."""
    return _ef().tpacks_ours_size(px)


def is_ours(path):
    """Is this a photo pack our writer made, beyond doubt? (a regular file at a pack's name, its size, header, mode 6
    and the flat AO in every sampled block)"""
    return _ef().tpacks_is_ours(os.fspath(path))


def familiar(pdir):
    """Does pdir look like the game's terrain texture cache and nothing else? (named so, only <16 hex>.dds files)"""
    return _ef().tpacks_familiar(os.fspath(pdir))


def wipe_v2(pdir, keep_keys, mark):
    """Once (mark written after it ran): our photo packs no photo on disk or live ledger entry has (keep_keys) deleted.
    Returns (files deleted, bytes), or None when it did not run (done before, no such folder, an unfamiliar one)."""
    what, n, freed, notes = _ef().tpacks_wipe_v2(os.fspath(pdir), list(keep_keys), os.fspath(mark))
    _debug(notes)
    if what == "unfamiliar":
        log.warning("terrain texture cache %s holds files that are not the game's packs: the leaked-pack clean-up "
                    "left it alone", pdir)
    if what != "done":
        return None
    log.info("terrain texture cache: %d leaked photo packs of ours deleted (%.2f GB)", n, freed / 1e9)
    return n, freed
