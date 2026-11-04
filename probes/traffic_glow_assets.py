"""Generate distant traffic glow assets into an output directory; --production writes the mod's material names."""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import cdae


def build(out, production=False):
    out.mkdir(parents=True, exist_ok=True)
    p = np.array([[-1, 0, -1], [1, 0, -1], [1, 0, 1], [-1, 0, 1]], np.float64)
    uv = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float64)
    name = "bne_distant_traffic_glow" if production else "bne_traffic_probe_glow"
    vfs = "/levels/beamng_earth/art/shapes/beamngEarth/traffic" if production else "/temp/beamngEarthTrafficProbe"
    cdae.write(str(out / "traffic.cdae"), p, [[0, 2, 1], [0, 3, 2]], uvs=uv, material=name)
    if not production:
        cdae.write(str(out / "occluder.cdae"), p, [[0, 2, 1], [0, 3, 2]], uvs=uv, material="bne_traffic_probe_occluder")
    axis = (np.arange(64) + 0.5) / 32 - 1
    r2 = axis[:, None] ** 2 + axis[None, :] ** 2
    glow = np.maximum(0, np.exp(-r2 * 7) - np.exp(-7)) / (1 - np.exp(-7))
    rgb = np.repeat(np.uint8(glow[:, :, None] * 255), 3, axis=2)
    Image.fromarray(rgb).save(out / "glow.color.dds", pixel_format="DXT1")
    material = {"name": name, "mapTo": name, "class": "Material",
                "version": 1.5, "Stages": [{"baseColorFactor": [0, 0, 0, 1], "metallicFactor": 0,
                "roughnessFactor": 1, "emissiveFactor": [1, 1, 1], "emissiveIntensityNits": 320 if production else 80,
                "instanceEmissive": True, "instanceOpacity": True, "opacityFactor": 1,
                "opacityMap": f"{vfs}/glow.color.dds"}, {}, {}, {}],
                "translucent": True, "translucentBlendOp": "LerpAlpha", "translucentZWrite": False,
                "castShadows": False, "doubleSided": True}
    occluder = {"name": "bne_traffic_probe_occluder", "mapTo": "bne_traffic_probe_occluder", "class": "Material",
                "version": 1.5, "Stages": [{"baseColorFactor": [0.005, 0.005, 0.005, 1], "metallicFactor": 0,
                "roughnessFactor": 1}, {}, {}, {}], "castShadows": False, "doubleSided": True}
    materials = {name: material}
    if not production:
        materials[occluder["name"]] = occluder
    (out / "traffic.materials.json").write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8")
    print(out)


if __name__ == "__main__":
    production = "--production" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--production"]
    build(Path(args[0]) if args else ROOT / "build" / "traffic-probe", production)
