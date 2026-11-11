"""OpenStreetMap traffic_sign values to the roles of server/data/sign_roles.json (task 017, stage 4). Pure; the codes of each
country are data (server/data/sign_osm_codes.json), so a country is added there.

Syntax of the tag (wiki Key:traffic_sign): ';' separates unrelated signs, ',' joins a sign and its plates, '[x]' is a sign's
value (DE:274.1[30]), a country prefix 'DE:' names the numbering. A plate (DE 1020-30, PL T-27, CZ E3a) belongs to the sign
before it and is not a role; so is any code we have no role for: nothing is shown for it.
"""
from functools import lru_cache
import json
import os
import re

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sign_osm_codes.json")
DE_PLATE = re.compile(r"^\d{4}(-\d+)?$")
VALUE = re.compile(r"^(.*?)\[(.*)\]$")


@lru_cache(maxsize=1)
def table():
    with open(DATA, encoding="utf-8") as f:
        return json.load(f)


def _split(token):
    """(country, code, value) of one token: 'DE:274.1[30]' -> ('DE', '274.1', '30'); a token with no prefix: country None."""
    token = token.strip()
    m = VALUE.match(token)
    value = None
    if m:
        token, value = m.group(1), m.group(2)
    country, _, code = token.partition(":")
    if not code:
        return None, country, value
    return country.upper(), code, value


def _role(country, code):
    t = table()
    if code in t["generic"]:
        return t["generic"][code]
    codes = (t["countries"].get(country) or {}).get("codes", {})
    if code in codes:
        return codes[code]
    return codes.get(code.split("-")[0]) if "-" in code else None


def is_plate(country, code):
    if DE_PLATE.match(code):
        return True
    pattern = (table()["countries"].get(country) or {}).get("plates")
    return bool(pattern and re.match(pattern, code))


def parse(value, country=None):
    """[{role, code, country, value, plates}] of an OSM traffic_sign value, in order; only signs with a role. country: the
    numbering of a token with no prefix (the place's own), or None."""
    out = []
    ignored = set(table()["ignored"])
    for group in str(value or "").split(";"):
        sign = None
        for k, token in enumerate(group.split(",")):
            if not token.strip() or token.strip() in ignored:
                continue
            c, code, val = _split(token)
            c = c or (sign["country"] if sign else None) or (country.upper() if country else None)
            if sign is not None and is_plate(c, code):
                sign["plates"].append(code)
                continue
            role = _role(c, code) if c or code in table()["generic"] else None
            if role is None:
                sign = None
                continue
            sign = {"role": role, "code": code, "country": c, "value": val, "plates": []}
            out.append(sign)
    return out


def roles(value, country=None):
    """The roles an OSM traffic_sign value stands for, in order."""
    return [s["role"] for s in parse(value, country)]
