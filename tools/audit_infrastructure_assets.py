"""Read installed BeamNG archives; export measurements, never proprietary meshes/textures.

python tools/audit_infrastructure_assets.py GAME_DIR OUTPUT_JSON
"""
import hashlib
import json
import posixpath
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

import numpy as np

NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
SHAPES = {
    "pylon": ("levels/automation_test_track.zip", "levels/automation_test_track/art/shapes/objects/pylon.dae"),
    "celltower": ("levels/johnson_valley.zip", "levels/johnson_valley/art/shapes/misc/celltower.dae"),
    "mast": ("levels/italy.zip", "levels/italy/art/shapes/buildings/italy_bld_antenna.dae"),
    "tower": ("levels/west_coast_usa.zip", "levels/west_coast_usa/art/shapes/buildings/antenna.dae"),
    "pole": ("art_shapes.zip", "art/shapes/common/power_lines_procedural/electric_pole_wood_old_01.dae"),
    "wire30": ("art_shapes.zip", "art/shapes/common/power_lines_procedural/wire_30m_01.dae"),
    "wire40": ("art_shapes.zip", "art/shapes/common/power_lines_procedural/wire_40m_01.dae"),
}


def measure(data):
    root = ET.fromstring(data)
    geometries = {}
    for g in root.findall("c:library_geometries/c:geometry", NS):
        mesh = g.find("c:mesh", NS)
        ref = mesh.find("c:vertices/c:input[@semantic='POSITION']", NS).get("source")[1:]
        source = mesh.find(f"c:source[@id='{ref}']/c:float_array", NS)
        if source is not None and source.text:
            geometries[g.get("id")] = np.fromstring(source.text, sep=" ").reshape(-1, 3)
    bounds, anchors, materials = {}, {}, set()

    def visit(node, parent):
        transform = parent.copy()
        for child in node:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "matrix":
                transform = transform @ np.fromstring(child.text, sep=" ").reshape(4, 4)
            elif tag == "translate":
                m = np.eye(4); m[:3, 3] = np.fromstring(child.text, sep=" ")
                transform = transform @ m
            elif tag == "scale":
                m = np.eye(4); m[np.arange(3), np.arange(3)] = np.fromstring(child.text, sep=" ")
                transform = transform @ m
            elif tag == "rotate":
                raise ValueError("Unmeasured COLLADA rotation: extend audit rather than ignore it")
        name = node.get("name", node.get("id", ""))
        if name.startswith("anchorPoint"):
            anchors[name] = transform[:3, 3].tolist()
        for inst in node.findall("c:instance_geometry", NS):
            if inst.get("url")[1:] not in geometries:
                continue
            v = geometries[inst.get("url")[1:]]
            v = v @ transform[:3, :3].T + transform[:3, 3]
            bounds[name] = {"min": v.min(0).tolist(), "max": v.max(0).tolist()}
            for mat in inst.findall(".//c:instance_material", NS):
                material = root.find(f"c:library_materials/c:material[@id='{mat.get('target')[1:]}']", NS)
                materials.add(material.get("name", material.get("id")))
        for child in node.findall("c:node", NS):
            visit(child, transform)
    for node in root.findall("c:library_visual_scenes/c:visual_scene/c:node", NS):
        visit(node, np.eye(4))
    return {"sha256": hashlib.sha256(data).hexdigest(), "up_axis": root.findtext("c:asset/c:up_axis", namespaces=NS),
            "unit": root.find("c:asset/c:unit", NS).attrib, "nodes": bounds,
            "anchors": anchors, "materials": sorted(materials)}


def audit(game):
    content = Path(game) / "content"
    archives = {name: zipfile.ZipFile(content / name) for name in {p[0] for p in SHAPES.values()}}
    definitions = {}
    material_defs = {}
    for z in archives.values():
        for name in z.namelist():
            if not name.endswith(".materials.json"):
                continue
            try:
                data = json.loads(z.read(name))
            except (ValueError, UnicodeError):
                continue
            for key, material in data.items():
                if isinstance(material, dict):
                    definitions.setdefault(material.get("mapTo", key), []).append("/" + name)
                    material_defs.setdefault(material.get("mapTo", key), []).append((name, material))
    # Resolve the selected materials' texture references, including installed .link indirection.
    extras = [zipfile.ZipFile(p) for p in (content / "assets").rglob("*.zip")]
    vfs = {name: z for z in list(archives.values()) + extras for name in z.namelist()}

    def texture_refs(value):
        if isinstance(value, dict):
            for v in value.values(): yield from texture_refs(v)
        elif isinstance(value, list):
            for v in value: yield from texture_refs(v)
        elif isinstance(value, str) and value.lower().endswith((".png", ".dds", ".jpg", ".bmp")):
            yield value

    def resolve(path):
        name = path.lstrip("/")
        compiled = posixpath.splitext(name)[0] + ".dds"
        if name not in vfs and name + ".link" not in vfs and compiled in vfs:
            return "installed DDS"
        if name in vfs:
            return "installed"
        if name + ".link" in vfs:
            target = vfs[name + ".link"].read(name + ".link").decode("utf-8").strip().lstrip("/")
            return "installed link" if target in vfs else "link target missing: " + target
        return "missing"
    result = {}
    for key, (archive, member) in SHAPES.items():
        r = result[key] = measure(archives[archive].read(member))
        r.update(archive=archive, path="/" + member, resource_type="COLLADA shape")
        r["material_files"] = {name: definitions.get(name, []) for name in r["materials"]}
        refs = {}
        for name in r["materials"]:
            for matpath, material in material_defs.get(name, []):
                for path in texture_refs(material):
                    absolute = path if path.startswith("/") else "/" + posixpath.normpath(posixpath.join(posixpath.dirname(matpath), path))
                    refs[absolute] = resolve(absolute)
        r["texture_references"] = refs
    for z in list(archives.values()) + extras:
        z.close()
    return result


if __name__ == "__main__":
    result = audit(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for name, r in result.items():
        print(name, r["path"], "materials:", r["material_files"])
        print("nodes:", r["nodes"])
        print("anchors:", r["anchors"])
        print("unresolved textures:", {p: status for p, status in r["texture_references"].items() if not status.startswith("installed")})
