"""Select the national face model without changing the junction's AI rules."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path

DATA_PATH = Path(__file__).with_name("data") / "quebec_bounds.zip"
DATA_SHA256 = "becfdfb3fc08c1adb450e2f941aef41a83512f8cf84800f76002f60a30fe38ae"
MODELS_PATH = DATA_PATH.with_name("eu_stop_models.json")


@lru_cache(maxsize=1)
def quebec():
    import countrybounds

    if hashlib.sha256(DATA_PATH.read_bytes()).hexdigest() != DATA_SHA256:
        raise ValueError("Quebec boundary checksum mismatch")
    return countrybounds.WorldIndex(DATA_PATH)


def style(country, lat, lon):
    if country == "TR":
        return "stop_dur"
    if country == "CA" and quebec().at(lat, lon) == "QC":
        return "stop_arret"
    return "stop"


@lru_cache(maxsize=1)
def models():
    return json.loads(MODELS_PATH.read_text(encoding="utf-8"))


def localize(rows, profile, frame, country_at):
    import euguardrails

    europe = isinstance(profile, (list, tuple)) and len(profile) == 3 and profile[1] == "europe"
    new = euguardrails.enabled()
    if not (europe or new) or frame is None:
        return rows
    result = []
    for row in rows:
        if row[0] == "stop":
            country = country_at(*row[1:4])
            model = models().get(country) if new else None
            if model is not None:
                row = [model[0], *row[1:6], model[1], *row[7:]]
            elif europe and country in ("TR", "CA"):
                lat, lon, _ = frame.enu_to_geodetic(*row[1:4])
                row = [style(country, float(lat), float(lon)), *row[1:]]
        result.append(row)
    return result
