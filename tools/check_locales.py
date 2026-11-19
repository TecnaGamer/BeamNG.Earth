#!/usr/bin/env python3
"""Validate BeamNG.Earth catalogs and report translation coverage."""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "mod/beamng_earth"
CATALOGS = MOD / "ui/ui-vue/mods/beamngEarth/locales"
KEY = r"[a-z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9]+)+"
TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
PARAM = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")
FORMS = set("zero one two few many other".split())
REQUIRED_FORMS = {"en": {"one", "other"}, "pl": {"one", "few", "many", "other"}}


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate key {name!r}")
        result[name] = value
    return result


def read_object(path, issues):
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except (OSError, ValueError) as error:
        issues.append(f"{path.name}: {error}")
        return {}


def signature(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("expected a non-empty string")
    if re.search(r"[{}]", PARAM.sub("", text)):
        raise ValueError("malformed placeholder")
    return set(PARAM.findall(text))


def validate(value, locale):
    if isinstance(value, str):
        return ("text", signature(value))
    if not isinstance(value, dict):
        raise ValueError("expected text or a plural object")
    required = REQUIRED_FORMS.get(locale.split("-")[0].lower(), {"other"})
    if not required <= value.keys() or not value.keys() <= FORMS:
        raise ValueError(f"invalid plural forms, expected at least {', '.join(sorted(required))}")
    params = signature(value["other"])
    if "count" not in params or any(signature(text) != params for text in value.values()):
        raise ValueError("all plural forms must have the same placeholders, including {count}")
    return ("plural", params)


def references(base):
    roots = [MOD / "ui/ui-vue/mods/beamngEarth", MOD / "lua/ge/extensions/beamngEarth"]
    paths = [ROOT / "launcher.py", ROOT / "apppaths.py", *MOD.glob("ui/modules/apps/BeamNGEarth*/app.js")]
    for root in roots:
        paths.extend(path for path in root.glob("*") if path.suffix in (".js", ".vue", ".lua"))
    quoted = re.compile(r"'(" + KEY + r")'|\"(" + KEY + r")\"")
    call = re.compile(r"\b(?:t|notice|translatedNotice)\(['\"](" + KEY + r")['\"]")
    known, explicit = set(), set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        known.update((left or right) for left, right in quoted.findall(text) if (left or right) in base)
        explicit.update(call.findall(text))
    return known, explicit


def host_report(issues):
    root = MOD / "locales/translations"
    english = read_object(root / "en-US/beamngEarth.translations.json", issues)
    polish = read_object(root / "pl_PL/beamngEarth.translations.json", issues)
    metadata = [read_object(path, issues) for path in MOD.glob("ui/modules/apps/BeamNGEarth*/app.json")]
    actions = read_object(MOD / "lua/ge/extensions/core/input/actions/beamngEarth.json", issues)
    expected = {item.get(field) for item in metadata for field in ("name", "description")}
    expected |= {item.get(field) for item in actions.values() if isinstance(item, dict) for field in ("title", "desc")}
    for key in expected | english.keys() | polish.keys():
        if not isinstance(key, str) or not key.startswith("beamngEarth.") or key not in expected:
            issues.append(f"invalid or unused game-owned message: {key}")
        elif not all(isinstance(catalog.get(key), str) and catalog[key].strip() for catalog in (english, polish)):
            issues.append(f"incomplete game-owned message: {key}")
    return {"en-US": len(english), "pl_PL": len(polish)}


def report(directory=CATALOGS, required=()):
    issues = []
    manifest = read_object(directory / "catalogs.json", issues)
    if "en" not in manifest:
        issues.append("catalogs.json: English (en) must be registered")
    seen = {tag.lower() for tag in manifest}
    for tag, meta in manifest.items():
        if not TAG.fullmatch(tag) or not isinstance(meta, dict):
            issues.append(f"invalid locale registration: {tag}")
            continue
        if not isinstance(meta.get("name"), str) or not meta["name"].strip() or meta.get("direction") not in ("ltr", "rtl"):
            issues.append(f"{tag}: name and ltr/rtl direction are required")
        aliases = meta.get("aliases", [])
        if not isinstance(aliases, list):
            issues.append(f"{tag}: aliases must be a list")
            continue
        for alias in aliases:
            if not isinstance(alias, str) or not TAG.fullmatch(alias) or alias.lower() in seen:
                issues.append(f"{tag}: invalid or duplicate alias: {alias!r}")
            else:
                seen.add(alias.lower())
    registered = {tag for tag in manifest if TAG.fullmatch(tag)} | {"en"}
    files = {path.stem for path in directory.glob("*.json")} - {"catalogs"}
    for tag in sorted(files - registered):
        issues.append(f"unregistered catalog: {tag}.json")
    base = read_object(directory / "en.json", issues)
    valid_base = {}
    for key, value in base.items():
        try:
            if not re.fullmatch(KEY, key):
                raise ValueError("invalid key")
            valid_base[key] = validate(value, "en")
        except ValueError as error:
            issues.append(f"en/{key}: {error}")
    if not valid_base:
        issues.append("the English catalog is empty or invalid")
    if directory == CATALOGS:
        used, explicit = references(valid_base)
        for key in sorted(explicit - valid_base.keys()):
            issues.append(f"missing English key: {key}")
        for key in sorted(valid_base.keys() - used):
            issues.append(f"unused English key: {key}")
    coverage = {}
    for tag in sorted(registered):
        entries = base if tag == "en" else read_object(directory / (tag + ".json"), issues)
        valid = set()
        extra = sorted(entries.keys() - base.keys())
        for key in extra:
            issues.append(f"{tag}/{key}: unknown key")
        for key in entries.keys() & valid_base.keys():
            try:
                if validate(entries[key], tag) != valid_base[key]:
                    raise ValueError("type or placeholders differ from English")
                valid.add(key)
            except ValueError as error:
                issues.append(f"{tag}/{key}: {error}")
        missing = sorted(valid_base.keys() - valid)
        total = len(valid_base)
        coverage[tag] = {"translated": len(valid), "total": total, "percent": round(100 * len(valid) / total, 2) if total else 0,
                         "missing": missing, "extra": extra}
        if tag in required and missing:
            issues.append(f"{tag}: {len(missing)} required messages missing")
    for tag in sorted(set(required) - registered):
        issues.append(f"{tag}: required locale is not registered")
    host = host_report(issues) if directory == CATALOGS else {}
    return {"locales": coverage, "game_metadata": host, "issues": sorted(set(issues))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--require", action="append", default=[], metavar="LOCALE",
                        help="fail if this locale is incomplete (repeat for multiple locales)")
    args = parser.parse_args()
    result = report(required=args.require)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print("BeamNG.Earth Localization Report")
        for tag, locale in result["locales"].items():
            print(f"{tag}: {locale['percent']:.2f}% ({locale['translated']}/{locale['total']})")
            for key in locale["missing"]:
                print(f"  Missing: {key}")
        for tag, count in result["game_metadata"].items():
            print(f"Game metadata {tag}: {count} messages")
        for issue in result["issues"]:
            print(f"  {issue}")
    return bool(result["issues"])


if __name__ == "__main__":
    sys.exit(main())
