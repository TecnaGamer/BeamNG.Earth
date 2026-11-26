"""Private cliff detail assets from the player's game; never inputs to packaging.

Call before streaming, with a destination under the game user's temp folder.
The stamp covers the recipe, encoder, source archive members and output bytes.
Missing or damaged stock data disables contact decoration, not the cliffs.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

import modtextures

RECIPE = "cliff-contact-1"
PREFIX = "assets/materials/terrain/rock/t_dirt_rocky/t_dirt_rocky"
COPIES = {
    "_b.color.dds": "source.color.dds",
    "_nm.normal.dds": "normal.dds",
    "_ao.data.dds": "ao.data.dds",
    "_r.data.dds": "roughness.data.dds",
}
MATERIAL = "bne_cliff_contact_strip"
VIRTUAL_ROOT = "/temp/beamngEarth/shapes/cliff_contact_v1/"
OUTPUTS = {*COPIES.values(), "coverage.data.dds", "threshold.data.dds", "contact.materials.json"}


def prepare_enabled(game_dir, user_dir, *, env=None, diagnostic=print):
    """Startup entry point; resolve the game lazily, only for enabled categories."""
    env = os.environ if env is None else env
    enabled = env.get("BNE_CLIFFS", "off").strip().lower() in {"1", "on", "true"}
    if not enabled:
        return None
    modtextures.native()  # Also makes server modules available to standalone installers.
    from cliffsettings import CliffSettings
    if not CliffSettings.from_env(dict(env, BNGE_USER_DIR=str(user_dir))).enabled:
        return None
    import clifftextures
    clifftextures.prepare(user_dir, env, diagnostic)
    game = game_dir() if callable(game_dir) else game_dir
    contact = prepare(game, user_dir, enabled=True, diagnostic=diagnostic)
    prepare_ground(game, user_dir, enabled=True, diagnostic=diagnostic)
    prepare_models(game, user_dir, enabled=True, diagnostic=diagnostic)
    return contact


def _hash(data):
    return hashlib.sha256(data).hexdigest()


MODEL_ROOT = "/temp/beamngEarth/shapes/cliff_talus_v4/"
MODEL_SOURCES = {
    "Utah": ("utah_boulder_1", "utah_boulder_3", "utah_boulder_5",
             "utah_rock_big_2_b", "utah_rock_big_2_d"),
    "johnson_valley": tuple(f"s_rocks_desert_collection_0{i}" for i in range(1, 4)),
}
MODEL_MATERIALS = {"utah_rocks_b", "rocks_desert_ca", "lod_vertcol"}


def model_slots(plain):
    """Locate material strings without touching binary vertex/collision buffers.

    The engine's body is a stream of MessagePack values. Walking values rather
    than searching bytes avoids replacing coincidental strings inside buffers.
    Offsets are one-based for Lua; replacement IDs must retain the string length.
    """
    import cdae
    shape = cdae.decode(plain)
    names = {m["name"] for m in shape["materials"]}
    if not names or not names <= MODEL_MATERIALS or shape["header"].get("compression"):
        raise ValueError("unsupported compiled cliff model materials or compression")
    _, offset = cdae.unpack_one(plain, 8)
    slots = []
    while offset < len(plain):
        value, end = cdae.unpack_one(plain, offset)
        if end <= offset or end > len(plain):
            raise ValueError("invalid compiled cliff model value")
        if isinstance(value, str) and value in names:
            slots.append([end - len(value) + 1, value])
        offset = end
    if {name for _, name in slots} != names:
        raise ValueError("compiled cliff model material bindings missing")
    return slots


def prepare_models(game_dir, user_dir, *, enabled, native=None, diagnostic=print):
    """Extract authored compiled rocks locally before driving; never package them."""
    if not enabled:
        return None
    target = Path(user_dir) / MODEL_ROOT.lstrip("/") / "templates"
    stamp_path = target / "sources.json"
    try:
        if not game_dir:
            raise FileNotFoundError("BeamNG installation was not found")
        ef = native if native is not None else modtextures.native()
        sources, members = {}, {}
        for level, names in MODEL_SOURCES.items():
            with zipfile.ZipFile(Path(game_dir) / "content/levels" / (level + ".zip")) as archive:
                for name in names:
                    member = archive.getinfo(f"levels/{level}/art/shapes/rocks/{name}.cdae")
                    members[name] = [member.CRC, member.file_size]
                    sources[name] = archive.read(member)
        identity = dict(recipe="cliff-models-2", members=members)
        try:
            stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
            expected = {name + ".cdae" for name in sources} | {"models.json"}
            if stamp["identity"] == identity and set(stamp["outputs"]) == expected and all(
                    _hash((target / name).read_bytes()) == digest for name, digest in stamp["outputs"].items()):
                return target
        except (OSError, ValueError, KeyError, TypeError):
            pass
        outputs, manifest = {}, {}
        for name, raw in sources.items():
            plain = bytes(ef.cdae_unpack(raw))
            manifest[name] = dict(bytes=len(plain), slots=model_slots(plain))
            outputs[name + ".cdae"] = plain
        cache_key = _hash(json.dumps(identity, sort_keys=True).encode())[:16]
        outputs["models.json"] = json.dumps(dict(cacheKey=cache_key, models=manifest), sort_keys=True).encode()
        target.mkdir(parents=True, exist_ok=True)
        stamp = dict(identity=identity, outputs={name: _hash(data) for name, data in outputs.items()})
        for name, data in [*outputs.items(), (stamp_path.name, json.dumps(stamp).encode())]:
            with tempfile.NamedTemporaryFile(dir=target, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            try:
                temporary.replace(target / name)
            finally:
                temporary.unlink(missing_ok=True)
        return target
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, modtextures.TexturesError) as error:
        diagnostic(f"Dynamic cliffs: compiled rock models unavailable: {error}")
        return None


def _material(factor):
    root = VIRTUAL_ROOT
    stage = dict(baseColorMap=root + "source.color.dds", baseColorFactor=factor, vertColor=True,
                 normalMap=root + "normal.dds", normalMapStrength=.65,
                 roughnessMap=root + "roughness.data.dds", roughnessFactor=1, metallicFactor=0,
                 ambientOcclusionMap=root + "ao.data.dds", ambientOcclusionFactor=.35,
                 opacityMap=root + "coverage.data.dds", opacityFactor=1, opacityMapUseUV=0,
                 opacityDetailMap=root + "threshold.data.dds", opacityDetailMapUseUV=1,
                 detailOpacityMapStrength=1, detailScale=[1, 1])
    # Opaque cutouts render before vehicle glass, unlike alpha-blended strips.
    return {MATERIAL: dict(name=MATERIAL, mapTo=MATERIAL, **{"class": "Material"}, version=1.5,
                           alphaTest=True, alphaRef=224, translucent=False, doubleSided=True,
                           castShadows=False, Stages=[stage, {}, {}, {}])}


def prepare(game_dir, user_dir, *, enabled, native=None, diagnostic=print):
    """Prepare the opaque contact-strip material and its private textures."""
    return _prepare(game_dir, user_dir, enabled=enabled, native=native, diagnostic=diagnostic, ground=False)


def prepare_ground(game_dir, user_dir, *, enabled, native=None, diagnostic=print):
    """Prepare neutral additive ground maps used by cliff material families."""
    return _prepare(game_dir, user_dir, enabled=enabled, native=native, diagnostic=diagnostic, ground=True)


def _prepare(game_dir, user_dir, *, enabled, native, diagnostic, ground):
    """Return the ready private directory, or None when disabled/unavailable.

    Write the stamp last. Interrupted preparation is repaired on the next call;
    callers must only expose the material after this function succeeds.
    """
    if not enabled:
        return None
    root = "/temp/beamngEarth/shapes/cliff_ground_v1/" if ground else VIRTUAL_ROOT
    recipe = "cliff-ground-1" if ground else RECIPE
    copies = {"_nm.normal.dds": "normal.dds"} if ground else COPIES
    sources = ("_b.png", "_ao.png", "_r.png") if ground else ("_h.png", "_b.png")
    expected = {"normal.dds", "detail.data.dds", "ao.data.dds", "roughness.data.dds"} if ground else OUTPUTS
    target = Path(user_dir) / root.lstrip("/")
    stamp_path = target / "sources.json"
    try:
        if not game_dir:
            raise FileNotFoundError("BeamNG installation was not found")
        archive_path = Path(game_dir) / "content/assets/materials/terrain.zip"
        ef = native if native is not None else modtextures.native()
        if not hasattr(ef, "cliff_ground_maps" if ground else "cliff_contact_color_factor"):
            raise ValueError("native module needs rebuilding for private cliff maps")
        with zipfile.ZipFile(archive_path) as archive:
            members = {suffix: archive.getinfo(PREFIX + suffix) for suffix in (*copies, *sources)}
            identity = dict(recipe=recipe, encoder=ef.TEXDDS_ID,
                            members={s: [i.CRC, i.file_size] for s, i in members.items()})
            try:
                stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
                if stamp["identity"] == identity and set(stamp["outputs"]) == expected and all(
                        _hash((target / name).read_bytes()) == digest for name, digest in stamp["outputs"].items()):
                    return target
            except (OSError, ValueError, KeyError, TypeError):
                pass
            outputs = {name: archive.read(PREFIX + suffix) for suffix, name in copies.items()}
            if any(not data.startswith(b"DDS ") for data in outputs.values()):
                raise ValueError("stock contact maps are not DDS textures")
            if ground:
                detail, ao, roughness = ef.cliff_ground_maps(*(archive.read(PREFIX + s) for s in sources))
                outputs.update({"detail.data.dds": detail, "ao.data.dds": ao, "roughness.data.dds": roughness})
            else:
                coverage, threshold = ef.cliff_contact_maps(archive.read(PREFIX + "_h.png"))
                factor = ef.cliff_contact_color_factor(archive.read(PREFIX + "_b.png"))
                outputs.update({"coverage.data.dds": coverage, "threshold.data.dds": threshold,
                                "contact.materials.json": (json.dumps(_material(factor), indent=2) + "\n").encode()})
        target.mkdir(parents=True, exist_ok=True)
        stamp = dict(identity=identity, outputs={name: _hash(data) for name, data in outputs.items()})
        # Temporary files in the same directory make replacement atomic even
        # when the user's cache is on another drive than the system temp folder.
        for name, data in [*outputs.items(), (stamp_path.name, json.dumps(stamp).encode())]:
            with tempfile.NamedTemporaryFile(dir=target, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            try:
                temporary.replace(target / name)
            finally:
                temporary.unlink(missing_ok=True)
        return target
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, modtextures.TexturesError) as error:
        label = "ground detail" if ground else "contact decoration"
        diagnostic(f"Dynamic cliffs: {label} unavailable: {error}")
        return None
