"""Explicit project road corrections, scoped by name and geographic bounds."""
import numpy as np

# A Collins Road, Logan County, Kentucky (OSM way 16282294). Bounds cover the
# whole road, including its connectors; matching its name also handles future
# source splits without changing an unrelated road elsewhere with the same name.
A_COLLINS_BOUNDS = (-86.811, 36.930, -86.797, 36.942)


def apply(feats, specs):
    names = feats.attrs.get('name')
    if names is None:
        return specs
    out = list(specs)
    west, south, east, north = A_COLLINS_BOUNDS
    for i, sp in enumerate(specs):
        if sp is None:
            continue
        name = ' '.join(str(names[i]).lower().replace('.', '').split())
        if name not in ('a collins road', 'a collins rd'):
            continue
        parts = feats.parts(i)
        points = [p[0] for p in parts if p and len(p[0])]
        if not points:
            continue
        ll = np.concatenate(points)
        if not np.all((ll[:, 0] >= west) & (ll[:, 0] <= east) & (ll[:, 1] >= south) & (ll[:, 1] <= north)):
            continue
        out[i] = sp._replace(kind='gravel', surf='gravel', material='gravel_road',
                             urban=False, sides=(), utility_poles=False)
    return out
