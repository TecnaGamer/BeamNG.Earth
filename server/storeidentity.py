"""Presentation identities shared by the streaming and offline storefront generators."""
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


@dataclass(frozen=True)
class Identity:
    key: str
    label: str
    style: str
    paint: str
    ink: str
    mark: str = ""
    variants: tuple = ()
    layout: str = "plain"


WINDOW_TRIMS = {"awning", "counter", "lintel", "pilaster", "rail", "timber", "transom"}
FORMS = {
    "wing", "shed_lobby", "drive_in", "streamline", "porch", "arcade", "hip", "market",
    "glazed_shed", "timber_portal", "masonry_hall", "step", "grid_showroom", "screen_showroom",
    "glass", "cinema_lobby", "outfitter",
}


class LocalSign(NamedTuple):
    name: str
    style: str


def category_index(rules, styles):
    index = {}
    for family, rule in rules.items():
        if family not in styles or not rule["label"].strip() or not rule["categories"]:
            raise ValueError(f"Invalid category family {family}")
        for tag in rule["categories"]:
            if not tag or tag in index:
                raise ValueError(f"Duplicate or empty storefront category {tag}")
            index[tag] = family
    return index


def category_family(categories):
    families = {CATEGORY_INDEX[tag] for tag in categories if tag in CATEGORY_INDEX}
    # A general food classification often accompanies a more specific place category.
    if len(families) > 1 and families <= DINING_STYLES:
        families.discard("dining_room")
        if len(families) > 1:
            families.discard("counter")
    return next(iter(families)) if len(families) == 1 else None


def normalized_name(value):
    return " ".join(str(value or "").casefold().replace("\u2019", "'").replace("-", " ").split())


def parse_catalog(data):
    styles = data["styles"]
    sources = {}
    groups = {group: [] for group in data["match_groups"]}
    labels = set()
    normalized_sources = set()
    for key, style in styles.items():
        if not all(math.isfinite(style[k]) and style[k] > 0 for k in ("height", "span")):
            raise ValueError(f"Invalid dimensions for {key}")
        if style.get("wall_finish") not in {None, "ribbed_metal", "timber_siding", "metal_panels", "stone_courses"}:
            raise ValueError(f"Invalid wall finish for {key}")
        windows = style["windows"]
        if (not all(math.isfinite(windows[k]) for k in ("span", "spacing", "sill", "head"))
                or not 1 <= windows["span"] < windows["spacing"]
                or not 0 <= windows["sill"] < windows["head"] < style["height"]
                or windows["trim"] not in WINDOW_TRIMS):
            raise ValueError(f"Invalid window profile for {key}")
        forms = style["variants"]
        if not forms or len(set(forms)) != len(forms) or not set(forms) <= FORMS:
            raise ValueError(f"Invalid entrance variants for {key}")
    for key, row in data["identities"].items():
        label = row["label"]
        if (not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key.startswith("generic_")
                or not label.strip() or len(label) > 64 or normalized_name(label) in labels
                or row["style"] not in styles or not row["matches"]):
            raise ValueError(f"Invalid fictional identity {key}")
        labels.add(normalized_name(label))
        if not all(re.fullmatch(r"#[a-fA-F0-9]{6}", row[k]) for k in ("paint", "ink")):
            raise ValueError(f"Invalid palette for {key}")
        mark = row.get("mark", "")
        if mark and not re.fullmatch(r"[.#]{5}(/[.#]{5}){4}", mark):
            raise ValueError(f"Invalid 5 by 5 mark for {key}")
        variants = tuple(row.get("variants", styles[row["style"]]["variants"]))
        if not variants or len(set(variants)) != len(variants) or not set(variants) <= set(styles[row["style"]]["variants"]):
            raise ValueError(f"Invalid identity variants for {key}")
        layout = row.get("layout", "plain")
        if layout not in {"plain", "frame", "rail", "split", "cap"}:
            raise ValueError(f"Invalid sign layout for {key}")
        identity = Identity(key, label, row["style"], row["paint"], row["ink"], mark, variants, layout)
        for group, names in row["matches"].items():
            if group not in groups or not isinstance(names, list) or not names:
                raise ValueError(f"Invalid source group {group} in {key}")
            for name in names:
                source = name.strip().casefold()
                normalized = normalized_name(name)
                if not normalized or normalized in normalized_sources:
                    raise ValueError(f"Duplicate or empty source in {key}: {source}")
                normalized_sources.add(normalized)
                sources[source] = identity
                groups[group].append(name)
    if labels.intersection(normalized_sources):
        raise ValueError("Fictional labels must differ from mapped source names")
    chain_groups = []
    for group, tags in data["match_groups"].items():
        for tag, values in tags.items():
            if tag not in {"shop", "amenity", "leisure"} or not values:
                raise ValueError(f"Invalid map tags in {group}")
            chain_groups.append(((tag, set(values)), groups[group]))
    return styles, sources, chain_groups


root = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parents[1])
catalog = (root / "tools/storefronts/fictional_catalog.json").read_bytes()
# The server fixes its presentation mode at startup. Both mesh and tile caches use this digest.
MAPPED_NAMES = os.environ.get("BNE_MAPPED_BUSINESS_NAMES", "1") == "1"
CATALOG_DIGEST = hashlib.sha256(catalog + (b"/mapped-names-v1" if MAPPED_NAMES else b"")).hexdigest()[:16]
data = json.loads(catalog)
STYLES, SOURCES, CHAIN_GROUPS = parse_catalog(data)
CATEGORY_RULES = data["category_rules"]
CATEGORY_INDEX = category_index(CATEGORY_RULES, STYLES)
DINING_STYLES = {key for key, style in STYLES.items() if style.get("dining")}

GENERIC_LABELS = {
    "bank": "Bank", "atm": "ATM", "market": "Neighborhood Market", "venue": "Music & Nightlife",
    "cafe": "Café", "convenience": "Neighborhood Market", "wash": "Car Wash", "fuel": "Fuel Station", "tobacco": "Tobacco Shop",
    "mall_entrance": "Shopping Center", "shop": "Shop",
    **{key: rule["label"] for key, rule in CATEGORY_RULES.items()},
}


SHOP_WORDS = {
    "books": "Bookshop", "bookshop": "Bookshop", "shoes": "Footwear", "footwear": "Footwear",
    "kitchen": "Restaurant", "restaurant": "Restaurant", "bakery": "Bakery",
    "pharmacy": "Pharmacy", "bank": "Bank", "market": "Market", "gallery": "Gallery",
}


def source_key(name):
    return hashlib.sha256(name.casefold().encode()).hexdigest()[:24]


def identity_for(name, panel="shop", *, mapped_name=False):
    if panel not in {"mall_entrance", "atm"}:
        from storefrontpack import active
        pack = active.get()
        known = (pack.identities.get(name.casefold()) if pack else None) or SOURCES.get(name.casefold())
        if known is not None:
            return known
    kind = panel if panel == "mall_entrance" else panel.removeprefix("mall_")
    style = kind if kind in CATEGORY_RULES else "shopfront"
    if MAPPED_NAMES and mapped_name and panel not in {"atm", "mall_entrance", "mall_banner"}:
        label = name.strip()
        if 2 <= len(label) <= 80 and label.isprintable():
            return Identity("mapped_" + source_key(label), label, style, "#35484b", "#fff5e1")
    if kind in {"shop", "shopfront", "brand", "banner"}:
        words = set(re.findall("[a-z]+", name.casefold()))
        for token, category in SHOP_WORDS.items():
            if token in words:
                return Identity("generic_" + token, category, "shopfront", "#35484b", "#fff5e1")
    if kind not in GENERIC_LABELS:
        kind = "shop"
    return Identity("generic_" + kind, GENERIC_LABELS[kind], style, "#35484b", "#fff5e1")


def variant_for(identity, center, *, site_key=None):
    variants = identity.variants or STYLES[identity.style]["variants"]
    site = f"{site_key or identity.key}/{float(center[0]):.5f}/{float(center[1]):.5f}"
    # Rendezvous hashing keeps previous choices wherever an added variant does not win.
    return max(variants, key=lambda form: hashlib.sha256(f"{site}/{form}".encode()).digest())
