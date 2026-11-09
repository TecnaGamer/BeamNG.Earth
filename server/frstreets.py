"""French street plaques on safe posts and suitable rendered facades, plus SOS.

Names come from the map. Plaque placement is scenery inferred from the junction,
its drivable approaches and adjacent buildings; actual mounts are not mapped.
"""

import math
import hashlib
import numpy as np
import geotiles
import frroadmesh


def place(frame, roads, box, country_at, mesh, names=None, ground=None, obstacles=None, decks=()):
    import furniture as f
    from roadphones import on_highways
    from signplacement import Context, arm_line, pavement, span

    obstacles = obstacles if obstacles is not None else Context()
    obstacles.ground = ground
    names = names or {}
    posts = []
    records = []
    used = set()
    corners = []
    lines = {}
    paved = None
    for fid, _, line, _ in roads:
        if len(line) > 1:
            lines.setdefault(fid, []).append(np.asarray(line))

    def available(p):
        return (
            geotiles.box_owns(box, *p[:2])
            and country_at(*p) == "FR"
            and not any(np.linalg.norm(p[:2] - other) < 3.0 for other in corners)
        )

    for arms in f._arms(roads).values():
        if len({str(a[0]) for a in arms}) < 2:
            continue
        if len({str(names.get(a[0], "")).strip().casefold() for a in arms if names.get(a[0])}) < 2:
            continue
        c = arms[0][3]
        if not geotiles.box_owns(box, *c[:2]) or country_at(*c) != "FR":
            continue
        for arm in sorted(arms, key=lambda a: str(a[0])):
            fid, spec, u, c, *_ = arm
            name = str(names.get(fid, "") or "").strip()
            if (
                not name
                or name in {"None", "null"}
                or spec.cls
                not in {"residential", "tertiary", "secondary", "unclassified", "living_street"}
                or spec.link
            ):
                continue
            if not spec.urban:
                continue
            key = (round(float(c[0]), 1), round(float(c[1]), 1), name.casefold())
            if key in used:
                continue
            used.add(key)
            line = arm_line(lines, arm)
            if line is None:
                continue
            if paved is None:
                paved = pavement(
                    [(sp.width / 2, ln) for _, sp, ln, _ in roads] + [(h, ln) for ln, h in decks]
                )
            width, height = frroadmesh.street_layout(name)[2:]
            mix = hashlib.blake2s((str(fid) + name).encode(), digest_size=1).digest()[0] < 140
            point = tangent = wall = None
            if not mix or not obstacles.walls:
                point, tangent = _post(line, spec, width, available, paved, obstacles)
            if mix or point is None:
                for *_, q, out in obstacles.mounts(c, u, width, height, line):
                    a, _, b = span(q, out, width)
                    if available(q) and paved.visible(a, b, 0.35):
                        wall = (q, out)
                        break
            if point is None and wall is None and mix and obstacles.walls:
                point, tangent = _post(line, spec, width, available, paved, obstacles)
            if wall is not None and (mix or point is None):
                point, out = wall
                frroadmesh.street(mesh, point, -out, name, centre_height=0.0)
                mount = "wall"
            else:
                if point is None:
                    continue
                point[2] = obstacles.z(point, point[2])
                frroadmesh.street(mesh, point, -tangent, name)
                frroadmesh.street(mesh, point, tangent, name, body=False)
                posts.append(["fr_street_post", *map(float, point), 0.0, 1.0, 2.49, 0.0])
                mount = "post"
            records.append(
                {
                    "id": "street:" + str(fid),
                    "name": name,
                    "source": "mapped-road-name / estimated-plaque-position",
                    "mount": mount,
                    "pos": point.tolist(),
                }
            )
            corners.append(point[:2])
            if len(records) >= 24:
                break
        if len(records) >= 24:
            break
    # Phones only beside a compatible ground highway, not on the opposite bridge.
    for phone in on_highways(frame, roads, box, 20):
        p = phone["point"].copy()
        if not geotiles.box_owns(box, *p[:2]) or country_at(*p) != "FR":
            continue
        if ground is not None:
            p[2] = float(np.asarray(ground(np.array([p[0]]), np.array([p[1]]))).ravel()[0])
        inward = phone["roadpoint"][:2] - p[:2]
        inward /= max(np.linalg.norm(inward), 1e-9)
        # The asset faces -local Y, with a separate near-field collision box.
        travel = -inward
        yaw = math.degrees(math.atan2(-travel[0], travel[1]))
        posts.append(["fr_sos_phone", *map(float, p), yaw, 1.0, 1.825, 0.0])
        records.append(
            {
                "id": "sos:" + str(phone["id"]),
                "source": "mapped-emergency-phone / ground-highway-filter",
                "pos": p.tolist(),
            }
        )
    return posts, records


def _post(line, spec, width, available, paved, obstacles):
    import furniture as f
    from signplacement import span

    length = f._arclen(line)[-1]
    for s in (8.0, 10.0, 14.0, 18.0, 22.0):
        if s + 1.0 > length:
            break
        pts, tangents = f._at(line, np.array([s, s + 12.0, s - 12.0]))
        u = tangents[0]
        normal = np.array([-u[1], u[0]])
        for offset in (1.2, 1.8, 2.5):
            for side in (1.0, -1.0):
                q = pts[0].copy()
                q[:2] += normal * side * (spec.width / 2 + offset)
                if not available(q):
                    continue
                a, _, b = span(q, u, width)
                if paved.visible(a, b, 0.30) and obstacles.post(q, u, width, pts[1:, :2]):
                    return q, u
    return None, None
