"""The requested I-65 Exit 76 lane correction, confined to its source way and location."""
import numpy as np

# I-65 southbound Exit 76 to KY 224, Upton. The source way has lanes=3,
# but the requested correction is one lane for this entire off-ramp. Keep
# the original downloaded tags; fix the shared count before width/paint/AI.
KY_I65_EXIT76_WAY = 16257189
KY_I65_EXIT76_BOUNDS = (-85.8844, 37.4583, -85.8824, 37.4632)


def lane_counts(feats):
    """Per-feature lane corrections, including source splits/reversed geometry."""
    n = len(feats)
    records = feats.attrs.get('src_record', [''] * n)
    ways = feats.attrs.get('lane_osm_way', [0] * n)
    classes = feats.attrs.get('class', [''] * n)
    subclasses = feats.attrs.get('subclass', [''] * n)
    subclass = feats.rules.get('subclass')
    oneway = feats.rules.get('oneway')
    out = {}
    west, south, east, north = KY_I65_EXIT76_BOUNDS
    for i in range(n):
        if (str(records[i]).partition('@')[0] != f'w{KY_I65_EXIT76_WAY}'
                and ways[i] != KY_I65_EXIT76_WAY):
            continue
        if classes[i] != 'motorway' or oneway is None or not len(oneway.rows(i)):
            continue
        if subclasses[i] != 'link' and not (subclass is not None and
                                           any(subclass.value[r] == 'link' for r in subclass.rows(i))):
            continue
        points = [p[0] for p in feats.parts(i) if p and len(p[0])]
        if not points:
            continue
        ll = np.concatenate(points)
        if np.all((ll[:, 0] >= west) & (ll[:, 0] <= east) &
                  (ll[:, 1] >= south) & (ll[:, 1] <= north)):
            out[i] = 1
    return out

