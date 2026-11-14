"""The game's packed terrain textures of every ground material, made by the server before the game needs them
(2026-11-14: a 12 s and a 5 s freeze at 128 mph, the game packing bngEarthGroundRock's textures on its main thread).

The engine packs each TerrainMaterial's detail and macro maps into BC7 texture arrays the first time a block lists that
material, ~3 s a texture and 4 a material (its log: "Texture added for packing, because cache not found",
"TextureCooker| ... imported in 2.9 secs"), and keeps them in <user>/temp/art/terrainmaterialcache/<key>.dds
(terraincache.py has the key). A wiped cache or a new player meets every material once, mid-drive. Here, at the
server's start, one paced background thread writes the missing or outdated ones.

What the game writes (its own files read 2026-11-14: 36 keys of the level's materials found by these rules, the four
rock ones decoded with Pillow against their sources):
- per slot (detail, macro, base) two textures, R G B A from four sources:
  colour  baseColor, baseColor, baseColor, ao       (RGB = the colour map's RGB, A = the ao map)
  pack    roughness, height, normal, normal         (R = roughness, G = height, B = the normal's R, A = its G: no swizzle,
                                                     the normal's z dropped)
  the key xxh64 of the four paths concatenated (terraincache.xxh64);
- BC7 with the full mip chain down to 1x1 (11 levels at 1024), box-filtered on the stored values (no linear light, the
  normal not renormalised: a plain 2x2 mean matched the game's mips best);
- DXGI_BC7_UNORM (98) for the detail and macro textures, colour too; the base slot's colour is BC7_UNORM_SRGB (99)
  (terraincache.base_entry's photos), its pack 98;
- at the TerrainMaterialTextureSet's slot size (detailTexSize / macroTexSize 1024, the base slot the ground photo's
  size): every source of ours is that size already; a pack whose source is not is left to the game (its resize is
  not known);
- the header byte for byte bc7.header's.
The game uses a cache file that exists, is newer than its sources and has their size ("cache outdated", "size
differs from cache" otherwise). A source inside a game archive counts here with the archive's time (or its entry's,
the later), a level file with the installed mod's copy's (install.py keeps unchanged files' times), a terraintex PNG
with its own. A source dated in the future gets the cache file dated just after it (the game would repack otherwise).

The materials (wanted()): the level's main.materials.json, which defines every ground material of ours in any level
(terrain.lua ensureMaterials), each with the base slot at every ground photo size (terrain.lua baseMapFor), plus the
looks terrain.lua can put on top: M.TILE_FALLBACK's, and M.LOOKS' for a material the JSON does not define (made at
runtime). tests/test_terrainprefill.py evaluates terrain.lua's tables under luajit and fails when one of them is not
covered here.

The encode is the Rust kernel's (bc7.encode, mode 6; the game's own files are no closer to the sources: over the 31
detail / macro files of the user's cache, 5.66 mean error per channel against ours 5.49), in bands of at most 2,047
blocks, which the kernel encodes on one thread, the thread paced (pacer.py, --background-duty) and held while a
teleport's first block builds. All 41 textures (the base slot at 4096 included) cost ~5.6 CPU-s on the PC. Off with BNGE_TERRAIN_PREFILL=0 or terraincache.NO_PACK (the tests' BNGE_NO_BASE_PACK=1); nothing without
the user folder or the game's folder.
"""
import io
import json
import logging
import os
import re
import sys
import time
import zipfile

import numpy as np

import bc7
import pacer
import staging
import terraincache

log = logging.getLogger("earthd.terrainprefill")

ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD_SRC = os.path.join(ROOT, "mod", "beamng_earth")     # (the build's copy; the installed one under <user>/mods)
MOD_NAME = "beamng_earth"
OUR_CODE = "lua/ge/extensions/beamngEarth/"     # (a mod holding this is a copy of ours: not an overlay)
LEVEL_TERRAINS = "/levels/beamng_earth/art/terrains/"
MATERIALS_VFS = LEVEL_TERRAINS + "main.materials.json"
TERRAIN_LUA = os.path.join("lua", "ge", "extensions", "beamngEarth", "terrain.lua")
STOCK_TERRAIN = "/assets/materials/terrain/"            # terrain.lua look()'s T
FLAT_H = LEVEL_TERRAINS + "bngEarth_flat_h_1024.png"    # terrain.lua look()'s height maps
BASE_SIZES = (1024, 2048, 4096)                         # earthd.BASE_SIZES / terrain.lua M.BASE_SIZES
BASE_DEFAULT = 2048                                     # terrain.lua M.BASE_DEFAULT (plain file names)
SLOTS = ("Detail", "Macro", "Base")
ENABLED = os.environ.get("BNGE_TERRAIN_PREFILL", "1") != "0"


class Pack:
    """One packed texture the game may need: its four sources (VFS paths, R G B A), slot size and format."""
    __slots__ = ("key", "sources", "px", "dxgi", "kind", "what")

    def __init__(self, kind, sources, px, srgb, what):
        self.kind, self.sources, self.px, self.what = kind, tuple(sources), px, what
        self.dxgi = bc7.DXGI_BC7_UNORM_SRGB if srgb else bc7.DXGI_BC7_UNORM
        self.key = key(self.sources)

    def __repr__(self):
        return f"Pack({self.kind} {self.key} {self.px}px {self.what})"


def key(sources):
    """The cache file name (no .dds) of a packed texture of these four VFS paths (terraincache.xxh64)."""
    return f"{terraincache.xxh64(''.join(sources).encode()):016x}"


def slot_packs(fields, slot, px, what=""):
    """The colour and pack textures of one slot ('Detail', 'Macro', 'Base') of a material's fields, or [] when a
    texture field is missing (the game makes a default texture then, not a cache file)."""
    b, ao, r, h, nm = (fields.get(f"{f}{slot}Tex") for f in ("baseColor", "ao", "roughness", "height", "normal"))
    out = []
    if b and ao:
        out.append(Pack("color", (b, b, b, ao), px, slot == "Base", f"{what} {slot.lower()} colour"))
    if r and h and nm:
        out.append(Pack("pack", (r, h, nm, nm), px, False, f"{what} {slot.lower()} pack"))
    return out


def base_map_for(path, n):
    """terrain.lua M.baseMapFor: a level flat base map's file at ground photo size n."""
    m = re.match(r"^(.*/bngEarth_base_[A-Za-z]+)(?:_\d+)?\.png$", path or "")
    if not m:
        return path
    return f"{m.group(1)}.png" if n == BASE_DEFAULT else f"{m.group(1)}_{n}.png"


def look_fields(detail, macro):
    """terrain.lua look()'s texture fields of a stock detail and macro texture stem."""
    f = {"heightDetailTex": FLAT_H, "heightMacroTex": FLAT_H}
    for k, suf in (("baseColor", "_b"), ("normal", "_nm"), ("roughness", "_r"), ("ao", "_ao")):
        f[k + "DetailTex"] = STOCK_TERRAIN + detail + suf + ".png"
        f[k + "MacroTex"] = STOCK_TERRAIN + macro + suf + ".png"
    return f


_LOOK_LINE = re.compile(r"^\s*(\w+)\s*=\s*(?:look|golfLook)\(\s*'([^']+)'\s*,\s*[^,]+,\s*'([^']+)'", re.M)


def lua_looks(text):
    """{table: {material: fields}} of terrain.lua's M.LOOKS and M.TILE_FALLBACK entries made by look() / golfLook()
    (a tileLook() entry is not read: such a material must be in the level's JSON, which the test checks)."""
    out = {}
    for table in ("LOOKS", "TILE_FALLBACK"):
        m = re.search(r"^M\.%s\s*=\s*\{(.*?)^\}" % table, text, re.M | re.S)
        out[table] = {} if not m else {g[0]: look_fields(g[1], g[2]) for g in _LOOK_LINE.findall(m.group(1))}
    return out


def level_slot_px(materials):
    """{slot: px} of the level's TerrainMaterialTextureSet (detail / macro; the base slot follows the photo size)."""
    px = {"Detail": 1024, "Macro": 1024}
    for v in materials.values():
        if isinstance(v, dict) and v.get("class") == "TerrainMaterialTextureSet":
            px["Detail"] = int((v.get("detailTexSize") or [1024])[0])
            px["Macro"] = int((v.get("macroTexSize") or [1024])[0])
    return px


def wanted(materials, lua_text=None, base_sizes=BASE_SIZES):
    """{key: Pack} of every packed texture our ground materials can list: materials = the level's
    main.materials.json (dict), lua_text = terrain.lua's source (its fallback and runtime-made looks), the base slot
    at each of base_sizes."""
    px = level_slot_px(materials)
    out = {}

    def add(packs):
        for p in packs:
            out.setdefault(p.key, p)
    level = {n: v for n, v in materials.items() if isinstance(v, dict) and v.get("class") == "TerrainMaterial"}
    for name, f in sorted(level.items()):
        for slot in ("Detail", "Macro"):
            add(slot_packs(f, slot, px[slot], name))
        for n in base_sizes:
            add(slot_packs({k: base_map_for(v, n) if k.endswith("BaseTex") else v for k, v in f.items()}, "Base", n,
                           f"{name} {n}"))
    looks = lua_looks(lua_text) if lua_text else {}
    for name, f in sorted(looks.get("TILE_FALLBACK", {}).items()):
        for slot in ("Detail", "Macro"):
            add(slot_packs(f, slot, px[slot], name + " fallback"))
    for name, f in sorted(looks.get("LOOKS", {}).items()):
        if name not in level:
            for slot in ("Detail", "Macro"):
                add(slot_packs(f, slot, px[slot], name + " runtime"))
    return out


# --- the sources -------------------------------------------------------------------------------------------------
class Sources:
    """VFS paths -> (bytes, mtime) from the game's archives (content/assets/materials/<x>.zip), the installed mod
    (<user>/mods/unpacked/beamng_earth; the build's copy when not there) and the user folder (/temp/...)."""

    def __init__(self, user_dir, game_dir, mod_src=MOD_SRC):
        self.user_dir, self.game_dir, self.mod_src = user_dir, game_dir, mod_src
        self._zips = {}
        self._overlay = None

    # Overlays (Codex review 2026-11-14): the game's VFS lets the user folder and mounted mods replace a stock or level
    # file, and the cache key is the path alone, so a pack made here from the stock pixels would be taken by the game
    # for the replacement's (newer than both). A source some other mod or a loose user-folder file provides is left to
    # the game: the pack is not written. Every mod zip and unpacked mod counts (enabled or not: a disabled one only
    # costs its packs' prefill), but our own.
    OVERLAY_PREFIXES = ("assets/materials/", "levels/")

    def _overlay_index(self):
        if self._overlay is None:
            found = set()
            mods = os.path.join(self.user_dir or "", "mods")
            for dirpath, dirnames, filenames in os.walk(mods):
                rel_dir = os.path.relpath(dirpath, mods)
                if rel_dir.split(os.sep)[:2] == ["unpacked", MOD_NAME]:
                    dirnames[:] = []
                    continue
                if rel_dir.split(os.sep)[0] == "unpacked" and len(rel_dir.split(os.sep)) == 2:
                    root = dirpath          # an unpacked mod: its files are VFS paths relative to it
                    dirnames[:] = []
                    if os.path.isdir(os.path.join(root, *OUR_CODE.split("/"))):
                        continue            # (a copy of BeamNG.Earth under another name)
                    for sub, _, files in os.walk(root):
                        for name in files:
                            vfs = os.path.relpath(os.path.join(sub, name), root).replace(os.sep, "/").lower()
                            if vfs.startswith(self.OVERLAY_PREFIXES):
                                found.add(vfs)
                    continue
                for name in filenames:
                    if not name.lower().endswith(".zip") or name.lower() == MOD_NAME + ".zip":
                        continue
                    try:
                        with zipfile.ZipFile(os.path.join(dirpath, name)) as z:
                            names = [n.lower().lstrip("/") for n in z.namelist()]
                            if any(n.startswith(OUR_CODE.lower()) for n in names):
                                continue    # (a copy of BeamNG.Earth: a BeamMP server's download of it, a renamed zip)
                            found.update(n for n in names if n.startswith(self.OVERLAY_PREFIXES))
                    except (OSError, zipfile.BadZipFile):
                        pass
            self._overlay = found
        return self._overlay

    def overridden(self, vfs):
        """Does a mod (not ours) or a loose file in the user folder replace this source in the game's VFS?"""
        rel = vfs.lstrip("/")
        if rel.lower() in self._overlay_index():
            return True
        if vfs.startswith(("/assets/", "/levels/")):  # (a loose user-folder file over the stock or the level's own; the
            # /temp/ squares are the server's own files there: the source itself, not an overlay)
            return os.path.isfile(os.path.join(self.user_dir or "", rel.replace("/", os.sep)))
        return False

    def close(self):
        for z, _, _ in self._zips.values():
            if z is not None:
                z.close()
        self._zips = {}

    def _zip(self, sub):
        if sub not in self._zips:
            path = os.path.join(self.game_dir or "", "content", "assets", "materials", sub + ".zip")
            try:
                z = zipfile.ZipFile(path)
                self._zips[sub] = (z, {n.lower(): n for n in z.namelist()}, os.path.getmtime(path))
            except (OSError, zipfile.BadZipFile) as e:
                if self.game_dir:
                    log.debug("terrain prefill: %s: %s", path, e)
                self._zips[sub] = (None, {}, 0.0)
        return self._zips[sub]

    def _file(self, vfs):
        rel = vfs.lstrip("/").replace("/", os.sep)
        if vfs.startswith("/levels/"):
            for root in (os.path.join(self.user_dir, "mods", "unpacked", MOD_NAME), self.mod_src):
                p = os.path.join(root, rel)
                if os.path.isfile(p):
                    return p
            return None
        p = os.path.join(self.user_dir, rel)
        return p if os.path.isfile(p) else None

    def mtime(self, vfs):
        """The source's time as the game would compare it, or None (missing)."""
        if vfs.startswith("/assets/materials/"):
            z, names, t = self._zip(vfs.split("/")[3])
            real = names.get(vfs[1:].lower())
            if z is None or real is None:
                return None
            return max(t, time.mktime(z.getinfo(real).date_time + (0, 0, -1)))
        p = self._file(vfs)
        return os.path.getmtime(p) if p else None

    def read(self, vfs):
        if vfs.startswith("/assets/materials/"):
            z, names, _ = self._zip(vfs.split("/")[3])
            real = names.get(vfs[1:].lower())
            return z.read(real) if z is not None and real is not None else None
        p = self._file(vfs)
        if not p:
            return None
        with open(p, "rb") as f:
            return f.read()


def channels(data, px):
    """(px, px, 3) uint8 RGB of an image file's bytes (a grey map's R = its value), or None: not that size, or not
    8-bit (L, LA, RGB, RGBA, P; the game's conversion of anything else is not known)."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        if im.size != (px, px) or im.mode not in ("L", "LA", "RGB", "RGBA", "P"):
            return None
        return np.asarray(im.convert("RGB"), np.uint8)
    except Exception:
        return None


def pack_image(kind, imgs):
    """The RGBA uint8 image of a pack from its four sources' RGB arrays (R G B A order, Pack.sources)."""
    if kind == "color":
        return np.ascontiguousarray(np.concatenate([imgs[0], imgs[3][..., :1]], 2))
    return np.ascontiguousarray(np.stack([imgs[0][..., 0], imgs[1][..., 0], imgs[2][..., 0], imgs[3][..., 1]], 2))


def box_mips(img):
    """The mip chain of a square power-of-two RGBA image down to 1x1: 2x2 means of the stored values, rounded."""
    chain = [np.asarray(img, np.uint8)]
    while chain[-1].shape[0] > 1:
        a = chain[-1].astype(np.uint16)
        chain.append(((a[0::2, 0::2] + a[1::2, 0::2] + a[0::2, 1::2] + a[1::2, 1::2] + 2) >> 2).astype(np.uint8))
        pacer.pace()
    return chain


def encode_paced(img):
    """bc7.encode of an image in bands of whole block rows of < 2,048 blocks each (the kernel's one-thread size:
    par.rs threads_for), pacing between bands: the same bytes as one call (blocks are row-major and independent)."""
    h, w = img.shape[:2]
    rows = 4 * max(1, 2047 // max(1, (w + 3) // 4))
    if h <= rows:
        return bc7.encode(img)
    parts = []
    for y in range(0, h, rows):
        parts.append(bc7.encode(img[y:y + rows]))
        pacer.pace()
    return b"".join(parts)


def dds_bytes(p, rgba):
    chain = box_mips(rgba)
    return bc7.header(p.px, p.px, len(chain), p.dxgi) + b"".join(encode_paced(m) for m in chain)


def expected_size(px):
    n, total = px, 148
    while True:
        blocks = max(1, (n + 3) // 4)
        total += blocks * blocks * 16
        if n == 1:
            return total
        n //= 2


def fresh(path, px, newest):
    """True when the cache file at path is one the game uses as it is: there, newer than its sources (newest: their
    latest time), px x px with its whole mip chain."""
    try:
        st = os.stat(path)
        if st.st_mtime < newest or st.st_size != expected_size(px):
            return False
        with open(path, "rb") as f:
            head = f.read(148)
        return head[:4] == b"DDS " and int.from_bytes(head[12:16], "little") == px \
            and int.from_bytes(head[16:20], "little") == px
    except OSError:
        return False


def write(path, data, newest):
    """Atomically into the game's cache (staging), dated after its newest source when that is in the future."""
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(data)
    if newest >= time.time():
        os.utime(tmp, (newest + 1.0, newest + 1.0))
    staging.replace(tmp, path)


def prefill(user_dir, game_dir, materials=None, lua_text=None, mod_src=MOD_SRC, duty=None, base_sizes=BASE_SIZES):
    """Write every missing or outdated packed ground texture (wanted()) into user_dir's terrain cache. -> {"written":
    [keys], "fresh": n, "skipped": [(key, why)]}; never raises. materials / lua_text: the level JSON (dict) and
    terrain.lua's text (default: mod_src's)."""
    out = {"written": [], "fresh": 0, "skipped": []}
    if not (ENABLED and not terraincache.NO_PACK and user_dir and game_dir):
        return out
    t0 = time.monotonic()
    pacer.enable(pacer.DUTY if duty is None else duty)
    src = Sources(user_dir, game_dir, mod_src)
    try:
        if materials is None:
            materials = json.loads(src.read(MATERIALS_VFS) or b"{}")
        if lua_text is None:
            try:
                with open(os.path.join(mod_src, TERRAIN_LUA), encoding="utf-8") as f:
                    lua_text = f.read()
            except OSError:
                lua_text = ""
        d = terraincache.cache_dir(user_dir)
        for k, p in sorted(wanted(materials, lua_text, base_sizes).items()):
            pacer.hold()
            if any(src.overridden(v) for v in set(p.sources)):
                out["skipped"].append((k, "source replaced by a mod or the user folder"))
                continue
            times = [src.mtime(v) for v in set(p.sources)]
            if any(t is None for t in times):
                out["skipped"].append((k, "source missing"))
                continue
            path = os.path.join(d, k + ".dds")
            if fresh(path, p.px, max(times)):
                out["fresh"] += 1
                continue
            imgs = {}
            for v in set(p.sources):
                imgs[v] = channels(src.read(v), p.px)
                pacer.pace()
            if any(imgs[v] is None for v in p.sources):
                out["skipped"].append((k, "source not %d px 8-bit" % p.px))
                continue
            data = dds_bytes(p, pack_image(p.kind, [imgs[v] for v in p.sources]))
            os.makedirs(d, exist_ok=True)
            write(path, data, max(times))
            out["written"].append(k)
            pacer.idle()
    except Exception as e:                  # (the game packs what is missing itself: never the server's end)
        log.warning("terrain prefill: %s", e, exc_info=True)
    finally:
        src.close()
    if out["written"] or out["skipped"]:
        log.info("terrain prefill: %d packed ground textures written, %d already there, %d left to the game%s "
                 "(%.1f s)", len(out["written"]), out["fresh"], len(out["skipped"]),
                 (": " + ", ".join(f"{k} {w}" for k, w in out["skipped"][:4])) if out["skipped"] else "",
                 time.monotonic() - t0)
    else:
        log.info("terrain prefill: all %d packed ground textures already there", out["fresh"])
    return out
