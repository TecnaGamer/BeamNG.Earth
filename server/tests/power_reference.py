"""Original NumPy tower avoidance, kept only as an independent test oracle."""
import numpy as np


def margin(pts, lines, clear):
    out = np.full(len(pts), np.inf)
    for line, half in lines:
        a, ab = line[:-1], line[1:] - line[:-1]
        ap = pts[:, None, :] - a[None]
        t = np.clip((ap * ab).sum(-1) / np.maximum((ab * ab).sum(-1), 1e-12), 0., 1.)
        distance = np.hypot(*(ap - t[..., None] * ab).transpose(2, 0, 1)).min(1)
        out = np.minimum(out, distance - half - clear)
    return out


def move(point, lines, around, clear):
    point = np.asarray(point, np.float64)
    if not lines or margin(point[None], lines, clear)[0] >= 0.:
        return point.copy()
    best = None
    for line, half in lines:
        a, ab = line[:-1], line[1:] - line[:-1]
        t = np.clip(((point - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0., 1.)
        foot = a + t[:, None] * ab
        distance = np.hypot(*(point - foot).T)
        k = int(np.argmin(distance))
        if best is None or distance[k] - half < best[0]:
            away = (point - foot[k]) / distance[k] if distance[k] > 1e-6 else np.array([-ab[k, 1], ab[k, 0]]) / max(
                float(np.hypot(*ab[k])), 1e-9)
            best = (distance[k] - half, foot[k] + away * (half + clear))
    candidates = np.vstack([best[1][None], point + around])
    values = margin(candidates, lines, clear)
    ok = np.flatnonzero(values >= -1e-6)
    return candidates[ok[0] if len(ok) else int(np.argmax(values))]
