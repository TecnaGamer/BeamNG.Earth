"""Shared parts of the port's byte-identity tools (tools/portcheck, the Rust port plan's phase 0b).

The tools prove that a port step changed no output: they build the frozen corpus (corpus.json) with one tree of the
server, again with another, and compare the two outputs exactly.

  build.py    the plans and cell facts of every corpus place's map cells, in-process (plancmp, factcmp)
  session.py  a real server session per place in the players' layout (the launcher's options, the mod's hello):
              every message and every file a message names (tilecmp, blkcmp, farcmp, msgcmp), and the run's plans
  plancmp.py  factcmp.py  tilecmp.py  blkcmp.py  farcmp.py  msgcmp.py: compare two outputs (exit 0: identical,
              1: something differs, 2: items only on one side)

Caches. The corpus cache is a folder of its own (never the player's ~/.cache/beamng-earth, never a folder of links
into it: its eviction deleted the user's files through links once, 2026-10-29). A first build fills it online
(--online); every later build reads it offline from a work copy (cp --reflink: a real copy, copy-on-write on btrfs),
so the corpus stays frozen and the two trees see the same inputs. Kept items (the revisit cache, decision archives,
baked packs) are never read: everything is built again.

The usual round, on the mini PC (the plan: the corpus's caches sit there):

    python3 tools/portcheck/build.py --cache ~/bnge-tests/portcheck/cache --out ~/bnge-tests/portcheck/before \\
        --online                                    # once: fills the corpus cache (network), builds "before"
    python3 tools/portcheck/session.py --cache ~/bnge-tests/portcheck/cache --out ~/bnge-tests/portcheck/before \\
        --online                                    # once: the sessions' inputs too (imagery, far DEM)
    # then for each tree (the release's, the port's), offline from the frozen cache:
    python3 tools/portcheck/build.py --cache ... --out OUT --server-dir TREE/server
    python3 tools/portcheck/session.py --cache ... --out OUT --server-dir TREE/server
    python3 tools/portcheck/plancmp.py BEFORE OUT   (factcmp, tilecmp, blkcmp, farcmp, msgcmp the same way)
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SERVER = os.path.join(ROOT, "server")
CORPUS = os.path.join(HERE, "corpus.json")
REAL_CACHE = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
# Switches pinned on both sides of every comparison: the CDN's ready-made products (decision archives' plans, lidar road
# heights, the DEM release) would replace what the tree's own code makes, and kept blocks would be read back
PINNED_ENV = {"BNGE_DECISIONS": "off", "BNGE_LIDARZ": "off", "BNGE_DEM_CDN": "off", "BNGE_BLOCKCACHE": "off"}
# what a fill leaves in the corpus cache that is made, not downloaded (removed after it: never read by a check)
DERIVED = ("revisit", "decisions", "plans", "blocks", "beamngEarth_staging")


def prune_derived(cache):
    for d in DERIVED:
        shutil.rmtree(os.path.join(cache, d), ignore_errors=True)


def corpus(path=CORPUS):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def places(names=None, path=CORPUS):
    """[(name, place)] of the corpus, all or the names given (comma-separated or a list), in the file's order."""
    every = corpus(path)["places"]
    if not names:
        return list(every.items())
    if isinstance(names, str):
        names = [n for n in names.split(",") if n]
    unknown = [n for n in names if n not in every]
    if unknown:
        raise SystemExit(f"not in the corpus: {', '.join(unknown)} (have {', '.join(every)})")
    return [(n, every[n]) for n in names]


def _inside(a, b):
    """Is real path a b or inside b?"""
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def protected():
    """[(real path, what)] the tools never use as a cache nor write or delete in: the player's download cache (the
    default, the one chosen on the settings page or with --cache-dir: app.json, BNGE_CACHE_DIR), the app's own folder
    and the game's user folder (the one the game said it uses, else the usual places)."""
    out = [(REAL_CACHE, "the player's default cache")]
    if os.environ.get("BNGE_CACHE_DIR"):
        out.append((os.path.realpath(os.environ["BNGE_CACHE_DIR"]), "BNGE_CACHE_DIR"))
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import apppaths
        dd = apppaths.data_dir()
        out.append((os.path.realpath(dd), "the app's folder"))
        for got, what in ((apppaths.saved_cache_dir(dd), "the player's chosen cache (app.json)"),
                          (apppaths.saved_user_dir(dd), "the game's user folder (app.json)")):
            if got:
                out.append((os.path.realpath(got), what))
    except Exception:           # (no app settings: the defaults below still hold)
        pass
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    flatpak = os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share")
    for p in (os.path.join(home, ".local", "share", "BeamNG"), os.path.join(home, ".local", "share", "BeamNG.drive"),
              os.path.join(flatpak, "BeamNG"), os.path.join(flatpak, "BeamNG.drive"),
              os.path.join(local, "BeamNG"), os.path.join(local, "BeamNG.drive")):
        out.append((os.path.realpath(p), "a game user folder"))
    return out


def check_cache(path):
    """Refuse the player's cache, the app's or the game's folders, anything inside them or holding them, or a cache
    holding links (the 2026-10-29 eviction)."""
    real = os.path.realpath(path)
    for p, what in protected():
        if _inside(real, p) or _inside(p, real):
            raise SystemExit(f"refusing {path}: {what} ({p}) (use a corpus cache of its own)")
    if os.path.isdir(path):
        for d, dirs, files in os.walk(path):
            for n in dirs + files:
                if os.path.islink(os.path.join(d, n)):
                    raise SystemExit(f"refusing {path}: it holds a link ({os.path.join(d, n)}); a corpus cache must "
                                     "be real files")
    return real


def work_copy(src, dst):
    """A real copy of the corpus cache src at dst (copy-on-write where the file system can: btrfs, XFS), replacing
    dst. Never one inside the other: replacing dst would delete the corpus."""
    check_apart(src, dst)
    check_cache(src)
    check_cache(dst)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    if os.path.isdir(src):
        subprocess.run(["cp", "-a", "--reflink=auto", src, dst], check=True)
    else:
        os.makedirs(dst)
    return dst


def check_apart(cache, other):
    """Refuse a folder (the work copy, the outputs) equal to, inside or holding the corpus cache: the tools replace and
    delete what is in theirs."""
    a, b = os.path.realpath(cache), os.path.realpath(other)
    if _inside(a, b) or _inside(b, a):
        raise SystemExit(f"refusing {other}: it overlaps the corpus cache {cache} (keep them apart)")


def nice_self(n=15):
    """Run below the game and the timing-sensitive tests sharing the machine."""
    try:
        os.nice(n - os.nice(0) if os.nice(0) < n else 0)
    except (OSError, AttributeError):
        pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import threading
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"     # (threads may write the same item)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---- comparing ----

def json_diff(a, b, path="$", limit=5):
    """Up to limit (path, a's value, b's value) where a and b differ (JSON values; floats compared exactly, ints and
    floats of other types as different)."""
    out = []

    def walk(x, y, p):
        if len(out) >= limit:
            return
        if type(x) is not type(y):
            out.append((p, _short(x), _short(y)))
        elif isinstance(x, dict):
            if list(x) != list(y):
                ka, kb = set(x), set(y)
                if ka != kb:
                    out.append((p + " keys", sorted(ka - kb)[:5], sorted(kb - ka)[:5]))
                    return
                out.append((p + " key order", list(x)[:8], list(y)[:8]))
            for k in x:
                walk(x[k], y[k], f"{p}.{k}")
        elif isinstance(x, list):
            if len(x) != len(y):
                out.append((p + " length", len(x), len(y)))
            for i, (u, v) in enumerate(zip(x, y)):
                walk(u, v, f"{p}[{i}]")
        elif isinstance(x, float):
            if not (x == y or (x != x and y != y)) or repr(x) != repr(y):
                out.append((p, x, y))
        elif x != y:
            out.append((p, _short(x), _short(y)))
    walk(a, b, path)
    return out


def _short(v, n=80):
    s = json.dumps(v) if not isinstance(v, str) else v
    return s if len(s) <= n else s[:n] + "..."


def npz_diff(pa, pb, limit=5):
    """Differences between two .npz files' arrays (names, dtypes, shapes, bytes; the largest |a - b| of a float array
    of the same shape), [] when they hold the same arrays bit for bit."""
    import numpy as np
    out = []
    with np.load(pa, allow_pickle=False) as fa, np.load(pb, allow_pickle=False) as fb:
        na, nb = set(fa.files), set(fb.files)
        if na != nb:
            out.append(f"arrays only in A {sorted(na - nb)}, only in B {sorted(nb - na)}")
        for n in sorted(na & nb):
            a, b = fa[n], fb[n]
            if a.dtype != b.dtype or a.shape != b.shape:
                out.append(f"{n}: {a.dtype}{a.shape} vs {b.dtype}{b.shape}")
            elif a.tobytes() != b.tobytes():
                if a.dtype.kind == "f":
                    # (bit for bit: -0.0 against 0.0 or two NaN payloads count as a difference of 0)
                    neq = int(np.count_nonzero(a.view(f"u{a.itemsize}") != b.view(f"u{b.itemsize}")))
                    d = np.abs(a.astype(np.float64) - b.astype(np.float64))
                    d = d[np.isfinite(d)]
                    worst = float(d.max()) if d.size else float("nan")
                    out.append(f"{n}: {neq} of {a.size} values differ, max |A-B| {worst:.3g}")
                elif n == "meta" or a.dtype.kind in "SUV" or (a.dtype == np.uint8 and a.ndim == 1):
                    try:
                        ja, jb = json.loads(bytes(a).decode("utf-8")), json.loads(bytes(b).decode("utf-8"))
                        out += [f"{n}{p[1:]}: {x} vs {y}" for p, x, y in json_diff(ja, jb, "$", limit)]
                    except (ValueError, UnicodeDecodeError):
                        out.append(f"{n}: bytes differ")
                else:
                    out.append(f"{n}: {int(np.count_nonzero(a != b))} of {a.size} values differ")
            if len(out) >= limit:
                break
    return out


def file_diff(pa, pb):
    """Why two files differ ([] when byte-equal): JSON compared as values, npz by array, PNG by pixel, else bytes."""
    with open(pa, "rb") as f:
        a = f.read()
    with open(pb, "rb") as f:
        b = f.read()
    if a == b:
        return []
    low = pa.lower()
    if low.endswith(".json"):
        try:
            return [f"{p}: {x} vs {y}" for p, x, y in json_diff(json.loads(a), json.loads(b))] or ["bytes differ"]
        except ValueError:
            pass
    if low.endswith(".npz"):
        return npz_diff(pa, pb) or ["bytes differ (same arrays)"]
    if low.endswith(".png"):
        try:
            return png_diff(pa, pb)
        except Exception as e:      # (not a readable PNG: bytes)
            return [f"bytes differ ({type(e).__name__})"]
    return [f"bytes differ ({len(a)} vs {len(b)} bytes, first at {_first_diff(a, b)})"]


def png_diff(pa, pb):
    import numpy as np
    from PIL import Image
    with Image.open(pa) as ia, Image.open(pb) as ib:
        a, b = np.asarray(ia), np.asarray(ib)
        ma, mb = ia.mode, ib.mode
    if ma != mb or a.shape != b.shape:
        return [f"image {ma}{a.shape} vs {mb}{b.shape}"]
    if np.array_equal(a, b):
        return ["the same pixels, other bytes (encoder)"]
    d = np.abs(a.astype(np.int64) - b.astype(np.int64))
    return [f"pixels: {int(np.count_nonzero(d.reshape(d.shape[0], d.shape[1], -1).any(axis=2)))} of "
            f"{a.shape[0] * a.shape[1]} differ, max {int(d.max())} ({ma})"]


def _first_diff(a, b):
    n = min(len(a), len(b))
    for i in range(0, n, 4096):
        if a[i:i + 4096] != b[i:i + 4096]:
            for j in range(i, min(n, i + 4096)):
                if a[j] != b[j]:
                    return j
    return n


class Report:
    """Counts and the first few differences of a comparison; exit code 0 (identical), 1 (differences), 2 (items on
    one side only)."""

    def __init__(self, what, show=20):
        self.what, self.show = what, show
        self.same = self.differ = 0
        self.only_a, self.only_b, self.lines = [], [], []

    def item(self, name, diffs):
        if diffs:
            self.differ += 1
            if self.differ <= self.show:
                self.lines.append(f"DIFF {name}: " + "; ".join(str(d) for d in diffs[:5]))
        else:
            self.same += 1

    def only(self, side, names):
        (self.only_a if side == "A" else self.only_b).extend(names)

    def finish(self, out=None):
        out = out or sys.stdout
        for ln in self.lines:
            print(ln, file=out)
        if self.differ > self.show:
            print(f"... and {self.differ - self.show} more", file=out)
        for side, names in (("A", self.only_a), ("B", self.only_b)):
            if names:
                print(f"only in {side}: {len(names)} ({', '.join(sorted(names)[:8])}{' ...' if len(names) > 8 else ''})",
                      file=out)
        print(f"{self.what}: {self.same} identical, {self.differ} differ, {len(self.only_a)} only in A, "
              f"{len(self.only_b)} only in B", file=out)
        return 1 if self.differ else 2 if (self.only_a or self.only_b) else 0


def compare_trees(a, b, sub, rep, select=None):
    """Compare the files under a/sub and b/sub by relative path into rep (select: a predicate on the path)."""
    def listing(root):
        got = {}
        base = os.path.join(root, sub)
        for d, _dirs, files in os.walk(base):
            for n in files:
                rel = os.path.relpath(os.path.join(d, n), base)
                if select is None or select(rel):
                    got[rel] = os.path.join(d, n)
        return got
    la, lb = listing(a), listing(b)
    for rel in sorted(set(la) & set(lb)):
        rep.item(rel, file_diff(la[rel], lb[rel]))
    rep.only("A", sorted(set(la) - set(lb)))
    rep.only("B", sorted(set(lb) - set(la)))
    return rep
