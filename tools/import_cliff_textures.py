"""Import authored cliff maps into the standard install-time DDS pipeline.

Usage: python tools/import_cliff_textures.py PATH_TO_AUTHORED_MATERIAL_DIRECTORY
The input contains cliff.materials.json and cliff/*.png. Stock game maps are
never imported. DDS normals use their uncompressed authoring PNG, avoiding a
second lossy encode. Runtime scalar/reference maps keep their native size.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
REL = Path("levels/beamng_earth/art/shapes/beamngEarth")
MAX_SIZE = 4096
RETIRED = {"bne_cliff_canyon", "bne_far_cliff_canyon", "bne_far_cliff_strata",
           "bne_cliff_canyon_blend", "bne_far_cliff_canyon_sat", "bne_far_cliff_strata_sat"}
STOCK = {
    "ground_detail.data.png": "detail.data.dds",
    "ground_detail.normal.dds": "normal.dds",
    "ground_detail_ao.data.png": "ao.data.dds",
    "ground_detail_roughness.data.png": "roughness.data.dds",
}
STOCK_ROOT = "/temp/beamngEarth/shapes/cliff_ground_v1/"


def texture_requirements(materials):
    """Derive opt-ins from material users, including maps shared by both kinds."""
    requirements = {}
    for name, material in materials.items():
        flags = {"BNE_CLIFFS"}
        for stage in material.get("Stages", []):
            for value in stage.values():
                if isinstance(value, str) and "/cliff/" in value:
                    requirements.setdefault(value.lstrip("/"), set()).update(flags)
    # Locally copied stock rocks use this neutral albedo in either category;
    # their private materials are generated on the client, outside this JSON.
    rock_base = (REL / "cliff/natural_base.color.dds").as_posix()
    if rock_base in requirements:
        requirements[rock_base].update(("BNE_CLIFFS",))
    return {path: sorted(flags) for path, flags in requirements.items()}


def prepare(path, role):
    """Keep authored detail, with a 4K ceiling and unit-length filtered normals."""
    with Image.open(path) as src:
        im = src.convert("RGB" if role in ("normal", "color") else "L")
    if max(im.size) <= MAX_SIZE:
        return im
    size = tuple(max(1, round(n*MAX_SIZE/max(im.size))) for n in im.size)
    im = im.resize(size, Image.Resampling.LANCZOS)
    if role == "normal":
        n = np.asarray(im, dtype=np.float32)/127.5-1
        n /= np.maximum(np.linalg.norm(n, axis=2, keepdims=True), 1e-8)
        im = Image.fromarray(np.clip(np.rint((n+1)*127.5), 0, 255).astype(np.uint8))
    return im


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    args = ap.parse_args()
    materials = json.loads((args.source/"cliff.materials.json").read_text(encoding="utf-8"))
    materials = {k: v for k, v in materials.items() if k not in RETIRED}
    manifest_path = ROOT/"mod/texture_sources/textures.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["textures"]
    imported = {}
    fingerprints = {}
    for material in materials.values():
        for stage in material.get("Stages", []):
            for key, value in list(stage.items()):
                if isinstance(value, str) and "/cliff_talus_v" in value:
                    # Keep imported material references aligned with the runtime cache.
                    stage[key] = "/temp/beamngEarth/shapes/cliff_talus_v4/" + Path(value).name
                    continue
                if not isinstance(value, str) or "/cliff/" not in value:
                    continue
                name = Path(value).name
                if name in STOCK:
                    stage[key] = STOCK_ROOT+STOCK[name]
                    continue
                if name.startswith(("ground_", "contact_")):
                    raise ValueError(f"stock-derived map cannot be imported: {name}")
                if name not in imported:
                    role = "normal" if ".normal." in name else "color" if ".color." in name else "data"
                    source = args.source/"cliff"/Path(name).with_suffix(".png")
                    im = prepare(source, role)
                    digest = hashlib.sha256(im.tobytes()).hexdigest()
                    fingerprint = (role, im.size, digest)
                    if fingerprint in fingerprints:
                        imported[name] = fingerprints[fingerprint]
                    else:
                        rel = (REL/"cliff"/Path(name).with_suffix(".dds")).as_posix()
                        source_rel = str(Path(rel).with_suffix(".webp")).replace("\\", "/")
                        dst = ROOT/"mod/texture_sources"/source_rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        im.save(dst, "WEBP", lossless=True, exact=True, method=4)
                        entries[rel] = {"source": source_rel,
                                        "format": {"normal":"BC5", "color":"BC7_SRGB", "data":"BC4"}[role],
                                        "role": role}
                        imported[name] = "/"+rel
                        fingerprints[fingerprint] = "/"+rel
                        print(f"{name}: {im.width}x{im.height}, {dst.stat().st_size:,} bytes", flush=True)
                stage[key] = imported[name]
    target = ROOT/"mod/beamng_earth"/REL
    for path, flags in texture_requirements(materials).items():
        entries[path]["requires_any"] = flags
    target.mkdir(parents=True, exist_ok=True)
    (target/"cliff_materials.src.json").write_text(json.dumps(materials, indent=2)+"\n", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, indent=1)+"\n", encoding="utf-8")
    print(f"Imported {len(fingerprints)} unique maps; stock maps excluded.")


if __name__ == "__main__":
    main()
