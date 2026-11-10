"""Select the national face model without changing the junction's AI rules."""

from functools import lru_cache
import json
from pathlib import Path

MODELS_PATH = Path(__file__).with_name("data") / "eu_stop_models.json"
VERSION = "regional-stops-2"     # 2: Quebec from the regional data's band file (regionaldata, quebec_bands.bin)


@lru_cache(maxsize=2)
def _quebec(sig):
    import countrybounds
    import regionaldata
    path = regionaldata.file("quebec_bands.bin")
    return countrybounds.BandIndex(path) if path else countrybounds.Absent()


def quebec():
    """Quebec's border (Statistics Canada, generalised 10 m): at(lat, lon) == "QC" inside."""
    import regionaldata
    return _quebec(regionaldata.signature())


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
