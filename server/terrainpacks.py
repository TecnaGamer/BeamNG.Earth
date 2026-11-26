"""Our packed photo copies in the game's terrain texture cache: what we wrote, what goes, and a one-time clean-up.

The server writes a BC7 pack of every block photo into <user>/temp/art/terrainmaterialcache/<key>.dds
(terraincache.base_entry; key = xxh64 of the photo's and the AO map's paths), so the game takes it instead of packing
the photo on its main thread. Those packs went only with a photo still on disk (earthd._base_cache_files): a photo
deleted any other way left its pack for good. 2026-11-25: 965 such packs (5.4 GB) on the dev PC, 687 (3.9 GB) on the
Steam Deck, after the one-time wipe of 2026-11-03.

- Ledger: one line per pack we write ("<key> <session id> <unix time>") in the server's cache folder, recorded before
  the write. A session's packs go with the session (earthd._base_cache_files adds them), and at a start with no game
  running every recorded pack of a session that is gone goes (prune), whether or not its photo is still there and in
  whatever order the rest was deleted; then the recorded packs over CAP_BYTES, oldest first (cap).
- wipe_v2: once (a marker), with no game running: the packs already leaked, recorded nowhere. Only files that are ours
  beyond doubt: a <16 hex>.dds file of a 2048 or 4096 px photo's exact size, our writer's exact DDS header
  (BC7 sRGB, the full mip chain: bc7.header), and every sampled block BC7 mode 6 (our encoders' only mode) with the
  flat AO 251 in its alpha endpoints (BASE_AO: the game's own packs of stock textures fail it, 37 of 37 tried), and no
  photo on disk (any session) or live ledger entry with its key. A folder holding anything else is left alone.
"""
import logging
import os
import re
import threading
import time

import numpy as np

import bc7

log = logging.getLogger("earthd.terrainpacks")

LEDGER = "terrainpacks.log"         # in the server's cache folder
WIPE_MARK = "terrainpacks_wiped_v2"
CAP_BYTES = 2 * 1024 ** 3
PACK_NAME = re.compile(r"[0-9a-fA-F]{16}\.dds")
BASE_AO = 251                       # the alpha of our photo packs (earthd.BASE_AO, the level's flat AO map)
AO_TOL = 2                          # (mode 6 alpha endpoints of a flat 251 come out 251 or 252)
SAMPLE_BLOCKS = 256
OURS_PX = (2048, 4096)


def pack_dir(user_dir):
    return os.path.join(user_dir, "temp", "art", "terrainmaterialcache")


class Ledger:
    """The packs we wrote, by key: { key: (sid, unix time) } (the last line of a key wins)."""

    def __init__(self, cache_dir):
        self.path = os.path.join(cache_dir, LEDGER) if cache_dir else None
        self.lock = threading.Lock()

    def record(self, key, sid, now=None):
        """A pack about to be written (before the write: a crash in between leaves a line, never a pack unknown)."""
        if not self.path or not key:
            return
        line = f"{key} {sid} {int(now if now is not None else time.time())}\n"
        with self.lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="ascii") as f:
                    f.write(line)
            except OSError as e:
                log.debug("terrain pack ledger: %s", e)

    def entries(self):
        out = {}
        if not self.path:
            return out
        with self.lock:
            try:
                with open(self.path, encoding="ascii", errors="replace") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) == 3 and PACK_NAME.fullmatch(parts[0] + ".dds") and parts[2].isdigit():
                            out[parts[0].lower()] = (parts[1], int(parts[2]))
            except OSError:
                pass
        return out

    def keys_of(self, sid):
        return [k for k, (s, _t) in self.entries().items() if s == sid]

    def rewrite(self, entries):
        """The ledger as `entries` (atomically: a crash keeps the old one)."""
        if not self.path:
            return
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="ascii") as f:
                    for k, (s, t) in sorted(entries.items(), key=lambda kv: kv[1][1]):
                        f.write(f"{k} {s} {t}\n")
                os.replace(tmp, self.path)
            except OSError as e:
                log.debug("terrain pack ledger: %s", e)


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _remove(path):
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.debug("terrain pack %s: %s", path, e)
        return False


def prune(ledger, pdir, live_sids, remove=_remove):
    """Every recorded pack of a session not in live_sids deleted, its line dropped; lines of packs gone dropped too.
    Returns (files deleted, bytes)."""
    entries = ledger.entries()
    keep, n, freed = {}, 0, 0
    for key, (sid, t) in entries.items():
        path = os.path.join(pdir, key + ".dds")
        size = _size(path)
        if sid in live_sids:
            keep[key] = (sid, t)
        elif size is not None and remove(path):
            n, freed = n + 1, freed + size
    if keep != entries:
        ledger.rewrite(keep)
    return n, freed


def cap(ledger, pdir, max_bytes=CAP_BYTES, remove=_remove, protect=()):
    """The recorded packs over max_bytes in all deleted, oldest first (keys in protect kept: the session in use).
    Returns (files deleted, bytes)."""
    entries = ledger.entries()
    live = []
    for key, (sid, t) in entries.items():
        size = _size(os.path.join(pdir, key + ".dds"))
        if size is not None:
            live.append((t, key, size))
    total = sum(s for _t, _k, s in live)
    n, freed = 0, 0
    for t, key, size in sorted(live):
        if total <= max_bytes:
            break
        if key in protect:
            continue
        if remove(os.path.join(pdir, key + ".dds")):
            total -= size
            n, freed = n + 1, freed + size
            entries.pop(key, None)
    if n:
        ledger.rewrite(entries)
    return n, freed


def _mips(px):
    return px.bit_length()          # (a power of two down to 1 x 1)


def ours_size(px):
    """The file size of our pack of a px x px photo: the header and every mip's BC7 blocks."""
    total, w = 0, px
    while True:
        n = max(1, (w + 3) // 4)            # (BC7 blocks per side; integers only)
        total += n * n * 16
        if w == 1:
            break
        w //= 2
    return 148 + total


OURS = {ours_size(px): px for px in OURS_PX}


def is_ours(path):
    """Is this a photo pack our writer made, beyond doubt? (size, header, mode 6 and the flat AO in every sampled
    block, from the top mip to the last)"""
    size = _size(path)
    px = OURS.get(size)
    if px is None or not PACK_NAME.fullmatch(os.path.basename(path)):
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(148)
            if head != bc7.header(px, px, _mips(px)):
                return False
            body = f.read()
    except OSError:
        return False
    n = len(body) // 16
    if n * 16 + 148 != size:
        return False
    idx = np.unique(np.concatenate([np.linspace(0, n - 1, SAMPLE_BLOCKS).astype(np.int64),
                                    np.arange(max(0, n - 32), n)]))
    blocks = np.frombuffer(body, np.uint8).reshape(n, 16)[idx]
    if not ((blocks[:, 0] & 0x7F) == 0x40).all():           # mode 6: the mode bits 0000001
        return False
    lo = blocks[:, :8].copy().view("<u8")[:, 0]
    hi = blocks[:, 8:].copy().view("<u8")[:, 0]
    a0 = (((lo >> np.uint64(49)) & np.uint64(0x7F)).astype(np.int64) << 1) | ((lo >> np.uint64(63)) & np.uint64(1)).astype(np.int64)
    a1 = (((lo >> np.uint64(56)) & np.uint64(0x7F)).astype(np.int64) << 1) | (hi & np.uint64(1)).astype(np.int64)
    return bool((np.abs(a0 - BASE_AO) <= AO_TOL).all() and (np.abs(a1 - BASE_AO) <= AO_TOL).all())


def familiar(pdir):
    """Does pdir look like the game's terrain texture cache and nothing else? (named so, only <16 hex>.dds files)"""
    parts = [p.lower() for p in os.path.normpath(pdir).split(os.sep)[-3:]]
    if parts != ["temp", "art", "terrainmaterialcache"]:
        return False
    try:
        with os.scandir(pdir) as it:
            for e in it:
                if not (e.is_file(follow_symlinks=False) and PACK_NAME.fullmatch(e.name)):
                    return False
    except OSError:
        return False
    return True


def wipe_v2(pdir, keep_keys, mark, remove=_remove):
    """Once (mark written after it ran): our photo packs no photo on disk or live ledger entry has (keep_keys) deleted.
    Returns (files deleted, bytes), or None when it did not run (done before, no such folder, an unfamiliar one)."""
    if os.path.exists(mark):
        return None
    if not os.path.isdir(pdir):
        return None
    if not familiar(pdir):
        log.warning("terrain texture cache %s holds files that are not the game's packs: the leaked-pack clean-up "
                    "left it alone", pdir)
        return None
    keep = {k.lower() for k in keep_keys}
    n, freed = 0, 0
    with os.scandir(pdir) as it:
        names = [e.name for e in it]
    for name in names:
        key = name[:-4].lower()
        path = os.path.join(pdir, name)
        if key in keep or not is_ours(path):
            continue
        size = _size(path) or 0
        if remove(path):
            n, freed = n + 1, freed + size
    log.info("terrain texture cache: %d leaked photo packs of ours deleted (%.2f GB)", n, freed / 1e9)
    try:
        os.makedirs(os.path.dirname(mark), exist_ok=True)
        open(mark, "w").close()
    except OSError:
        pass
    return n, freed
