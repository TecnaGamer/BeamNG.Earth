"""Package frozen official contours for the French house scenery experiment.

No runtime dependency or network access. Inputs: departments-100m.geojson,
basque-communes.geojson, toulouse-communes.geojson in --source-dir.
The bundle retains their geometry and useful identifiers, with provenance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "departments": ("departments-100m.geojson", "https://etalab-datasets.geo.data.gouv.fr/contours-administratifs/2026/geojson/departements-100m.geojson"),
    "basque": ("basque-communes.geojson", "https://geo.api.gouv.fr/epcis/200067106/communes?fields=nom,code,contour&format=geojson&geometry=contour"),
    "toulouse": ("toulouse-communes.geojson", "https://geo.api.gouv.fr/epcis/243100518/communes?fields=nom,code,contour&format=geojson&geometry=contour"),
}


def build(source_dir, target):
    meta = {"retrieved_utc": "2026-11-03", "licence": "ODbL 1.0",
            "attribution": "data.gouv.fr / Etalab, IGN Admin Express; published Contours administratifs dataset",
            "dataset": "https://www.data.gouv.fr/datasets/contours-administratifs", "sources": {}, "bounds": {}}
    members = {}
    for name, (filename, url) in SOURCES.items():
        raw = (Path(source_dir) / filename).read_bytes()
        data = json.loads(raw)
        feats = []
        coords = []
        for f in data["features"]:
            if name == "departments" and len(f["properties"]["code"]) > 2:
                continue
            g = f["geometry"]
            feats.append({"type": "Feature", "properties": {k: f["properties"][k] for k in ("code", "nom")},
                          "geometry": g})
            for p in [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]:
                coords.extend(p[0])
        feats.sort(key=lambda f: f["properties"]["code"])
        members[name + ".geojson"] = {"type": "FeatureCollection", "features": feats}
        meta["sources"][name] = {"url": url, "sha256": hashlib.sha256(raw).hexdigest(), "features": len(feats)}
        meta["bounds"][name] = [min(c[0] for c in coords), min(c[1] for c in coords),
                                max(c[0] for c in coords), max(c[1] for c in coords)]
    members["sources.json"] = meta
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, obj in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    return hashlib.sha256(target.read_bytes()).hexdigest()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-dir", required=True, type=Path)
    p.add_argument("--output", type=Path, default=ROOT / "server/data/fr_house_regions.zip")
    p.add_argument("--update-checksum", action="store_true")
    args = p.parse_args()
    digest = build(args.source_dir, args.output)
    if args.update_checksum:
        src = ROOT / "server/house_regions.py"
        src.write_text(re.sub(r'DATA_SHA256 = "[^"]+"', 'DATA_SHA256 = "' + digest + '"',
                             src.read_text(encoding="utf-8")), encoding="utf-8")
    print(args.output.name, digest, args.output.stat().st_size, "bytes")
