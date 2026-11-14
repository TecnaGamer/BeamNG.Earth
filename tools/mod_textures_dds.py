#!/usr/bin/env python3
"""mod_textures_dds.py: the mod's engine textures shipped as lossless sources, made into DDS at install (2026-11-13).

    python3 tools/mod_textures_dds.py            # every engine PNG in the level -> a source + its manifest entry
    python3 tools/mod_textures_dds.py --list     # what would be converted, with the format each gets
    python3 tools/mod_textures_dds.py FILE.png   # just these
    python3 tools/mod_textures_dds.py --build DIR  # the DDS made into DIR (a copy of mod/beamng_earth), as install.py does

The game must load only DDS: a PNG a material names is cooked by the engine's TextureCooker at run time, which has
deadlocked the game under heavy loading ("TextureCooker| Texture[] started importing" as the log's last line: direction
signs 2026-11-05, station boards 2026-11-11), and its cache is per copy of the mod (a BeamMP server's copy cooked
them all again). A DDS with its mips is uploaded as it is. But BC7 / BC5 / BC4 hardly compress (the 162 MB of
2026-11-11 made the download 58 MB bigger), so the repository and the download carry each texture's lossless source
(mod/texture_sources/<its path>.webp, a WebP lossless, exact) and modtextures.py encodes it at install time into the
DDS a material names (earthfast.texture_dds_files: BC7 by texture-compressor, deterministic). A texture set's normal
and roughness maps are computed from its colour map by the tools that drew them (ingest_rail_textures.py,
make_station_textures.py, make_sett_texture.py / make_island_textures.py): those carry no source of their own, only
how they are derived (DERIVED below; the result checked bit for bit before it is used).

The build tools (tools/build_*.py, generate_kmh_speed_signs.py, make_*.py, ...) still draw PNGs into the level: run
this after any of them (server/tests/test_mod_textures.py fails while a PNG is left in the level). It writes the
source, the manifest entry (mod/texture_sources/textures.json), rewrites the references to the .dds name and removes
the PNG.

The formats are the engine cooker's own choices (its output for this mod's PNGs, read 2026-11-11):
    *.color.png                  BC7 sRGB (DXGI 99)
    *.normal.png                 BC5 (DXGI 83: the shader reads x, y and rebuilds z, decodeNormal's BC5 path)
    *.data.png, one channel      BC4 (DXGI 80: roughness / opacity are read from .r)
    *.data.png, RGB(A)           BC7 (DXGI 98)
A PNG without a suffix takes the role of the material slot naming it (baseColorMap / colorMap / emissiveMap ->
colour, normalMap -> normal, any other -> data) and the plain UNORM format (BC7 98 for colour): a PNG carries no sRGB
flag either, so the engine treats the DDS as it treated the PNG. Sides that are not powers of two are stretched up to
them in the source (the engine refuses other sizes compressed). Mips (native texdds.rs): the full chain to 1 x 1,
2 x 2 box averages, colour in linear light (alpha-weighted where alpha varies), data as they are, normals renormalised.

Not converted: terrain sources (levels/*/art/terrains: heightmaps and layer maps are imported, the TerrainMaterial maps
go through the terrain packer, not the cooker), the UI's images (CEF), the level preview."""
import argparse
import io
import json
import os
import re
import struct
import sys

from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
import modtextures  # noqa: E402

MOD = os.path.join(ROOT, "mod", "beamng_earth")
LEVEL = os.path.join(MOD, "levels", "beamng_earth")
SOURCES = modtextures.SOURCES
SKIP_DIRS = (os.path.join(LEVEL, "art", "terrains"),)

DXGI_BC4, DXGI_BC5, DXGI_BC7, DXGI_BC7_SRGB = 80, 83, 98, 99
NAMES = {DXGI_BC4: "BC4", DXGI_BC5: "BC5", DXGI_BC7: "BC7", DXGI_BC7_SRGB: "BC7_SRGB"}
COLOR_SLOTS = {"baseColorMap", "colorMap", "diffuseMap", "emissiveMap", "baseColorDetailMap", "detailMap",
               "overlayMap", "colorPaletteMap"}
NORMAL_SLOTS = {"normalMap", "detailNormalMap", "normalDetailMap", "clearCoatBottomNormalMap"}
STEP_SUFFIX = re.compile(r"_(late|dawn)(?=(\.[a-z]+)?\.(png|dds)$)")     # nightlights.lua's later steps of a map

# Texture sets whose normal / roughness maps are functions of the colour map (the tools' parameters):
# ingest_rail_textures.TEXTURES and make_station_textures (normal_map / roughness_map: <base>_b.color ->
# <base>_nm.normal, <base>_r.data) ...
DERIVED_FFT = {"bne_rail_xing_rubber": (2.2, 0.82, 0.95), "bne_rail_ballast_stone": (5.0, 0.75, 0.95),
               "bne_rail_galv": (1.0, 0.35, 0.6), "bne_rail_mast_concrete": (2.5, 0.8, 0.95),
               "bne_cat_galv_v3": (1.0, 0.35, 0.6), "bne_cat_paint_v3": (1.2, 0.55, 0.8),
               "bne_cat_porcelain_v3": (0.8, 0.15, 0.4), "bne_cat_foundation_v3": (2.5, 0.8, 0.95),
               "bne_cat_concrete_v3": (2.5, 0.8, 0.95), "bne_platform_pavers": (3.0, 0.72, 0.92),
               "bne_platform_tactile": (4.5, 0.65, 0.88), "bne_canopy_steel": (0.8, 0.45, 0.7)}
# ... and make_sett_texture.py / make_island_textures.py (normal_map with NORMAL_STRENGTH, roughness: <name>.color ->
# <name>.normal, <name>.data)
DERIVED_BOX = {"bne_sidewalk_sett": 2.2, "bne_island_lawn": 1.4, "bne_island_shrubs": 2.4, "bne_island_gravel": 2.6,
               "bne_island_mulch": 2.0}


# --- roles -----------------------------------------------------------------------------------------------------
def engine_pngs():
    """Every PNG under the level the engine loads as a texture (not the terrain sources)."""
    out = []
    for dp, _dn, fn in os.walk(LEVEL):
        if any(dp == s or dp.startswith(s + os.sep) for s in SKIP_DIRS):
            continue
        out += [os.path.join(dp, f) for f in fn if f.lower().endswith(".png")]
    return sorted(out)


def material_files():
    return sorted(os.path.join(dp, f) for dp, _dn, fn in os.walk(LEVEL) for f in fn if f.endswith(".materials.json"))


def slot_roles():
    """{vfs path without its extension (lower case): set of material slots naming it} over every materials.json."""
    roles = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and v.lower().endswith((".png", ".dds")):
                    roles.setdefault(v.lower()[:-4], set()).add(k)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for p in material_files():
        with open(p, encoding="utf-8") as f:
            walk(json.load(f))
    return roles


def vfs(path):
    return "/" + os.path.relpath(path, MOD).replace(os.sep, "/")


def role_of(path, roles):
    """('color', srgb) / ('normal', False) / ('data', False) for a PNG: its suffix, else its material slot."""
    name = os.path.basename(path).lower()
    if name.endswith(".color.png"):
        return "color", True
    if name.endswith(".normal.png"):
        return "normal", False
    if name.endswith(".data.png"):
        return "data", False
    if name.endswith(".emissive.png"):
        return "color", False
    v = vfs(path)
    slots = roles.get(v.lower()[:-4]) or roles.get(STEP_SUFFIX.sub("", v).lower()[:-4]) or set()
    if slots & NORMAL_SLOTS:
        return "normal", False
    if slots & COLOR_SLOTS or not slots:
        return "color", False
    return "data", False


def pow2_size(w, h):
    """The sides stretched up to powers of two: the engine refuses a block-compressed texture of any other size
    ("_validateTexParams| ... Unable to load compressed texture with non pow2 size": the 1024 x 384 crossing
    signs, 2026-11-11); the shapes map their textures 0..1, so a stretch keeps every UV."""
    return 1 << max(0, (w - 1).bit_length()), 1 << max(0, (h - 1).bit_length())


def load(path):
    """A PIL image ('L', 'RGB' or 'RGBA': alpha kept only when it is used) of a PNG, its sides stretched to powers
    of two (pow2_size)."""
    with Image.open(path) as im:
        im = im.convert("L") if im.mode in ("L", "I;16", "I", "F") else im.convert("RGBA")
        size = pow2_size(*im.size)
        if size != im.size:
            im = im.resize(size, Image.LANCZOS)
        im.load()
    if im.mode == "RGBA" and im.getchannel("A").getextrema() == (255, 255):
        im = im.convert("RGB")
    return im


def choose(path, roles, img=None):
    """(role, DXGI format) of a PNG."""
    role, srgb = role_of(path, roles)
    if img is None:
        img = load(path)
    if role == "normal":
        return role, DXGI_BC5
    if role == "data":
        return role, DXGI_BC4 if img.mode == "L" else DXGI_BC7
    return role, DXGI_BC7_SRGB if srgb else DXGI_BC7


# --- sources ---------------------------------------------------------------------------------------------------
def rel(path):
    return os.path.relpath(path, MOD).replace(os.sep, "/")


def dds_path(png):
    return png[:-4] + ".dds"


def webp_bytes(im):
    """A lossless, exact WebP of a PIL image (the colour under transparent texels kept)."""
    b = io.BytesIO()
    im.save(b, "WEBP", lossless=True, quality=100, method=6, exact=True)
    return b.getvalue()


def pixels(entry_or_bytes, derive=""):
    """A PIL image of what the encoder takes for a manifest entry (or a source's bytes and its derive)."""
    ef = modtextures.native()
    if isinstance(entry_or_bytes, dict):
        with open(os.path.join(SOURCES, *entry_or_bytes["source"].split("/")), "rb") as f:
            data = f.read()
        derive = entry_or_bytes.get("derive", "")
    else:
        data = entry_or_bytes
    w, h, c, px = ef.texture_pixels(data, derive)
    im = Image.frombytes({1: "L", 3: "RGB", 4: "RGBA"}[c], (w, h), px)
    if isinstance(entry_or_bytes, dict) and entry_or_bytes["format"] == "BC4" and im.mode != "L":
        im = im.getchannel(0)       # (a one-channel map, as its DDS decodes)
    return im


def same(a, b):
    """Two PIL images hold the same texels (a grey image against the first channel of a colour one)."""
    if a.size != b.size:
        return False
    if a.mode == "L" or b.mode == "L":
        return a.getchannel(0).tobytes() == b.getchannel(0).tobytes()
    return a.mode == b.mode and a.tobytes() == b.tobytes()


def derive_candidates(png):
    """[(colour map's PNG path, derive)] that may make this PNG (DERIVED_FFT / DERIVED_BOX)."""
    d, name = os.path.split(png)
    out = []
    for base, (s, lo, hi) in DERIVED_FFT.items():
        if name == base + "_nm.normal.png":
            out.append((os.path.join(d, base + "_b.color.png"), f"normal_fft:{s}"))
        if name == base + "_r.data.png":
            out.append((os.path.join(d, base + "_b.color.png"), f"rough_fft:{lo}:{hi}"))
    for base, s in DERIVED_BOX.items():
        if name == base + ".normal.png":
            out.append((os.path.join(d, base + ".color.png"), f"normal_box:{s}"))
        if name == base + ".data.png":
            out.append((os.path.join(d, base + ".color.png"), "rough_box"))
    return out


def write_manifest(entries):
    os.makedirs(SOURCES, exist_ok=True)
    doc = ("The mod's DDS textures made at install time (modtextures.py, tools/mod_textures_dds.py): DDS path in the "
           "mod -> its lossless source under mod/texture_sources, DXGI format, mip role, and for a normal / roughness "
           "map computed from a colour map how (native texdds.rs derive_map).")
    tmp = modtextures.MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"_": doc, "textures": dict(sorted(entries.items()))}, f, indent=1)
        f.write("\n")
    os.replace(tmp, modtextures.MANIFEST)


def convert(pngs, keep=False, quiet=False):
    """PNGs in the level -> sources + manifest entries; references rewritten to .dds; the PNGs removed (unless keep).
    Returns the manifest entries made."""
    roles = slot_roles()
    entries = modtextures.manifest()
    made = {}
    by_png = {}
    # colour maps first: a derived map's source is its colour map's
    order = sorted(pngs, key=lambda p: bool(derive_candidates(p)))
    for p in order:
        img = load(p)
        role, dxgi = choose(p, roles, img)
        target = rel(dds_path(p))
        entry = None
        for colour, derive in derive_candidates(p):
            src = by_png.get(colour) or entries.get(rel(dds_path(colour)), {}).get("source")
            if not src or entries.get(rel(dds_path(colour)), made.get(rel(dds_path(colour)), {})).get("derive"):
                continue
            cand = {"source": src, "format": NAMES[dxgi], "role": role, "derive": derive}
            if same(pixels(cand), img):
                entry = cand
                break
        if entry is None:
            src = rel(p)[:-4] + ".webp"
            data = webp_bytes(img)
            if not same(pixels(data), img):
                raise SystemExit(f"{p}: its WebP does not decode to the same texels")
            out = os.path.join(SOURCES, *src.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(data)
            entry = {"source": src, "format": NAMES[dxgi], "role": role}
            by_png[p] = src
        made[target] = entry
        entries[target] = entry
        stale = os.path.join(MOD, *target.split("/"))
        if os.path.exists(stale):
            os.remove(stale)        # (a DDS of it shipped before: now made at install)
        if not quiet:
            how = f"from {entry['source']} ({entry['derive']})" if entry.get("derive") else entry["source"]
            print(f"{entry['format']:9s} {role:6s} {img.size[0]}x{img.size[1]} {target} <- {how}", flush=True)
    write_manifest(entries)
    changed = rewrite_refs(pngs)
    if not quiet:
        for p in changed:
            print("references:", os.path.relpath(p, ROOT))
    if not keep:
        for p in pngs:
            os.remove(p)
    prune_sources()
    return made


def prune_sources():
    """Remove the source files no manifest entry names."""
    named = {os.path.join(SOURCES, *e["source"].split("/")) for e in modtextures.manifest().values()}
    for dp, _dn, fn in os.walk(SOURCES):
        for f in fn:
            p = os.path.join(dp, f)
            if p != modtextures.MANIFEST and p not in named:
                os.remove(p)


def drop(dds_rels):
    """Remove manifest entries (and their sources when nothing else names them): a texture no material uses."""
    entries = modtextures.manifest()
    for r in dds_rels:
        entries.pop(r, None)
    write_manifest(entries)
    prune_sources()


def shipped(obj):
    """A build tool's output as the mod ships it after this conversion: a file name 'x.png' -> 'x.dds'; in a
    material (a dict / list / str), every level texture path (not a terrain's) '.png' -> '.dds'. For the tests that
    check a tool still makes the shipped files."""
    if isinstance(obj, dict):
        return {k: shipped(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shipped(v) for v in obj]
    if isinstance(obj, str):
        if "/" not in obj and obj.lower().endswith(".png"):
            return obj[:-4] + ".dds"
        return _REF.sub(r"\1.dds", obj)
    return obj


_REF = re.compile(r"(/levels/beamng_earth/(?!art/terrains/)[A-Za-z0-9_./-]+?)\.png$", re.I)


def entry_of(path):
    """The manifest entry of a DDS path in the mod (absolute or relative to it), or None."""
    p = os.path.normpath(str(path))
    r = rel(p) if os.path.isabs(p) else p.replace(os.sep, "/")
    return modtextures.manifest().get(r)


def is_shipped(path):
    """Is a texture of the mod (a path in mod/beamng_earth) in what the game gets: in the tree, or made at install
    from its source (modtextures.py)? For the tests that checked a DDS file's existence in the tree."""
    p = os.path.normpath(str(path))
    return os.path.isfile(p) or entry_of(p) is not None


def shipped_names(folder):
    """The file names a folder of the mod holds as the game gets it: its files and the DDS made into it at install."""
    folder = os.path.normpath(str(folder))
    d = rel(folder).rstrip("/") + "/"
    names = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    return names | {r[len(d):] for r in modtextures.manifest() if r.startswith(d) and "/" not in r[len(d):]}


def texture_psnr(image, dds):
    """PSNR (dB) of a drawn texture (a PIL image or a path) against what the mod ships for a DDS path: the DDS if it is
    in the tree (decoded, level 0; the channels the DDS holds: BC4 one, BC5 two, BC7 RGBA), else the source the DDS is
    made from at install (99: the same texels)."""
    import numpy as np
    a = image if isinstance(image, Image.Image) else Image.open(image)
    entry = None if os.path.exists(str(dds)) else entry_of(dds)
    if entry is not None:
        b = pixels(entry)
        if a.size != b.size:
            a = a.resize(b.size, Image.LANCZOS)
        if b.mode == "L" or entry["format"] == "BC4":
            aa, bb = np.asarray(a.convert("L"), np.float64), np.asarray(b.getchannel(0), np.float64)
        else:
            aa, bb = np.asarray(a.convert(b.mode), np.float64), np.asarray(b, np.float64)
            if entry["format"] == "BC5":
                aa, bb = aa[..., :2], bb[..., :2]
    else:
        with Image.open(dds) as b:
            mode = b.mode
            bb = np.asarray(b).astype(np.float64)
        aa = np.asarray(a.convert(mode)).astype(np.float64)
        if aa.shape != bb.shape:
            return 0.0
        if mode == "RGB" and dds_format(dds) == DXGI_BC5:
            aa, bb = aa[..., :2], bb[..., :2]
    m = np.mean((aa - bb) ** 2)
    return 99.0 if m == 0 else float(10 * np.log10(255.0 ** 2 / m))


def dds_format(path):
    with open(path, "rb") as f:
        head = f.read(132)
    return struct.unpack_from("<I", head, 128)[0] if head[84:88] == b"DX10" else head[84:88].decode("latin1")


def open_texture(path):
    """A PIL image of a mod texture by either name: the PNG if it is there, else its DDS in the tree, else the source
    the DDS is made from at install. For the build tools and tests that read a texture the mod ships."""
    p = str(path)
    if p.lower().endswith(".png") and not os.path.exists(p):
        p = dds_path(p)
    if os.path.exists(p):
        return Image.open(p)
    entry = entry_of(p)
    if entry is None:
        raise FileNotFoundError(p)
    return pixels(entry)


# --- references ------------------------------------------------------------------------------------------------
def rewrite_refs(converted):
    """Every materials.json / Lua string naming a converted PNG (by its VFS path) now names the DDS. Returns the
    files changed. (The .dae files' own image paths are left: every material they name is defined in a
    materials.json, which wins, and a changed .dae is imported again on the players' machines.)"""
    names = {vfs(p): vfs(dds_path(p)) for p in converted}
    lower = {k.lower(): v for k, v in names.items()}
    pat = re.compile(r"/levels/beamng_earth/[A-Za-z0-9_./-]+?\.png", re.I)
    changed = []
    files = material_files()
    for dp, _dn, fn in os.walk(os.path.join(MOD, "lua")):
        files += [os.path.join(dp, f) for f in fn if f.endswith(".lua")]
    for p in files:
        with open(p, encoding="utf-8") as f:
            s = f.read()
        t = pat.sub(lambda m: lower.get(m.group(0).lower(), m.group(0)), s)
        if t != s:
            with open(p, "w", encoding="utf-8") as f:
                f.write(t)
            changed.append(p)
    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*", help="PNGs to convert (default: every engine PNG in the level)")
    ap.add_argument("--list", action="store_true", help="only list what would be converted")
    ap.add_argument("--keep", action="store_true", help="keep the PNGs (for a comparison)")
    ap.add_argument("--build", metavar="DIR", help="make the DDS into DIR (a copy of mod/beamng_earth), as install does")
    args = ap.parse_args()
    if args.build:
        todo = modtextures.plan()
        n, r, s = modtextures.build(args.build, todo, modtextures.reusable(args.build, todo))
        print(f"{n} encoded, {r} reused, {s:.1f} s")
        return 0
    pngs = [os.path.abspath(f) for f in args.files] or engine_pngs()
    if args.list:
        roles = slot_roles()
        for p in pngs:
            img = load(p)
            role, dxgi = choose(p, roles, img)
            print(f"{NAMES[dxgi]:9s} {role:6s} {img.size[0]}x{img.size[1]} {img.mode:4s} {rel(p)}")
        return 0
    if pngs:
        convert(pngs, keep=args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
