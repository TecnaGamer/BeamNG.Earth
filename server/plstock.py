"""Use the same Polish sign artwork for measured signs and the project's generated sign layers."""
from collections import defaultdict
from functools import lru_cache
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import numpy as np
import plsigns

# Only roles that specify this exact Polish design. The three roundabout roles are independent.
ROLES = {
    "warn.roundabout": "A-8", "mand.roundabout": "C-12", "prio.give_way": "A-7", "prio.stop": "B-20",
    "reg.no_entry": "B-2", "info.dead_end": "D-4a", "info.crossing_right": "D-6",
    "warn.curve_right": "A-1", "warn.curve_left": "A-2", "warn.double_right": "A-3", "warn.double_left": "A-4",
    "warn.signals": "A-29", "info.motorway": "D-9", "info.motorway_end": "D-10",
    "warn.side_right": "A-6b", "warn.side_left": "A-6c",
    "info.living_street": "D-40", "info.living_street_end": "D-41", "info.oneway": "D-3",
    "warn.narrow_both": "A-12a", "info.tunnel": "D-37", "mand.bicycle": "C-13",
    "info.bus_stop": "D-15", "info.parking": "D-18",
}
FURNITURE = {"giveway": "A-7", "stop": "B-20"}
MATCH_M = 12.


def in_country(frame, x, y):
    lat, lon, _ = frame.enu_to_geodetic(x, y, 0.)
    return 14. < lon < 25. and 48.9 < lat < 55.1 and plsigns.inside_poland(float(lon), float(lat))


class Nearby:
    def __init__(self, rows):
        import guardrails
        self.items = defaultdict(list)
        for row in rows:
            self.items[guardrails.polish_groups()[row[0]]].append(row)

    def matched(self, code, x, y, yaw):
        a = math.radians(yaw)
        # Yield/stop and posted speed values describe one control on this approach.
        # Roundabout warning, mandatory circulation and yield are separate controls.
        name = plsigns.group(code)
        groups = ("pl_a_7", "pl_b_20") if code in ("A-7", "B-20") else (name,)
        if code.startswith("B-33-"):
            groups = [g for g in self.items if g.startswith("pl_b_33_")]
        for g in groups:
            for r in self.items.get(g, ()):
                dx, dy = r[1] - x, r[2] - y
                if (abs((r[4] - yaw + 180) % 360 - 180) <= 30 and abs(dx*math.cos(a) + dy*math.sin(a)) <= 2.5
                        and dx*dx + dy*dy <= MATCH_M**2):
                    return True
        return False

    def same_post(self, code, x, y, yaw):
        return next((r for r in self.items.get(plsigns.group(code), ())
                     if math.hypot(r[1]-x, r[2]-y) < 0.75 and abs((r[4]-yaw+180) % 360-180) < 20), None)


def furniture(rows, frame, nearby):
    if not rows:
        return rows
    result = []
    for row in rows:
        code = FURNITURE.get(row[0])
        if not code or not in_country(frame, row[1], row[2]):
            result.append(row)
        elif not nearby.matched(code, row[1], row[2], row[4]):
            localized = [plsigns.group(code), *row[1:6], plsigns.HEIGHT, *row[7:]]
            if code == "A-7" and (post := nearby.same_post("C-12", row[1], row[2], row[4])):
                localized[1:5] = [post[1], post[2], post[3] + .85, post[4]]
            result.append(localized)
    return result


def sign_stream(md, frame, box, elev, context, left_hand, mph):
    """Include signs on the far side of a tile seam when comparing two placements."""
    import speedsigns
    x0, y0, x1, y1 = box
    bounds = (x0 - MATCH_M, y0 - MATCH_M, x1 + MATCH_M, y1 + MATCH_M)
    def roads():
        for ll, xy, meta, _, _, skips, _ in speedsigns._tile_rows(
                frame, bounds, context, left_hand, mph, speedsigns.STYLE_VIENNA, margin=plsigns.REACH):
            if not skips:
                yield None, SimpleNamespace(width=2*meta[0], dir=meta[1], oneway=bool(meta[1])), xy
    found = plsigns.tile_rows(frame, bounds, roads(), elev, getattr(md, "cache_dir", None), getattr(md, "fetcher", None))
    return Nearby(found)


@lru_cache(maxsize=128)
def template(code):
    """Read the display geometry from the same DAE as the Forest instance, with its original UVs."""
    import plsignart
    group = plsigns.group(code)
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    file = root / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/pl_signs" / (group + "_plate.dae")
    if not file.is_file():
        file = plsignart.folder(os.environ["BNGE_USER_DIR"]) / file.name
    mesh = ET.parse(file).find(".//{*}geometry/{*}mesh")
    pos_id = mesh.find("{*}vertices/{*}input").get("source")[1:]
    def array(ident, width):
        elem = mesh.find(f"{{*}}source[@id='{ident}']/{{*}}float_array")
        return np.fromstring(elem.text, sep=" ").reshape(-1, width)
    pos = array(pos_id, 3)
    blocks = mesh.findall("{*}triangles")
    uv = array(blocks[0].find("{*}input[@semantic='TEXCOORD']").get("source")[1:], 2)
    tris, mats, indices = [], [], []
    for b in blocks:
        t = np.fromstring(b.find("{*}p").text, dtype=np.int64, sep=" ").reshape(-1, 3)
        mats.append(b.get("material").removesuffix("-mat"))
        indices.append(np.full(len(t), len(mats)-1, dtype=np.int64))
        tris.append(t)
    bottom = pos[tris[mats.index(group)].ravel(), 2].min()
    return pos, np.vstack(tris), uv, mats, np.concatenate(indices), bottom


def replacement(code, row, origin, mount=None):
    pos, triangles, uv, mats, indices, bottom = template(code)
    x, y, zg, zr, tx, ty = row[:6]
    length = math.hypot(tx, ty)
    tx, ty = tx / length, ty / length
    px, py, pz = pos.T
    dz = 0 if mount is None else max(zg + 1.2, zr + mount) - zg - bottom
    world = np.column_stack((x - origin[0] + px*ty + py*tx, y - origin[1] - px*tx + py*ty,
                             zg + pz + np.where(pz > 0, dz, 0)))
    return world, triangles, uv, mats, indices


def split(frame, signs, table, codes, nearby, origin, stacked=False):
    """Resolve signs after the stock rules ran, retaining the stock artwork for unverified variants."""
    import dezones
    keep, extra = [], None
    ready = set(plsigns.supported_codes())
    for i, (s, row, code) in enumerate(zip(signs, table, codes)):
        if not code or not in_country(frame, s[0], s[1]):
            keep.append(i)
            continue
        yaw = math.degrees(math.atan2(-s[2], s[3]))
        if nearby.matched(code, s[0], s[1], yaw):
            continue
        if code not in ready:
            keep.append(i)
            continue
        try:
            mount = row[9] if stacked else None
            if code == "C-12" and (post := nearby.same_post("A-7", s[0], s[1], yaw)):
                # Keep the round blue face below the surveyed priority triangle.
                row = row.copy()
                row[:3] = post[1:4]
                mount = plsigns.HEIGHT - 1.55 + row[2] - row[3]
            elif code == "A-7" and (post := nearby.same_post("C-12", s[0], s[1], yaw)):
                row = row.copy()
                row[:3] = post[1:4]
                mount = plsigns.HEIGHT + .1 + row[2] - row[3]
            part = replacement(code, row, origin, mount)
        except (OSError, KeyError, ET.ParseError):
            keep.append(i)
            continue
        extra = dezones._merged(extra, part)
    return table[keep], extra
