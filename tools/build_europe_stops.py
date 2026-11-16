"""Build the European STOP prop kit from local, attributed sign drawings.

Development-only dependencies: numpy, Pillow, CairoSVG. No network and no
installed system fonts are used. The game uses the generated DAE and PNG files.
"""

from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import sys
import uuid
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

from build_rail_crossing_props import Mesh, write_dae

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import euguardrails

ART = ROOT / "tools/sign_art/stop"
VFS = "/levels/beamng_earth/art/shapes/beamngEarth/europe_stops"
OUT = ROOT / "mod/beamng_earth" / VFS.lstrip("/")
STEEL = "bne_eur_steel"
RIM = "bne_eu_stop_rim"
CENTRE = 2.10
TEX_SIZE = 512


def texture(path, face):
    import cairosvg

    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != face["sha256"]:
        raise ValueError(f"STOP source checksum mismatch: {path.name}")
    root = ET.fromstring(data)
    # Do not resolve external images, fonts, URLs or live text at build time.
    for e in root.iter():
        if e.tag.rsplit("}", 1)[-1] in {"text", "image", "script", "foreignObject"}:
            raise ValueError(f"STOP art must consist of local vector paths: {path.name}")
        for k, v in e.attrib.items():
            if k.rsplit("}", 1)[-1] == "href" and not v.startswith("#"):
                raise ValueError(f"External SVG resource: {path.name}")
    rgba = Image.open(
        BytesIO(cairosvg.svg2png(bytestring=data, output_width=1024, output_height=1024))
    ).convert("RGBA")
    # Crop transparent margins without changing the original letter proportions.
    bbox = rgba.getchannel("A").point(lambda a: 255 if a >= 128 else 0).getbbox()
    if bbox is None:
        raise ValueError(f"Empty STOP drawing: {path.name}")
    rgba = rgba.crop(bbox)
    if abs(rgba.width - rgba.height) > 0.035 * max(rgba.size):
        raise ValueError(f"Unexpected non-square STOP drawing: {path.name}: {rgba.size}")
    # The octagonal mesh clips the unused corners. White padding also prevents
    # a black fringe from the transparent corners during mipmap generation.
    base = Image.new("RGBA", rgba.size, (248, 248, 244, 255))
    base.alpha_composite(rgba)
    size = max(base.size)
    square = Image.new("RGB", (size, size), (248, 248, 244))
    square.paste(base.convert("RGB"), ((size - base.width) // 2, (size - base.height) // 2))
    return square.resize((TEX_SIZE, TEX_SIZE), Image.Resampling.LANCZOS)


def panel(face_material, width, detail=True):
    mesh = Mesh(10 if detail else 5)
    # Front faces -Y, matching furniture.approach_pose and the stock prop.
    cut = math.sqrt(2.0) - 1.0
    shape = [(-cut, -1), (cut, -1), (1, -cut), (1, cut), (cut, 1), (-cut, 1), (-1, cut), (-1, -cut)]
    ring = lambda r, y: [(x * r, y, CENTRE + z * r) for x, z in shape]
    front = ring(width / 2, -0.055)
    edge = ring(width / 2 + 0.006, -0.048)
    back = ring(width / 2 + 0.006, -0.028)
    mesh.face(face_material, front, [((x + 1) * 0.5, (z + 1) * 0.5) for x, z in shape])
    mesh.face(STEEL, back[::-1])
    for a, b in ((front, edge), (edge, back)):
        for i in range(8):
            j = (i + 1) % 8
            mesh.face(RIM, [a[i], b[i], b[j], a[j]])
    mesh.cylinder(STEEL, 0.0, 0.0, -0.035, CENTRE + width * 0.26, 0.032)
    if detail:
        for z in (CENTRE - 0.16, CENTRE + 0.16):
            mesh.box(STEEL, (0.0, -0.015, z), (0.095, 0.022, 0.023))
    return mesh


def height(width):
    return round(CENTRE + width / 2 + 0.006, 3)


def collision(width):
    mesh = Mesh(4)
    mesh.box(
        STEEL, (0.0, 0.0, (CENTRE + width * 0.26) / 2), (0.036, 0.036, (CENTRE + width * 0.26) / 2)
    )
    return mesh


def material(name, **stage):
    return {
        "name": name,
        "mapTo": name,
        "class": "Material",
        "version": 1.5,
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + name)),
        "Stages": [stage, {}, {}, {}],
    }


def main():
    sources = json.loads((ART / "sources.json").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    models, entries = {}, []
    materials = {
        RIM: material(
            RIM, baseColorFactor=[0.70, 0.72, 0.73, 1.0], metallicFactor=0.6, roughnessFactor=0.46
        )
    }
    notice = [
        "# European STOP face artwork",
        "",
        "Original SVG drawings: `tools/sign_art/stop/`. The PNG faces are "
        "rasterized, cropped to the sign, padded with white at the edges "
        "and downsampled. Letter outlines and colours come from the "
        "individual drawings. Each image retains its listed license; "
        "the code and original 3D meshes use the repository license.",
        "",
        "| Face | Drawing and author | License |",
        "| --- | --- | --- |",
    ]
    for code, face in sorted(sources["faces"].items()):
        group = "eu_stop_" + code
        name = "bne_" + group
        width = face["width_m"]
        face_material = name + "_face"
        tex_name = name + ".png"
        texture(ART / (code + ".svg"), face).save(OUT / tex_name, optimize=True)
        materials[face_material] = material(
            face_material,
            baseColorMap=VFS + "/" + tex_name,
            baseColorFactor=[1.0, 1.0, 1.0, 1.0],
            roughnessFactor=0.48,
            metallicFactor=0.0,
            retroreflectivity=0.8,
        )
        write_dae(
            str(OUT / (name + ".dae")),
            group,
            [(40, panel(face_material, width)), (4, panel(face_material, width, False))],
            collision(width),
        )
        entries.append(f"  {group} = {{ '{VFS}/{name}', {height(width)}, 500 }},")
        models[code.upper()] = [group, height(width)]
        license_text = face["license"]
        if face["license_url"]:
            license_text = f'[{license_text}]({face["license_url"]})'
        artist = face["artist"].replace("|", "\\|")
        notice.append(
            f'| {code.upper()} | [{face["title"]}]({face["page"]}) — {artist} | {license_text} |'
        )
    notice += ["", "Shared and fallback faces:", ""]
    for code, (face, reason) in sorted(sources["aliases"].items()):
        models[code] = models[face.upper()]
        notice.append(f"- {code}: {face.upper()}, {reason}.")
    models.pop("VIENNA")
    if set(models) != set(euguardrails.PROFILES):
        raise ValueError(
            f"Uncovered/extra STOP territories: {set(models)^set(euguardrails.PROFILES)}"
        )
    (OUT / "main.materials.json").write_text(
        json.dumps(materials, indent=2) + "\n", encoding="utf-8"
    )
    (OUT / "STOP_ART.NOTICE.md").write_text("\n".join(notice) + "\n", encoding="utf-8")
    (ROOT / "server/data/eu_stop_models.json").write_text(
        json.dumps(models, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lua = ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/europeStopAssets.lua"
    lua.write_text(
        "-- Generated by tools/build_europe_stops.py\nreturn {\n" + "\n".join(entries) + "\n}\n",
        encoding="utf-8",
    )
    print(f"{len(entries)} STOP face models for {len(models)} country/territory codes")


if __name__ == "__main__":
    main()
