"""Modular restaurant exterior. All dimensions are metres in the facade frame."""

import numpy as np

MATERIALS = [
    (0.075, 0.065, 0.056),  # dark wood panels
    (0.028, 0.04, 0.045),  # anodized metal
    (0.12, 0.24, 0.28),  # storefront glass
    (0.47, 0.05, 0.027),  # red sign blade
    (0.92, 0.64, 0.08),  # warm canopy trim
    (0.37, 0.25, 0.14),  # timber battens
]


def restaurant(xy, front, height, mesh):
    def part(origin, along, outward, x0, x1, y0, y1, z0, z1, material):
        corners = [
            (*(origin + x * along + y * outward), z)
            for z in (z0, z1)
            for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        ]
        for a, b, c, d in [
            (0, 1, 2, 3),
            (4, 7, 6, 5),
            (0, 4, 5, 1),
            (1, 5, 6, 2),
            (2, 6, 7, 3),
            (3, 7, 4, 0),
        ]:
            mesh([corners[j] for j in (a, b, c, a, c, d)], material)

    def draw_wall(i, a):
        delta = xy[(i + 1) % len(xy)] - a
        width = np.linalg.norm(delta)
        if width < 4:
            return
        axis = delta / width
        norm = np.array([axis[1], -axis[0]])

        def box(x0, x1, y0, y1, z0, z1, mat):
            part(a, axis, norm, x0, x1, y0, y1, z0, z1, mat)

        box(0, width, 0, 0.09, 0, height, 3)
        box(0, width, 0.09, 0.26, height - 0.22, height + 0.10, 4)
        # A compact entrance and dining area is the fallback when glazing is unmapped.
        # Adjacent footprint segments may represent service walls or small setbacks.
        if i != front:
            return
        start = 3.8
        available = min(width - start - 0.65, 8.0)
        if available >= 1.2:
            door_end = start + 1.05
            box(start, door_end, 0.10, 0.17, 0.06, 2.45, 4)
            box(start + 0.07, door_end - 0.07, 0.17, 0.19, 0.15, 2.36, 5)
            box(door_end - 0.18, door_end - 0.14, 0.19, 0.26, 0.90, 1.22, 4)
            glass_start = door_end + 0.45
            glass_width = start + available - glass_start
            if glass_width >= 1.4:
                panes = max(1, round(glass_width / 1.8))
                bay = glass_width / panes
                box(glass_start, glass_start + glass_width, 0.10, 0.17, 0.65, 2.45, 4)
                for index in range(panes):
                    x = glass_start + index * bay
                    box(x + 0.045, x + bay - 0.045, 0.17, 0.19, 0.73, 2.37, 5)
        box(0.25, 2.75, 0.10, 0.40, 0, height + 0.85, 6)
        # Fixed-aspect sign art above the entry; all remaining width is real bays.
        for start, size, base, crop in [
            (0.52, 1.95, height - 1.05, (55, 15, 238, 170)),
            (3.25, min(5.2, width - 3.6), 3.16, (386, 54, 1040, 160)),
        ]:
            if size < 1:
                continue
            l, t, r, b = crop
            high = size * (b - t) / (r - l)
            off = 0.42 if start < 1 else 0.22
            p = a + axis * start + norm * off
            q = p + axis * size
            pts = [
                (*p, base),
                (*q, base),
                (*q, base + high),
                (*p, base),
                (*q, base + high),
                (*p, base + high),
            ]
            mesh(
                pts,
                1,
                [
                    (l / 1200, b / 480),
                    (r / 1200, b / 480),
                    (r / 1200, t / 480),
                    (l / 1200, b / 480),
                    (r / 1200, t / 480),
                    (l / 1200, t / 480),
                ],
            )
        box(3.1, min(width - 0.35, 12.1), 0.09, 1.05, 2.76, 2.97, 4)
        box(3.1, min(width - 0.35, 12.1), 1.05, 1.08, 2.90, 2.97, 7)
        for x in np.arange(2.82, 3.50, 0.13):
            box(x, x + 0.055, 0.10, 0.17, 0.1, height - 0.24, 8)

    for index, origin in enumerate(xy):
        draw_wall(index, origin)
