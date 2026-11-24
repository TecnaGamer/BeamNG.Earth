"""Hot-swappable, data-only storefronts from the player's BeamNG user folder."""
from contextvars import ContextVar
from functools import lru_cache, wraps
import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
import threading
import zipfile
import zlib

_pinned = ContextVar("storefront_pack_pinned", default=False)


active = ContextVar("storefront_pack", default=None)
_lock = threading.RLock()
_seen = {}
_ASSET = re.compile(r"[a-z0-9_-]{1,100}\.(glb|png)")
_MAX_SIZE = 256 << 20
_RESERVED = {"CON", "PRN", "NUL", "AUX"} | {f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(10)}


def archive_path(root):
    root = Path(root)
    if root.name.lower() == "beamngearth" and root.parent.name.lower() == "temp":
        return root.parent.parent / "settings" / "beamngEarth" / "storefront-pack" / "pack.zip"


class Pack:
    def __init__(self, digest, folder, data):
        from storeidentity import Identity, STYLES, normalized_name, source_key

        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("chains", {}), dict):
            raise ValueError("Unsupported storefront pack version")
        self.digest = digest
        self.folder = folder
        self.names = {}
        self.identities = {}
        self.assets = {}
        self.sites = []
        self.replaces = set()
        for source, row in data.get("chains", {}).items():
            if not isinstance(row, dict) or not isinstance(source, str) or not source.strip():
                raise ValueError("Invalid storefront source")
            key = "pack_" + source_key(source)
            label, style = row["label"], row["style"]
            if style not in STYLES or not isinstance(label, str) or not 1 <= len(label) <= 80:
                raise ValueError("Invalid chain label or style")
            colors = row["paint"], row["ink"]
            if not all(re.fullmatch(r"#[0-9a-fA-F]{6}", color) for color in colors):
                raise ValueError("Invalid storefront colors")
            for field, suffix in (("model", ".glb"), ("sign", ".png")):
                if field in row:
                    self.asset(row[field], suffix)
            if "model" in row and not all(math.isfinite(v) and v > 0 for v in (row["width"], row["depth"])):
                raise ValueError("Invalid template footprint")
            aliases = row.get("aliases", [])
            if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
                raise ValueError("Invalid storefront aliases")
            for alias in [source, *aliases]:
                name = normalized_name(alias)
                if name in self.names and self.names[name] != source:
                    raise ValueError("Ambiguous chain alias")
                self.names[name] = source
            self.identities[source.casefold()] = Identity(key, label, style, *colors)
            self.assets[key] = row
        for index, row in enumerate(data.get("sites", [])):
            if not isinstance(row, dict):
                raise ValueError("Invalid placed model")
            filename = row["file"]
            self.asset(filename, ".glb")
            if not (-90 <= row["lat"] <= 90 and -180 <= row["lon"] <= 180):
                raise ValueError("Invalid site coordinates")
            site = dict(row, file=f"{folder.name}/{filename}", id=f"local_pack_{digest}_{index}")
            self.sites.append(site)
            replacements = row.get("replaces", [])
            if not isinstance(replacements, list) or not all(isinstance(f, str) for f in replacements):
                raise ValueError("Invalid replacement filenames")
            self.replaces.update(replacements)

    def asset(self, name, suffix):
        if not isinstance(name, str) or not _ASSET.fullmatch(name) or not name.endswith(suffix) or not (self.folder / name).is_file():
            raise ValueError(f"Missing or invalid pack asset {name!r}")
        return self.folder / name


def validate_asset(path):
    if path.suffix == ".png":
        from PIL import Image
        try:
            image = Image.open(path)
        except Image.DecompressionBombError as error:
            raise ValueError("Oversized storefront sign") from error
        with image as img:
            if img.format != "PNG" or img.width * img.height > 4096 * 4096:
                raise ValueError("Invalid storefront sign")
            img.verify()
        return
    import threedmr
    try:
        blob = path.read_bytes()
        if struct.unpack_from("<4sII", blob) != (b"glTF", 2, len(blob)):
            raise ValueError("Invalid GLB header")
        doc, _ = threedmr._parse_glb(blob)
        if not doc["scenes"][doc.get("scene", 0)]["nodes"]:
            raise ValueError("Missing GLB scene")
        if not len(threedmr.gltf_mesh(blob)[2]):
            raise ValueError("Empty GLB mesh")
    except (threedmr.UnsupportedModelError, IndexError, AttributeError, struct.error, OverflowError) as error:
        raise ValueError(f"Unreadable model {path.name}") from error


def _load(path, root):
    with path.open("rb") as stream, zipfile.ZipFile(stream) as archive:
        if os.fstat(stream.fileno()).st_size > _MAX_SIZE:
            raise ValueError("Pack exceeds 256 MiB")
        stream.seek(0)
        hasher = hashlib.sha256()
        for block in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(block)
        digest = hasher.hexdigest()
        cache = root / "threedmr_cache"
        dest = cache / ("pack_" + digest)
        files = archive.infolist()
        if (len(files) > 6000 or sum(f.file_size for f in files) > _MAX_SIZE
                or len({f.filename for f in files}) != len(files)
                or any(Path(f.filename).stem.upper() in _RESERVED for f in files)
                or any(f.filename != "pack.json" and not _ASSET.fullmatch(f.filename) for f in files)):
            raise ValueError("Invalid storefront pack contents")
        if archive.getinfo("pack.json").file_size > 4 << 20:
            raise ValueError("Oversized storefront manifest")
        data = json.loads(archive.read("pack.json"))
        if not dest.is_dir():
            cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=cache) as tmp:
                stage = Path(tmp) / "assets"
                stage.mkdir()
                for f in files:
                    if f.filename != "pack.json":
                        with archive.open(f) as src, (stage / f.filename).open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        validate_asset(stage / f.filename)
                Pack(digest, stage, data)
                try:
                    stage.rename(dest)
                except OSError:
                    if not dest.is_dir():
                        raise
    return Pack(digest, dest, data)


def snapshot(root):
    if _pinned.get():
        return active.get()
    if not root or os.environ.get("BNE_STOREFRONTS") != "1":
        return None
    root = Path(root)
    path = archive_path(root)
    if path is None:
        return None
    with _lock:
        previous, pack = _seen.get(root, (None, None))
        try:
            st = path.stat()
            stamp = st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size
        except FileNotFoundError:
            _seen.pop(root, None)
            return None
        except OSError:
            return pack
        if previous != stamp:
            try:
                pack = _load(path, root)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, SyntaxError, OverflowError, EOFError, zlib.error, zipfile.BadZipFile) as error:
                logging.getLogger("earthd.storefronts").warning("Could not load %s: %s", path, error)
            _seen[root] = stamp, pack
        return pack


def tile_scope(parameter, *, session_dir=False):
    """Pin one revision through a tile's cache lookup, model selection and cache write."""
    def decorate(fn):
        index = list(inspect.signature(fn).parameters).index(parameter)

        @wraps(fn)
        def generate(*args, **kwargs):
            if _pinned.get() or os.environ.get("BNE_STOREFRONTS") != "1":
                return fn(*args, **kwargs)
            root = args[index] if len(args) > index else kwargs.get(parameter)
            if root and session_dir:
                root = Path(root).parent
            selection = active.set(snapshot(root))
            pinned = _pinned.set(True)
            try:
                return fn(*args, **kwargs)
            finally:
                _pinned.reset(pinned)
                active.reset(selection)
        return generate
    return decorate


@lru_cache(maxsize=128)
def _sign(path):
    from PIL import Image
    with Image.open(path) as img:
        if img.width * img.height > 4096 * 4096:
            raise ValueError("Oversized storefront sign")
        img.thumbnail((1200, 128), Image.Resampling.LANCZOS)
        return img.convert("RGB")


def sign(identity, aspect):
    pack = active.get()
    row = pack.assets.get(identity.key) if pack else None
    if row and "sign" in row:
        from PIL import Image, ImageOps
        size = (max(64, min(1200, round(128 * aspect))), 128)
        return ImageOps.pad(_sign(pack.folder / row["sign"]), size, method=Image.Resampling.LANCZOS, color=identity.paint)
