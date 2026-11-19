"""Prepare verified Commons sign models in a separate unpacked mod before the game starts."""
import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import plmapillary

VFS = "levels/beamng_earth/art/shapes/beamngEarth/pl_signs_extra"
LICENSES = {"Public domain", "CC0", "CC BY 3.0", "CC BY 4.0", "CC BY-SA 2.5", "CC BY-SA 3.0", "CC BY-SA 4.0"}


def folder(user_dir):
    return Path(user_dir) / "mods/unpacked/beamng_earth_pl_signs" / VFS


def download(url):
    request = Request(url, headers={"User-Agent": "BeamNG.Earth sign art preparation/1.0 (personal mod)"})
    with urlopen(request, timeout=12) as response:
        data = response.read(6 * 1024 * 1024 + 1)
    if len(data) > 6 * 1024 * 1024:
        raise ValueError("File too large")
    return data


def provenance(code, entry):
    if entry.get("license") in LICENSES:
        return entry
    args = dict(action="query", format="json", titles="File:PL road sign " + code + ".svg", prop="imageinfo",
                iiprop="sha1|extmetadata", iiextmetadatafilter="Artist|LicenseShortName")
    page = next(iter(json.loads(download("https://commons.wikimedia.org/w/api.php?" + urlencode(args)))["query"]["pages"].values()))
    info = page["imageinfo"][0]
    if info["sha1"] != entry["sha1"]:
        raise ValueError("Original changed")
    meta = info["extmetadata"]
    license_name = meta["LicenseShortName"]["value"]
    if license_name not in LICENSES:
        raise ValueError("Unverified licence")
    return dict(entry, license=license_name, credit=html.unescape(re.sub("<[^>]+>", "", meta.get("Artist", {}).get("value", ""))))


def prepare(user_dir, originals=None):
    import netfetch
    root = folder(user_dir)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "prepare.lock").open("a+b") as guard:
        if not netfetch._flock(guard.fileno(), blocking=False):
            print("Przygotowanie modeli jest juz uruchomione.")
            return
        try:
            _prepare(root, originals)
        finally:
            netfetch._funlock(guard.fileno())


def _prepare(root, originals):
    from build_pl_signs import make_model, material
    inventory = json.loads((plmapillary.DATA / "commons.json").read_bytes())
    bundled = set(json.loads((plmapillary.DATA / "catalog.json").read_bytes()))
    registry = root / "ready.json"
    try:
        ready = json.loads(registry.read_bytes())
    except FileNotFoundError:
        ready = {}
    wanted = set(plmapillary.classes().values())
    wanted.update(code for code in inventory if code.startswith("B-33-"))
    sample = {r[3] for r in json.loads((plmapillary.DATA / "mapillary.json").read_bytes())}
    ordered = sorted(inventory, key=lambda code: (code not in sample, code not in wanted, code))
    pause = root / "retry-after.txt"
    if not originals and pause.is_file():
        try:
            if float(pause.read_text()) > time.time():
                print("Wikimedia poprosila o przerwe. Dotychczasowe modele sa gotowe.")
                return
        except ValueError:
            pass
    for code in ordered:
        if code in bundled or not re.fullmatch(r"[A-Z]-[A-Za-z0-9.-]+", code):
            continue
        name = "pl_" + code.lower().replace("-", "_").replace(".", "_")
        if ready.get(code, {}).get("sha1") == inventory[code]["sha1"] and all(
                (root / (name + suffix)).is_file() for suffix in ("_plate.dae", "_face_256.png")):
            continue
        try:
            local = None
            if originals:
                local = next((originals / n for n in (code + ".svg", "PL_road_sign_" + code + ".svg",
                            "PL road sign " + code + ".svg") if (originals / n).is_file()), None)
                if local is None:
                    continue
            entry = provenance(code, inventory[code])
            data = local.read_bytes() if local else download(entry["url"])
            if hashlib.sha1(data).hexdigest() != entry["sha1"]:
                raise ValueError("Wikimedia checksum mismatch")
            with tempfile.TemporaryDirectory(dir=root) as tmp:
                stage = Path(tmp)
                (stage / (code + ".svg")).write_bytes(data)
                make_model(code, {"sha256": hashlib.sha256(data).hexdigest()}, art=stage, out=stage)
                for f in stage.iterdir():
                    os.replace(f, root / f.name)
            credit = dict(entry, group=name)
            if not credit.get("license_url"):
                license_name = credit["license"]
                if license_name.startswith("CC BY"):
                    family, version = license_name.removeprefix("CC ").lower().split()
                    credit["license_url"] = f"https://creativecommons.org/licenses/{family}/{version}/"
                elif license_name == "CC0":
                    credit["license_url"] = "https://creativecommons.org/publicdomain/zero/1.0/"
            ready[code] = credit
            mats = {v["group"]: material(v["group"], VFS) for v in ready.values()}
            plmapillary.atomic_write(root / "extra.materials.json", json.dumps(mats).encode())
            plmapillary.atomic_write(registry, json.dumps(ready, ensure_ascii=False).encode("utf8"))
            print("Gotowy model:", code, flush=True)
            if not local:
                time.sleep(1)
        except HTTPError as exc:
            if exc.code == 429:
                delay = exc.headers.get("Retry-After", "600")
                delay = int(delay) if delay.isdigit() else 600
                plmapillary.atomic_write(pause, str(time.time() + delay).encode())
                print("Wikimedia ograniczyla pobieranie. Uruchom przygotowanie modeli ponownie pozniej.")
                break
            if exc.code in (403, 500, 502, 503, 504):
                print("Serwer Wikimedia jest niedostepny (HTTP %s)." % exc.code)
                break
            print("Pominieto", code, "HTTP", exc.code)
        except URLError:
            print("Brak polaczenia z Wikimedia. Mozesz wznowic pobieranie pozniej.")
            break
        except (ValueError, KeyError, IndexError) as exc:
            print("Pominieto", code, str(exc))
    print("Modele w paczce:", len(bundled), "| Dodatkowe przygotowane:", len(ready))
