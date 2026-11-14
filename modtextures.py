"""The mod's block-compressed textures, made on the player's computer (2026-11-13; standard library + the native module).

The game must only ever load DDS (a PNG a material names is converted by the engine's TextureCooker at run time, which
has deadlocked the game under heavy loading: 2026-11-11), but BC7 / BC5 / BC4 hardly compress, and 162 MB of them
(v1.9.1-beta.dev.414) made the download 58 MB bigger. So the download carries their lossless sources
(mod/texture_sources: WebP, and for a texture set's normal and roughness maps only the colour map they are computed
from), and install.py encodes them into the installed mod at the paths the materials name (earthfast.texture_dds_files,
native/earthfast/src/texdds.rs: deterministic, every CPU).

    mod/texture_sources/textures.json   {"textures": {DDS path in the mod: {"source": path under mod/texture_sources,
                                         "format": "BC7_SRGB" | "BC7" | "BC5" | "BC4", "role": "color" | "normal" |
                                         "data", "derive": "" | "normal_fft:S" | ...}}}

The installed copy keeps STAMP (<mod>/.bne_textures.json): each DDS's key (its source's SHA-256, its format, role,
derive and the encoder's TEXDDS_ID) and its own SHA-256, so an update encodes only the textures whose key changed
(tools/mod_textures_dds.py makes the sources from the build tools' PNGs; tools/package.py encodes them for the BeamMP
server's client zip)."""
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCES = os.path.join(ROOT, "mod", "texture_sources")
MANIFEST = os.path.join(SOURCES, "textures.json")
STAMP = ".bne_textures.json"
NATIVE = 285                    # the earthfast version with texdds


class TexturesError(RuntimeError):
    """The textures could not be made (no native module, a source missing, an encode failed)."""


def native():
    """The earthfast module (bundled in the app, else server/earthfast.abi3.so / .pyd beside this file) at NATIVE or newer."""
    try:
        import earthfast
    except ImportError:
        server = os.path.join(ROOT, "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        try:
            import earthfast
        except ImportError as e:
            raise TexturesError(f"the native module (server/earthfast) could not be loaded: {e}") from None
    if getattr(earthfast, "VERSION", 0) < NATIVE or not hasattr(earthfast, "texture_dds_files"):
        raise TexturesError(f"the native module (server/earthfast) is version {getattr(earthfast, 'VERSION', 0)}, "
                            f"older than {NATIVE}: rebuild it with python3 tools/build_native.py")
    return earthfast


def manifest(path=MANIFEST):
    """{DDS path in the mod: entry} of the textures made at install time ({} without a manifest)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["textures"]
    except FileNotFoundError:
        return {}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan(sources=SOURCES, entries=None):
    """{DDS path in the mod: dict(entry, path=its source file, key=its key)}. Raises TexturesError for a missing source."""
    entries = manifest(os.path.join(sources, "textures.json")) if entries is None else entries
    if not entries:
        return {}
    texdds = native().TEXDDS_ID
    hashes = {}
    out = {}
    for rel, e in sorted(entries.items()):
        src = os.path.join(sources, *e["source"].split("/"))
        if src not in hashes:
            try:
                hashes[src] = _sha256_file(src)
            except OSError as err:
                raise TexturesError(f"texture source missing: {err}") from None
        key = hashlib.sha256("\0".join((texdds, e["format"], e["role"], e.get("derive", ""), hashes[src]))
                             .encode()).hexdigest()
        out[rel] = dict(e, path=src, key=key)
    return out


def read_stamp(mod_dir):
    try:
        with open(os.path.join(mod_dir, STAMP), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def reusable(mod_dir, todo):
    """The DDS of `todo` already in mod_dir as they would be made now: the stamp's key the same and the file's own
    SHA-256 as the stamp recorded it."""
    stamp = read_stamp(mod_dir)
    keep = set()
    for rel, e in todo.items():
        s = stamp.get(rel)
        path = os.path.join(mod_dir, *rel.split("/"))
        if not isinstance(s, dict) or s.get("key") != e["key"] or not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) == s.get("size") and _sha256_file(path) == s.get("sha256"):
                keep.add(rel)
        except OSError:
            pass
    return keep


def current(mod_dir, todo):
    """Every DDS of `todo` in mod_dir as it would be made now (and no other in the stamp)."""
    return set(read_stamp(mod_dir)) == set(todo) and reusable(mod_dir, todo) == set(todo)


def build(mod_dir, todo, keep=(), threads=0, cache_dir=None):
    """Encode every DDS of `todo` not in `keep` into mod_dir (a copy of mod/beamng_earth), then write the stamp.
    cache_dir: a folder of <key>.dds kept between builds (tools/package.py). Returns (encoded, reused, seconds)."""
    t0 = time.time()
    jobs, names, cached = [], [], 0
    for rel, e in sorted(todo.items()):
        if rel in keep:
            continue
        dst = os.path.join(mod_dir, *rel.split("/"))
        hit = cache_dir and os.path.join(cache_dir, e["key"] + ".dds")
        if hit and os.path.isfile(hit):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(hit, "rb") as f, open(dst + ".tmp", "wb") as g:
                g.write(f.read())
            os.replace(dst + ".tmp", dst)
            cached += 1
            continue
        jobs.append((e["path"], dst, e["format"], e["role"], e.get("derive", "")))
        names.append(rel)
    if jobs:
        errs = native().texture_dds_files(jobs, int(threads or os.cpu_count() or 4))
        bad = [f"{n}: {err}" for n, err in zip(names, errs) if err]
        if bad:
            raise TexturesError("could not make the textures:\n  " + "\n  ".join(bad[:10]))
    stamp = {}
    for rel, e in sorted(todo.items()):
        path = os.path.join(mod_dir, *rel.split("/"))
        stamp[rel] = {"key": e["key"], "size": os.path.getsize(path), "sha256": _sha256_file(path)}
        if cache_dir and rel in names:
            os.makedirs(cache_dir, exist_ok=True)
            hit = os.path.join(cache_dir, e["key"] + ".dds")
            if not os.path.isfile(hit):
                with open(path, "rb") as f, open(hit + ".tmp", "wb") as g:
                    g.write(f.read())
                os.replace(hit + ".tmp", hit)
    tmp = os.path.join(mod_dir, STAMP + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=0, sort_keys=True)
    os.replace(tmp, os.path.join(mod_dir, STAMP))
    return len(jobs), len(keep) + cached, time.time() - t0


def generated(todo):
    """The paths (relative to the mod, '/'-separated) this module writes into an installed mod: not part of its source."""
    return set(todo) | {STAMP}


if __name__ == "__main__":
    # python3 modtextures.py DIR: the textures made into DIR (a copy of mod/beamng_earth), with the timing
    import argparse
    ap = argparse.ArgumentParser(description="Make the mod's DDS textures from mod/texture_sources into a mod folder.")
    ap.add_argument("mod_dir")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="encode every texture again")
    a = ap.parse_args()
    todo = plan()
    keep = set() if a.force else reusable(a.mod_dir, todo)
    n, r, s = build(a.mod_dir, todo, keep, a.threads)
    print(f"{n} textures encoded, {r} reused, {s:.1f} s ({a.threads or os.cpu_count()} threads)")
