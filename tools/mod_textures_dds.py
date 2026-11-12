#!/usr/bin/env python3
"""mod_textures_dds.py: the mod's engine textures as DDS, never PNG (2026-11-11).

    python3 tools/mod_textures_dds.py            # every engine PNG in the mod -> DDS, references rewritten, PNGs removed
    python3 tools/mod_textures_dds.py --list     # what would be converted, with the format each gets
    python3 tools/mod_textures_dds.py FILE.png   # just these (references still rewritten)

Why: a PNG a material names is converted by the engine's TextureCooker at run time. Under heavy loading the cooker
has deadlocked the game for good ("TextureCooker| Texture[] started importing" as the log's last line: direction
signs 2026-11-05, station boards 2026-11-11), and its cache is per copy of the mod: every switch between the
player's mod and a BeamMP server's copy cooked all of them again (~160 s per join on the Steam Deck). A DDS with its
mips is uploaded as it is. The build tools (tools/build_*.py, generate_kmh_speed_signs.py, make_*.py, ...) still
draw PNGs: run this after any of them (server/tests/test_mod_textures.py fails while a PNG is left in the level).

The formats are the engine's own cooker's choices (its output for this mod's PNGs, <user>/temp/levels/beamng_earth/
art/shapes/beamngearth/*.dds, read 2026-11-11), so a texture looks as it did after cooking:
    *.color.png                  BC7 sRGB (DXGI 99)
    *.normal.png                 BC5 (DXGI 83: the shader reads x, y and rebuilds z, decodeNormal's BC5 path)
    *.data.png, one channel      BC4 (DXGI 80: roughness / opacity are read from .r)
    *.data.png, RGB(A)           BC7 (DXGI 98)
A PNG without a suffix (the engine never cooked those: it loaded them whole, RGBA8 and its own mips) takes the role
of the material slot naming it (baseColorMap / colorMap / emissiveMap -> colour, normalMap -> normal, any other
-> data) and the plain UNORM format (BC7 98 for colour): a PNG carries no sRGB flag either, so the engine treats
the DDS as it treated the PNG, as it does stock DXT1 colour maps.
Sides that are not powers of two are stretched up to them (the engine refuses other sizes compressed).
Mips: the full chain to 1 x 1, 2 x 2 box averages: colour in linear light (alpha-weighted where alpha varies),
data as they are, normals as unit vectors (renormalised).

Not converted: terrain sources (levels/*/art/terrains: heightmaps and layer maps are imported, the TerrainMaterial
maps go through the terrain packer into temp/art/terrainmaterialcache, not the cooker, and the server keys that
cache on their paths), the UI's images (CEF), the level preview.

BC7 needs etcpak (PyPI, bc7enc by Rich Geldreich) in a venv:
    python3 -m venv ~/.venv-tex --system-site-packages && ~/.venv-tex/bin/pip install etcpak archspec
    ~/.venv-tex/bin/python3 tools/mod_textures_dds.py
BC4 / BC5 are encoded here (numpy: each block's endpoints searched round its extremes, indices nearest)."""
import argparse
import json
import os
import re
import struct
import sys

import numpy as np
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MOD = os.path.join(ROOT, "mod", "beamng_earth")
LEVEL = os.path.join(MOD, "levels", "beamng_earth")
SKIP_DIRS = (os.path.join(LEVEL, "art", "terrains"),)

DXGI_BC4, DXGI_BC5, DXGI_BC7, DXGI_BC7_SRGB = 80, 83, 98, 99
COLOR_SLOTS = {"baseColorMap", "colorMap", "diffuseMap", "emissiveMap", "baseColorDetailMap", "detailMap",
               "overlayMap", "colorPaletteMap"}
NORMAL_SLOTS = {"normalMap", "detailNormalMap", "normalDetailMap", "clearCoatBottomNormalMap"}
STEP_SUFFIX = re.compile(r"_(late|dawn)(?=(\.[a-z]+)?\.png$)")     # nightlights.lua's later steps of a map


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
    """{vfs path (lower case): set of material slots naming it} over every materials.json in the level."""
    roles = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and v.lower().endswith(".png"):
                    roles.setdefault(v.lower(), set()).add(k)
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
    slots = roles.get(vfs(path).lower()) or roles.get(STEP_SUFFIX.sub("", vfs(path)).lower()) or set()
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
    """uint8 [h, w, c] (c 1 for a grey-only PNG, 3 or 4 otherwise: alpha kept only when it is used), its sides
    stretched to powers of two (pow2_size)."""
    with Image.open(path) as im:
        im = im.convert("L") if im.mode in ("L", "I;16", "I", "F") else im.convert("RGBA")
        size = pow2_size(*im.size)
        if size != im.size:
            im = im.resize(size, Image.LANCZOS)
        a = np.asarray(im)
    if a.ndim == 2:
        return a[..., None]
    return a if (a[..., 3] != 255).any() else a[..., :3]


def choose(path, roles, img=None):
    """(role, DXGI format) of a PNG."""
    role, srgb = role_of(path, roles)
    if img is None:
        img = load(path)
    if role == "normal":
        return role, DXGI_BC5
    if role == "data":
        return role, DXGI_BC4 if img.shape[2] == 1 else DXGI_BC7
    return role, DXGI_BC7_SRGB if srgb else DXGI_BC7


# --- mips ------------------------------------------------------------------------------------------------------
_TO_LIN = ((np.arange(256) / 255.0) ** 2.2).astype(np.float32)   # (as imagery.py's mips)


def _to_lin(u8):
    x = u8.astype(np.float32) / 255.0
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def _to_srgb(lin):
    x = np.clip(lin, 0.0, 1.0)
    s = np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)
    return np.clip(np.rint(s * 255.0), 0, 255).astype(np.uint8)


def _half(f):
    """A float32 [h, w, c] image halved (2 x 2 box; an odd side's last row / column folds into its neighbour, as a
    box filter down to max(1, n // 2))."""
    h, w = f.shape[:2]
    nh, nw = max(1, h // 2), max(1, w // 2)
    chans = [np.asarray(Image.fromarray(np.ascontiguousarray(f[..., c]), "F").resize((nw, nh), Image.BOX))
             for c in range(f.shape[2])]
    return np.stack(chans, -1).astype(np.float32)


def mip_chain(img, role):
    """[level 0, 1, ...] uint8 arrays down to 1 x 1."""
    levels = [img]
    h, w, c = img.shape
    if role == "normal":
        v = img[..., :3].astype(np.float32) / 127.5 - 1.0
        v /= np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-6)
        cur = v
        while max(cur.shape[:2]) > 1:
            cur = _half(cur)
            cur /= np.maximum(np.linalg.norm(cur, axis=-1, keepdims=True), 1e-6)
            u = np.clip(np.rint((cur + 1.0) * 127.5), 0, 255).astype(np.uint8)
            levels.append(u)
        return levels
    if role == "color":
        lin = _to_lin(img[..., :3])
        alpha = img[..., 3:4].astype(np.float32) / 255.0 if c == 4 else None
        cur_rgb, cur_a = lin, alpha
        while max(cur_rgb.shape[:2]) > 1:
            if cur_a is not None:
                pre = _half(np.concatenate([cur_rgb * cur_a, cur_a], -1))
                a = pre[..., 3:4]
                avg = _half(cur_rgb)       # (where every texel is transparent: the plain average)
                cur_rgb = np.where(a > 1e-4, pre[..., :3] / np.maximum(a, 1e-4), avg)
                cur_a = a
                levels.append(np.concatenate([_to_srgb(cur_rgb), np.clip(np.rint(a * 255), 0, 255).astype(np.uint8)], -1))
            else:
                cur_rgb = _half(cur_rgb)
                levels.append(_to_srgb(cur_rgb))
        return levels
    cur = img.astype(np.float32)
    while max(cur.shape[:2]) > 1:
        cur = _half(cur)
        levels.append(np.clip(np.rint(cur), 0, 255).astype(np.uint8))
    return levels


# --- block encoders --------------------------------------------------------------------------------------------
def _pad4(a):
    """An [h, w, c] array edge-padded to multiples of 4."""
    h, w = a.shape[:2]
    ph, pw = (-h) % 4, (-w) % 4
    return np.pad(a, ((0, ph), (0, pw), (0, 0)), mode="edge") if ph or pw else a


def _blocks(ch):
    """[n, 16] float32 texels (row-major blocks, row-major texels) of a padded [h, w] channel."""
    h, w = ch.shape
    return ch.reshape(h // 4, 4, w // 4, 4).transpose(0, 2, 1, 3).reshape(-1, 16).astype(np.float32)


def _bc4_blocks(t):
    """BC4 blocks (uint8 [n, 8]) of [n, 16] float texels 0..255: the 8-value mode (e0 > e1) with endpoints searched
    in a +-3 window round the block's extremes (an least-squares refit tried too), the 6-value mode with 0 / 255
    (e0 <= e1) when a block holds either extreme; the lower squared error wins."""
    n = len(t)
    lo, hi = t.min(1), t.max(1)
    best_err = np.full(n, np.inf, np.float32)
    best_e = np.zeros((n, 2), np.int32)
    best_idx = np.zeros((n, 16), np.int32)
    best_mode6 = np.zeros(n, bool)

    def try8(e0, e1):
        nonlocal best_err
        e0 = np.clip(e0, 0, 255).astype(np.int32)
        e1 = np.clip(e1, 0, 255).astype(np.int32)
        ok = e0 > e1
        span = np.maximum(e0 - e1, 1).astype(np.float32)
        k = np.clip(np.rint((t - e1[:, None]) / span[:, None] * 7.0), 0, 7)
        val = (e1[:, None] * (7 - k) + e0[:, None] * k) / 7.0
        err = ((val - t) ** 2).sum(1)
        err = np.where(ok, err, np.inf)
        better = err < best_err
        idx = np.where(k == 7, 0, np.where(k == 0, 1, 8 - k)).astype(np.int32)
        best_err = np.where(better, err, best_err)
        best_e[better] = np.stack([e0, e1], 1)[better]
        best_idx[better] = idx[better]
        best_mode6[better] = False
        return k, better

    lo_i, hi_i = np.floor(lo).astype(np.int32), np.ceil(hi).astype(np.int32)
    for d0 in range(-3, 4):
        for d1 in range(-3, 4):
            try8(hi_i + d0, lo_i + d1)
    # least-squares refit of the best 8-value endpoints for their indices, twice
    for _ in range(2):
        k = np.where(best_idx == 0, 7, np.where(best_idx == 1, 0, 8 - best_idx)).astype(np.float32) / 7.0
        k = np.where(best_mode6[:, None], 0.5, k)
        a00 = ((1 - k) ** 2).sum(1); a01 = ((1 - k) * k).sum(1); a11 = (k ** 2).sum(1)
        b0 = ((1 - k) * t).sum(1); b1 = (k * t).sum(1)
        det = a00 * a11 - a01 * a01
        with np.errstate(divide="ignore", invalid="ignore"):
            e1f = (b0 * a11 - b1 * a01) / det
            e0f = (a00 * b1 - a01 * b0) / det
        good = np.abs(det) > 1e-6
        e0f = np.where(good, e0f, hi); e1f = np.where(good, e1f, lo)
        try8(np.rint(e0f), np.rint(e1f))
    # 6-value mode: interior endpoints, 0 and 255 exact
    has_ext = ((t <= 0.5) | (t >= 254.5)).any(1)
    if has_ext.any():
        inner = np.where((t > 0.5) & (t < 254.5), t, np.nan)
        with np.errstate(all="ignore"):
            ilo = np.nan_to_num(np.nanmin(inner, 1), nan=0.0)
            ihi = np.nan_to_num(np.nanmax(inner, 1), nan=0.0)
        e0 = np.clip(np.floor(ilo), 0, 255).astype(np.int32)
        e1 = np.clip(np.ceil(ihi), 0, 255).astype(np.int32)
        e1 = np.maximum(e1, e0)
        cand = np.stack([(e0 * (5 - j) + e1 * j) / 5.0 for j in range(6)] +
                        [np.zeros(n), np.full(n, 255.0)], 1)                    # [n, 8] values of k = 0..5, 0, 255
        d = (cand[:, None, :] - t[:, :, None]) ** 2                              # [n, 16, 8]
        j = d.argmin(2)
        err = np.take_along_axis(d, j[..., None], 2)[..., 0].sum(1)
        # index of value j: j 0 -> e0 (idx 0), 5 -> e1 (idx 1), 1..4 -> idx 2..5, 6 -> idx 6 (0), 7 -> idx 7 (255)
        idx = np.where(j == 0, 0, np.where(j == 5, 1, np.where(j <= 4, j + 1, j))).astype(np.int32)
        better = has_ext & (err < best_err)
        best_err = np.where(better, err, best_err)
        best_e[better] = np.stack([e0, e1], 1)[better]
        best_idx[better] = idx[better]
        best_mode6[better] = True
    # a flat block: e0 == e1 (decoded by the 6-value rule: idx 0 = e0 everywhere)
    flat = hi - lo < 0.5
    if flat.any():
        v = np.clip(np.rint(lo), 0, 255).astype(np.int32)
        best_e[flat] = np.stack([v, v], 1)[flat]
        best_idx[flat] = 0
    bits = np.zeros(n, np.uint64)
    for i in range(16):
        bits |= best_idx[:, i].astype(np.uint64) << np.uint64(3 * i)
    out = np.zeros((n, 8), np.uint8)
    out[:, 0] = best_e[:, 0]
    out[:, 1] = best_e[:, 1]
    for b in range(6):
        out[:, 2 + b] = ((bits >> np.uint64(8 * b)) & np.uint64(0xFF)).astype(np.uint8)
    return out


def encode_bc4(level):
    a = _pad4(level[..., :1])
    return _bc4_blocks(_blocks(a[..., 0])).tobytes()


def encode_bc5(level):
    a = _pad4(level)
    r = _bc4_blocks(_blocks(a[..., 0]))
    g = _bc4_blocks(_blocks(a[..., 1]))
    return np.concatenate([r, g], 1).tobytes()


def encode_bc7(level, perceptual):
    import etcpak
    a = _pad4(level)
    h, w, c = a.shape
    rgba = a if c == 4 else np.concatenate([np.repeat(a, 3, 2) if c == 1 else a, np.full((h, w, 1), 255, np.uint8)], 2)
    p = etcpak.BC7CompressBlockParams()
    if perceptual:
        p.init_perceptual_weights()
    p.m_uber_level = 2
    return etcpak.compress_bc7(np.ascontiguousarray(rgba).tobytes(), w, h, p)


# --- DDS -------------------------------------------------------------------------------------------------------
def dds_header(w, h, mips, dxgi):
    block = 8 if dxgi == DXGI_BC4 else 16
    linear = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000      # CAPS HEIGHT WIDTH PIXELFORMAT MIPMAPCOUNT LINEARSIZE
    head = struct.pack("<4s7I", b"DDS ", 124, flags, h, w, linear, 1, mips)
    head += b"\0" * 44
    head += struct.pack("<2I4s5I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
    head += struct.pack("<4I", 0x401008 if mips > 1 else 0x1000, 0, 0, 0)
    head += struct.pack("<I", 0)
    head += struct.pack("<5I", dxgi, 3, 0, 1, 0)
    return head


def encode_dds(img, role, dxgi):
    levels = mip_chain(img, role)
    h, w = img.shape[:2]
    body = []
    for lv in levels:
        if dxgi == DXGI_BC4:
            body.append(encode_bc4(lv))
        elif dxgi == DXGI_BC5:
            body.append(encode_bc5(lv))
        else:
            body.append(encode_bc7(lv, perceptual=(role == "color")))
    return dds_header(w, h, len(levels), dxgi) + b"".join(body)


def dds_path(png):
    return png[:-4] + ".dds"


_REF = re.compile(r"(/levels/beamng_earth/(?!art/terrains/)[A-Za-z0-9_./-]+?)\.png$", re.I)


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


def texture_psnr(image, dds):
    """PSNR (dB) of a drawn texture (a PIL image or a path) against the mod's DDS of it, decoded (level 0; the
    channels the DDS holds: BC4 one, BC5 two, BC7 RGBA)."""
    a = image if isinstance(image, Image.Image) else Image.open(image)
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
    """A PIL image of a mod texture by either name (the PNG if it is there, else its DDS: Pillow decodes BC4/5/7).
    For the build tools and tests that read a texture the mod now ships as DDS."""
    p = str(path)
    if p.lower().endswith(".png") and not os.path.exists(p) and os.path.exists(dds_path(p)):
        p = dds_path(p)
    return Image.open(p)


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
    args = ap.parse_args()
    roles = slot_roles()
    pngs = [os.path.abspath(f) for f in args.files] or engine_pngs()
    names = {DXGI_BC4: "BC4", DXGI_BC5: "BC5", DXGI_BC7: "BC7", DXGI_BC7_SRGB: "BC7_SRGB"}
    done = []
    for p in pngs:
        img = load(p)
        role, dxgi = choose(p, roles, img)
        rel = os.path.relpath(p, MOD)
        if args.list:
            print(f"{names[dxgi]:9s} {role:6s} {img.shape[1]}x{img.shape[0]}x{img.shape[2]} {rel}")
            continue
        data = encode_dds(img, role, dxgi)
        out = dds_path(p)
        with open(out + ".tmp", "wb") as f:
            f.write(data)
        os.replace(out + ".tmp", out)
        done.append(p)
        print(f"{names[dxgi]:9s} {os.path.getsize(p) // 1024:6d} KB -> {len(data) // 1024:6d} KB  {rel}", flush=True)
    if args.list or not done:
        return
    for p in rewrite_refs(done):
        print("references:", os.path.relpath(p, ROOT))
    if not args.keep:
        for p in done:
            os.remove(p)


if __name__ == "__main__":
    sys.exit(main())
