#!/usr/bin/env python3
"""Build the documented OSM-key inventory and Taginfo project file without importing server modules.

python tools/build_taginfo.py          # refresh docs/OSM_TAGS.md and docs/taginfo.json
python tools/build_taginfo.py --check  # fail if the checked-in outputs have drifted
"""
import argparse
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "docs/osm-tag-sources.json"
URL = "https://github.com/TecnaGamer/BeamNG.Earth/blob/main/"


def constants(path, names):
    """Read literal tag-key collections, including local tuple concatenations, without executing any code."""
    definitions = {n.targets[0].id: n.value for n in ast.parse(path.read_text(encoding="utf-8")).body
                   if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}

    def value(node, seen):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return [ast.literal_eval(n) for n in node.elts]
        if isinstance(node, ast.Name) and node.id not in seen:
            return value(definitions[node.id], seen | {node.id})
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return value(node.left, seen) + value(node.right, seen)
        raise ValueError(f"{path.name}: unsupported tag-key definition: {ast.dump(node)}")

    return [key for name in names for key in value(definitions[name], {name})]


def outputs():
    config = json.loads(SOURCES.read_text(encoding="utf-8"))
    entries = {}
    for source in config["sources"]:
        path = ROOT / source["path"]
        strings = {n.value for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        keys = source.get("keys", []) + constants(path, source.get("constants", []))
        for key in keys:
            if not isinstance(key, str) or not key or key not in strings:
                raise ValueError(f"{source['path']}: tag key {key!r} is no longer present in the source")
            entry = entries.setdefault(key, {"descriptions": [], "paths": []})
            if source["description"] not in entry["descriptions"]:
                entry["descriptions"].append(source["description"])
            if source["path"] not in entry["paths"]:
                entry["paths"].append(source["path"])
    tags, rows = [], []
    for key, entry in sorted(entries.items()):
        description = " ".join(entry["descriptions"])
        tags.append({"key": key, "description": description,
                     "doc_url": config["project"]["doc_url"]})
        links = ", ".join(f"[{p}]({URL}{p})" for p in entry["paths"])
        rows.append(f"| `{key}` | {description} | {links} |")
    project = {"data_format": 1, "data_url": "https://raw.githubusercontent.com/TecnaGamer/BeamNG.Earth/main/docs/taginfo.json",
               "project": config["project"], "tags": tags}
    markdown = """# OpenStreetMap tags used by BeamNG.Earth

This is a curated inventory of direct OSM inputs and the keys retained by the project's weekly data builders.
It is not a claim that every value of each key is rendered, nor an exhaustive list of the OSM data transformed
by Overture before it reaches the mod. Data availability, source snapshots and rendering support all matter.

The source links identify the actual consumers or shard builders. To refresh this page and the
[Taginfo project file](taginfo.json), run `python tools/build_taginfo.py`; `--check` verifies they are current.
Edit `docs/osm-tag-sources.json` to document another consumer. Named key collections are read from its Python AST,
without importing the server or fetching map data. Explicit keys are checked against their consumer's literals.

## Registration

Once these files are merged, submit the following line to the alphabetically sorted `project_list.txt` in
[taginfo/taginfo-projects](https://github.com/taginfo/taginfo-projects/blob/master/CONTRIBUTING.md):

```text
beamng_earth https://raw.githubusercontent.com/TecnaGamer/BeamNG.Earth/main/docs/taginfo.json
```

Register after the URL is available on the upstream default branch. The JSON uses Taginfo data format 1;
its contact is the project maintainer (TecnaGamer). See the
[Taginfo project format](https://wiki.openstreetmap.org/wiki/Taginfo/Projects).

## Inventory

| OSM key | Use in this project | Source |
|---|---|---|
""" + "\n".join(rows) + "\n"
    return {ROOT / "docs/taginfo.json": json.dumps(project, ensure_ascii=False, indent=2) + "\n",
            ROOT / "docs/OSM_TAGS.md": markdown}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for path, content in outputs().items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(content, encoding="utf-8")
            print(path.relative_to(ROOT))
    if stale:
        parser.exit(1, "Out of date: " + ", ".join(stale) + "; run python tools/build_taginfo.py\n")


if __name__ == "__main__":
    main()
