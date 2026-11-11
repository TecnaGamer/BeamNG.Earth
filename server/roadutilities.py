"""Verified corrections to inferred roadside utilities, keyed by source OSM way.

These affect procedural poles/wires only, not mapped infrastructure, lighting or
road geometry. Keeping the rules in this module includes them in the normal
revisit-cache source fingerprint, so edits invalidate generated road tiles.
"""
import railtags

# User confirmed no telephone lines along the Vedauwoo Picnic Ground Complex
# entrance road (2026-11-09). Include the separately mapped service continuation.
NO_INFERRED_OVERHEAD = {
    15674422: 'Vedauwoo Picnic Ground Complex Road',
    1524224932: 'Vedauwoo Picnic Ground Complex Road service continuation',
}


def override(feats, i):
    records = feats.attrs.get('src_record')
    if records is None:
        return None
    wid = railtags.way_id(str(records[i]))
    return False if wid is not None and int(wid) in NO_INFERRED_OVERHEAD else None
