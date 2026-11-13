"""Experimental regional traffic-sign appearance and inferred placements.

Enabled with BNE_FR_ROADS=on and the existing --regional-poles europe profile.
Bend geometry is an estimate, not a survey of sign locations. Existing
priority/AI control logic is unchanged. No-entry placement has been withdrawn.
"""
import numpy as np
import dnp

import franceroads as fr


def localize(rows, country_at):
    if not fr.ON:
        return rows
    signals = [np.asarray(r[1:3]) for r in rows if r[0] == "signal"]
    out = []
    for row in rows:
        if (row[0] == "giveway" and country_at(*row[1:4]) == "FR"
                and not any(dnp.norm(np.asarray(row[1:3]) - p) < 30 for p in signals)):
            row = ["fr_giveway_m9c", *row[1:6], 2.45, row[7]]
        out.append(row)
    return out


def place(roads, box, country_at):
    import furniture as f
    from geotiles import box_owns
    import islands
    if not fr.ON:
        return []
    out = []
    ring = islands.ring_fids(roads)
    lines = [(fid, sp.width / 2, np.asarray(p)[:, :2]) for fid, sp, p, _ in roads if len(p) >= 2]

    def put(group, p, right, height, fid):
        if not box_owns(box, p[0], p[1]):
            return
        if any(other != fid and f._seg_dist_min(p[:2], line) < half + .35
               for other, half, line in lines):
            return
        code = country_at(*map(float, p))
        if group.startswith("fr_") and code != "FR":
            return
        out.append([group, *[round(float(v), 2) for v in p],
                    round(f._yaw(*right), 1), 1., height, 0.])

    for fid, sp, line, _ in roads:
        if (fid in ring or not fr.enabled(sp) or sp.cls not in fr.RURAL_WIDTH
                or sp.urban or sp.oneway or sp.link or len(line) < 3):
            continue
        line = np.asarray(line)
        length = fr.stations(line)[-1]
        events = fr.bends(line)
        for direction in (1, -1):
            ev = events if direction == 1 else [(length - s, -t) for s, t in events[::-1]]
            covered_until = -1.
            for j, (s, turn) in enumerate(ev):
                if s <= covered_until or s < 170:
                    continue
                multiple = j + 1 < len(ev) and 45 <= ev[j + 1][0] - s <= 250 and turn * ev[j + 1][1] < 0
                if multiple:
                    covered_until = ev[j + 1][0] + 40
                kind = ("a1d" if turn > 0 else "a1c") if multiple else ("a1b" if turn > 0 else "a1a")
                station = s - 150
                station = station if direction == 1 else length - station
                p, d = fr.at(line, station)
                d *= direction
                right = np.array([d[1], -d[0]])
                for offset in (.9, 1.5, 2.1):
                    q = p.copy()
                    q[:2] += right * (sp.width / 2 + offset)
                    if any(other != fid and f._seg_dist_min(q[:2], ln) < half + .35
                           for other, half, ln in lines):
                        continue
                    put("fr_" + kind, q, right, 1.8, fid)
                    break
    return out
